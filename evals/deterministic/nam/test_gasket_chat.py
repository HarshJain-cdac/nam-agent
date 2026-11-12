"""DETERMINISTIC EVAL: an officer's "i want to purchase gasket and capillary" chat.

  - Suggestions were named "Gasket replacement of HE cover" while their keys
    were air filter regulators: a name must say what its records are, and a
    suggestion's records are the ones NRL bought, not a search by the name.
  - "gasket" as goods had only service lines in SAP: the option says so.
  - Every question about the gasket services was refused as "found by
    meaning", even without calling them types: 6 to 9 refusals a turn.
  - "...has been added to the cart" when it had not: the sentence goes.
  - The reply wrote SAP's key in other capitals, a PUR case SAP holds nowhere,
    and a typed copy of the PUR cards.
  - The officer asked which PR to choose: the agent had nothing to compare.
"""

from __future__ import annotations

import sqlite3

from evals.deterministic.nam.test_cart_flow import URL
from evals.deterministic.nam.test_pr_flow import _kg
from nam_agent.nam import case, chatstate, stage_tools


def _l(code, desc, pr, kind="PO", date="2024-01-01", qty=2.0, po="4500000001"):
    return {"description": desc, "codes": {code: desc} if code else {}, "lines": [
        {"kind": kind, "doc_no": po, "date": date, "material": code or None, "pr_no": pr,
         "po_no": po, "quantity": qty, "unit": "NOS", "rate_inr": 100.0, "pur_case_id": "PUR-MECH-1"}]}


SAP = {
    "10000020": {"results": [_l("CAP-1", "TG CAPILLARY U600", "10000020"),
                             _l("AFR-1", "AFR RELIEVING 1/2 NPTF", "10000020"),
                             _l("TW-1", "THERMOWELL SS316 U320", "10000020")]},
    "10000030": {"results": [_l("", "Gasket replacement of HE cover", "10000030", kind="SERVICE"),
                             _l("AFR-1", "AFR RELIEVING 1/2 NPTF", "10000030")]},
    "AFR-1": {"results": [_l("AFR-1", "AFR RELIEVING 1/2 NPTF", "10000020")]},
    "capillary": {"results": [_l("CAP-1", "TG CAPILLARY U600", "10000020")]},
    "gasket": {"results": [_l("", "Gasket replacement of HE cover", "10000030", kind="SERVICE"),
                           _l("", "Valve gland tightening", "10000031", kind="SERVICE")]},
    # SAP's search by a made-up name finds other records
    "air filter": {"results": [_l("", "Filter cleaning above 4 inch", "10000040", kind="SERVICE")]},
    "thermowell": {"results": [_l("TW-1", "THERMOWELL SS316 U320", "10000020")]},
}


def _tools(tmp_path, sap=SAP):
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(sap)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    tools["_conn"] = conn
    return tools


def test_a_suggestion_is_named_for_its_own_records_and_is_those_records(tmp_path):
    t = _tools(tmp_path)
    token = chatstate.enter("chat-g")
    try:
        t["nam_need"].fn(category="goods", need="gasket and capillary", items=["gasket", "capillary"])
        t["nam_related"].fn()
        q = "Which go forward?"
        out = t["nam_choose"].fn(question=q, suggest=[
            {"name": "Gasket replacement of HE cover", "why": "x", "keys": ["AFR-1"]}])
        assert "is not what AFR-1 is (AFR RELIEVING 1/2 NPTF)" in out
        shown = []
        out = t["nam_choose"].fn(question=q, suggest=[
            {"name": "air filter regulator AFR", "why": "fitted on the gauge line",
             "keys": ["AFR-1"]}], _notify=lambda k, e: shown.append(e))
    finally:
        chatstate.leave(token)
    assert out.startswith('Asked: "Which go forward?"')
    token = chatstate.enter("chat-g")
    try:
        # bought together once is no pattern: the thermowell was on one PR of three
        once = t["nam_choose"].fn(question="Which go forward?", suggest=[
            {"name": "thermowell", "why": "fitted with it", "keys": ["TW-1"]}])
    finally:
        chatstate.leave(token)
    assert "on only one of the need's 3 past PRs" in once
    data = case.for_session(t["_conn"], "chat-g")
    sug = data["suggested"][0]
    assert [c["key"] for c in sug["candidates"]] == ["AFR-1"]        # NRL's record, not a search
    # "gasket" is goods with only service lines in SAP: the option says so
    assert "gasket (2 service lines, no material in SAP)" in shown[-1]["options"]


