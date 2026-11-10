"""DETERMINISTIC EVAL — a screen edit reaches the harness as the flow's own answer.

The Procurix screens used to send an edit as a sentence ("Change the quantity
of X to 70") for the harness to interpret. Now nam_agent/nam/edits.py writes the
exact structured answer each flow takes (playbooks/lines.py format), names
the flow explicitly, and refuses what the format cannot carry. Every answer
here is checked with the harness's own parser and key sets.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nam_agent.nam import edits
from nam_agent.nam import need as nam
from nam_agent.nam.playbooks import lines

ROOT = Path(__file__).resolve().parents[3]


def _case(conn):
    need = nam.create(conn, "Supply of transmitters")
    art = {
        "quantity": {"lines": [{"item": "DPT 3in FL", "qty": 40, "uom": "NOS"},
                               {"item": "DPT 2in FLN", "qty": 70, "uom": "NOS"}]},
        "estimate": {"lines": [{"item": "DPT 3in FL", "qty": 40, "rate": 100.0},
                               {"item": "DPT 2in FLN", "qty": 70, "rate": 200.0},
                               {"item": "PPE reimbursement", "qty": 1, "rate": 5.0}]},
        "documents": {"documents": [{"kind": "scc", "clauses": [
            {"text": "Contract period: 12 months", "selected": True},
            {"text": "Old clause", "selected": False},
            {"text": "Termination: 60 days", "selected": True}]}]},
        "compliance": {"requirements": [{"id": "C1", "text": "CPSE"}, {"id": "C2", "text": "Govt"}]},
    }
    flows = {"estimate": "estimate.goods"}
    need.workflow = {"stage": "S5", "artifacts": {
        k: {"value": v, "flow": flows.get(k, "x"), "at_version": 1} for k, v in art.items()}}
    nam.save(conn, need)
    return nam.load(conn, need.need_id)


@pytest.fixture
def need():
    return _case(sqlite3.connect(":memory:"))


def test_quantity_edits_map_estimate_lines_to_quantity_lines(need):
    [(flow, qid, val)] = edits.plan(need, "quantity", {"edits": [
        {"line": 1, "qty": 75}, {"line": 0, "remove": True},
        {"add": {"item": "Gasket, spiral wound", "qty": 5, "uom": "NOS"}}]})
    assert (flow, qid) == ("quantity.finalise", "lines")
    assert val == '2: qty=75; 1: remove; add: item="Gasket, spiral wound", qty=5, uom=NOS'
    parsed = lines.parse(val, {"item", "qty", "uom", "code"})
    assert parsed[0].index == 1 and parsed[1].remove and parsed[2].fields["item"] == "Gasket, spiral wound"
    with pytest.raises(edits.EditError, match="worked out by the estimate"):
        edits.plan(need, "quantity", {"edits": [{"line": 2, "qty": 3}]})   # the PPE lump sum


def test_rates_go_to_the_flow_that_built_the_estimate(need):
    [(flow, qid, val)] = edits.plan(need, "rates", {"edits": [{"line": 2, "rate": 7.5}]})
    assert (flow, qid, val) == ("estimate.goods", "rates", "3: rate=7.5")
    assert lines.parse(val, {"rate", "kg"})[0].fields == {"rate": "7.5"}
    with pytest.raises(edits.EditError):
        edits.plan(need, "rates", {"edits": [{"line": 9, "rate": 1}]})


def test_a_changed_clause_is_dropped_and_added_with_its_reason(need):
    [(flow, qid, val)] = edits.plan(need, "clauses", {"doc": "scc", "edits": [
        {"clause": 2, "text": "Termination: 30 days; by NRL only", "why": "policy change"},
        {"add": "Safety of contractor personnel", "why": "checker found it missing"}]})
    assert (flow, qid) == ("docs.generate", "scc")
    parsed = lines.parse(val, {"keep", "why", "text", "required"})
    assert parsed[0].index == 2 and parsed[0].fields["keep"] == "no"
    assert parsed[1].target == "add" and parsed[1].fields["text"] == "Termination: 30 days, by NRL only"
    assert parsed[2].fields["why"] == "checker found it missing"
    with pytest.raises(edits.EditError, match="needs a reason"):
        edits.plan(need, "clauses", {"doc": "scc", "edits": [{"clause": 0, "keep": False}]})


def test_requirements_and_classification(need):
    [(flow, qid, val)] = edits.plan(need, "requirements", {"edits": [
        {"n": 1, "apply": False, "why": "not divisible"}]})
    assert (flow, qid) == ("compliance.build", "requirements")
    assert lines.parse(val, {"apply", "why", "text"})[0].fields == {"apply": "no", "why": "not divisible"}
    assert edits.plan(need, "classify", {"category": "goods", "tender_type": "Open"}) == [
        ("need.classify", "category", "goods"), ("need.classify", "tender_type", "Open")]
    # "Supply of ..." is a Goods case: its form has no service code
    assert edits.plan(need, "form", {"fields": {"service_code": "4400", "purpose": "x"}}) == [
        ("need.guided_form", "purpose", "x")]
    nam.apply_update(need, {"category": "services"})
    assert edits.plan(need, "form", {"fields": {"service_code": "4400", "purpose": " ",
                                                "specification": "only goods have this"}}) == [
        ("need.guided_form", "service_code", "4400")]


def test_apply_stops_at_the_first_refusal(need):
    sent = []

    def run(request):
        sent.append(request)
        return {"status": "refused", "error": {"code": "stage_gate", "message": "frozen"}}

    out = edits.apply(need, "classify", {"category": "goods", "tender_type": "Open"}, run)
    assert len(sent) == 1 and out["status"] == "error"
    assert sent[0]["system"] == "need.classify" and sent[0]["case_id"] == need.need_id


def test_the_dashboard_and_the_chat_are_wired():
    src = "".join((ROOT / "nam_agent/server" / n).read_text(encoding="utf-8") for n in ("nam_routes.py", "chat.py", "http.py"))
    assert 'route == "edit"' in src and '"/api/nam/norms"' in src
    assert "focus = chats.case_focus(payload)" in src
    session = (ROOT / "nam_agent/runtime/session.py").read_text(encoding="utf-8")
    assert "open on the Procurix" in session
