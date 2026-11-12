"""DETERMINISTIC EVAL — the PR Creation Flow in NAM (the manual, steps 1a-3b).

The agent works the flow with its tools; the harness keeps the case and
checks every gate the manual sets. These cases drive the tools the way the
agent would, against a fake knowledge graph shaped like the real one's
sap_search reply, and check what the harness lets through
and what it refuses (each refusal says why, so the agent can put it right).
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from evals.helpers import make_agent
from nam_agent.nam import (audit, case, chatstate, estimate, flow, identity, llm, narrow,
                      permissions, stage_tools)

URL = "http://127.0.0.1:9/mcp"     # nothing listens there


def _sap_res(code, desc, rate, po_date, pur, po="4500024964", pr="10073969"):
    return {"description": desc, "codes": {code: desc}, "lines": [
        {"kind": "PO", "doc_no": po, "date": po_date, "material": code, "quantity": 10.0,
         "unit": "NOS", "rate_inr": rate, "pur_case_id": pur, "pr_no": pr, "po_no": po},
        {"kind": "PR", "doc_no": pr, "date": "2021-09-28", "material": code, "quantity": 10.0,
         "unit": "NOS", "rate_inr": 90.0, "pur_case_id": pur, "pr_no": pr, "po_no": po}]}


SAP = {"status": "ok", "results": [
    _sap_res("I-TG-ARC-G080-01", "TG Bimetallic U100mm 6mm dia 0-80Cel", 100.0, "2024-03-01",
             "PUR-INSTMNT-1003188"),
    _sap_res("I-TG-ARC-G200-01", "TG Bimetallic U320mm 6mm dia 0-200Cel", 1060.0, "2022-12-28",
             "PUR-INSTMNT-1003188", po="4500025893"),
    _sap_res("I-TG-ARC-G200-02", "TG Bimetallic U400mm 6mm dia 0-200Cel", 1240.0, "2022-07-12",
             "PUR-INSTMNT-1002364", po="4500024953"),
    _sap_res("I-TE-TC-K0320-06-1", "Thermocouple , K ,U-320mm, 6mm", 2100.0, "2023-01-10",
             "PUR-INSTMNT-1000001", po="4500026000"),
    # raised in a PR, never ordered
    {"description": "Supply and installation of Temperature Gauge as per standard",
     "codes": {"I-TG-NEW-01": "Supply and installation of Temperature Gauge as per standard"},
     "lines": [{"kind": "PR", "doc_no": "10090001", "date": "2025-02-01",
                "material": "I-TG-NEW-01", "quantity": 2.0, "unit": "NOS", "rate_inr": 16800.0,
                "pur_case_id": "PUR-PRJNUM-1008552", "pr_no": "10090001", "po_no": None}]},
]}


def _kg(sap=SAP, fail=()):
    """A fake SAP: one reply for every search, or (a dict of replies) the
    reply whose key the query holds."""
    sent = []

    def call(url, tool, **args):
        sent.append(tool)
        if tool in fail:
            raise OSError("refused")
        if "results" in sap:
            return sap
        return next((v for k, v in sap.items() if k in args.get("query", "")), {"results": []})
    return call, sent


@pytest.fixture
def steps(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, sent = _kg()
    tools = {t.name: t for t in stage_tools.make_tools(conn, tmp_path, URL, call)}
    token = chatstate.enter("chat-1")
    shown = []
    tools["_shown"], tools["_sent"], tools["_conn"] = shown, sent, conn
    yield tools
    chatstate.leave(token)


def _ask(tools, end_turn=True, **kw):
    """The agent asks; with end_turn the officer's next message is a new turn."""
    out = tools["nam_ask"].fn(_notify=lambda kind, ev: tools["_shown"].append(ev), **kw)
    if end_turn:
        chatstate.mark_asked("")
    return out


GAUGE_Q = {"question": "What temperature range do you need?", "options": [
    {"label": "0-80 °C", "words": ["0-80"]}, {"label": "0-200 °C", "words": ["0-200"]}]}


# -- the flow as the agent's guide ------------------------------------------------

