"""need.replicate (FRS F2, plan Step 15.1): create by copy, through
nam_agent.nam.run() against the scripted KG.

A named tender opens a case that is an exact copy of it, with that tender as
the selected precedent; anything short of one exact record (nothing found,
several tenders, a PR or PO number, the KG down) opens no case at all."""

import pytest
from nam_fakes import OFFICER, TENDERS, case_record, classified, ctx, fake_kg, run

from nam_agent.nam import audit, need, orchestrator, schema, store


def _valid(body):
    name = "error" if "error" in body else "response"
    assert schema.validate(body, schema.load(name)) == [], body
    if "error" not in body:
        assert schema.validate(body["result"], schema.load("flow.need.replicate")) == []
    return body


def _replicate(c, text, system="Create by copy", **extra):
    return _valid(run({"system": system, "messages": [{"role": "user", "content": text}],
                       **extra}, c))


def _cases(c):
    store.ensure(c.conn)
    return c.conn.execute("SELECT COUNT(*) FROM nam_cases").fetchone()[0]


def _replica(c, case_id):
    return orchestrator.workflow(need.load(c.conn, case_id))["artifacts"]["replica"]["value"]


def _with_case(change):
    """The scripted KG, with /api/kg/case changed by `change(record)`."""
    base = fake_kg()

    def fetch(kg_url, path, params):
        if path == "/api/kg/case":
            record = case_record(params["case_key"])
            change(record)
            return record
        return base(kg_url, path, params)
    return fetch


def test_a_named_tender_opens_an_exact_copy(tmp_path):
    c = ctx(tmp_path)
    body = _replicate(c, "replicate PUR-4")
    assert body["flow"] == "need.replicate" and body["status"] == "ok", body
    case_id = body["case_id"]
    record = need.load(c.conn, case_id)
    assert record.selected_cases == ["PUR-4"] and record.raw_text == "Supply of Ring Joint Gaskets"
    assert record.category["value"] == need.GOODS and "work category" in record.category["basis"]
    # need and precedent are both done; the procurement and tender type are not
    assert body["stage"] == "S1" and body["next"] == ["need.classify"]
    assert classified(c, case_id)["stage"] == "S3"
    assert body["citations"] == [{"source": "kg", "ref": "tender:PUR-4",
                                  "authority": "precedent"}]

    kg = case_record("PUR-4")
    replica = _replica(c, case_id)
    assert replica["precedent"] == "PUR-4" and replica["complete"] is True
    assert replica["clauses"]["pqc"] == kg["pqc"] and replica["clauses"]["scc"] == kg["scc"]
    assert replica["clauses"]["tech_spec"] == kg["tech_spec"]
    assert [(ln["item"], ln["material_code"], ln["qty"], ln["unit_price"], ln["historical"])
            for ln in replica["boq"]] == [(b["item"], b["material_code"], "20", "450.00", True)
                                          for b in kg["boq"]]
    assert replica["fields"]["tender_no"] == {"value": "OC01000435/RJ",
                                              "source": "kg /api/kg/case PUR-4"}
    assert replica["fields"]["emd_amount"]["value"] == "9,000"     # as recorded
    assert [d["filename"] for d in replica["documents"]] == ["nit.pdf"]
    assert "path" not in replica["documents"][0]                    # no KG host path
    assert body["result"]["copied"]["counts"] == {"boq": 1, "pqc": 1, "scc": 2, "tech_spec": 1,
                                                  "sor_references": 0, "documents": 1}
    assert any("historical" in w and "R7" in w for w in body["warnings"])


def test_every_read_is_an_audited_tool_call(tmp_path):
    c = ctx(tmp_path)
    body = _replicate(c, "replicate PUR-4")
    calls = [e["name"] for e in audit.events(c.conn, run_id=body["run_id"], kind="tool")]
    assert calls == ["resolve_reference", "tender_record", "documents"]


def test_a_tender_number_resolved_to_one_tender_counts_as_named(tmp_path):
    c = ctx(tmp_path)
    body = _replicate(c, "copy tender T-PUR-2 for the CDU again")
    assert body["status"] == "ok", body
    assert need.load(c.conn, body["case_id"]).selected_cases == ["PUR-2"]
    assert body["result"]["reference"] == {"ref": "T-PUR-2", "tier": "tender_no",
                                           "case_key": "PUR-2"}


def test_a_shared_base_number_is_asked_about_never_picked(tmp_path):
    c = ctx(tmp_path)
    asked = _replicate(c, "replicate OC01000435")
    assert asked["status"] == "needs_input" and asked["case_id"] is None
    assert _cases(c) == 0
    question = asked["questions"][0]
    assert question["id"] == "reference"
    assert [o["value"] for o in question["options"]] == ["PUR-4", "PUR-5"]
    picked = _replicate(c, "replicate OC01000435",
                        answer={"question_id": "reference", "value": "PUR-5"})
    assert picked["status"] == "ok", picked
    assert need.load(c.conn, picked["case_id"]).selected_cases == ["PUR-5"]


