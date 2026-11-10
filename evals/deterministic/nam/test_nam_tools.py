"""The NAM tool layer (plan Step 10): nam_agent/nam/tools.py over the contracts of
nam_agent/nam/contracts/, answered by a scripted KG.

Every contract has a schema test (good input passes, bad input is refused
before the KG is asked, the result passes its output schema) and a failure
test (the KG down is kg_unavailable after the retries, never found: False).
Then the layer itself: retries for reads only, idempotent writes, the
permission checks, one audit row per call, and the model adapter.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error

import pytest
from nam_fakes import OFFICER, ask, fake_kg, run
from nam_fakes import ctx as make_ctx

from nam_agent.nam import audit, playbooks, schema, store, tools
from nam_agent.nam.contracts.base import CONTRACTS, IRREVERSIBLE, READ, WRITE, Rejected, Unavailable
from nam_agent.nam.errors import NamError

# -- a scripted KG, in the shapes /api/kg/* answers with ----------------------

_STATS = {"n": 3, "min": "100.00", "median": "120.50", "max": "150"}
_CITE_SRC = {"tender_no": "T-PUR-1", "pur_case_id": "PUR-1", "department": "Materials",
             "job_name": "Supply of Spiral Wound Gaskets", "work_category": None,
             "source_document": "nit.pdf"}

REPLIES = {
    "/api/kg/tenders": {"query": "gasket", "terms": ["gasket", "gaskets"], "count": 2,
                        "tenders": [
                            {"case_key": "PUR-1", "tender_no": "T-PUR-1",
                             "job_name": "Supply of Spiral Wound Gaskets",
                             "matched_by_name": True, "matching_items": ["gasket 4in"],
                             "matching_item_count": 1},
                            {"case_key": "PUR-2", "tender_no": None,
                             "job_name": "Ring Joint Gaskets", "matched_by_name": True,
                             "matching_items": [], "matching_item_count": 0}]},
    "/api/kg/precedent": {"requirement": "gasket", "precedent_found": True,
                          "precedent_gap": None, "best_similarity": 0.812,
                          "tenders": [{**_CITE_SRC, "matched_items": ["gasket 4in"],
                                       "items": [], "pqc": [], "scc": []}],
                          "retrieval_error": None},
    "/api/kg/resolve": {"ref": "OC01000435", "tier": "core", "ambiguous": True,
                        "matches": [{"case_key": "PUR-9", "tender_no": "OC01000435/PRV",
                                     "job_name": "Valves", "match": "core"},
                                    {"case_key": "PUR-3", "tender_no": "OC01000435/AB",
                                     "job_name": "Valves 2", "match": "core"}]},
    "/api/kg/prices": {"requirement": "gasket", "precedent_found": True, "precedent_gap": None,
                       "rows": [
                           {"item": "Gasket 4in", "uom": "Nos", "price_level": "awarded",
                            "unit_price": _STATS, "estimated_rate": None,
                            "typical_qty": {"n": 2, "min": "10", "median": "15", "max": "20"},
                            "totals": None, "matched": True, "zero_rates": 0,
                            "observations": 3, "years": [2021, 2023], "spans_years": False,
                            "unparsed": [], "sources": ["T-PUR-1"]},
                           {"item": "Gasket 4in", "uom": "Nos", "price_level": "estimate",
                            "unit_price": None, "estimated_rate": _STATS,
                            "typical_qty": {"n": 1, "min": "12", "median": "12", "max": "12"},
                            "totals": None, "matched": True, "zero_rates": 1,
                            "observations": 3, "years": [2023], "spans_years": False,
                            "unparsed": [], "sources": ["T-PUR-1"]}],
                       "formatted": "...", "retrieval_error": None},
    "/api/kg/clauses": {"requirement": "gasket", "kind": "pqc", "count": 1,
                        "clauses": [{"kind": "pqc", "text": "The bidder shall have supplied...",
                                     "source": _CITE_SRC}], "retrieval_error": None},
    "/api/kg/boilerplate": {"section": "mse", "query": "MSE purchase preference", "count": 2,
                            "clauses": [{"text": "MSEs quoting within L1+15% ...",
                                         "source": _CITE_SRC},
                                        {"text": "MSEs quoting within L1+15% ...",
                                         "source": _CITE_SRC}], "retrieval_error": None},
    "/api/kg/documents": {"case_key": "PUR-1", "count": 1, "documents": [
        {"document_type": "NIT", "filename": "nit.pdf", "path": "/srv/corpus/PUR-1/nit.pdf",
         "byte_size": 1200, "extension": "pdf", "readable": True}],
        "best_nit": {"document_type": "NIT", "filename": "nit.pdf",
                     "path": "/srv/corpus/PUR-1/nit.pdf", "byte_size": 1200,
                     "extension": "pdf", "readable": True}},
    "/api/kg/case": {"case_key": "PUR-1", "tender_no": "T-PUR-1",
                     "job_name": "Supply of Spiral Wound Gaskets", "estimated_value": 450000,
                     "emd_amount": "9000", "bid_type": "Two bid", "pr_no": "1000012345",
                     "department": "Materials", "work_category": "Goods",
                     "pqc": [], "scc": [], "tech_spec": [], "boq": [], "sor_references": [],
                     "awarded_to": ["Beta Seals", "Acme Gaskets", "Acme Gaskets"],
                     "counts": {"pqc": 1, "scc": 1, "tech_spec": 0, "boq": 1,
                                "sor_references": 0}},
    "/api/kg/case_summary": {"cases": [
        {"case_key": "PUR-1", "tender_no": "T-PUR-1", "boq_lines": 4, "pqc_clauses": 3,
         "scc_clauses": 2, "spec_clauses": 1, "awarded_to": ["Acme Gaskets"]},
        {"case_key": "PUR-2", "tender_no": "T-PUR-2", "boq_lines": 1, "pqc_clauses": 0,
         "scc_clauses": 0, "spec_clauses": 0, "awarded_to": ["Acme Gaskets", "Beta Seals"]}]},
}

EMPTY = {
    "/api/kg/tenders": {"query": "x", "terms": ["x"], "count": 0, "tenders": []},
    "/api/kg/precedent": {"precedent_found": False, "tenders": [], "retrieval_error": None},
    "/api/kg/resolve": {"ref": "x", "tier": None, "ambiguous": False, "matches": []},
    "/api/kg/prices": {"precedent_found": False, "rows": [], "retrieval_error": None},
    "/api/kg/clauses": {"count": 0, "clauses": [], "retrieval_error": None},
    "/api/kg/boilerplate": {"section": "mse", "count": 0, "clauses": [], "retrieval_error": None},
    "/api/kg/documents": {"case_key": "PUR-1", "count": 0, "documents": [], "best_nit": None},
    "/api/kg/case_summary": {"cases": []},
}


class KG:
    """fetch(kg_url, path, params) from REPLIES; `fail` makes the next n calls
    raise `error` (default: connection refused)."""

    def __init__(self, replies=None, fail: int = 0, error: Exception | None = None):
        self.replies = dict(REPLIES if replies is None else replies)
        self.fail, self.error = fail, error
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, kg_url, path, params):
        self.calls.append((path, dict(params)))
        if self.fail:
            self.fail -= 1
            raise self.error or urllib.error.URLError("connection refused")
        if path not in self.replies:
            raise urllib.error.HTTPError(kg_url + path, 404, "not found", {}, None)
        return json.loads(json.dumps(self.replies[path]))


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://kg/x", code, "err", {}, None)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    slept = []
    monkeypatch.setattr(tools, "_sleep", slept.append)
    return slept


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


def _ctx(tmp_path, conn, kg=None):
    return make_ctx(tmp_path, fetch=kg if kg is not None else KG(), conn=conn)


def _rows(conn, **kw):
    return audit.events(conn, **kw)


# one good call per contract: (name, args, KG paths it reads)
GOOD = {
    "tenders": ({"query": "gasket"}, ["/api/kg/tenders"]),
    "resolve_reference": ({"ref": "OC01000435"}, ["/api/kg/resolve"]),
    "price_history": ({"requirement": "gasket", "limit": 3}, ["/api/kg/prices"]),
    "quantity_history": ({"requirement": "gasket"}, ["/api/kg/prices"]),
    "clauses": ({"requirement": "gasket", "kind": "pqc"}, ["/api/kg/clauses"]),
    "documents": ({"case_key": "PUR-1"}, ["/api/kg/documents"]),
    "boilerplate": ({"section": "mse"}, ["/api/kg/boilerplate"]),
    "enrichment": ({"case_key": "PUR-1"}, ["/api/kg/case", "/api/kg/case_summary"]),
    "suppliers": ({"requirement": "gasket"}, ["/api/kg/tenders", "/api/kg/case_summary"]),
    "tender_record": ({"case_key": "PUR-1"}, ["/api/kg/case"]),
}

# per contract, arguments its input schema must refuse
BAD = {
    "tenders": [{}, {"query": "g"}, {"query": "gasket", "limit": 0},
                {"query": "gasket", "mode": "fuzzy"}, {"query": "gasket", "sql": "x"}],
    "resolve_reference": [{}, {"ref": "ab"}, {"ref": 12345}],
    "price_history": [{}, {"requirement": "gasket", "limit": 11},
                      {"requirement": "x" * 201}],
    "quantity_history": [{}, {"requirement": "gasket", "limit": "3"}],
    "clauses": [{"requirement": "gasket"}, {"requirement": "gasket", "kind": "boq"}],
    "documents": [{}, {"case_key": "../../etc"}, {"case_key": "PUR 1"}],
    "boilerplate": [{}, {"section": "anything"}, {"section": "mse", "limit": 11}],
    "enrichment": [{}, {"case_key": "PUR-1", "extra": 1}, {"case_key": ""}],
    "suppliers": [{}, {"requirement": "gasket", "limit": 21}],
    "tender_record": [{}, {"case_key": "../x"}, {"case_key": "PUR-1", "item_limit": 5}],
}


def test_every_contract_is_covered_here():
    assert set(GOOD) == set(CONTRACTS) == set(BAD)


def test_the_flows_name_only_real_tools():
    assert playbooks.TOOLS == set(CONTRACTS) | {"ocr_document"}
    for flow in playbooks.registry().values():
        assert set(flow.tools) <= playbooks.TOOLS, flow.id


def test_every_contract_result_carries_source_and_authority():
    for c in CONTRACTS.values():
        assert c.effect == READ
        assert {"found", "source", "authority", "warnings"} <= set(
            c.output_schema["required"]), c.name


# -- per contract: schema and failure paths -----------------------------------

@pytest.mark.parametrize("name", sorted(GOOD))
def test_contract_good_call(name, tmp_path, conn):
    args, paths = GOOD[name]
    kg = KG()
    result = tools.call(_ctx(tmp_path, conn, kg), name, args)
    assert schema.validate(result, CONTRACTS[name].output_schema) == []
    assert result["found"] is True
    assert result["authority"] == "precedent" and result["source"]["system"] == "kg"
    assert [p for p, _ in kg.calls] == paths
    [row] = _rows(conn)
    assert (row["kind"], row["name"], row["effect"], row["status"], row["attempts"]) == \
        ("tool", name, READ, "ok", 1)


@pytest.mark.parametrize("name", sorted(BAD))
def test_contract_refuses_bad_input_before_asking(name, tmp_path, conn):
    kg = KG()
    for args in BAD[name]:
        with pytest.raises(NamError) as err:
            tools.call(_ctx(tmp_path, conn, kg), name, args)
        assert err.value.code == "schema" and err.value.details, args
    assert kg.calls == []
    assert [r["status"] for r in _rows(conn)] == ["rejected"] * len(BAD[name])


@pytest.mark.parametrize("name", sorted(GOOD))
def test_contract_kg_down_is_unavailable_not_no_data(name, tmp_path, conn, no_backoff):
    kg = KG(fail=99)
    with pytest.raises(NamError) as err:
        tools.call(_ctx(tmp_path, conn, kg), name, GOOD[name][0])
    assert err.value.code == "kg_unavailable"
    assert err.value.body()["error"]["retryable"] is True
    assert "not the same as 'no data'" in err.value.message
    assert len(kg.calls) == 3 and no_backoff == [0.25, 0.5]
    [row] = _rows(conn)
    assert (row["status"], row["error_code"], row["attempts"]) == \
        ("unavailable", "kg_unavailable", 3)


@pytest.mark.parametrize("name", sorted(set(GOOD) - {"enrichment"}))
def test_contract_empty_kg_is_no_data(name, tmp_path, conn):
    result = tools.call(_ctx(tmp_path, conn, KG(EMPTY)), name, GOOD[name][0])
    assert result["found"] is False
    assert schema.validate(result, CONTRACTS[name].output_schema) == []
    assert _rows(conn)[0]["status"] == "ok"


@pytest.mark.parametrize("name", sorted(GOOD))
def test_contract_result_is_deterministic(name, tmp_path, conn):
    context = _ctx(tmp_path, conn)
    assert tools.call(context, name, GOOD[name][0]) == tools.call(context, name, GOOD[name][0])


def test_enrichment_unknown_tender_is_no_data(tmp_path, conn):
    replies = {k: v for k, v in REPLIES.items() if k != "/api/kg/case"}
    result = tools.call(_ctx(tmp_path, conn, KG(replies)), "enrichment", {"case_key": "PUR-404"})
    assert result["found"] is False and result["cite"] is None


def test_kg_not_configured_is_unavailable(tmp_path, conn):
    context = _ctx(tmp_path, conn)
    context.kg_url = ""
    with pytest.raises(NamError) as err:
        tools.call(context, "tenders", {"query": "gasket"})
    assert err.value.code == "kg_unavailable" and "NAM_KG_URL" in err.value.message


def test_retrieval_error_without_rows_is_unavailable_with_rows_a_warning(tmp_path, conn):
    broken = {**EMPTY, "/api/kg/clauses": {"clauses": [], "retrieval_error": "pgvector down"}}
    with pytest.raises(NamError) as err:
        tools.call(_ctx(tmp_path, conn, KG(broken)), "clauses",
                   {"requirement": "gasket", "kind": "pqc"})
    assert err.value.code == "kg_unavailable"
    partial = {**REPLIES, "/api/kg/clauses": {**REPLIES["/api/kg/clauses"],
                                              "retrieval_error": "semantic lane off"}}
    result = tools.call(_ctx(tmp_path, conn, KG(partial)), "clauses",
                        {"requirement": "gasket", "kind": "pqc"})
    assert result["found"] and "partial" in result["warnings"][0]


def test_http_5xx_is_retried_4xx_is_not(tmp_path, conn):
    kg = KG(fail=2, error=_http(503))
    assert tools.call(_ctx(tmp_path, conn, kg), "tenders", {"query": "gasket"})["found"]
    assert len(kg.calls) == 3 and _rows(conn)[0]["attempts"] == 3
    kg = KG(fail=5, error=_http(422))
    with pytest.raises(NamError) as err:
        tools.call(_ctx(tmp_path, conn, kg), "tenders", {"query": "gasket"})
    assert err.value.code == "bad_request" and len(kg.calls) == 1


def test_a_garbled_kg_body_is_unavailable(tmp_path, conn):
    kg = KG(fail=9, error=json.JSONDecodeError("Expecting value", "<html>", 0))
    with pytest.raises(NamError) as err:
        tools.call(_ctx(tmp_path, conn, kg), "resolve_reference", {"ref": "PUR-1"})
    assert err.value.code == "kg_unavailable"


def test_documents_never_expose_the_kg_host_path(tmp_path, conn):
    result = tools.call(_ctx(tmp_path, conn), "documents", {"case_key": "PUR-1"})
    assert "/srv/corpus" not in json.dumps(result)
    assert result["best_nit"] == "nit.pdf"


def test_contract_shapes(tmp_path, conn):
    context = _ctx(tmp_path, conn)
    resolved = tools.call(context, "resolve_reference", {"ref": "OC01000435"})
    assert resolved["ambiguous"] and [m["cite"]["case_key"] for m in resolved["matches"]] == \
        ["PUR-3", "PUR-9"]
    qty = tools.call(context, "quantity_history", {"requirement": "gasket"})
    assert len(qty["rows"]) == 1 and qty["rows"][0]["typical_qty"]["n"] == 2
    enriched = tools.call(context, "enrichment", {"case_key": "PUR-1"})
    assert enriched["fields"]["estimated_value"] == "450000"
    assert enriched["awarded_to"] == ["Acme Gaskets", "Beta Seals"]
    assert enriched["counts"] == {"boq_lines": 4, "pqc_clauses": 3, "scc_clauses": 2,
                                  "spec_clauses": 1}
    sup = tools.call(context, "suppliers", {"requirement": "gasket"})
    assert [(s["name"], s["awards"]) for s in sup["suppliers"]] == \
        [("Acme Gaskets", 2), ("Beta Seals", 1)]
    similar = tools.call(context, "tenders", {"query": "gasket", "mode": "similar"})
    assert similar["best_similarity"] == 0.812 and similar["tenders"][0]["cite"]["case_key"] \
        == "PUR-1"


# -- the layer: output checks, timeouts, unknown tools ------------------------

def _tool(name="probe", effect=READ, fn=None, out=None, **kw) -> tools.NamTool:
    in_schema = {"type": "object", "additionalProperties": False,
                 "properties": {"x": {"type": "integer"}}}
    out_schema = out or {"type": "object", "required": ["found"],
                         "properties": {"found": {"type": "boolean"}}}
    return tools.NamTool(name, "a probe", effect, in_schema, out_schema,
                         fn or (lambda **a: {"found": True}), **kw)


def test_unknown_tool_is_refused_and_audited(tmp_path, conn):
    with pytest.raises(NamError) as err:
        tools.call(_ctx(tmp_path, conn), "sap_delete_everything", {})
    assert err.value.code == "bad_request"
    assert _rows(conn)[0]["status"] == "rejected"


def test_a_result_outside_the_contract_is_never_passed_on(tmp_path, conn):
    kg = KG({**REPLIES, "/api/kg/prices": {**REPLIES["/api/kg/prices"], "rows": [
        {**REPLIES["/api/kg/prices"]["rows"][0], "unit_price": {"n": 1, "min": "cheap",
                                                                "median": "1", "max": "1"}}]}})
    with pytest.raises(NamError) as err:
        tools.call(_ctx(tmp_path, conn, kg), "price_history", {"requirement": "gasket"})
    assert err.value.code == "internal_schema"
    assert len(kg.calls) == 1                       # a wrong answer is not retried
    assert _rows(conn)[0]["status"] == "bad_output"


def test_a_slow_read_times_out_and_is_retried(tmp_path, conn):
    calls = []

    def slow(**a):
        calls.append(1)
        if len(calls) == 1:
            time.sleep(0.5)
        return {"found": True}

    probe = _tool(fn=slow, timeout_s=0.05)
    assert tools.call(_ctx(tmp_path, conn), "probe", {}, tools={"probe": probe})["found"]
    assert len(calls) == 2 and _rows(conn)[0]["attempts"] == 2


def test_a_tool_bug_is_internal_and_audited(tmp_path, conn):
    def broken(**a):
        raise KeyError("oops")

    with pytest.raises(NamError) as err:
        tools.call(_ctx(tmp_path, conn), "probe", {}, tools={"probe": _tool(fn=broken)})
    assert err.value.code == "internal" and "oops" not in err.value.message
    row = _rows(conn)[0]
    assert (row["status"], row["error_code"], row["attempts"]) == ("failed", "internal", 1)


def test_a_tool_must_declare_a_known_effect():
    with pytest.raises(ValueError):
        _tool(effect="maybe")


# -- writes: never retried, idempotent ----------------------------------------

class Counter:
    def __init__(self, raise_=None, result=None):
        self.n, self.keys, self.raise_ = 0, [], raise_
        self.result = result

    def __call__(self, idempotency_key, **args):
        self.n += 1
        self.keys.append(idempotency_key)
        if self.raise_:
            raise self.raise_
        return self.result or {"found": True, "n": self.n}


def _writer(fn, effect=WRITE):
    return {"w": _tool("w", effect, fn=fn, out={"type": "object", "required": ["found"],
                                                 "properties": {"found": {"type": "boolean"},
                                                                "n": {"type": "integer"}}})}


def test_a_write_needs_a_key(tmp_path, conn):
    fn = Counter()
    for key in ("", "short", "has space in it"):
        with pytest.raises(NamError) as err:
            tools.call(_ctx(tmp_path, conn), "w", {"x": 1}, idempotency_key=key,
                       tools=_writer(fn))
        assert err.value.code == "bad_request"
    assert fn.n == 0


def test_a_write_replays_instead_of_running_twice(tmp_path, conn):
    fn, context = Counter(), _ctx(tmp_path, conn)
    first = tools.call(context, "w", {"x": 1}, idempotency_key="pr-submit-0001",
                       tools=_writer(fn))
    again = tools.call(context, "w", {"x": 1}, idempotency_key="pr-submit-0001",
                       tools=_writer(fn))
    assert first == again == {"found": True, "n": 1}
    assert fn.n == 1 and fn.keys == ["pr-submit-0001"]
    assert [r["status"] for r in _rows(conn)] == ["ok", "replayed"]
    with pytest.raises(NamError) as err:
        tools.call(context, "w", {"x": 2}, idempotency_key="pr-submit-0001", tools=_writer(fn))
    assert err.value.code == "bad_request" and fn.n == 1
    assert tools.call(context, "w", {"x": 2}, idempotency_key="pr-submit-0002",
                      tools=_writer(fn))["n"] == 2


def test_keys_are_per_tenant(tmp_path, conn):
    fn, context = Counter(), _ctx(tmp_path, conn)
    for tenant in ("nrl", "iocl"):
        with store.acting(store.Actor(tenant, "u1")):
            tools.call(context, "w", {"x": 1}, idempotency_key="same-key-01", tools=_writer(fn))
    assert fn.n == 2


def test_a_failed_write_is_not_retried(tmp_path, conn, no_backoff):
    fn, context = Counter(raise_=Unavailable("SAP down")), _ctx(tmp_path, conn)
    with pytest.raises(NamError) as err:
        tools.call(context, "w", {"x": 1}, idempotency_key="write-key-01", tools=_writer(fn))
    assert err.value.code == "kg_unavailable" and fn.n == 1 and no_backoff == []
    assert _rows(conn)[0]["attempts"] == 1
    # the backend said it did not act, so the same key may be sent again
    fn.raise_ = None
    assert tools.call(context, "w", {"x": 1}, idempotency_key="write-key-01",
                      tools=_writer(fn))["n"] == 2


def test_a_write_that_stopped_midway_is_never_run_again(tmp_path, conn):
    fn, context = Counter(raise_=RuntimeError("connection reset mid-write")), \
        _ctx(tmp_path, conn)
    with pytest.raises(NamError) as err:
        tools.call(context, "w", {"x": 1}, idempotency_key="write-key-02", tools=_writer(fn))
    assert err.value.code == "internal"
    fn.raise_ = None
    with pytest.raises(NamError) as err:
        tools.call(context, "w", {"x": 1}, idempotency_key="write-key-02", tools=_writer(fn))
    assert err.value.code == "internal" and "unknown" in err.value.message and fn.n == 1


def test_a_write_answering_outside_its_contract_is_not_run_again(tmp_path, conn):
    fn, context = Counter(result={"found": "yes"}), _ctx(tmp_path, conn)
    with pytest.raises(NamError) as err:
        tools.call(context, "w", {"x": 1}, idempotency_key="write-key-03", tools=_writer(fn))
    assert err.value.code == "internal_schema"
    with pytest.raises(NamError):
        tools.call(context, "w", {"x": 1}, idempotency_key="write-key-03", tools=_writer(fn))
    assert fn.n == 1


def test_a_write_still_running_is_in_progress(tmp_path):
    db = tmp_path / "w.db"
    gate, entered = threading.Event(), threading.Event()

    def slow(idempotency_key, **a):
        entered.set()
        gate.wait(5)
        return {"found": True}

    results = []

    def first():
        c = sqlite3.connect(db)
        results.append(tools.call(make_ctx(tmp_path, conn=c), "w", {"x": 1},
                                  idempotency_key="write-key-04", tools=_writer(slow)))

    t = threading.Thread(target=first)
    t.start()
    assert entered.wait(5)
    with pytest.raises(NamError) as err:
        tools.call(make_ctx(tmp_path, conn=sqlite3.connect(db)), "w", {"x": 1},
                   idempotency_key="write-key-04", tools=_writer(slow))
    assert err.value.code == "in_progress" and err.value.body()["error"]["retryable"]
    gate.set()
    t.join(5)
    assert results == [{"found": True}]


def test_an_abandoned_write_claim_is_outcome_unknown(tmp_path, conn, monkeypatch):
    fn, context = Counter(), _ctx(tmp_path, conn)
    tools.call(context, "w", {"x": 1}, idempotency_key="write-key-05", tools=_writer(fn))
    conn.execute("UPDATE nam_tool_results SET state = 'running', "
                 "at = datetime('now', '-1 hour')")
    conn.commit()
    with pytest.raises(NamError) as err:
        tools.call(context, "w", {"x": 1}, idempotency_key="write-key-05", tools=_writer(fn))
    assert err.value.code == "internal" and fn.n == 1


# -- who may call what --------------------------------------------------------

def test_an_irreversible_tool_runs_only_from_the_confirm_path(tmp_path, conn):
    fn, context = Counter(), _ctx(tmp_path, conn)
    with store.acting(store.Actor("nrl", "u1", flow="pr.submit", roles=("U1",))), \
            pytest.raises(NamError) as err:
        tools.call(context, "w", {"x": 1}, idempotency_key="submit-key-1",
                   tools=_writer(fn, IRREVERSIBLE))
    assert err.value.code == "forbidden_action" and fn.n == 0
    with store.acting(store.Actor("nrl", "u1", flow=tools.ORCHESTRATOR, roles=("U1",))):
        tools.call(context, "w", {"x": 1}, idempotency_key="submit-key-1",
                   tools=_writer(fn, IRREVERSIBLE))
    assert fn.n == 1
    assert [r["status"] for r in _rows(conn)] == ["forbidden", "ok"]


def test_a_flow_calls_only_the_tools_it_lists(tmp_path, conn):
    context = _ctx(tmp_path, conn)
    with store.acting(store.Actor("nrl", "u1", flow="estimate.goods", roles=("U1",))):
        assert tools.call(context, "price_history", {"requirement": "gasket"})["found"]
        with pytest.raises(NamError) as err:
            tools.call(context, "clauses", {"requirement": "gasket", "kind": "pqc"})
    assert err.value.code == "forbidden_action" and "price_history" in err.value.details[0]


def test_a_tool_with_roles_checks_them(tmp_path, conn):
    probe = {"probe": _tool(roles=("U3",))}
    context = _ctx(tmp_path, conn)
    with store.acting(store.Actor("nrl", "u1", roles=("U1",))), \
            pytest.raises(NamError) as err:
        tools.call(context, "probe", {}, tools=probe)
    assert err.value.code == "forbidden_role"
    with store.acting(store.Actor("nrl", "u3", roles=("U1", "U3"))):
        assert tools.call(context, "probe", {}, tools=probe)["found"]


# -- the audit log ------------------------------------------------------------

def test_one_audit_row_per_call_whatever_the_outcome(tmp_path, conn):
    context = _ctx(tmp_path, conn)
    with store.acting(store.Actor("nrl", "u7", flow="precedent.search", run_id="run_x",
                                  case_id="NAM-2026-0001", roles=("U1",))):
        tools.call(context, "tenders", {"query": "gasket"})
        for bad in ({"query": ""}, {"nope": 1}):
            with pytest.raises(NamError):
                tools.call(context, "tenders", bad)
        with pytest.raises(NamError):
            tools.call(context, "documents", {"case_key": "PUR-1"})     # not its tool
    rows = _rows(conn, run_id="run_x")
    assert [r["status"] for r in rows] == ["ok", "rejected", "rejected", "forbidden"]
    assert {(r["tenant"], r["user"], r["flow"], r["case_id"]) for r in rows} == \
        {("nrl", "u7", "precedent.search", "NAM-2026-0001")}
    assert rows[0]["detail"]["args_hash"] and "gasket" in rows[0]["detail"]["args"]


def test_the_audit_log_is_append_only(tmp_path, conn):
    tools.call(_ctx(tmp_path, conn), "tenders", {"query": "gasket"})
    for sql in ("UPDATE nam_events SET status = 'ok'", "DELETE FROM nam_events"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute(sql)


def test_a_run_audits_the_flows_own_kg_fetches(tmp_path, conn):
    context = make_ctx(tmp_path, fetch=fake_kg(), conn=conn)
    body = run(ask("We need spiral wound gaskets"), context, OFFICER)
    rows = _rows(conn, run_id=body["run_id"], kind="kg_fetch")
    assert rows and {r["name"] for r in rows} >= {"/api/kg/tenders", "/api/kg/precedent"}
    assert {r["flow"] for r in rows} == {"need.capture"}
    assert all(r["tenant"] == "nrl" and r["status"] == "ok" for r in rows)


def test_a_run_audits_failed_kg_fetches_too(tmp_path, conn):
    context = make_ctx(tmp_path, fetch=fake_kg(down=True), conn=conn)
    body = run(ask("We need spiral wound gaskets"), context, OFFICER)
    rows = _rows(conn, run_id=body["run_id"], kind="kg_fetch")
    assert rows and all(r["status"] == "unavailable" for r in rows)


# -- for a model --------------------------------------------------------------

def test_model_tools_expose_reads_and_report_errors_as_json(tmp_path, conn):
    context = _ctx(tmp_path, conn, KG(fail=99))
    [tool] = tools.model_tools(context, ["price_history"])
    assert tool.name == "nam_price_history"
    assert tool.input_schema == CONTRACTS["price_history"].input_schema
    out = json.loads(tool.fn(requirement="gasket"))
    assert out["error"]["code"] == "kg_unavailable" and out["error"]["retryable"]
    # an argument the schema does not name is refused, not passed through
    out = json.loads(tool.fn(requirement="gasket", _name="suppliers"))
    assert out["error"]["code"] == "schema"


def test_model_tools_refuse_a_write(tmp_path, conn, monkeypatch):
    context = _ctx(tmp_path, conn)
    table = {**tools.registry(context), **_writer(Counter())}
    monkeypatch.setattr(tools, "registry", lambda ctx: table)
    with pytest.raises(ValueError, match="not given to a model"):
        tools.model_tools(context, ["w"])


def test_the_rejected_backend_error_is_not_retried(tmp_path, conn):
    calls = []

    def refuses(**a):
        calls.append(1)
        raise Rejected("bad id")

    with pytest.raises(NamError) as err:
        tools.call(_ctx(tmp_path, conn), "probe", {}, tools={"probe": _tool(fn=refuses)})
    assert err.value.code == "bad_request" and calls == [1]