def test_the_guide_is_the_manuals_steps_and_where_the_case_stands(steps):
    g = flow.guide(None)
    assert [s.id for s in flow.STEPS] == ["1a", "1b", "1c", "1d", "1e", "1f", "1g", "1h",
                                          "1i", "1j", "2a", "3a", "3b"]
    assert "No case is open" in g and "<- now" in g
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    data = case.for_session(steps["_conn"], "chat-1")
    assert flow.stage_of(data) == ("1e", "1h")       # one material: searched, worked at once
    assert "Open case in this chat: NAM-" in flow.guide(data)


def test_the_guide_names_the_next_act_from_the_case(steps):
    assert "nam_need" in flow.next_hint(None)
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    conn = steps["_conn"]
    # first, once: what NRL bought with it before (a suggestion only if one belongs)
    assert "nam_related" in flow.next_hint(case.for_session(conn, "chat-1"))
    steps["nam_related"].fn()
    assert "5 records are still in" in flow.next_hint(case.for_session(conn, "chat-1"))
    _ask(steps, **GAUGE_Q)
    assert "read it with nam_answer" in flow.next_hint(case.for_session(conn, "chat-1"))
    steps["nam_answer"].fn(answer="0-80")
    assert "one record is left" in flow.next_hint(case.for_session(conn, "chat-1"))


# -- stage 1 ---------------------------------------------------------------------

def test_a_request_with_several_items_is_split_and_each_searched(steps):
    out = steps["nam_need"].fn(need="Supply of temperature gauge, thermocouple",
                               items=["temperature gauge", "thermocouple"], category="goods")
    assert "| 1 | temperature gauge | 5 materials | 1 by short text, 4 by meaning |" in out
    assert "| 2 | thermocouple | 5 materials | 1 by short text, 4 by meaning |" in out
    assert "nam_choose" in out
    assert "REFUSED" in steps["nam_need"].fn(category="spares")
    out = steps["nam_search"].fn(item="temperature gauge")
    assert set(steps["_sent"]) == {"sap_search"}              # the numbers are SAP's
    assert out.startswith("SAP has 5 materials for 'temperature gauge' (1 by short text, 4 by "
                          "meaning).")
    assert "FOR YOU, NOT TO SHOW" in out and "| I-TG-ARC-G200-01 |" not in out
    # TG is the item's own initials: first, and not another material; the
    # thermocouple was found by meaning only, and the digest says so
    assert "Types (the first word of the short text that not every record shares): tg 3, " \
           "supply 1, thermocouple 1 (found by meaning only" in out
    assert "0-200 2" in out and "320 2" in out    # a value however the records write it
    assert "- I-TG-ARC-G200-01: TG Bimetallic U320mm 6mm dia 0-200Cel" in out
    assert "0-200 2" in out                        # a word that splits them, counted


def test_sap_down_is_unavailable_and_no_line_is_said(tmp_path):
    conn = sqlite3.connect(":memory:")
    token = chatstate.enter("c")
    try:
        call, _ = _kg(fail=("sap_search",))
        t = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
        out = t["nam_need"].fn(category="goods", need="gauge")
        assert "Not searched: 'gauge': SAP could not be read (OSError)" in out
        out = t["nam_search"].fn()
        assert "SAP could not be read (OSError)" in out and "unavailable" in out
        call, _ = _kg(sap={"results": []})
        t = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
        assert t["nam_search"].fn().startswith("NO RECORD")
    finally:
        chatstate.leave(token)


