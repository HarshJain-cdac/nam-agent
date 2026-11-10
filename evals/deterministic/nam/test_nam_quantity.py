"""quantity.finalise (FRS F10, plan Step 15.5) and the officer's line edits
(playbooks/lines.py): the lines are seeded from the precedent or the form,
edited by the officer, and put to the quantity gate only once complete."""

import pytest
from nam_fakes import ctx
from nam_walk import call, confirm, goods_at_s3, services_at_s4

from nam_agent.nam import audit, need, orchestrator, store
from nam_agent.nam.playbooks import lines

Q = "Finalise the quantity"


def _quantity(c, case_id):
    return orchestrator._artifact(orchestrator.workflow(need.load(c.conn, case_id)), "quantity")


# -- the edit format ------------------------------------------------------------------

def test_edits_are_parsed_exactly():
    edits = lines.parse('2: qty=40, uom=Nos; 3: remove\nadd: item="valve, 2 inch", qty=5; '
                        "all: insurance=esi", {"item", "qty", "uom", "insurance"})
    assert [(e.target, e.fields, e.remove) for e in edits] == [
        ("2", {"qty": "40", "uom": "Nos"}, False), ("3", {}, True),
        ("add", {"item": "valve, 2 inch", "qty": "5"}, False),
        ("all", {"insurance": "esi"}, False)]
    assert edits[0].index == 1


@pytest.mark.parametrize("text", ["", "qty=4", "0: qty=4", "2: colour=red", "2: qty", "add: remove",
                                  "2: qty=4,, uom=x"])
def test_an_unreadable_edit_is_an_error(text):
    with pytest.raises(lines.LineError):
        lines.parse(text, {"qty", "uom"})


@pytest.mark.parametrize(("value", "ok"), [("20", True), ("1,200.5", True), ("0", False),
                                           ("-1", False), ("nan", False), ("inf", False),
                                           ("ten", False)])
def test_a_number_is_positive_and_finite(value, ok):
    if ok:
        assert lines.number(value, "qty") > 0
    else:
        with pytest.raises(lines.LineError):
            lines.number(value, "qty")


# -- the flow -------------------------------------------------------------------------

def test_the_precedents_boq_seeds_the_lines_and_the_gate_follows(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    body = call(c, case_id, Q)
    assert body["result"]["lines"] == [{"item": "ring joint gasket R24", "qty": 20, "uom": "Nos",
                                        "material_code": "MC-PUR-4-1", "source": "PUR-4 BOQ"}]
    assert any("precedent's" in w for w in body["warnings"])
    assert body["status"] == "needs_approval" and body["pending_action"]["kind"] == "gate:quantity"
    assert body["next"] == []                     # the approval comes first
    calls = [e["name"] for e in audit.events(c.conn, run_id=body["run_id"], kind="tool")]
    assert calls == ["tender_record"]
    assert confirm(c, case_id, body)["stage"] == "S4"


def test_edits_change_add_and_remove_lines(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    call(c, case_id, Q)
    body = call(c, case_id, Q, answer="lines",
                value="1: qty=30; add: item=ring joint gasket R31, qty=10, uom=Nos, code=MC-9")
    rows = body["result"]["lines"]
    assert [(r["item"], r["qty"], r["material_code"]) for r in rows] == [
        ("ring joint gasket R24", 30, "MC-PUR-4-1"), ("ring joint gasket R31", 10, "MC-9")]
    assert rows[0]["source"] == "PUR-4 BOQ, changed by the officer" and rows[1]["source"] == "officer"
    body = call(c, case_id, Q, answer="lines", value="1: remove")
    assert [r["item"] for r in body["result"]["lines"]] == ["ring joint gasket R31"]


def test_an_incomplete_line_holds_the_gate_and_gets_a_suggestion(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    call(c, case_id, Q)
    body = call(c, case_id, Q, answer="lines", value="add: item=ring joint gasket R31, uom=Nos")
    assert body["status"] == "needs_input" and body["pending_action"] is None
    assert body["result"]["missing"] == ["line 2 (ring joint gasket R31): quantity"]
    assert body["questions"][0]["id"] == "lines"
    hint = body["result"]["suggestions"][0]
    assert hint["line"] == 2 and hint["typical_qty"]["median"] == "20"
    assert _quantity(c, case_id)["complete"] is False
    assert _quantity(c, case_id)["lines"][1]["qty"] is None          # never applied
    refused = call(c, case_id, "Goods estimate")
    assert refused["error"]["code"] == "stage_gate"


def test_a_bad_edit_changes_nothing(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    call(c, case_id, Q)
    before = store.version_of(c.conn, case_id)
    body = call(c, case_id, Q, answer="lines", value="1: qty=40; 7: qty=3")
    assert any("no line 7" in w for w in body["warnings"])
    assert body["result"]["lines"][0]["qty"] == 20
    assert store.version_of(c.conn, case_id) == before


def test_a_codified_line_without_its_code_is_flagged(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    call(c, case_id, "Confirm the classification", answer="subtype", value="Codified")
    call(c, case_id, Q)
    body = call(c, case_id, Q, answer="lines", value="add: item=gasket R40, qty=2, uom=Nos")
    assert any("line 2: a Codified item needs its material code" in w for w in body["warnings"])


def test_a_change_after_the_gate_is_rework(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    confirm(c, case_id, call(c, case_id, Q))
    body = call(c, case_id, Q, answer="lines", value="1: qty=25")
    assert body["stage"] == "S3" and body["pending_action"]["kind"] == "gate:quantity"


def test_the_form_seeds_a_line_for_a_case_without_a_precedent(tmp_path):
    c = ctx(tmp_path)
    case_id = services_at_s4(c, "1: qty=3000, uom=MDY")
    rows = _quantity(c, case_id)["lines"]
    assert rows[0]["item"] == "housekeeping manpower" and rows[0]["source"] == \
        "guided form, changed by the officer"


def test_quantity_asks_only_for_lines(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    assert call(c, case_id, Q, answer="colour", value="red")["error"]["code"] == "bad_request"


def test_the_quantity_carries_the_scope_of_work_from_the_guided_form(tmp_path):
    c = ctx(tmp_path)
    case_id = services_at_s4(c, "1: qty=3000, uom=MDY")
    wf = orchestrator.workflow(need.load(c.conn, case_id))
    assert orchestrator._artifact(wf, "quantity")["scope"].startswith("daily cleaning")
