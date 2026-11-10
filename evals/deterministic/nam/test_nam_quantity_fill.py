"""DETERMINISTIC EVAL — a long quantity sheet can be completed without typing
every line.

A precedent's BOQ can bring hundreds of lines with no quantity. The officer
fills them in one step (answer "fill"): past tenders first (quantity_history),
then the AI for the rest, each line marked with where its value came from and
the AI's answer checked in code. Bulk edits ("3-5: uom=Nos", "incomplete:
remove") act on many lines at once. The sheet still reaches the quantity gate
only once every line has a quantity and a unit.
"""

from __future__ import annotations

import json
import re

from nam_fakes import ctx
from nam_walk import call, goods_at_s3

from nam_agent.nam import need, orchestrator


def _sheet(c, case_id):
    return orchestrator._artifact(orchestrator.workflow(need.load(c.conn, case_id)), "quantity")


def _with_lines(c, case_id, *items):
    call(c, case_id, "Finalise the quantity")
    value = "; ".join(f"add: item={i}" for i in items)
    return call(c, case_id, "Finalise the quantity", answer="lines", value=value)


def test_ranges_and_incomplete_edit_many_lines_at_once(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    _with_lines(c, case_id, "flange bolt", "hex nut", "washer plate")
    n = len(_sheet(c, case_id)["lines"])
    call(c, case_id, "Finalise the quantity", answer="lines", value=f"{n - 2}-{n}: uom=Set")
    rows = _sheet(c, case_id)["lines"]
    assert [r["uom"] for r in rows[-3:]] == ["Set"] * 3
    body = call(c, case_id, "Finalise the quantity", answer="lines", value="incomplete: remove")
    rows = _sheet(c, case_id)["lines"]
    assert all(r["qty"] is not None and r["uom"] for r in rows) and len(rows) == n - 3
    assert body["status"] == "needs_approval"                  # complete: the gate is put


def test_fill_takes_past_tenders_first_then_the_checked_ai(tmp_path):
    asked = []

    def model(system, user):
        if "Lines to fill:" not in user:
            return "{}"                                   # the other tasks: no model answer
        asked.append(user)
        asked_lines = re.findall(r"^(\d+)\. (.*?)(?=^\d+\. |\Z)", user.split("Lines to fill:")[1],
                                 re.MULTILINE | re.DOTALL)
        # a good entry, one for a line never asked, and the valve key with no number
        return json.dumps({"lines": [
            {"line": int(n), "qty": "lots" if "valve" in text else 12, "uom": "Nos"}
            for n, text in asked_lines] + [{"line": 999, "qty": 5, "uom": "Nos"}]})

    c = ctx(tmp_path)
    c.ask_model = model
    case_id = goods_at_s3(c)
    _with_lines(c, case_id, "spiral gasket 6in", "anchor bolt", "valve key")
    body = call(c, case_id, "Finalise the quantity", answer="fill", value="missing")
    rows = {r["item"]: r for r in _sheet(c, case_id)["lines"]}
    gasket = rows["spiral gasket 6in"]                     # the fake KG has gasket history
    assert gasket["qty"] == 20 and gasket["uom"] == "Nos"
    assert gasket["source"].startswith("past tenders")
    ai = rows["anchor bolt"]
    assert ai["qty"] == 12 and ai["source"] == "AI suggestion"
    assert rows["valve key"]["qty"] is None                # the AI's bad entry is not used
    assert "spiral gasket" not in asked[0].split("Lines to fill:")[1]   # past data won
    assert any("from past tenders" in w for w in body["warnings"])
    assert body["status"] == "needs_input"                 # one line still open: no gate


def test_fill_without_a_model_says_so_and_keeps_the_rest_open(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    _with_lines(c, case_id, "anchor bolt")
    body = call(c, case_id, "Finalise the quantity", answer="fill", value="missing")
    assert any("no AI model" in w for w in body["warnings"])
    assert _sheet(c, case_id)["lines"][-1]["qty"] is None


def test_lines_the_ai_left_out_are_asked_again(tmp_path):
    def model(system, user):
        if "Lines to fill:" not in user:
            return "{}"
        first = re.search(r"^(\d+)\. ", user, re.MULTILINE).group(1)    # answers one line a call
        return json.dumps({"lines": [{"line": int(first), "qty": 3, "uom": "Set"}]})

    c = ctx(tmp_path)
    c.ask_model = model
    case_id = goods_at_s3(c)
    _with_lines(c, case_id, "anchor bolt", "valve key", "pipe clamp")
    body = call(c, case_id, "Finalise the quantity", answer="fill", value="missing")
    assert all(r["qty"] is not None and r["uom"] for r in _sheet(c, case_id)["lines"])
    assert body["status"] == "needs_approval"           # complete: the gate is put


def test_the_sheet_starts_from_the_items_the_officer_asked_for(tmp_path):
    # live, 2026-11-03: "CPVC pipes, 200 mm, 5 RM" drafted 140 lines from a
    # civil-works precedent and none of them was the officer's pipe
    from nam_agent.nam.playbooks import quantity

    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    n = need.load(c.conn, case_id)
    n.followups = {**(n.followups or {}), "details": {"quantity": "5", "uom": "RM"},
                   "answers": {"q1": "200 mm", "d:quantity": "5", "d:uom": "RM"}}
    asked = quantity._asked_for(n)
    assert asked == [{"item": f"{n.raw_text}, 200 mm", "qty": 5, "uom": "RM"}]


def test_a_sheet_with_gaps_asks_how_to_fill_them_not_drafts_again(tmp_path):
    from nam_agent.nam import fill

    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    _with_lines(c, case_id, "anchor bolt")
    kinds = [x["kind"] for x in fill.needs(need.load(c.conn, case_id))]
    assert "quantity_gaps" in kinds and "run" not in kinds
