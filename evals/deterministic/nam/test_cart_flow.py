"""DETERMINISTIC EVAL — the need understood, item by item, into a cart.

A job description names several materials. Each is searched in SAP at once
and counted (by short text, by long text); the officer chooses the ones that
go forward; each is narrowed to one material code and put in the cart, one
at a time; the PUR cases are ranked by how many of the materials each holds;
each cart item gets its own PR; and undo takes the officer's acts back, one
at a time, as far as they want.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from evals.deterministic.nam.test_pr_flow import _kg
from nam_agent.nam import case, chatstate, narrow, stage_tools

URL = "http://127.0.0.1:9/mcp"


def _line(code, desc, pur, long_text="", po="4500000001", pr="10000001", date="2024-01-10"):
    return {"description": desc, "codes": {code: desc}, "lines": [
        {"kind": "PO", "doc_no": po, "date": date, "material": code, "quantity": 4.0,
         "unit": "NOS", "rate_inr": 500.0, "pur_case_id": pur, "pr_no": pr, "po_no": po,
         "long_text": long_text}]}


# PUR-B holds all three materials; PUR-A two; PUR-C one
SAP = {
    "gasket": {"results": [
        _line("G-1", "GASKET SPW 2IN 150#", "PUR-A"),
        _line("G-1", "GASKET SPW 2IN 150#", "PUR-B", po="4500000002", date="2025-02-01"),
        _line("G-2", "GASKET SPW 4IN 300#", "PUR-C")]},
    "valve": {"results": [
        _line("V-1", "GATE VALVE 2IN", "PUR-A", "body CS flanged class 150"),
        _line("V-2", "GATE VALVE 2IN", "PUR-B", "body SS screwed class 800",
              po="4500000003", pr="10000002", date="2025-02-01"),
        _line("V-3", "GLOBE VALVE 2IN", "PUR-C", "body CS flanged")]},
    "thermowell": {"results": [
        _line("T-1", "THERMOWELL SS316 U320", "PUR-B", po="4500000004", date="2025-02-01"),
        _line("T-2", "THERMOWELL SS316 U400", "PUR-C", "drilled bar stock",
              po="4500000005")]},
    "bolt": {"results": [{"description": "Seal plate work", "codes": {}, "lines": [
        {"kind": "SERVICE", "doc_no": "4300000001", "date": "2025-03-01", "material": None,
         "rate_inr": 100.0, "pur_case_id": "PUR-D", "long_text": "stud bolt replacement"}]}]},
}
JOB = "Overhaul of crude unit line 4: replace gaskets, gate valves and thermowells"


@pytest.fixture
def t(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(SAP)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    token = chatstate.enter("chat-c")
    tools["_shown"], tools["_conn"], tools["_home"] = [], conn, tmp_path
    yield tools
    chatstate.leave(token)


def _data(t):
    return case.for_session(t["_conn"], "chat-c")


def _choose_q(t):
    out = t["nam_choose"].fn(question="Which of these do you want to take forward?",
                             _notify=lambda k, e: t["_shown"].append(e))
    chatstate.mark_asked("")             # the officer answers in the next turn
    return out


def _need(t, items=("gasket", "valve", "thermowell")):
    return t["nam_need"].fn(category="goods", need=JOB, items=list(items))


# -- 1c: the materials, counted in SAP -------------------------------------------------

def test_each_material_of_a_job_description_is_counted_in_sap_at_once(t):
    out = _need(t, ("gasket", "valve", "thermowell", "bolt"))
    assert "| 1 | gasket | 2 materials | 2 by short text |" in out
    assert "| 2 | valve | 3 materials | 3 by short text |" in out
    # the service line is found by its long text only, and named for what it is
    assert "| 4 | bolt | 1 service line | 1 by long text only |" in out
    assert "nam_choose" in out


def test_the_choice_question_carries_each_material_with_its_sap_count(t):
    _need(t)
    _choose_q(t)
    assert t["_shown"][-1]["options"] == ["gasket (2 materials)", "valve (3 materials)",
                                          "thermowell (2 materials)", "All of them"]
    assert stage_tools.reminder(t["_conn"], "chat-c")[0].startswith("Which of these")


def test_no_question_about_an_item_before_the_officer_chooses(t):
    _need(t)
    out = t["nam_ask"].fn(item="valve", question="Which body?", options=[
        {"label": "CS", "words": ["cs"]}, {"label": "SS", "words": ["ss"]}])
    assert out.startswith("REFUSED: The officer has not chosen which materials go forward")


def test_the_chosen_items_are_worked_one_at_a_time_in_the_officers_order(t):
    _need(t)
    _choose_q(t)
    out = t["nam_choose"].fn(items=["thermowell (2 materials)", "2"])
    assert out.startswith("Taken forward, in this order: thermowell, valve. Not taken: gasket.")
    assert "Now 'thermowell'" in out
    out = t["nam_ask"].fn(item="gasket", question="Size?", options=[
        {"label": "2IN", "words": ["2in"]}, {"label": "4IN", "words": ["4in"]}])
    assert "was not taken forward" in out
    data = _data(t)
    assert [i["name"] for i in case.chosen(data)] == ["thermowell", "valve"]
    assert case.current_item(data)["name"] == "thermowell" and data["choosing"] is None


# -- precise questions: one code at the end ------------------------------------------

def test_codes_sharing_a_short_text_are_told_apart_by_their_long_text(t):
    _need(t, ("valve",))
    digest = narrow.digest(case.remaining(_data(t)["items"][0]))
    assert "Same short text, different material codes" in digest
    assert "'GATE VALVE 2IN' (2 codes): V-1: 150, cs, flanged; V-2: 800, screwed, ss" in digest
    out = t["nam_ask"].fn(question="Which body and end?", options=[
        {"label": "CS flanged", "words": ["cs", "flanged"]},
        {"label": "SS screwed", "words": ["ss", "screwed"]}])
    assert out.startswith('Asked: "Which body and end?"')
    chatstate.mark_asked("")
    out = t["nam_answer"].fn(answer="SS screwed")
    assert "1 material still in" in out


def test_the_cart_fills_item_by_item_and_shows_the_next_one(t):
    _need(t)
    t["nam_choose"].fn(items=["valve", "thermowell"])
    t["nam_answer"].fn(answer="screwed")
    out = t["nam_pick"].fn(key="V-2", quantity=4)
    assert "In the cart for 'valve': V-2" in out and "Cart: 1 of 2 chosen item(s)" in out
    # the next item starts with its questions, not with cards
    assert "Next item: 'thermowell'" in out and "FOR YOU, NOT TO SHOW" in out
    # the PR chosen, nothing more is asked: its quantity is the PR's own
    out = t["nam_pick"].fn(key="T-1")
    assert "quantity 4 NOS (as in that PR)" in out and "Asked" not in out
    assert "Every chosen item is in the cart" in out
    chatstate.mark_asked("")                      # the next turn: the cart question was asked
    # the officer may still change it
    out = t["nam_pick"].fn(item="thermowell", quantity=2, unit="NOS")
    assert "quantity 2 NOS" in out and "(as in that PR)" not in out
    out = t["nam_pick"].fn(item="valve", remove=True)
    assert out.startswith("Took 'valve' out of the cart")
    assert case.current_item(_data(t))["name"] == "valve"


# -- 1j: the matching PURs ------------------------------------------------------------

def _cart(t, chosen=("valve", "thermowell"), complete=True):
    _need(t)
    t["nam_choose"].fn(items=list(chosen))
    if "valve" in chosen:
        t["nam_answer"].fn(item="valve", answer="screwed")
        t["nam_pick"].fn(item="valve", key="V-2", quantity=4)
    if "thermowell" in chosen:
        t["nam_pick"].fn(item="thermowell", key="T-1", quantity=2)
    chatstate.mark_asked("")                   # the next turn
    if complete:                               # the officer: "That's everything"
        assert t["nam_cart"].fn(answer="done").startswith("The cart is complete")


def test_the_pur_holding_every_material_even_one_not_chosen_is_the_100_match(t):
    _cart(t)
    out = t["nam_tenders"].fn()
    assert "A PUR holds all 3: 100%." in out
    rows = [r for r in out.splitlines() if r.startswith("| ") and "PUR-" in r]
    assert rows[0].startswith("| 100% | PUR-B | 3 of 3 materials |")
    assert "valve: V-2" in rows[0] and "4500000003" in rows[0]
    assert "| 33% | PUR-C | 1 of 3 materials |" in out
    assert _data(t)["matched"][0] == {"pur": "PUR-B", "match": 100, "covers": "3 of 3 materials"}


def test_when_no_pur_holds_all_the_one_holding_most_is_100(t):
    pool = [{"name": n, "picked": None, "records": narrow._sap(SAP[n])}
            for n in ("gasket", "valve", "bolt")]
    m = narrow.coverage(pool)
    assert not m["all"] and m["most"] == 2
    assert [(p["pur"], p["match"]) for p in m["purs"][:2]] == [("PUR-B", 100), ("PUR-A", 100)]
    assert next(p for p in m["purs"] if p["pur"] == "PUR-D")["match"] == 50


# -- stage 3: one PR per cart item ----------------------------------------------------

def test_each_cart_item_gets_its_own_pr_and_the_officers_fields_carry_over(t):
    _cart(t)
    t["nam_pr"].fn(action="split")              # the officer wants a PR for each item
    t["nam_pr"].fn(item="valve", action="set", delivery_date="2026-12-31", plant="1100",
                   storage_location="S001", justification="Line 4 overhaul")
    out = t["nam_pr"].fn(item="valve", action="confirm")
    assert "CONFIRMED" in out and "PRs still to do: thermowell" in out
    out = t["nam_pr"].fn(item="thermowell", action="fill")
    assert "| Plant | 1100 (as for 'valve') |" in out and "PR for 'thermowell' (draft)" in out
    out = t["nam_pr"].fn(item="thermowell", action="confirm")
    assert "Every chosen item has its PR" in out
    from nam_agent.nam import flow
    assert flow.stage_of(_data(t)) == ("3b", "done")


def test_a_flagged_price_waits_for_the_officers_decision(t):
    _cart(t, ("thermowell",))
    data = _data(t)
    it = case.item(data, "thermowell")
    from nam_agent.nam import pr
    p = pr.fill(data, it)
    p["price"].update(flag=True, result="AHR")
    assert "a decision on the price, flagged AHR" in "; ".join(pr.missing(p))
    pr.set_price(p, accept=True)
    assert not any("decision" in m for m in pr.missing(p))
    with pytest.raises(ValueError, match="no flag to accept"):
        pr.set_price(p, accept=True)


# -- undo ----------------------------------------------------------------------------

def test_undo_goes_back_one_officer_act_at_a_time(t):
    _cart(t, complete=False)
    out = t["nam_undo"].fn()
    assert out.startswith("Took back: put T-1 in the cart for 'thermowell'")
    out = t["nam_undo"].fn()
    assert out.startswith("Took back: put V-2 in the cart for 'valve'")
    out = t["nam_undo"].fn()
    assert out.startswith("Took back: the answer 'screwed'")
    assert len(case.remaining(case.item(_data(t), "valve"))) == 3
    out = t["nam_undo"].fn()
    assert out.startswith("Took back: chose valve, thermowell")
    assert not any("selected" in i for i in _data(t)["items"])
    out = t["nam_undo"].fn()
    assert out.startswith("Took back: set the need")
    assert "nothing to take back" in t["nam_undo"].fn()


def test_undo_after_a_new_act_takes_back_that_act_first(t):
    _cart(t, complete=False)
    t["nam_undo"].fn()                                   # T-1 out
    t["nam_pick"].fn(item="thermowell", key="T-2", quantity=1)
    chatstate.mark_asked("")                             # the next turn
    out = t["nam_undo"].fn()
    assert out.startswith("Took back: put T-2 in the cart")
    out = t["nam_undo"].fn(steps=2)
    assert out.startswith("Took back: put V-2 in the cart for 'valve' from PR 0010000002; the "
                          "answer 'screwed'")


def test_undo_brings_the_question_back_and_the_word_undo_works_too(t):
    _need(t)
    _choose_q(t)
    t["nam_choose"].fn(items=["valve"])
    out = t["nam_answer"].fn(answer="undo")
    assert "The question of which materials go forward is open again." in out
    assert stage_tools.reminder(t["_conn"], "chat-c")[1]["options"][-1] == "All of them"


def test_a_confirmed_pr_is_never_taken_back(t):
    _cart(t, ("thermowell",))
    t["nam_pr"].fn(action="set", delivery_date="2026-12-31", plant="1100",
                   storage_location="S001", justification="Line 4")
    t["nam_pr"].fn(action="confirm")
    out = t["nam_undo"].fn()
    assert out.startswith("REFUSED: that would take back the confirmed PR of thermowell")
    assert "has a confirmed PR" in t["nam_pick"].fn(item="thermowell", remove=True)



# -- 1a: goods, services or works, before SAP is searched ---------------------------

def test_the_category_is_asked_before_sap_is_searched(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, sent = _kg(SAP)
    t = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    shown = []
    token = chatstate.enter("chat-a")
    try:
        # a need of one word, given only as its material, opens the case
        out = t["nam_need"].fn(items=["gasket"], _notify=lambda k, e: shown.append(e))
        assert "Is this goods, services or works?" in out and not sent
        assert shown[-1]["options"] == ["Goods", "Services", "Works"]
        assert case.for_session(conn, "chat-a")["need"] == "gasket"
        # the agent cannot answer it itself in the same turn
        assert "has not answered yet" in t["nam_need"].fn(category="goods")
        assert stage_tools.reminder(conn, "chat-a")[0] == "Is this goods, services or works?"
        chatstate.mark_asked("")                       # the officer's next turn
        out = t["nam_need"].fn(category="goods")
        assert sent == ["sap_search"] and "| 1 | gasket | 2 materials |" in out
        assert stage_tools.reminder(conn, "chat-a") is None
    finally:
        chatstate.leave(token)


def test_the_digest_names_the_types_sap_holds():
    cands = narrow._sap({"results": [
        _line("G-1", "GASKET SPW 2IN 150#", "PUR-A"),
        _line("G-2", "GASKET SPW 4IN 300#", "PUR-A"),
        _line("G-3", "GASKET RTJ R24", "PUR-A"),
        _line("G-4", "GASKET CAF 2IN", "PUR-A")]})
    assert "Types (the first word of the short text that not every record shares): " \
           "spw 2, caf 1, rtj 1." in narrow.digest(cands)


# -- the cart node: the requirements, kept by the harness ---------------------------

def _node(t):
    from nam_agent.nam import cart

    return cart.node_data(_data(t)), cart.node(_data(t))


def test_the_node_lists_every_item_its_types_and_where_it_stands(t):
    _need(t)
    d, text = _node(t)
    assert [r["status"] for r in d["items"]] == ["waiting for the choice of items"] * 3
    valve = d["items"][1]
    assert valve["types"] == [{"type": "gate", "count": 2}, {"type": "globe", "count": 1}]
    assert text.startswith("CART for NAM-") and "Required: 1. gasket" in text
    t["nam_choose"].fn(items=["valve", "thermowell"])
    d, _ = _node(t)
    assert [r["status"] for r in d["items"]] == ["now", "waiting", "not taken forward"]
    assert (d["now"], d["next"]) == ("valve", "thermowell")


def test_details_and_the_cart_are_saved_item_by_item(t):
    _cart(t)
    d, text = _node(t)
    valve = d["items"][0]
    assert valve["status"] == "in the cart" and valve["details"] == [
        "(the officer's own words): screwed"]
    assert valve["cart"] == {"code": "V-2", "short_text": "GATE VALVE 2IN", "quantity": 4.0,
                             "unit": "NOS", "pur": "PUR-B"}
    assert "- valve: IN CART V-2 GATE VALVE 2IN, quantity 4.0 NOS, PUR PUR-B." in text
    assert d["items"][1]["cart"]["code"] == "T-1" and (d["now"], d["next"]) == (None, None)


def test_an_undo_takes_the_cart_and_the_detail_back_from_the_node(t):
    _cart(t, complete=False)
    t["nam_undo"].fn(steps=3)                 # T-1, V-2 and the answer 'screwed'
    d, _ = _node(t)
    assert [r["cart"] for r in d["items"][:2]] == [None, None]
    assert d["items"][0]["details"] == [] and d["items"][0]["status"] == "now"
    assert d["items"][0]["still_in"] == 3


def test_the_node_comes_first_in_the_guide_and_in_nam_case(t):
    from nam_agent.nam import flow

    _need(t)
    assert flow.guide(_data(t)).lstrip().startswith("CART for NAM-")
    assert t["nam_case"].fn().startswith("CART for NAM-")


def test_the_types_found_at_search_stay_after_narrowing(t):
    _need(t, ("valve",))
    before = _node(t)[0]["items"][0]["types"]
    t["nam_answer"].fn(answer="screwed")
    after = _node(t)[0]["items"][0]
    assert after["types"] == before and after["still_in"] == 1


def test_a_new_question_waits_until_the_officers_reply_is_read(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(SAP)
    t = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    q = {"item": "valve", "question": "Which body?", "options": [
        {"label": "CS", "words": ["cs"]}, {"label": "SS", "words": ["ss"]}]}
    token = chatstate.enter("chat-r")
    try:
        t["nam_need"].fn(need="valves", items=["valve"], category="goods")
        t["nam_ask"].fn(**q)
    finally:
        chatstate.leave(token)
    token = chatstate.enter("chat-r", "SS please")         # the officer's next turn
    try:
        out = t["nam_ask"].fn(item="valve", question="Which end?", options=[
            {"label": "Flanged", "words": ["flanged"]}, {"label": "Screwed", "words": ["screwed"]}])
        assert out.startswith("REFUSED") and 'has replied "SS please"' in out
        assert "nam_answer(item='valve'" in out
        assert "1 material still in" in t["nam_answer"].fn(item="valve", answer="SS please")
    finally:
        chatstate.leave(token)


def test_a_material_code_is_found_by_its_tail():
    rec = narrow._sap({"results": [_line("I-TW-03TE1321-00", "THERMOWELL SS316", "PUR-A")]})[0]
    for typed in (["03TE1321-00"], ["03te1321"], ["I-TW-03TE1321-00"]):
        assert narrow.carries(rec, [w for x in typed for w in narrow.words(x)]), typed
    assert not narrow.carries(rec, narrow.words("03TE1322"))


# -- several choices, cards, the PR after each item --------------------------------

def test_several_options_chosen_at_once_keep_every_record_of_each(t):
    _need(t, ("thermowell",))
    q = narrow.check("Which stem?", [{"label": "U320", "words": ["u320"]},
                                     {"label": "U400", "words": ["u400"]}],
                     case.remaining(_data(t)["items"][0]))
    keys, how = narrow.answer(q, case.remaining(_data(t)["items"][0]), "U320 (1 material); U400")
    assert sorted(keys) == ["T-1", "T-2"] and how.startswith("options")


def test_the_choice_of_items_takes_several_at_once(t):
    _need(t)
    _choose_q(t)
    assert t["_shown"][-1]["multi"] is True
    out = t["nam_choose"].fn(items=["valve (3 materials); thermowell (2 materials)"])
    assert out.startswith("Taken forward, in this order: valve, thermowell.")


def test_an_option_that_is_only_a_material_code_is_refused(t):
    _need(t, ("valve",))
    out = t["nam_ask"].fn(question="Which one?", options=[
        {"label": "V-1", "words": ["V-1"]}, {"label": "V-2", "words": ["V-2"]}])
    assert out.startswith("REFUSED") and "does not know the codes" in out


def _emitted(t, fn):
    seen = []
    token = chatstate.set_emitter(lambda k, e: seen.append((k, e)))
    try:
        out = fn()
    finally:
        chatstate.reset_emitter(token)
    return out, [e for k, e in seen if k == "nam_cards"]


def test_the_matches_are_cards_once_the_item_is_described_not_after_each_question(t):
    _need(t, ("valve",))
    # two records still differ (screwed / flanged): a question comes, no cards
    out, cards = _emitted(t, lambda: t["nam_answer"].fn(answer="gate"))
    assert "FOR YOU, NOT TO SHOW" in out and not cards
    # the agent judges the description complete: the matches, as PR cards
    out, cards = _emitted(t, lambda: t["nam_matches"].fn())
    shown = cards[-1]
    assert shown["group"] == "records" and shown["by_year"] is True
    # one card per PR, as SAP writes its number, the latest year first
    assert [c["title"] for c in shown["cards"]] == ["PR 0010000002", "PR 0010000001"]
    first = shown["cards"][0]
    assert first["status"] == "PR + PO" and first["year"] == "2025"
    assert first["chips"][:2] == [{"text": "1 matching item", "strong": True},
                                  {"text": "PO 4500000003"}]
    item = first["items"][0]
    assert item["code"] == "V-2" and item["title"] == "GATE VALVE 2IN"
    assert item["match"] == "100% · 1 of 1 details"
    assert item["send"] == 'Use "GATE VALVE 2IN" (V-2) from PR 0010000002'
    assert item["detail"].startswith("Last rate INR 500.00 per NOS · quantity 4.0 NOS · PO "
                                     "4500000003 · 2025-02-01")
    # a card's click is read exactly: that record, that PR
    assert stage_tools.read_click(t["_conn"], "chat-c", item["send"]) == (
        "nam_pick", {"item": "valve", "key": "V-2", "pr": "10000002"})
    # one record left: its card comes by itself
    _, cards = _emitted(t, lambda: t["nam_answer"].fn(answer="screwed"))
    assert cards and cards[-1]["cards"][0]["items"][0]["code"] == "V-2"


def test_the_matches_wait_for_the_officers_answer(t):
    _need(t, ("valve",))
    t["nam_ask"].fn(question="Which end?", options=[{"label": "Flanged", "words": ["flanged"]},
                                                    {"label": "Screwed", "words": ["screwed"]}])
    assert "come once the officer has answered" in t["nam_matches"].fn()

def test_each_item_is_finished_on_one_pr_its_pr_drafted_quietly_then_the_next(t):
    _need(t)
    t["nam_choose"].fn(items=["valve", "thermowell"])
    t["nam_answer"].fn(answer="screwed")
    out, cards = _emitted(t, lambda: t["nam_pick"].fn(key="V-2", quantity=4))
    assert "from PR 0010000002" in out and "Next item: 'thermowell'" in out
    assert not [c for c in cards if c["group"] == "pr"]         # no PR card at the pick
    valve = case.item(_data(t), "valve")
    assert valve["picked"]["pr_no"] == "10000002" and valve["pr"]["status"] == "draft"


def test_a_record_in_several_prs_shows_them_and_waits_for_the_officers_pr(t):
    sap = {"valve": {"results": [
        _line("V-9", "GATE VALVE 4IN", "PUR-A", pr="10000011", date="2023-01-01"),
        _line("V-9", "GATE VALVE 4IN", "PUR-B", pr="10000012", po="4500000012",
              date="2025-01-01")]}}
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(sap)
    tools = {x.name: x for x in stage_tools.make_tools(conn, t["_home"], URL, call)}
    token = chatstate.enter("chat-p")
    try:
        tools["nam_need"].fn(need="valve", category="goods")
        out, cards = _emitted(t, lambda: tools["nam_pick"].fn(key="V-9", quantity=1))
        assert "is in 2 PRs" in out and not case.item(case.for_session(conn, "chat-p"),
                                                         "valve").get("picked")
        assert [c["title"] for c in cards[-1]["cards"]] == ["PR 0010000012", "PR 0010000011"]
        assert "not in PR" in tools["nam_pick"].fn(key="V-9", pr="999", quantity=1)
        out = tools["nam_pick"].fn(key="V-9", pr="0010000011", quantity=1)
        assert "from PR 0010000011" in out
    finally:
        chatstate.leave(token)

def test_the_pur_ranking_is_sent_as_cards(t):
    _cart(t)
    _, cards = _emitted(t, lambda: t["nam_tenders"].fn())
    purs = cards[-1]
    assert purs["group"] == "purs" and purs["cards"][0]["title"] == "PUR-B"
    assert purs["cards"][0]["badge"] == "100% match" and purs["cards"][0]["status"] == "PR + PO"


def test_a_click_on_the_harnesss_own_choice_is_read_before_the_model(tmp_path, monkeypatch):
    from evals.helpers import ScriptedClient, make_agent, response, text_block

    call, _ = _kg(SAP)
    monkeypatch.setattr("nam_agent.nam.kg_mcp.call", call)
    client = ScriptedClient([])
    agent = make_agent(tmp_path, client=client, kg_url=URL)
    sid = agent.session.session_id
    token = chatstate.enter(sid)
    try:
        agent.tools._tools["nam_need"].fn(need="valves", items=["valve"], category="goods")
        agent.tools._tools["nam_ask"].fn(item="valve", question="Which body?", options=[
            {"label": "CS", "words": ["cs"]}, {"label": "SS", "words": ["ss"]}])
    finally:
        chatstate.leave(token)
    # the officer's own words are the agent's to read; a clicked label is not
    assert stage_tools.read_click(agent.conn, sid, "the stainless one please") is None
    assert stage_tools.read_click(agent.conn, sid, "SS (1 material)") == (
        "nam_answer", {"item": "valve", "answer": "SS (1 material)"})
    client._script += [response([text_block("SS it is. How many do you need?")])]
    out = agent.respond("SS (1 material)")
    assert out.tool_calls[0]["tool"] == "nam_answer"
    assert out.tool_calls[0]["output"].startswith("Read as option 'SS'")
    assert len(case.remaining(case.for_session(agent.conn, sid)["items"][0])) == 1


def test_a_refused_question_names_the_words_that_split_the_records():
    cands = narrow._sap({"results": [
        _line("G-1", "GASKET SPW 2IN B16.5", "PUR-A"), _line("G-2", "GASKET SPW 4IN B16.5", "PUR-A"),
        _line("G-3", "GASKET RTJ R24 B16.5", "PUR-A"), _line("G-4", "GASKET CAF 2IN", "PUR-A")]})
    with pytest.raises(narrow.Refused) as err:
        narrow.check("Which?", [{"label": "B16.5", "words": ["b16.5"]},
                                {"label": "16.5", "words": ["16.5"]}], cands)
    assert "records placed: 'B16.5' 3 of 4" in str(err.value)
    assert "Words that split these records:" in str(err.value) and "spw (2)" in str(err.value)


def test_with_no_question_shown_the_harness_asks_one_from_sap_s_types(t):
    _need(t, ("gasket", "valve"))
    t["nam_choose"].fn(items=["valve"])
    data = _data(t)
    item = case.item(data, "valve")
    item["candidates"] += [dict(c, key=c["key"] + "x", code=c["code"] + "X")
                           for c in item["candidates"]]
    item["keys"] = [c["key"] for c in item["candidates"]]
    case.save(t["_conn"], data, "chat-c")
    q, shown = stage_tools.reminder(t["_conn"], "chat-c")
    assert q == "Which type of valve do you need?" and shown["multi"] is True
    # each type as the records write it, without the words every record shares
    assert shown["options"] == ["GATE (4 materials)", "GLOBE (2 materials)"]
    assert case.item(_data(t), "valve")["pending"]["question"] == q


def test_a_card_group_keeps_the_event_kind_on_the_way_to_the_page():
    from nam_agent.nam import cards

    group = cards.records("valve", narrow._sap(SAP["valve"]))
    assert {"kind": "nam_cards", **group}["kind"] == "nam_cards"       # as the stream sends it


# -- one common PR for several items, when the officer asks --------------------------

def test_with_every_item_drafted_the_agent_is_pointed_to_offer_a_common_pr(t):
    from nam_agent.nam import flow

    _cart(t)
    assert "matching PURs" in flow.next_hint(_data(t))           # the PURs come first
    t["nam_tenders"].fn()
    hint = flow.next_hint(_data(t))
    assert "whether they want one common PR for valve, thermowell" in hint
    assert "items not taken forward (gasket)" in hint


def test_a_common_pr_holds_every_item_and_one_set_of_fields(t):
    _cart(t)
    t["nam_pr"].fn(item="valve", action="set", plant="1100")
    out, cards = _emitted(t, lambda: t["nam_pr"].fn(action="combine",
                                                    items=["valve", "thermowell"]))
    assert out.startswith("One common PR for valve, thermowell:")
    data = _data(t)
    cp = data["common_pr"]
    assert [ln["item"] for ln in cp["lines"]] == ["valve", "thermowell"]
    assert cp["header"]["plant"] == "1100" and cp["offered"] == {"plant": "valve"}
    assert [case.item(data, n)["pr"]["status"] for n in ("valve", "thermowell")] == ["merged"] * 2
    card = cards[-1]
    assert card["title"] == "Common PR" and len(card["cards"][0]["items"]) == 2
    assert "cannot be confirmed yet" in t["nam_pr"].fn(common=True, action="confirm")
    t["nam_pr"].fn(common=True, action="set", delivery_date="2026-12-31",
                   storage_location="S001", justification="Line 4 overhaul")
    out = t["nam_pr"].fn(common=True, action="confirm")
    assert "CONFIRMED" in out and "no SAP PR number" in out
    from nam_agent.nam import flow
    assert flow.stage_of(_data(t)) == ("3b", "done")
    assert (t["_home"] / "nam" / data["case_id"] / "pr.xlsx").is_file()


def test_a_common_pr_is_refused_for_one_item_or_an_item_not_in_the_cart(t):
    _cart(t, ("thermowell",))
    assert "at least two items" in t["nam_pr"].fn(action="combine", items=["thermowell"])
    assert "not in the cart" in t["nam_pr"].fn(action="combine", items=["thermowell", "gasket"])


def test_separate_prs_are_kept_when_the_officer_wants_them_and_undo_takes_a_common_back(t):
    from nam_agent.nam import flow

    _cart(t)
    t["nam_pr"].fn(action="combine", items=["valve", "thermowell"])
    t["nam_undo"].fn()
    data = _data(t)
    assert not data.get("common_pr") and case.item(data, "valve")["pr"]["status"] == "draft"
    t["nam_tenders"].fn()
    out = t["nam_pr"].fn(action="split")
    assert "Each item keeps its own PR" in out
    assert "common PR" not in flow.next_hint(_data(t))


def test_with_several_items_no_pr_is_confirmed_before_the_common_pr_question(t):
    _cart(t)
    fields = dict(delivery_date="2026-12-31", plant="1100", storage_location="S001",
                  justification="Line 4")
    out = t["nam_pr"].fn(item="valve", action="confirm", **fields)
    assert out.startswith("REFUSED: Several items are in the cart (valve, thermowell)")
    assert "items not taken forward (gasket)" in out
    t["nam_pr"].fn(action="split")                     # the officer wants separate PRs
    assert "CONFIRMED" in t["nam_pr"].fn(item="valve", action="confirm", **fields)


def test_a_reply_that_claims_a_confirmation_the_case_does_not_hold_is_corrected(t):
    _cart(t)
    said = "1. **Valve** – confirmed.  \nAll PRs are now finalized and ready for SAP."
    fix = stage_tools.claims(t["_conn"], "chat-c", said, [])
    assert fix.startswith("**From the case**") and "no PR is confirmed yet" in fix
    assert "still drafts: valve, thermowell" in fix
    po = stage_tools.claims(t["_conn"], "chat-c", "I've created the purchase orders.", [])
    assert "NAM raises no purchase order" in po
    # honest words, or a confirmation the case holds, are left alone
    assert stage_tools.claims(t["_conn"], "chat-c", "Both PRs are drafts; confirm once the "
                                                    "plant is given.", []) is None
    t["nam_pr"].fn(action="split")
    for n in ("valve", "thermowell"):
        t["nam_pr"].fn(item=n, action="confirm", delivery_date="2026-12-31", plant="1100",
                       storage_location="S001", justification="Line 4")
    assert stage_tools.claims(t["_conn"], "chat-c", "Both PRs are confirmed.", []) is None


# -- "Continue with this", "Show me something else", "Undo last step" ---------------

def test_show_me_something_else_gives_the_closest_other_then_asks_again(t):
    _need(t, ("thermowell",))
    t["nam_answer"].fn(answer="U320")                    # T-1 left
    out, cards = _emitted(t, lambda: t["nam_other"].fn())
    assert "The closest other record" in out and "T-2" in out
    group = cards[-1]
    assert group["title"] == "The closest other match for 'thermowell'"
    assert group["cards"][0]["items"][0]["code"] == "T-2"
    assert group["cards"][0]["action"]["label"] == "Continue with this"
    assert group["cards"][0]["action"]["other"] == f"{stage_tools.OTHER_LABEL} for thermowell"
    # the suggestion can be taken
    assert "In the cart for 'thermowell': T-2" in t["nam_pick"].fn(key="T-2", quantity=1)
    chatstate.mark_asked("")                             # the next turn
    t["nam_undo"].fn()
    # asked again: the last answer is taken back, the specification is open again
    out = t["nam_other"].fn()
    assert "open again (took back the answer 'U320'" in out
    item = case.item(_data(t), "thermowell")
    assert item["answers"] == [] and len(case.remaining(item)) == 2


def test_the_pages_own_buttons_are_read_by_the_harness(t):
    _need(t, ("valve",))
    assert stage_tools.read_click(t["_conn"], "chat-c", "Show me something else") == (
        "nam_other", {"item": "valve"})
    assert stage_tools.read_click(t["_conn"], "chat-c", "Undo the last step") == (
        "nam_undo", {})
    js = (Path(__file__).resolve().parents[3] / "nam_agent" / "server" / "static" / "js"
          / "render.js").read_text()
    assert f'const UNDO_LABEL = "{stage_tools.UNDO_LABEL}";' in js
    assert "undoRow(t, latest)" in js and "c.action.other" in js


def test_the_closest_other_is_a_record_of_the_item_itself_before_one_that_only_shares_a_tag(t):
    sap = {"thermowell": {"results": [
        _line("TW-1", "THERMOWELL 06-TI-3903", "PUR-A"),
        _line("TC-1", "THERMOCOUPLE 06-TI-3903", "PUR-A", date="2025-05-01"),
        _line("TW-2", "THERMOWELL 06-TI-3901", "PUR-A", date="2023-01-01")]}}
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(sap)
    tools = {x.name: x for x in stage_tools.make_tools(conn, t["_home"], URL, call)}
    token = chatstate.enter("chat-x")
    try:
        tools["nam_need"].fn(need="thermowell", category="goods")
        tools["nam_answer"].fn(answer="TW-1")
        out = tools["nam_other"].fn()
    finally:
        chatstate.leave(token)
    assert "| TW-2 |" in out and "| TC-1 |" not in out


# -- kinds found by meaning are not types of the item; one copy of the options -------

def _tg_search():
    return narrow._sap({"results": [
        _line("TG-1", "TG Bimetallic U100mm 0-80Cel", "PUR-A", "temperature gauge bimetallic"),
        _line("TG-2", "TG Bimetallic U320mm 0-200Cel", "PUR-A", "temperature gauge bimetallic"),
        _line("TC-1", "Thermocouple K U320mm", "PUR-A"),
        _line("TW-1", "Thermowell SS316 U320", "PUR-A")]})


def test_kinds_found_by_meaning_only_are_marked_and_not_asked_as_types():
    cands = _tg_search()
    assert narrow.by_meaning(cands, "temperature gauge") == {"thermocouple", "thermowell"}
    assert "thermocouple 1 (found by meaning only: another material" in narrow.digest(
        cands, name="temperature gauge")
    q = narrow.suggest(cands, "temperature gauge")
    assert q["question"] == ("SAP's search for 'temperature gauge' found these kinds of "
                             "material: which one do you need?")


def test_a_typed_copy_of_the_options_is_dropped_when_the_buttons_show_them():
    ask = {"question": "Which type of Temperature Gauge do you need?",
           "options": ["Thermocouple (16 materials)", "TG (36 materials)", "Skin (10 materials)"]}
    reply = ("Which type of Temperature Gauge do you need?\n- Thermocouple\n- **TG**\n- Skin\n"
             "- Something else\nSAP holds 53 materials for it.")
    # the question is said once, last, right above its buttons
    q = "Which type of Temperature Gauge do you need?"
    assert stage_tools.drop_typed_options(reply, ask) == f"SAP holds 53 materials for it.\n\n{q}"
    assert stage_tools.drop_typed_options("Pick one. Options: Thermocouple | TG | Skin",
                                          ask) == q          # "Pick one." is the buttons' job
    plain = "SAP holds 53 materials. Most are TG bimetallic gauges."
    assert stage_tools.drop_typed_options(plain, ask) == f"{plain}\n\n{q}"


def test_the_items_initials_are_the_item_itself_and_filler_words_are_no_type():
    cands = narrow._sap({"results": [
        _line("TG-1", "TG Bimetallic U100mm", "PUR-A"), _line("TG-2", "TG Bimetallic U320mm", "PUR-A"),
        _line("TC-1", "Thermocouple K U320mm", "PUR-A"), _line("TC-2", "Thermocouple J", "PUR-A"),
        _line("TC-3", "Thermocouple K", "PUR-A")]})
    assert narrow.types(cands, "temperature gauge")[0] == ("tg", 2)        # before 3 thermocouples
    assert narrow.by_meaning(cands, "temperature gauge") == {"thermocouple"}
    tw = narrow._sap({"results": [_line("W-1", "TW for DCU Furnace", "PUR-A"),
                                  _line("W-2", "TW for CCU", "PUR-A")]})
    assert "for" not in dict(narrow.types(tw, "thermowell"))


def test_a_type_question_over_other_materials_is_refused_and_typed_extras_are_dropped(tmp_path):
    sap = {"temperature gauge": {"results": [
        _line("TG-1", "TG Bimetallic U100mm", "PUR-A"), _line("TG-2", "TG Bimetallic U320", "PUR-A"),
        _line("TC-1", "Thermocouple K U320mm", "PUR-A"), _line("TC-2", "Thermocouple J", "PUR-A")]}}
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(sap)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    opts = [{"label": "TG", "words": ["tg"]}, {"label": "Thermocouple", "words": ["thermocouple"]}]
    token = chatstate.enter("chat-tg")
    try:
        tools["nam_need"].fn(need="temperature gauge", category="goods")
        out = tools["nam_ask"].fn(question="Which type of temperature gauge do you need?",
                                  options=opts)
        assert "another material SAP found by meaning" in out
        out = tools["nam_ask"].fn(question="Which kind of material do you need?", options=opts)
        assert out.startswith('Asked: "Which kind of material do you need?"')
    finally:
        chatstate.leave(token)
    ask = {"question": "Which kind of material do you need?", "options": ["TG (2 materials)",
                                                                         "Thermocouple (2 materials)"]}
    reply = "Which kind of material do you need?\n- TG\n- Skin\n- Ceramic\n\nSAP holds 4 records."
    assert stage_tools.drop_typed_options(reply, ask) == (
        "SAP holds 4 records.\n\nWhich kind of material do you need?")


def test_the_category_is_asked_unless_the_officer_said_it(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(SAP)
    t = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    token = chatstate.enter("chat-k", "I need gaskets")          # no category in their words
    try:
        out = t["nam_need"].fn(need="gaskets", items=["gasket"], category="goods",
                               why="gaskets are supplied as materials")
        # the agent's guess is not taken: the officer confirms or changes it
        assert 'Asked: "This looks like goods: gaskets are supplied as materials.' in out
        assert "Options: Yes, go with Goods | Change to Services | Change to Works" in out
        assert case.for_session(conn, "chat-k").get("category") in (None, "")
    finally:
        chatstate.leave(token)
    token = chatstate.enter("chat-k2", "Goods: supply of gaskets")
    try:
        out = t["nam_need"].fn(need="supply of gaskets", items=["gasket"], category="goods")
        assert "Is this goods" not in out and "| 1 | gasket |" in out
    finally:
        chatstate.leave(token)


def test_every_question_names_its_material_even_for_one_and_pr_numbers_read_as_sap_writes_them(t):
    _need(t, ("valve",))
    assert case.progress(_data(t), case.item(_data(t), "valve")) == {"n": 1, "of": 1,
                                                                    "item": "valve"}
    assert narrow.doc_no("10082938") == "0010082938" and narrow.doc_no("4500024964") == "4500024964"
    assert narrow.doc_no("PUR-X") == "PUR-X"


def test_a_list_of_codes_offered_as_choices_never_reaches_the_officer():
    reply = ("**Which thermowell record do you want to add to the cart?**\n"
             "**Options (keys):**\n- I\u2011TW\u2011U240\u2011S321\u201101\n- I-TW-U210-SS316-02\n"
             "SAP holds 29 of them.")
    out = stage_tools.drop_code_lists(reply, {"I-TW-U240-S321-01", "I-TW-U210-SS316-02"})
    assert "I-TW" not in out and "I\u2011TW" not in out and "Options (keys)" not in out
    assert "SAP holds 29 of them." in out


def test_a_typed_list_under_an_options_heading_goes_with_the_heading():
    ask = {"question": "Which U size do you need?", "options": ["U-210 (1 material)",
                                                               "U-240 (1 material)"]}
    reply = ("**Which U size do you need?**  \nOptions:  \n\n- U\u2011210 (1 material)  \n"
             "- U\u2011240 (1 material)  \n\nPlease select one.")
    assert stage_tools.drop_typed_options(reply, ask) == "Which U size do you need?"


def test_when_the_types_do_not_split_the_harness_asks_by_the_values_that_do():
    cands = narrow._sap({"results": [
        _line("W-1", "Thermowell;U-210;150#;SS316", "PUR-A"),
        _line("W-2", "Thermowell;U-240;150#;SS316", "PUR-A"),
        _line("W-3", "Thermowell;U-240;300#;SS316", "PUR-A"),
        _line("W-4", "Thermowell;U-400;300#;SS316", "PUR-A")]})
    q = narrow.suggest(cands, "thermowell")
    assert q is not None and q["question"] == "Which of these does the thermowell you need have?"
    assert len(q["options"]) >= 2


def test_the_question_is_said_once_however_the_model_words_it():
    """From an officer's chat: the model typed the question in its own words with
    the options as "0-200 °C" while the buttons said "0-200 DegC (3 materials)",
    so the page showed the question and its options twice."""
    ask = {"question": "What is the temperature range?",
           "options": ["0-200 DegC (3 materials)", "0-150 DegC (2 materials)",
                       "Something else (36 materials)"]}
    reply = ("**What temperature range do you need?**  \n\n- 0‑200 °C  \n"
             "- 0‑150 °C  \n\n(If none of these fit, let me know.)")
    assert stage_tools.drop_typed_options(reply, ask) == "What is the temperature range?"
    cat = {"question": "Is this goods, services or works?", "options": ["Goods", "Services", "Works"]}
    said = ("Got it. I’ve opened a case for **capillary**.  \n"
            "Please select whether this is **Goods**, **Services** or **Works**.")
    assert stage_tools.drop_typed_options(said, cat) == (
        "Got it. I’ve opened a case for **capillary**.\n\nIs this goods, services or works?")
    yes = {"question": "Add this thermowell to the cart?", "options": ["Yes, add it", "No, show the others"]}
    assert stage_tools.drop_typed_options("**Add this thermowell to the cart?**\n\n[options: Yes, No]",
                                          yes) == "Add this thermowell to the cart?"
    # a sentence about the records is not the question: it stays
    other = {"question": "Which U code do you need?", "options": ["U320 (1 material)", "U400 (1 material)"]}
    assert stage_tools.drop_typed_options("The 0-200 range suits a hot line.", other) == (
        "The 0-200 range suits a hot line.\n\nWhich U code do you need?")


def test_a_question_said_again_over_two_lines_or_inside_a_line_is_said_once():
    """Live, the model wrote the category question as two lines, then once
    more in one line, above the buttons that ask it."""
    q = ("This looks like goods: gauges and thermowells are supplied. Shall we go ahead "
         "with goods, or change it?")
    ask = {"question": q, "options": ["Yes, go with Goods", "Change to Services"]}
    split = ("This looks like goods: gauges and thermowells are supplied.  \n"
             "**Shall we go ahead with goods, or change it?**\n\n" + q)
    assert stage_tools.drop_typed_options(split, ask) == q
    assert stage_tools.drop_typed_options("Got it, a case for gauges. " + q, ask) == (
        f"Got it, a case for gauges.\n\n{q}")


def test_a_short_instruction_about_the_buttons_goes_and_advice_stays():
    ask = {"question": "Which U code do you need?", "options": ["U240 (1 material)", "U360 (1 material)"]}
    q = ask["question"]
    for hint in ("Please select one.", "Choose the ones you need below.", "(Select the ones you need.)"):
        assert stage_tools.drop_typed_options(hint, ask) == q, hint
    for advice in ("Pick the 300# one if it is for the CDU.",
                   "Select the U code for the CDU furnace thermowell, which runs hot."):
        assert stage_tools.drop_typed_options(advice, ask) == f"{advice}\n\n{q}"
