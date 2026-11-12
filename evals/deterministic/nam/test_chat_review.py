"""DETERMINISTIC EVAL — the mistakes found in the officers' chats of
2026-11-10, each held in code so it cannot come back.

Each case is built from what a reply or a tool did then: a cart said to hold
what it did not, PR numbers that do not exist, a real PR said to be missing,
a pick the officer never made, a second case in one turn, "something else"
applied to the wrong item, a material SAP holds nothing of left unsaid,
first-word type labels, raw SAP tables in the middle of the flow, and the
open question asked again under a reply that asked its own.
"""

from __future__ import annotations

import sqlite3

import pytest

from evals.deterministic.nam.test_cart_flow import SAP, URL, _kg, _line
from nam_agent.loop.agent import LoopResult
from nam_agent.nam import answer_data, case, chatstate, narrow, stage_tools


@pytest.fixture
def t(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(SAP)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    token = chatstate.enter("chat-r")
    tools["_conn"] = conn
    yield tools
    chatstate.leave(token)


def _need(t):
    t["nam_need"].fn(need="job", items=["gasket", "valve", "thermowell", "gauge"], category="goods")
    t["nam_choose"].fn(items=["valve", "thermowell"])


def test_a_cart_said_to_hold_what_it_does_not_is_corrected(t):
    _need(t)
    out = stage_tools.check_reply(t["_conn"], "chat-r", "**Valve added to the cart.** Quantity 20 m.", [])
    assert "'valve' is not in the cart yet" in out


def test_pr_numbers_sap_does_not_hold_are_taken_out(t):
    _need(t)
    reply = ("Which PR should we use?\n- **PR 4300098994** – rate 530.00\n"
             "- **PR 0010000002** – rate 500.00\n(If you want a new PR, choose Create new PR.)")
    out = stage_tools.check_reply(t["_conn"], "chat-r", reply, [])
    assert "4300098994" not in out and "PR 0010000002" in out
    assert "a PR or PO number SAP does not hold for this case was taken out" in out


def test_a_real_pr_said_to_have_no_line_is_corrected(t):
    _need(t)
    out = stage_tools.check_reply(t["_conn"], "chat-r",
                                  "SAP has no line for PR 10000002, so it does not exist.", [])
    assert "PR/PO 0010000002 is in SAP's records for this case: it exists" in out


def test_no_pick_in_the_turn_the_officer_answered_unless_they_chose_the_card(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(SAP)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    token = chatstate.enter("chat-p", "Goods: gate valves")
    try:
        tools["nam_need"].fn(need="valves", items=["valve"], category="goods")
    finally:
        chatstate.leave(token)
    token = chatstate.enter("chat-p", "screwed")
    try:
        tools["nam_answer"].fn(answer="screwed")
        out = tools["nam_pick"].fn(key="V-2")
        assert out.startswith("REFUSED: The officer has just answered")
    finally:
        chatstate.leave(token)
    token = chatstate.enter("chat-p", 'Use "GATE VALVE 2IN" (V-2) from PR 0010000002')
    reading = chatstate.reading_set("nam_pick")          # the harness reads the card click
    try:
        assert "In the cart for 'valve': V-2" in tools["nam_pick"].fn(key="V-2", pr="10000002")
    finally:
        chatstate.reading_reset(reading)
        chatstate.leave(token)


def test_a_second_new_case_in_the_same_turn_is_the_same_case(t):
    t["nam_need"].fn(need="gaskets", items=["gasket"], category="goods")
    t["nam_need"].fn(need="gaskets", items=["gasket"], category="goods", new=True)
    assert t["_conn"].execute("SELECT COUNT(*) FROM nam_flow_case").fetchone()[0] == 1


def test_something_else_goes_to_the_item_its_card_is_about(t):
    _need(t)
    assert stage_tools.read_click(t["_conn"], "chat-r", "Show me something else for thermowell") \
        == ("nam_other", {"item": "thermowell"})


def test_a_material_sap_holds_nothing_of_is_said(t):
    out = t["nam_need"].fn(need="job", items=["valve", "gauge"], category="goods")
    assert "SAP holds no record of: gauge. Tell the officer so plainly" in out


def test_types_are_labelled_as_the_records_write_them():
    cands = narrow._sap({"results": [
        {"description": d, "codes": {}, "lines": [{"kind": "SERVICE", "date": "2026-01-01",
                                                    "pur_case_id": "PUR-A", "rate_inr": 1.0}]}
        for d in ("Floating roof rim seal repair/replacemen", "Apply sealing material/caulking",
                  "Valve gland tightening")]})
    q = narrow.suggest(cands, "gasket")
    assert narrow.labels(q) == ["Apply sealing material/caulking (1 service line)",
                                "Floating roof rim seal (1 service line)",
                                "Valve gland tightening (1 service line)"]


def test_no_raw_sap_table_in_a_turn_while_a_case_is_open():
    sap_out = ("SAP — 1 line(s) for 'x'.\n\n| Kind | Doc no. | Date | Material | Description | Qty | "
               "Unit | Rate (INR) | PR | PO | PUR case |\n|---|---|---|---|---|---:|---|---:|---|---|---|\n"
               "| PO | 4500024964 | 2022-07-13 | X | x | 1 | NOS | 10 | 10073969 | 4500024964 | PUR-A |")
    calls = [{"tool": "sap_search", "output": sap_out}]
    assert answer_data.complete("Here it is.", calls, case_open=True) == "Here it is."
    assert answer_data.SAP_HEADING in answer_data.complete("Here it is.", calls)


def test_the_open_question_is_not_asked_again_under_a_reply_that_asks_its_own(tmp_path, monkeypatch):
    from evals.deterministic.nam.test_pr_flow import GAUGE_Q
    from evals.deterministic.nam.test_pr_flow import _kg as kg2
    from evals.helpers import make_agent

    agent = make_agent(tmp_path, kg_url=URL)
    call, _ = kg2()
    monkeypatch.setattr("nam_agent.nam.kg_mcp.call", call)
    token = chatstate.enter(agent.session.session_id)
    try:
        agent.tools._tools["nam_need"].fn(category="goods", need="temperature gauge")
        agent.tools._tools["nam_ask"].fn(**GAUGE_Q)
    finally:
        chatstate.leave(token)
    out = LoopResult(reply="SAP holds two ranges for it.\nWhich one suits your process?",
                     tool_calls=[], iterations=1)
    agent._nam_reminder(out, lambda *a: None, False)
    assert "Back to the need" not in out.reply
    assert case.for_session(agent.conn, agent.session.session_id) is not None


def test_a_choice_in_the_officers_own_words_is_shown_and_confirmed_before_the_cart(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(SAP)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    token = chatstate.enter("chat-y", "Goods: gate valves")
    try:
        tools["nam_need"].fn(need="valves", items=["valve"], category="goods")
    finally:
        chatstate.leave(token)
    shown, cards = [], []
    token = chatstate.enter("chat-y", "choose the first one")
    emit = chatstate.set_emitter(lambda k, e: cards.append(e) if k == "nam_cards" else None)
    try:
        out = tools["nam_pick"].fn(key="V-2", pr="10000002", _notify=lambda k, e: shown.append(e))
    finally:
        chatstate.reset_emitter(emit)
        chatstate.leave(token)
    assert "It is not in the cart until they say yes" in out
    assert shown[-1]["question"] == "Add this valve to the cart?"
    assert shown[-1]["options"] == [stage_tools.YES_LABEL, stage_tools.NO_LABEL]
    assert cards[-1]["title"] == "Is this the valve you mean?"
    assert [c["title"] for c in cards[-1]["cards"]] == ["PR 0010000002"]
    data = case.for_session(conn, "chat-y")
    assert not case.item(data, "valve").get("picked")
    # the officer's yes, read by the harness: in the cart, and shown so
    assert stage_tools.read_click(conn, "chat-y", "Yes, add it") == (
        "nam_pick", {"item": "valve", "key": "V-2", "pr": "10000002"})
    assert stage_tools.read_click(conn, "chat-y", "No, show the others") == (
        "nam_matches", {"item": "valve"})
    cards.clear()
    token = chatstate.enter("chat-y", "Yes, add it")
    emit = chatstate.set_emitter(lambda k, e: cards.append(e) if k == "nam_cards" else None)
    reading = chatstate.reading_set("nam_pick")
    try:
        out = tools["nam_pick"].fn(item="valve", key="V-2", pr="10000002")
    finally:
        chatstate.reading_reset(reading)
        chatstate.reset_emitter(emit)
        chatstate.leave(token)
    assert "In the cart for 'valve': V-2" in out
    added = next(c for c in cards if c.get("title") == "Added to the cart")
    assert added["group"] == "cart"
    # the last item in: the whole cart is shown too, for the officer to check
    assert any(c.get("title") == "Your cart" for c in cards)
    fields = dict((k, v) for k, v, _ in added["cards"][0]["fields"])
    assert fields["Code"] == "V-2" and fields["PR"] == "0010000002" and fields["Quantity"] == "4 NOS"
    assert added["cards"][0]["badge"] == "1 of 1 in the cart"


def test_a_reply_that_asks_itself_still_gets_the_buttons_but_not_the_question_twice(tmp_path,
                                                                                  monkeypatch):
    from evals.deterministic.nam.test_pr_flow import GAUGE_Q
    from evals.deterministic.nam.test_pr_flow import _kg as kg2
    from evals.helpers import make_agent

    agent = make_agent(tmp_path, kg_url=URL)
    call, _ = kg2()
    monkeypatch.setattr("nam_agent.nam.kg_mcp.call", call)
    token = chatstate.enter(agent.session.session_id)
    try:
        agent.tools._tools["nam_need"].fn(category="goods", need="temperature gauge")
        agent.tools._tools["nam_ask"].fn(**GAUGE_Q)
    finally:
        chatstate.leave(token)
    shown = []
    out = LoopResult(reply="Which range do you need?\n- 0-80 °C\n- 0-200 °C", tool_calls=[],
                     iterations=1)
    agent._nam_reminder(out, lambda k, e: shown.append((k, e)), False)
    assert "Back to the need" not in out.reply and "- 0-80" not in out.reply
    assert shown and shown[-1][0] == "ask_options"


def test_records_no_question_can_split_are_shown_as_cards_by_the_harness(tmp_path):
    same = {"capillary": {"results": [
        _line("C-1", "TG Capillary U320mm 0-200Cel", "PUR-A", pr="10075940"),
        _line("C-2", "TG Capillary U320mm 0-200Cel", "PUR-B", pr="10073969")]}}
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(same)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    token = chatstate.enter("chat-s")
    try:
        tools["nam_need"].fn(need="capillary", category="goods")
    finally:
        chatstate.leave(token)
    cards = []
    token = chatstate.enter("chat-s", "320mm")
    emit = chatstate.set_emitter(lambda k, e: cards.append(e) if k == "nam_cards" else None)
    try:
        assert stage_tools.reminder(conn, "chat-s") is None
    finally:
        chatstate.reset_emitter(emit)
        chatstate.leave(token)
    assert cards and [c["title"] for c in cards[-1]["cards"]] == ["PR 0010075940", "PR 0010073969"] \
        or sorted(c["title"] for c in cards[-1]["cards"]) == ["PR 0010073969", "PR 0010075940"]