def test_a_question_is_shown_only_when_the_records_bear_it_out(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    bad = _ask(steps, question="Which make?", options=[
        {"label": "Wika", "words": ["wika"]}, {"label": "Baumer", "words": ["baumer"]}])
    assert bad.startswith("REFUSED: no record carries ['wika']") and not steps["_shown"]
    sap_only = narrow._sap(SAP)[:4]                # the four that all say 6mm
    with pytest.raises(narrow.Refused, match="every record carries"):
        narrow.check("Size?", [{"label": "6mm", "words": ["6mm"]},
                               {"label": "TG", "words": ["TG"]}], sap_only)
    out = _ask(steps, **GAUGE_Q)
    assert out.startswith('Asked: "What temperature range do you need?"')
    assert steps["_shown"][-1]["options"] == ["0-80 °C (1 material)", "0-200 °C (2 materials)",
                                              "Something else (2 materials)"]


def test_a_unit_written_two_ways_is_dropped_and_said(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    out = _ask(steps, question="Which range?", options=[
        {"label": "0-80 °C", "words": ["0-80", "DegC"]},
        {"label": "0-200 °C", "words": ["0-200", "DegC"]}])
    assert out.startswith('Asked: "Which range?"')
    assert "'0-80 °C': matched on ['0-80'], ['degc'] set aside" in out
    assert steps["_shown"][-1]["options"][:2] == ["0-80 °C (1 material)", "0-200 °C (2 materials)"]


def test_the_range_a_label_states_is_never_the_word_dropped(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    out = _ask(steps, question="Which range and stem?", options=[
        {"label": "0-200 °C, U400", "words": ["0-200", "U400mm", "TG", "D150"]},
        {"label": "0-80 °C, U100", "words": ["0-80", "U100mm", "TG", "D150"]},
        {"label": "0-200 °C, U320", "words": ["0-200", "U320mm"]}])
    assert "matched on ['0-200', 'u400mm'], ['tg', 'd150'] set aside" in out
    assert steps["_shown"][-1]["options"][:2] == ["0-200 °C, U400 (1 material)",
                                                  "0-80 °C, U100 (1 material)"]


def test_a_unit_the_label_does_not_state_does_not_hide_records():
    cands = narrow._sap({"results": [
        _sap_res("A", "TG BiMet 0-200 DegC, U400T50", 1.0, "2022-01-01", "PUR-X"),
        _sap_res("B", "TG Bimetallic U320mm 0-200Cel", 1.0, "2022-01-01", "PUR-X"),
        _sap_res("C", "TG Bimetallic U320mm 0-100Cel", 1.0, "2022-01-01", "PUR-X")]})
    q = narrow.check("Which range?", [{"label": "0-200 °C", "words": ["0-200", "DegC"]},
                                      {"label": "0-100 °C", "words": ["0-100", "Cel"]}], cands)
    assert [o["keys"] for o in q["options"]] == [["A", "B"], ["C"]]


def test_a_word_finds_a_part_of_a_written_range_but_never_a_longer_number():
    rec = narrow._sap({"results": [_sap_res("C1", "TG Bimetallic U210mm 0-700Cel", 1.0,
                                            "2022-01-01", "PUR-X")]})[0]
    for w in (["700"], ["0-700"], ["700cel"], ["u210"], ["210", "mm"][:1]):
        assert narrow.carries(rec, w), w
    for w in (["70"], ["0-70"], ["u21"], ["21"]):
        assert not narrow.carries(rec, w), w


def test_a_second_ask_in_the_same_turn_does_not_replace_the_checked_one(steps):
    from nam_agent.tools import clarify

    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    _ask(steps, end_turn=False, **GAUGE_Q)
    seen = []
    out = clarify.make_tool().fn(question="Which range?", options=["0-200", "0-80"],
                                 _notify=lambda k, e: seen.append(e))
    assert not seen and "Do not ask again" in out


def test_made_up_options_are_not_shown_while_a_case_is_open(tmp_path):
    from nam_agent.tools import clarify

    nam_agent = make_agent(tmp_path, kg_url=URL)
    token = chatstate.enter(nam_agent.session.session_id)
    try:
        nam_agent.tools._tools["nam_need"].fn(category="goods", need="temperature gauge")
        nam_agent._nam_guide()
        seen = []
        out = clarify.make_tool().fn(question="How many?", options=["10", "20"],
                                     _notify=lambda k, e: seen.append(e))
    finally:
        chatstate.leave(token)
    assert not seen and "asked in plain words" in out
    seen = []
    clarify.make_tool().fn(question="Which day?", options=["Mon", "Tue"],
                           _notify=lambda k, e: seen.append(e))
    assert seen                      # outside a case the generic question still works


def test_a_case_opened_in_this_turn_already_blocks_made_up_options(steps):
    from nam_agent.tools import clarify

    assert not chatstate.case_open()
    steps["nam_need"].fn(category="goods", need="pipes")
    seen = []
    clarify.make_tool().fn(question="Size?", options=["50mm", "75mm"],
                           _notify=lambda k, e: seen.append(e))
    assert chatstate.case_open() and not seen


def test_three_refusals_in_a_row_say_stop_and_ask_plainly(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    wrong = {"question": "Which make?", "options": [{"label": "Wika", "words": ["wika"]},
                                                    {"label": "Baumer", "words": ["baumer"]}]}
    outs = [_ask(steps, **wrong) for _ in range(3)]
    assert "No record carries any of these words." in outs[0]
    assert "stop retrying" not in outs[1] and "refusal 3 in a row: stop retrying" in outs[2]
    assert "stop retrying" not in _ask(steps, **GAUGE_Q)


def test_an_answer_narrows_and_one_that_fits_nothing_gets_one_clarifying_question(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    _ask(steps, **GAUGE_Q)
    out = steps["nam_answer"].fn(answer="0-200 °C (2 materials)")
    assert out.startswith("Read as option '0-200 °C': 2 materials still in")
    out = steps["nam_answer"].fn(answer="titanium")
    assert out.startswith("REFUSED") and "do not restart" in out
    out = steps["nam_answer"].fn(answer="U400")
    assert "1 material still in" in out and "nam_pick" in out
    out = steps["nam_answer"].fn(answer="undo")
    assert out.startswith("Took back: the answer 'U400'")
    data = case.for_session(steps["_conn"], "chat-1")
    assert data["items"][0]["answers"] == [
        {"question": "What temperature range do you need?", "answer": "0-200 °C (2 materials)"}]


def test_a_narrowed_item_is_not_searched_again_by_mistake(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    _ask(steps, **GAUGE_Q)
    for again in (False, True):
        out = steps["nam_search"].fn(query="temperature gauge 320mm", restart=again)
        assert out.startswith("REFUSED") and "read it with nam_answer first" in out
    steps["nam_answer"].fn(answer="0-200")
    out = steps["nam_search"].fn(restart=True)
    assert "The earlier answers ['0-200'] still apply: 2 of 5 records are in" in out


def test_the_open_question_comes_back_after_a_side_question(tmp_path, monkeypatch):
    from nam_agent.loop.agent import LoopResult

    nam_agent = make_agent(tmp_path, kg_url=URL)
    call, _ = _kg()
    monkeypatch.setattr("nam_agent.nam.kg_mcp.call", call)
    token = chatstate.enter(nam_agent.session.session_id)
    try:
        tools = nam_agent.tools._tools
        tools["nam_need"].fn(category="goods", need="temperature gauge")
        tools["nam_search"].fn()
        tools["nam_ask"].fn(**GAUGE_Q)
    finally:
        chatstate.leave(token)
    shown = []
    side = LoopResult(reply="The last PO was INR 1,060.", tool_calls=[], iterations=1)
    nam_agent._nam_reminder(side, lambda k, e: shown.append((k, e)), False)
    assert side.reply.endswith("Back to the need: What temperature range do you need?")
    assert shown[-1][1]["options"][0] == "0-80 °C (1 material)"
    # a turn that showed its question with buttons is not asked again
    asked = LoopResult(reply="?", tool_calls=[{"tool": "nam_ask"}], iterations=1)
    token = chatstate.enter(nam_agent.session.session_id)
    try:
        chatstate.mark_asked("Which range?")
        nam_agent._nam_reminder(asked, lambda *a: None, False)
    finally:
        chatstate.leave(token)
    assert asked.reply == "?"
    # a turn whose question was refused (nam_ask called, nothing shown) is
    refused = LoopResult(reply="Pick one: A, B", tool_calls=[{"tool": "nam_ask"}], iterations=1)
    nam_agent._nam_reminder(refused, lambda *a: None, False)
    assert refused.reply.endswith("Back to the need: What temperature range do you need?")


def test_a_typed_size_keeps_every_record_of_that_number():
    cands = narrow._sap({"results": [
        _sap_res("A", "TG BiMet 0-200 DegC,D150,OD6mm, U320T50", 1.0, "2022-01-01", "PUR-X"),
        _sap_res("B", "TG Bimetallic U320mm 6mm dia 0-200Cel", 1.0, "2022-01-01", "PUR-X"),
        _sap_res("C", "TG Bimetallic U400mm 6mm dia 0-200Cel", 1.0, "2022-01-01", "PUR-X")]})
    keys, how = narrow.answer(None, cands, "320 mm")
    assert keys == ["A", "B"] and how == "the numbers ['320']"
    assert narrow.answer(None, cands, "U320mm")[0] == ["B"]


def test_few_records_left_are_a_table_to_pick_from(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    out = steps["nam_answer"].fn(answer="0-200")
    # U320 and U400 still tell them apart: the next question first
    assert "FOR YOU, NOT TO SHOW" in out and "| I-TG-ARC-G200-01 |" not in out
    out = steps["nam_matches"].fn()
    assert "| I-TG-ARC-G200-01 | material |" in out and "shown to the officer as cards" in out


def test_a_flow_turn_gets_no_raw_kg_table_but_a_lookup_turn_does():
    from nam_agent.nam import answer_data

    sap_out = ("SAP — 1 line(s) for 'gauge' (PR: 10073969; PO: 4500024964).\n\n| Kind | Doc no. | "
               "Date | Material | Description | Qty | Unit | Rate (INR) | PR | PO | PUR case |\n"
               "|---|---|---|---|---|---:|---|---:|---|---|---|\n| PO | 4500024964 | 2022-07-13 "
               "| X | gauge | 1 | NOS | 10 | 10073969 | 4500024964 | PUR-INSTMNT-1002364 |")
    lookup = [{"tool": "sap_search", "output": sap_out}]
    assert answer_data.SAP_HEADING in answer_data.complete("We bought it.", lookup)   # SAP's, named so
    flow_turn = lookup + [{"tool": "nam_ask", "output": "Asked"}]
    assert answer_data.HEADING not in answer_data.complete("Which range?", flow_turn)


def test_a_detail_some_records_carry_narrows_them_too(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    out = steps["nam_answer"].fn(field="range", value="0-200")
    assert out.startswith("Recorded range = 0-200") and "narrows the records: 2 still in" in out
    out = steps["nam_answer"].fn(field="quantity", value="320")
    assert "narrows" not in out                    # a count, though "U320mm" holds 320


def test_the_agent_cannot_answer_its_own_question_in_the_same_turn(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    _ask(steps, end_turn=False, **GAUGE_Q)
    out = steps["nam_answer"].fn(answer="0-200")
    assert out.startswith("REFUSED") and "has not answered yet" in out
    assert "REFUSED" in steps["nam_answer"].fn(field="range", value="0-200")


def test_a_default_detail_needs_its_source(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    assert "REFUSED" in steps["nam_answer"].fn(field="pressure rating", value="PN40",
                                               default=True)
    out = steps["nam_answer"].fn(field="pressure rating", value="PN40", default=True,
                                 source="NRL instrument spec 4.2")
    assert "(NRL default)" in out
    data = case.for_session(steps["_conn"], "chat-1")
    assert "pressure rating = PN40 (default)" in case.summary(data)


def test_the_tenders_rank_ordered_before_raised_latest_first(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    out = steps["nam_tenders"].fn()
    rows = [ln for ln in out.splitlines() if ln.startswith("| PUR-")]
    # the latest year first, then PR + PO before PR ongoing
    assert rows[0].startswith("| PUR-PRJNUM-1008552 | PR ongoing | 2025-02-01")
    assert rows[1].startswith("| PUR-INSTMNT-1003188 | PR + PO | 2024-03-01")
    assert "SAP shows no PR or PO for PUR-PRJNUM-1008552: its tender record in the " \
           "knowledge graph (kg_tender_record) may." in out
    ranked = narrow.tenders([{"key": "x", "code": "x", "lines": [
        {"kind": "PO", "po_no": "1", "date": "2020-01-01", "delivery_date": "2020-02-01",
         "pur_case_id": "PUR-A"}]}, {"key": "y", "code": "y", "lines": [
        {"kind": "PO", "po_no": "2", "date": "2025-01-01", "pur_case_id": "PUR-B"}]}])
    assert [t["status"] for t in ranked] == ["ordered", "delivered"]     # 2025 before 2020
    same_year = narrow.tenders([{"key": "x", "code": "x", "lines": [
        {"kind": "PR", "pr_no": "9", "date": "2025-06-01", "pur_case_id": "PUR-R"}]},
        {"key": "y", "code": "y", "lines": [
        {"kind": "PO", "po_no": "2", "date": "2025-01-01", "pur_case_id": "PUR-B"}]},
        {"key": "z", "code": "z", "lines": [
        {"kind": "PO", "po_no": "3", "date": "2025-09-01", "pur_case_id": "PUR-C",
         "status": "Deleted"}]}])
    assert [t["label"] for t in same_year] == ["PR + PO", "PR ongoing", "PR ongoing, PO cancelled"]


def test_only_a_record_that_was_found_can_be_picked(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_search"].fn()
    assert "REFUSED: 'I-TG-INVENTED' is not one of the records found" in steps["nam_pick"].fn(
        key="I-TG-INVENTED")
    out = steps["nam_pick"].fn(key="I-TG-ARC-G080-01", quantity=20)
    assert out.startswith("REFUSED: 5 records are still in") and "nam_answer" in out
    steps["nam_answer"].fn(answer="0-200")
    assert "not among the records still in" in steps["nam_pick"].fn(key="I-TG-ARC-G080-01")
    out = steps["nam_pick"].fn(key="I-TG-ARC-G200-01", quantity=20)
    assert out.startswith("In the cart for 'temperature gauge': I-TG-ARC-G200-01")
    assert "Every chosen item is in the cart" in out


# -- stage 2 ---------------------------------------------------------------------

def test_the_price_rises_7_percent_a_year_as_the_manual_shows():
    assert estimate.escalate(100.0, "2024-05-01", date(2025, 1, 1)) == 107.0
    assert estimate.escalate(100.0, "2024-05-01", date(2026, 6, 1)) == 114.49


def test_each_rate_gets_the_manuals_result():
    until = date(2026, 6, 1)

    def result(rate, last, when):
        return estimate.judge({"rate": rate, "last_rate": last, "last_date": when}, until)
    assert result(107, 100, "2025-06-01")["result"] == "Cleared"           # rose < 10%
    assert result(114.49, 100, "2024-06-01")["result"] == "Cleared"        # 7% a year
    assert result(120, 100, "2026-01-01")["result"] == "Manual review"     # >= 10% in a year
    ahr = result(9500, 6370, "2024-06-01")                                 # Fig 13
    assert ahr["result"] == "AHR" and ahr["flag"] and ahr["cagr"] > 10
    assert result(50, None, None)["result"] == "New item"


def _picked(steps):
    steps["nam_need"].fn(category="goods", need="temperature gauge")
    steps["nam_answer"].fn(answer="0-80")
    steps["nam_pick"].fn(key="I-TG-ARC-G080-01", quantity=20)


def test_the_estimate_waits_for_every_item_and_every_flag(steps, tmp_path):
    steps["nam_need"].fn(category="goods", need="gauge and thermocouple", items=["gauge", "thermocouple"])
    steps["nam_choose"].fn(items=["All of them"])
    steps["nam_answer"].fn(item="gauge", answer="0-80")
    steps["nam_pick"].fn(item="gauge", key="I-TG-ARC-G080-01", quantity=20)
    assert "every chosen item first: thermocouple" in steps["nam_estimate"].fn(action="build")
    steps["nam_choose"].fn(items=["gauge"])
    out = steps["nam_estimate"].fn(action="build", evaluation_date="2026-06-01")
    assert "| 1 | I-TG-ARC-G080-01 |" in out and "114.49" in out and "Cleared" in out
    assert "SAP PO 4500024964 dated 2024-03-01, PUR-INSTMNT-1003188" in out
    out = steps["nam_estimate"].fn(action="set", line=1, rate=200)
    assert "AHR" in out and "line 1 is AHR: accept it, or revise its rate" in out
    assert "cannot be locked yet" in steps["nam_estimate"].fn(action="lock")
    steps["nam_estimate"].fn(action="decide", line=1, decision="accept", note="market rise")
    out = steps["nam_estimate"].fn(action="lock")
    assert "LOCKED" in out
    assert (tmp_path / "nam" / case.for_session(steps["_conn"], "chat-1")["case_id"]
            / "estimate.xlsx").is_file()
    assert "locked" in steps["nam_estimate"].fn(action="set", line=1, rate=150)


def test_a_reply_that_hides_the_estimate_gets_its_table(steps):
    from nam_agent.nam import answer_data

    _picked(steps)
    out = steps["nam_estimate"].fn(action="build", evaluation_date="2026-06-01")
    calls = [{"tool": "nam_estimate", "output": out}]
    thin = answer_data.complete("The estimate is ready. Lock it?", calls)
    assert answer_data.CASE_HEADING in thin and "| 1 | I-TG-ARC-G080-01 |" in thin
    assert "Total excl. GST: INR 2,289.80" in thin
    full = "Line 1: 20 at INR 114.49 = INR 2,289.80. Total INR 2,289.80."
    assert answer_data.complete(full, calls) == full


# -- stage 3 ---------------------------------------------------------------------

def test_the_pr_is_confirmed_only_when_complete_and_never_gets_a_made_up_number(steps, tmp_path):
    _picked(steps)
    from nam_agent.nam import chatstate

    chatstate.mark_asked("")                      # the next turn: "That's everything"
    assert steps["nam_cart"].fn(answer="done").startswith("The cart is complete")
    out = steps["nam_pr"].fn(action="fill")       # no estimate needed: it is optional
    assert "| 1 | I-TG-ARC-G080-01 | TG Bimetallic U100mm 6mm dia 0-80Cel | 20.0 | NOS |" in out
    assert "PUR-INSTMNT-1003188" in out and "SAP PO 4500024964" in out
    assert "missing" in steps["nam_pr"].fn(action="confirm").lower()
    assert "YYYY-MM-DD" in steps["nam_pr"].fn(action="set", delivery_date="next month")
    steps["nam_pr"].fn(action="set", delivery_date="2026-12-31", plant="1100",
                       storage_location="S001", justification="Replacement of failed gauges")
    out = steps["nam_pr"].fn(action="confirm")
    assert "CONFIRMED" in out and "no SAP PR number" in out
    data = case.for_session(steps["_conn"], "chat-1")
    pr_ = data["items"][0]["pr"]
    assert pr_["status"] == "ready_for_sap" and pr_["pr_no"] is None
    assert (tmp_path / "nam" / data["case_id"] / "pr.xlsx").is_file()
    assert "no longer change" in steps["nam_estimate"].fn(action="build")
    assert "no longer change" in steps["nam_pr"].fn(action="set", plant="1200")


# -- the case: memory, versions, tenants, roles, audit -------------------------------

def test_every_write_is_a_version_and_a_stale_write_is_refused(steps):
    _picked(steps)
    conn = steps["_conn"]
    data = case.for_session(conn, "chat-1")
    assert [v["version"] for v in case.versions(conn, data["case_id"])] == list(
        range(1, data["version"] + 1))
    old = {**data, "version": data["version"] - 1}
    with pytest.raises(case.Stale):
        case.save(conn, old)


def test_another_tenant_cannot_see_or_change_the_case(steps):
    _picked(steps)
    conn = steps["_conn"]
    case_id = case.for_session(conn, "chat-1")["case_id"]
    other = identity.parse("other-co", "u9", "officer")
    with case.acting(other):
        with pytest.raises(case.NotYours):
            case.load(conn, case_id)
        assert case.for_session(conn, "chat-1") is None
        assert "REFUSED" in steps["nam_case"].fn(case_id=case_id)


def test_a_role_that_may_not_lock_is_refused(steps):
    _picked(steps)
    steps["nam_estimate"].fn(action="build", evaluation_date="2026-06-01")
    with case.acting(case.with_actor(roles=("U2",))):
        out = steps["nam_estimate"].fn(action="lock")
    assert out.startswith("REFUSED: 'estimate.lock' needs one of the roles U1, U3")


def test_the_identity_context_is_consumed_never_invented():
    a = identity.parse("nrl", "u123", "officer,authorized_user")
    assert (a.tenant, a.user, a.roles) == ("nrl", "u123", ("U1", "U3"))
    assert identity.from_headers({}) is None
    for bad in (("nrl", "", "U1"), ("local", "u1", "U1"), ("nrl", "u1", "wizard")):
        with pytest.raises(identity.BadIdentity):
            identity.parse(*bad)
    with pytest.raises(permissions.Forbidden):
        permissions.require(("U7",), "pr.confirm")


def test_every_tool_call_is_one_audit_row_on_a_whole_chain(steps):
    _picked(steps)
    rows = audit.events(steps["_conn"], kind="tool")
    assert [r["name"] for r in rows] == ["nam_need", "nam_answer", "nam_pick"]
    assert audit.verify(steps["_conn"])["ok"]


def test_the_chat_remembers_its_case_every_turn(tmp_path, monkeypatch):
    nam_agent = make_agent(tmp_path, kg_url=URL)
    call, _ = _kg()
    monkeypatch.setattr("nam_agent.nam.kg_mcp.call", call)
    token = chatstate.enter(nam_agent.session.session_id)
    try:
        tools = nam_agent.tools._tools
        tools["nam_need"].fn(category="goods", need="temperature gauge")
        tools["nam_search"].fn()
        tools["nam_ask"].fn(**GAUGE_Q)
    finally:
        chatstate.leave(token)
    token = chatstate.enter(nam_agent.session.session_id)
    try:
        nam_agent.session.nam_guide = nam_agent._nam_guide()
    finally:
        chatstate.leave(token)
    system = nam_agent.session.build_system("what is the difference between them?")
    assert "NAM — the PR Creation Flow (goods)" in system
    assert "Open question: What temperature range do you need?" in system
    assert "I-TG-ARC-G200-02 TG Bimetallic U400mm" in system
    assert "1f Follow-up questions" in system and "<- now" in system


def test_an_old_models_setting_does_not_stop_the_server(caplog):
    chains = llm.parse_chains("planner=a; followup=b; domain=c", "main", "small")
    assert chains["domain"] == ("c",) and chains["followup"] == ("b",)
    assert "planner" not in chains and "no task 'planner'" in caplog.text


def test_the_number_beside_an_option_names_what_sap_counts():
    cands = narrow._sap({"results": [
        _sap_res("A", "Gasket SS 0-200", 1.0, "2022-01-01", "PUR-X"),
        _sap_res("B", "Gasket SS 0-300", 1.0, "2022-01-01", "PUR-X"),
        {"description": "Gasket replacement of HE cover", "codes": {}, "lines": [
            {"kind": "SERVICE", "doc_no": "4300098995", "date": "2026-08-31", "material": None,
             "rate_inr": 3904.57, "pur_case_id": "PUR-MECHMNT-1009590", "pr_no": "10082938",
             "po_no": "4300098995"}]}]})
    assert narrow.count(cands) == "2 materials, 1 service line"
    q = narrow.check("Which?", [{"label": "Stainless", "words": ["SS"]},
                                {"label": "HE cover", "words": ["cover"]}], cands)
    assert narrow.labels(q) == ["Stainless (2 materials)", "HE cover (1 service line)"]
    assert narrow.answer(q, cands, "Stainless (2 materials)")[0] == ["A", "B"]


def test_a_turn_out_of_steps_on_the_need_gives_the_open_question_not_the_limit(tmp_path, monkeypatch):
    from nam_agent.loop.agent import LoopResult

    nam_agent = make_agent(tmp_path, kg_url=URL)
    call, _ = _kg()
    monkeypatch.setattr("nam_agent.nam.kg_mcp.call", call)
    token = chatstate.enter(nam_agent.session.session_id)
    try:
        tools = nam_agent.tools._tools
        tools["nam_need"].fn(category="goods", need="temperature gauge")
        tools["nam_ask"].fn(**GAUGE_Q)
    finally:
        chatstate.leave(token)
    out = LoopResult(reply="(I hit my iteration limit before finishing — try breaking the request "
                           "into smaller steps.)", tool_calls=[], iterations=10)
    nam_agent._nam_reminder(out, lambda *a: None, False)
    assert out.reply == "Let me ask that again: What temperature range do you need?"