def test_records_found_by_meaning_may_be_asked_about_unless_called_types(tmp_path):
    t = _tools(tmp_path)
    token = chatstate.enter("chat-g2")
    try:
        t["nam_need"].fn(category="services", need="gasket", items=["gasket"])
        opts = [{"label": "HE cover replacement", "words": ["cover"]},
                {"label": "Valve gland tightening", "words": ["valve", "gland"]}]
        refused = t["nam_ask"].fn(question="Which type of gasket do you need?", options=opts)
        assert "found by meaning, not a type of 'gasket'" in refused
        out = t["nam_ask"].fn(question="Which gasket work do you need?", options=opts)
    finally:
        chatstate.leave(token)
    assert out.startswith('Asked: "Which gasket work do you need?"')


def test_a_false_cart_claim_goes_and_the_case_says_where_it_stands(tmp_path):
    t = _tools(tmp_path)
    token = chatstate.enter("chat-g3")
    try:
        t["nam_need"].fn(category="goods", need="capillary", items=["capillary"])
    finally:
        chatstate.leave(token)
    reply = "The capillary has been added to the cart.\n\nAnything else?"
    out = stage_tools.check_reply(t["_conn"], "chat-g3", reply, [])
    assert "has been added to the cart" not in out
    assert "'capillary' is not in the cart yet" in out


def test_keys_in_any_capitals_made_up_purs_and_a_copy_of_the_pur_cards_go():
    data = {"items": [{"name": "gasket", "candidates": [
        {"key": "SAP:gasket replacement of he cover,all sizes", "code": "",
         "description": "Gasket replacement of HE cover,All sizes",
         "lines": [{"pur_case_id": "PUR-MECHMNT-1009590"}]}]}]}
    reply = ("Pick **SAP:gasket replacement of HE cover,All sizes**.\n"
             "It was ordered under PUR-2024-0012 at 1,200.\n\n"
             "| Match | PUR |\n|---|---|\n| 100% | PUR-MECHMNT-1009590 |\n| 50% | PUR-MECHMNT-1009590 |\n\n"
             "Shall I go on?")
    out = stage_tools.plain_words(reply, data, [{"tool": "nam_tenders", "output": ""}])
    assert out == "Pick Gasket replacement of HE cover,All sizes.\n\nShall I go on?"


def test_the_agent_can_explain_which_pr_when_a_record_was_bought_on_several():
    rec = {"key": "G-1", "code": "G-1", "description": "GASKET SPW 4IN", "lines": [
        {"kind": "PO", "pr_no": "10000050", "po_no": "4500000050", "date": "2026-08-31",
         "quantity": 3.0, "unit": "NOS", "rate_inr": 18380.8, "pur_case_id": "PUR-A-1"},
        {"kind": "PO", "pr_no": "10000049", "po_no": "4500000049", "date": "2025-04-26",
         "quantity": 5.0, "unit": "NOS", "rate_inr": 17000.0, "pur_case_id": "PUR-A-2"}]}
    table = stage_tools.records_table([rec])
    assert "'GASKET SPW 4IN' is on 2 PRs (the latest first)" in table
    assert "- PR 0010000050: 2026-08-31" in table and "quantity 3 NOS" in table
    assert "- PR 0010000049: 2025-04-26" in table


def test_before_the_choice_the_reminder_asks_the_choice_never_an_item_question(tmp_path):
    t = _tools(tmp_path)
    token = chatstate.enter("chat-g4")
    try:
        t["nam_need"].fn(category="goods", need="gasket and capillary", items=["gasket", "capillary"])
    finally:
        chatstate.leave(token)
    text, ask = stage_tools.reminder(t["_conn"], "chat-g4")
    assert text == "Which of these do you want to take forward?" and ask["multi"] is True
    assert ask["options"] == ["gasket (2 service lines)", "capillary (1 material)",
                              stage_tools.ALL]
    # the officer's click reads the choice; then the item's own questions
    tool, args = stage_tools.read_click(t["_conn"], "chat-g4", "capillary (1 material)")
    assert tool == "nam_choose"


def test_made_up_dates_are_never_offered_as_choices():
    from nam_agent.tools import clarify

    out = clarify.make_tool().fn(question="When do you need the gasket?",
                                 options=["2026-11-15", "2026-11-20", "2026-12-01"])
    assert out.startswith("Dates are the officer's to give")