@pytest.mark.parametrize("text", ["replicate ZZ99999999", "replicate the usual one",
                                  "replicate PR 1000012345"])
def test_no_exact_record_opens_no_case(tmp_path, text):
    c = ctx(tmp_path)
    body = _replicate(c, text)
    assert body["status"] == "needs_input" and body["case_id"] is None, body
    assert body["questions"][0]["id"] == "reference"
    assert _cases(c) == 0
    if "PR" in text:
        assert any("SAP connector" in w for w in body["warnings"])
        assert not [e for e in audit.events(c.conn, run_id=body["run_id"], kind="tool")]


def test_a_reference_without_a_record_opens_no_case(tmp_path):
    base = fake_kg()

    def fetch(kg_url, path, params):
        if path == "/api/kg/resolve":
            return {"ref": params["ref"], "tier": "case_key", "ambiguous": False,
                    "matches": [{"case_key": "PUR-77", "tender_no": "T-77", "job_name": "x",
                                 "match": "case_key"}]}
        return base(kg_url, path, params)

    c = ctx(tmp_path, fetch=fetch)
    body = _replicate(c, "replicate PUR-77")
    assert body["status"] == "needs_input" and _cases(c) == 0


def test_the_kg_down_opens_no_case(tmp_path, monkeypatch):
    monkeypatch.setattr("nam_agent.nam.tools._sleep", lambda s: None)
    c = ctx(tmp_path, fetch=fake_kg(down=True))
    body = _replicate(c, "replicate PUR-4")
    assert body["error"]["code"] == "kg_unavailable" and "case_id" not in body
    assert _cases(c) == 0


def test_an_amount_that_is_not_a_number_is_left_empty_not_guessed(tmp_path):
    c = ctx(tmp_path, fetch=_with_case(lambda r: r["boq"][0].update(unit_price="Rs. 450/-")))
    body = _replicate(c, "replicate PUR-4")
    assert _replica(c, body["case_id"])["boq"][0]["unit_price"] is None
    assert any("could not be read as numbers" in w for w in body["warnings"])


def test_a_tender_larger_than_one_record_says_the_copy_is_not_whole(tmp_path):
    many = [{"item": f"gasket {n}", "uom": "Nos", "qty": 1} for n in range(2000)]
    c = ctx(tmp_path, fetch=_with_case(lambda r: r.update(boq=many)))
    body = _replicate(c, "replicate PUR-4")
    assert body["result"]["copied"]["complete"] is False
    assert _replica(c, body["case_id"])["complete"] is False
    assert any("not the whole tender" in w for w in body["warnings"])


@pytest.mark.parametrize(("recorded", "category"), [
    ("Works", need.WORKS), ("Manpower", need.SERVICES), ("Misc", need.GOODS), (None, need.GOODS),
])
def test_the_category_is_the_tenders_own_else_classified(tmp_path, recorded, category):
    c = ctx(tmp_path, fetch=_with_case(lambda r: r.update(work_category=recorded)))
    body = _replicate(c, "replicate PUR-4")          # "Supply of Ring Joint Gaskets": goods
    record = need.load(c.conn, body["case_id"])
    assert record.category["value"] == category
    sub = record.subtype["value"]
    assert sub is None or sub in need.SUBTYPES[category]


def test_an_unclassifiable_tender_waits_for_its_category(tmp_path):
    c = ctx(tmp_path, fetch=_with_case(lambda r: r.update(work_category=None,
                                                          job_name="Annual rate list 2023")))
    body = _replicate(c, "replicate PUR-4")
    assert body["status"] == "needs_input" and body["stage"] == "S1"
    assert any("case.edit" in w for w in body["warnings"])


def test_copying_the_same_tender_twice_is_flagged(tmp_path):
    c = ctx(tmp_path)
    first = _replicate(c, "replicate PUR-4")
    again = _replicate(c, "replicate PUR-4")
    assert any(first["case_id"] in w for w in again["warnings"]), again["warnings"]


def test_replicate_opens_a_case_and_refuses_one(tmp_path):
    c = ctx(tmp_path)
    case_id = _replicate(c, "replicate PUR-4")["case_id"]
    body = _replicate(c, "replicate PUR-1", system="run need.replicate", case_id=case_id)
    assert body["error"]["code"] in ("bad_request", "stage_gate"), body


def test_the_fake_kg_has_a_record_for_every_tender():
    assert {k for k, _, _ in TENDERS} == {case_record(k)["case_key"] for k, _, _ in TENDERS}


def test_replicate_is_permitted_for_the_officer_only(tmp_path):
    from nam_agent.nam import identity

    c = ctx(tmp_path)
    drafter = identity.Caller(tenant="nrl", user="d1", roles=("U4",))
    body = _valid(run({"system": "Create by copy",
                       "messages": [{"role": "user", "content": "replicate PUR-4"}]}, c,
                      drafter))
    assert body["error"]["code"] == "forbidden_role"
    assert OFFICER.roles == ("U1",)
