"""Knowledge and retrieval (plan Step 13): every fact on a case carries its
source and an authority flag, a fact without one is refused by the citation
guardrail, NRL's issued wording comes from the KG with its tender, and the
sources that are not connected say so."""

import copy
import importlib.util
from pathlib import Path

import pytest
from nam_fakes import ctx, fake_kg
from nam_walk import DEPT, call, goods_drafted

from nam_agent.nam import guardrails, knowledge, need, orchestrator, playbooks, store, trace


@pytest.mark.parametrize(("source", "schedule", "want"), [
    ({"tool": "price_history", "evidence": "ev_1", "path": "rows/0", "value": "450"}, None,
     "precedent"),
    ({"officer": "u1", "quote": "rate=245"}, "nrl_sor", "nrl_schedule"),
    ({"officer": "u1", "quote": "rate=7800"}, "dsr", "reference"),
    ({"officer": "u1", "quote": "rate=520"}, None, "officer"),
    ({"rule": "statutory", "pct": 13.13, "of": [0]}, None, "policy"),
    ({"rule": "round_up", "of": [2], "step": 50000}, None, "derived"),
    ({"line": 0, "component": 0}, None, "derived"),
    ("PUR-4", None, "precedent"), ("suggested from PUR-4", None, "precedent"),
    ("officer", None, "officer"), ("department", None, "officer"), ("policy", None, "policy"),
    ("", None, ""), ({}, None, ""), (None, None, ""),
])
def test_each_source_has_its_authority(source, schedule, want):
    assert knowledge.authority(source, schedule) == want


def _drafted(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_drafted(c)
    return c, case_id, orchestrator.workflow(need.load(c.conn, case_id))


def test_a_drafted_case_has_no_uncited_fact(tmp_path):
    _c, _id, wf = _drafted(tmp_path)
    arts = {n: orchestrator._artifact(wf, n) for n in ("estimate", "documents", "compliance")}
    found = knowledge.facts(arts)
    assert found and knowledge.uncited(arts) == []
    kinds = {f["authority"] for f in found}
    assert {"precedent", "policy", "derived"} <= kinds


@pytest.mark.parametrize("blank", ["clause", "rate", "requirement"])
def test_a_fact_without_its_source_is_found(tmp_path, blank):
    _c, _id, wf = _drafted(tmp_path)
    arts = copy.deepcopy({n: orchestrator._artifact(wf, n)
                          for n in ("estimate", "documents", "compliance")})
    if blank == "clause":
        doc = next(d for d in arts["documents"]["documents"] if d["kind"] == "scc")
        doc["clauses"][0]["source"] = ""
    elif blank == "rate":
        arts["estimate"]["lines"][0]["source"] = {}
    else:
        arts["compliance"]["requirements"][0]["source"] = ""
    bare = knowledge.uncited(arts)
    assert len(bare) == 1, bare


def test_the_citation_guardrail_refuses_an_uncited_clause(tmp_path):
    c, case_id, wf = _drafted(tmp_path)
    docs = copy.deepcopy(orchestrator._artifact(wf, "documents"))
    next(d for d in docs["documents"] if d["kind"] == "pqc")["clauses"][0]["source"] = ""
    flow = playbooks.registry()["docs.generate"]
    with store.acting(store.Actor(tenant="nrl", user="d1", run_id="run_x")):
        breaches, _ = guardrails._artifact(c.conn, flow, {}, need.load(c.conn, case_id), wf,
                                           "documents", docs)
    assert [b.rule for b in breaches] == ["citation"]
    assert "pqc clause 1" in breaches[0].says


def test_compliance_shows_nrls_issued_wording_with_its_tender(tmp_path):
    _c, _id, wf = _drafted(tmp_path)
    reqs = {r["id"]: r for r in orchestrator._artifact(wf, "compliance")["requirements"]}
    mse = reqs["mse_preference"]["issued"]
    assert [x["case_key"] for x in mse] == ["PUR-4", "PUR-6"] and "Micro and Small" in mse[0]["text"]
    assert [x["case_key"] for x in reqs["mse_emd_exemption"]["issued"]] == ["PUR-6"]
    assert "Delivery within" not in str(reqs)          # only the section's own wording
    assert reqs["make_in_india"]["issued"][0]["case_key"] == "PUR-6"
    assert "issued" not in reqs["oem_authorisation"]


def test_compliance_without_the_kg_still_proposes_and_says_so(tmp_path):
    c, case_id, _wf = _drafted(tmp_path)
    rec = need.load(c.conn, case_id)
    wf = orchestrator.workflow(rec)
    del wf["artifacts"]["compliance"]
    rec.workflow = wf
    need.save(c.conn, rec, reason="test: no compliance yet")
    down = ctx(tmp_path, fetch=fake_kg(down=True), conn=c.conn)
    body = call(down, case_id, "Build the compliance requirements", caller=DEPT)
    assert body["result"]["requirements"] and not any(
        r["issued"] for r in body["result"]["requirements"])
    assert any("could not be reached" in w for w in body["warnings"])


def test_the_trace_gives_each_fact_its_authority(tmp_path):
    c, case_id, _wf = _drafted(tmp_path)
    t = trace.build(c.conn, case_id)
    assert t["evidence"]["uncited"] == []
    assert t["evidence"]["estimate"][0]["authority"] == "precedent"
    assert {x["authority"] for x in t["evidence"]["clauses"]} == {"precedent"}
    assert any(f["authority"] == "policy" for f in t["evidence"]["facts"])


def test_the_sources_say_what_is_not_connected():
    on = {s["name"]: s for s in knowledge.sources(True)}
    off = {s["name"]: s for s in knowledge.sources(False)}
    assert on["past tenders"]["connected"] and not off["past tenders"]["connected"]
    assert not on["NRL SOR"]["connected"] and "officer types" in on["NRL SOR"]["note"]
    assert not on["procurement manual and circulars"]["connected"]
    assert on["NAM policy files"]["connected"] and off["NAM policy files"]["connected"]
    assert {s["authority"] for s in on.values()} - {""} <= set(knowledge.AUTHORITIES)


def _labels_script():
    path = Path(__file__).resolve().parents[3] / "scripts" / "nam_retrieval_labels.py"
    spec = importlib.util.spec_from_file_location("nam_retrieval_labels", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_label_builder_phrases_a_tender_by_its_subject(tmp_path):
    mod = _labels_script()
    assert mod.subject("Supply of Spiral Wound Gaskets for CDU 2024-25") == \
        "spiral wound gaskets cdu"
    assert mod.subject("Annual Rate Contract for Housekeeping Services at NRL") == \
        "housekeeping"
    for key in ("PUR-A-1", "PUR-B-2", "notacase"):
        (tmp_path / "Dept" / key).mkdir(parents=True)
    assert mod.case_keys([tmp_path]) == ["PUR-A-1", "PUR-B-2"]

