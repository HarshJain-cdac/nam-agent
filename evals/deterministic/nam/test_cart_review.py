"""DETERMINISTIC EVAL: the category guessed and confirmed, the cart reviewed,
more items added to the same cart, and no second pick after a click.

1. The agent judges goods, services or works from the need and says why; the
   officer confirms or changes it with buttons before anything is searched.
2. With every chosen item in the cart, the harness shows the whole cart and
   asks whether that is everything. The PURs and the PRs wait for the answer.
   "Add more items" puts new materials into the same case and cart, and the
   counts grow; the review comes back once they are in.
3. From an officer's chat: a card click put the item in the cart, and the
   model picked it again, so the officer was asked to confirm it a second time.
"""

from __future__ import annotations

import sqlite3

import pytest

from evals.deterministic.nam.test_cart_flow import JOB, SAP, URL
from evals.deterministic.nam.test_pr_flow import _kg
from nam_agent.nam import case, cart, chatstate, flow, stage_tools


@pytest.fixture
def t(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(SAP)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    tools["_conn"] = conn
    yield tools


def _turn(t, message, fn, *, click=""):
    """One officer turn: their message, the events it shows, the tool's output."""
    shown: list = []
    token = chatstate.enter("chat-r", message)
    emit = chatstate.set_emitter(lambda kind, ev: shown.append((kind, ev)))
    reading = chatstate.reading_set(click) if click else None
    try:
        out = fn(lambda kind, ev: shown.append((kind, ev)))
        if click:
            chatstate.mark_clicked(click)
        return out, shown
    finally:
        if reading is not None:
            chatstate.reading_reset(reading)
        chatstate.reset_emitter(emit)
        chatstate.leave(token)


def _note(tool, n) -> dict:
    """The tool's own callback for what it shows, when it takes one."""
    return {"_notify": n} if tool.wants_notify else {}


def _data(t):
    return case.for_session(t["_conn"], "chat-r")


def _click(t, message):
    """The officer presses one of the harness's buttons: it reads the click itself."""
    tool, args = stage_tools.read_click(t["_conn"], "chat-r", message)
    return _turn(t, message, lambda n: t[tool].fn(**args, **_note(t[tool], n)), click=tool)


# -- 1. the category -------------------------------------------------------------------

def test_the_agents_guess_is_confirmed_by_the_officer_before_any_search(t):
    out, shown = _turn(t, JOB, lambda n: t["nam_need"].fn(**_note(t["nam_need"], n), 
        need=JOB, items=["gasket", "valve"], category="goods",
        why="gaskets and valves are materials to be supplied"))
    ask = next(ev for kind, ev in shown if kind == "ask_options")
    assert ask["question"] == ("This looks like goods: gaskets and valves are materials to be "
                               "supplied. Shall we go ahead with goods, or change it?")
    assert ask["options"] == ["Yes, go with Goods", "Change to Services", "Change to Works"]
    assert "values" not in ask and "guess" not in ask         # the harness's own, not shown
    data = _data(t)
    assert not data.get("category") and not any(i.get("searched") for i in data["items"])
    assert cart.node_data(data)["category_guess"] == "goods"
    assert "looks like goods, waiting for the officer to confirm" in cart.node(data)
    assert "a yes keeps it" in flow.next_hint(data)
    # a re-ask shows the same question and buttons
    assert stage_tools.reminder(t["_conn"], "chat-r")[1]["options"] == ask["options"]
    out, _ = _click(t, "Yes, go with Goods")
    data = _data(t)
    assert data["category"] == "goods" and not data.get("asking_category")
    assert all(i.get("searched") for i in data["items"]) and "| 1 | gasket |" in out


def test_the_officer_changes_the_guess_with_a_button_or_in_words(t):
    _turn(t, JOB, lambda n: t["nam_need"].fn(**_note(t["nam_need"], n), need=JOB, items=["gasket"], category="goods",
                                           why="gaskets are supplied"))
    _click(t, "Change to Services")
    assert _data(t)["category"] == "services"


def test_the_guess_is_the_agents_own_and_no_guess_asks_plainly(t, tmp_path):
    _, shown = _turn(t, "I need gaskets", lambda n: t["nam_need"].fn(**_note(t["nam_need"], n), 
        need="gaskets", items=["gasket"], category="works", why="the agent's judgement"))
    ask = next(ev for kind, ev in shown if kind == "ask_options")
    assert ask["options"] == ["Yes, go with Works", "Change to Goods", "Change to Services"]
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(SAP)
    other = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    shown = []
    token = chatstate.enter("chat-plain", "I need gaskets")
    emit = chatstate.set_emitter(lambda kind, ev: shown.append((kind, ev)))
    try:
        other["nam_need"].fn(need="gaskets", items=["gasket"],
                             _notify=lambda kind, ev: shown.append((kind, ev)))
    finally:
        chatstate.reset_emitter(emit)
        chatstate.leave(token)
    ask = next(ev for kind, ev in shown if kind == "ask_options")
    assert ask["question"] == "Is this goods, services or works?"
    assert ask["options"] == ["Goods", "Services", "Works"]


# -- 2. the cart review ----------------------------------------------------------------

def _all_in_cart(t):
    _turn(t, JOB, lambda n: t["nam_need"].fn(**_note(t["nam_need"], n), need=JOB, items=["gasket", "valve", "thermowell"],
                                           category="goods", why="materials to be supplied"))
    _click(t, "Yes, go with Goods")
    _turn(t, "valve; thermowell", lambda n: t["nam_choose"].fn(**_note(t["nam_choose"], n), items=["valve", "thermowell"]))
    _turn(t, "screwed", lambda n: t["nam_answer"].fn(**_note(t["nam_answer"], n), item="valve", answer="screwed"))
    _turn(t, "", lambda n: t["nam_pick"].fn(**_note(t["nam_pick"], n), item="valve", key="V-2", quantity=4))
    return _turn(t, "", lambda n: t["nam_pick"].fn(**_note(t["nam_pick"], n), item="thermowell", key="T-1", quantity=2))


def test_with_every_item_in_the_whole_cart_is_shown_and_the_officer_is_asked(t):
    out, shown = _all_in_cart(t)
    groups = [ev for kind, ev in shown if kind == "nam_cards"]
    whole = next(g for g in groups if g["title"] == "Your cart")
    assert [c["fields"][0][1] for c in whole["cards"]] == ["valve", "thermowell"]
    assert [c["badge"] for c in whole["cards"]] == ["1 of 2", "2 of 2"]
    ask = next(ev for kind, ev in shown if kind == "ask_options")
    assert ask["question"] == stage_tools.REVIEW_QUESTION
    assert ask["options"] == [stage_tools.DONE_LABEL, stage_tools.MORE_LABEL]
    # the agent summarises in plain words; the question is the harness's
    assert "summary in plain words" in out and "no material codes" in out
    assert case.review(_data(t)) == "asked"
    assert "summarise it for the officer in plain words" in flow.next_hint(_data(t))
    assert "Cart review: asked whether that is everything" in cart.node(_data(t))


def test_the_purs_and_the_prs_wait_until_the_officer_says_that_is_everything(t):
    _all_in_cart(t)
    out, _ = _turn(t, "show the PURs", lambda n: t["nam_tenders"].fn(**_note(t["nam_tenders"], n), ))
    assert out.startswith("REFUSED: Not yet the PURs: the officer has not said")
    out, _ = _turn(t, "", lambda n: t["nam_pr"].fn(**_note(t["nam_pr"], n), item="valve", action="fill"))
    assert out.startswith("REFUSED: Not yet the PR")
    assert t["nam_pr"].fn(item="valve", action="show")         # looking is fine
    out, _ = _click(t, stage_tools.DONE_LABEL)
    assert out.startswith("The cart is complete: the need is understood.")
    assert "- valve: GATE VALVE 2IN; 4 NOS" in out and "Then stop: ask for nothing" in out
    assert case.review(_data(t)) == "done"
    out, _ = _turn(t, "", lambda n: t["nam_tenders"].fn(**_note(t["nam_tenders"], n), ))
    assert not out.startswith("REFUSED")


def test_more_items_go_into_the_same_cart_and_the_counts_grow(t):
    _all_in_cart(t)
    out, _ = _click(t, stage_tools.MORE_LABEL)
    assert "which materials to add" in out and case.review(_data(t)) == "adding"
    assert "add=true" in flow.next_hint(_data(t))
    assert _turn(t, "", lambda n: t["nam_tenders"].fn(**_note(t["nam_tenders"], n), ))[0].startswith(
        "REFUSED: Not yet the PURs: the officer is adding items")
    case_id = _data(t)["case_id"]
    out, _ = _turn(t, "add a gasket too", lambda n: t["nam_need"].fn(**_note(t["nam_need"], n), items=["gasket"], add=True))
    data = _data(t)
    assert data["case_id"] == case_id                          # the same case
    assert [i["name"] for i in case.chosen(data)] == ["valve", "thermowell", "gasket"]
    assert data["items"][0]["picked"]["key"] == "V-2"           # the cart stays as it was
    assert "added to the same case and cart: gasket. The cart holds 2 of 3 items" in out
    assert case.current_item(data)["name"] == "gasket" and case.review(data) == ""
    assert case.progress(data, case.item(data, "gasket")) == {"n": 3, "of": 3, "item": "gasket"}
    out, shown = _turn(t, "", lambda n: t["nam_pick"].fn(**_note(t["nam_pick"], n), item="gasket", key="G-2", quantity=1))
    added = next(ev for kind, ev in shown if kind == "nam_cards" and ev["title"] == "Added to the cart")
    assert added["cards"][0]["badge"] == "3 of 3 in the cart"
    # every item in again: the review comes back
    assert any(kind == "ask_options" and ev["question"] == stage_tools.REVIEW_QUESTION
               for kind, ev in shown)


def test_an_item_already_in_the_case_is_not_added_twice(t):
    _all_in_cart(t)
    _click(t, stage_tools.MORE_LABEL)
    out, _ = _turn(t, "", lambda n: t["nam_need"].fn(**_note(t["nam_need"], n), items=["Valve"], add=True))
    assert out.startswith("REFUSED: Every one of these is already in the case")


def test_the_cart_question_is_asked_once_and_undo_takes_the_answer_back(t):
    _all_in_cart(t)
    # the officer changes a quantity: the cart question is not asked again
    out, shown = _turn(t, "make it 3", lambda n: t["nam_pick"].fn(**_note(t["nam_pick"], n), item="thermowell", quantity=3))
    assert not any(kind == "ask_options" for kind, _ in shown)
    _click(t, stage_tools.DONE_LABEL)
    out, _ = _click(t, stage_tools.UNDO_LABEL)
    assert out.startswith("Took back: the officer said the cart is complete")
    assert case.review(_data(t)) == "asked"


def test_a_cart_full_before_this_change_is_reviewed_by_the_reminder(t):
    _all_in_cart(t)
    data = _data(t)
    data.pop("review")
    case.save(t["_conn"], data, "chat-r", "pick", "an older case")
    shown = []
    token = chatstate.enter("chat-r", "hi")
    emit = chatstate.set_emitter(lambda kind, ev: shown.append((kind, ev)))
    try:
        text, ask = stage_tools.reminder(t["_conn"], "chat-r")
    finally:
        chatstate.reset_emitter(emit)
        chatstate.leave(token)
    assert text == stage_tools.REVIEW_QUESTION
    assert any(ev.get("title") == "Your cart" for kind, ev in shown if kind == "nam_cards")
    assert case.review(_data(t)) == "asked"


# -- 3. no second pick after the officer's click ---------------------------------------

def test_a_click_puts_it_in_the_cart_once_and_the_model_cannot_ask_again(t):
    _turn(t, JOB, lambda n: t["nam_need"].fn(**_note(t["nam_need"], n), need=JOB, items=["valve", "thermowell"],
                                           category="goods", why="materials"))
    _click(t, "Yes, go with Goods")
    _turn(t, "valve; thermowell", lambda n: t["nam_choose"].fn(**_note(t["nam_choose"], n), items=["valve", "thermowell"]))
    _turn(t, "screwed", lambda n: t["nam_answer"].fn(**_note(t["nam_answer"], n), item="valve", answer="screwed"))
    from nam_agent.nam import cards

    data = _data(t)
    send = next(iter(cards.records("valve", case.remaining(case.item(data, "valve")))["choices"]))
    token = chatstate.enter("chat-r", send)
    try:
        tool, args = stage_tools.read_click(t["_conn"], "chat-r", send)
        reading = chatstate.reading_set(tool)
        try:
            assert "In the cart for 'valve': V-2" in t[tool].fn(**args)
        finally:
            chatstate.reading_reset(reading)
        chatstate.mark_clicked(tool)
        # the model, in the same turn, picks it again: refused, nothing is asked
        out = t["nam_pick"].fn(item="valve", key="V-2")
        assert out.startswith("REFUSED: The officer's click already put their choice in the cart")
    finally:
        chatstate.leave(token)
    assert not case.item(_data(t), "valve").get("pending_pick")
    out, _ = _turn(t, "the valve one", lambda n: t["nam_pick"].fn(**_note(t["nam_pick"], n), item="valve", key="V-2"))
    assert out.startswith("REFUSED: 'V-2' is already in the cart for 'valve'")


# -- 4. suggesting what NRL bought together with these before --------------------------

def _rel_line(code, desc, pr, pur="PUR-X", date="2024-05-01"):
    return {"description": desc, "codes": {code: desc}, "lines": [
        {"kind": "PO", "doc_no": "4500000009", "date": date, "material": code, "quantity": 2.0,
         "unit": "NOS", "rate_inr": 900.0, "pur_case_id": pur, "pr_no": pr,
         "po_no": "4500000009"}]}


# a fake SAP keyed by what is searched: the material, its past PR, and a suggestion
REL_SAP = {
    "10000010": {"results": [
        _rel_line("TC-1", "THERMOCOUPLE TYPE K U320", "10000010"),        # the need's own
        _rel_line("TW-9", "THERMOWELL FOR TC K SS316 U320", "10000010"),
        _rel_line("RTD-1", "RTD FOR BEARING 3X20MM", "10000010"),
        _rel_line("CAP-1", "TG CAPILLARY U600", "99999999")]},                # another PR
    "thermocouple": {"results": [_rel_line("TC-1", "THERMOCOUPLE TYPE K U320", "10000010")]},
    "thermowell": {"results": [_rel_line("TW-9", "THERMOWELL FOR TC K SS316 U320", "10000010")]},
}


@pytest.fixture
def r(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(REL_SAP)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    tools["_conn"] = conn
    _turn(tools, "goods: a thermocouple", lambda n: tools["nam_need"].fn(
        need="a thermocouple", items=["thermocouple"], category="goods", _notify=n))
    return tools


def test_what_was_bought_on_the_needs_past_prs_is_read_from_sap_for_the_agent(r):
    out, _ = _turn(r, "", lambda n: r["nam_related"].fn())
    assert out.startswith("FOR YOU, NOT TO SHOW") and "on their latest past PRs (1)" in out
    assert "| TW-9 | THERMOWELL FOR TC K SS316 U320 | 1 of 1 | 0010000010 | - |" in out
    assert "| RTD-1 |" in out
    assert "| TC-1 |" not in out and "CAP-1" not in out     # its own, and another PR's
    assert "A material only bought on the same PR does not belong" in out


def test_a_suggestion_rests_on_sap_and_is_offered_beside_the_needs_own(r):
    q = "Which of these do you want to take forward?"
    # no suggestion before nam_related, none from outside its evidence
    out, _ = _turn(r, "", lambda n: r["nam_choose"].fn(question=q, suggest=[
        {"name": "thermowell", "why": "fitted with it", "keys": ["TW-9"]}], _notify=n))
    assert out.startswith("REFUSED: Call nam_related first")
    _turn(r, "", lambda n: r["nam_related"].fn())
    out, _ = _turn(r, "", lambda n: r["nam_choose"].fn(question=q, suggest=[
        {"name": "thermowell", "why": "fitted with it", "keys": ["INVENTED-1"]}], _notify=n))
    assert out.startswith("REFUSED: INVENTED-1 is not on the need's past PRs")
    out, _ = _turn(r, "", lambda n: r["nam_choose"].fn(question=q, suggest=[
        {"name": "Thermocouple", "why": "same", "keys": ["TW-9"]}], _notify=n))
    assert "already one of the need's materials" in out
    out, shown = _turn(r, "", lambda n: r["nam_choose"].fn(question=q, suggest=[
        {"name": "thermowell", "why": "the thermocouple is fitted in a thermowell",
         "keys": ["TW-9"]}], _notify=n))
    ask = next(ev for kind, ev in shown if kind == "ask_options")
    assert ask["options"] == ["thermocouple (1 material)",
                              f"thermowell (1 material), {stage_tools.SUGGESTED_TAG}",
                              stage_tools.ALL]
    assert "thermowell (the thermocouple is fitted in a thermowell)" in out
    data = case.for_session(r["_conn"], "chat-r")
    assert [i["name"] for i in data["items"]] == ["thermocouple"]     # not in the case yet
    assert "Suggested (bought with these before" in cart.node(data)
    # the officer takes both: the suggestion is one of the need's materials now
    _click(r, f"thermocouple (1 material); thermowell (1 material), {stage_tools.SUGGESTED_TAG}")
    data = case.for_session(r["_conn"], "chat-r")
    assert [i["name"] for i in case.chosen(data)] == ["thermocouple", "thermowell"]
    assert case.item(data, "thermowell")["candidates"][0]["key"] == "TW-9"
    assert not data.get("suggested")


def test_a_suggestion_the_officer_leaves_leaves_no_trace(r):
    _turn(r, "", lambda n: r["nam_related"].fn())
    _turn(r, "", lambda n: r["nam_choose"].fn(question="Which go forward?", suggest=[
        {"name": "thermowell", "why": "fitted with it", "keys": ["TW-9"]}], _notify=n))
    _click(r, "thermocouple (1 material)")
    data = case.for_session(r["_conn"], "chat-r")
    assert [i["name"] for i in data["items"]] == ["thermocouple"]
    assert not data.get("suggested") and case.current_item(data)["name"] == "thermocouple"


def test_nothing_suggested_when_the_past_prs_hold_nothing_else(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, _ = _kg({"thermocouple": REL_SAP["thermocouple"]})
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    tools["_conn"] = conn
    _turn(tools, "goods: a thermocouple", lambda n: tools["nam_need"].fn(
        need="a thermocouple", items=["thermocouple"], category="goods", _notify=n))
    out, _ = _turn(tools, "", lambda n: tools["nam_related"].fn())
    assert "hold nothing else" in out and "Suggest nothing" in out
    data = case.for_session(conn, "chat-r")
    assert "nam_related" not in flow.next_hint(data)          # asked once, then on with it


def test_after_that_is_everything_the_chat_ends_on_a_plain_summary_not_pr_fields(t):
    _all_in_cart(t)
    out, _ = _click(t, stage_tools.DONE_LABEL)
    assert "Write a short summary of the whole need for" in out
    assert "following past PR 0010000002, its PO 4500000003" in out
    # the agent may not start the PR, nor ask for its fields, until the officer asks
    out, _ = _turn(t, "ok thanks", lambda n: t["nam_pr"].fn(item="valve", action="fill"))
    assert out.startswith("REFUSED: The need is complete and summarised: the PR is raised only")
    out, _ = _turn(t, "now raise the PR for the valve",
                   lambda n: t["nam_pr"].fn(item="valve", action="fill"))
    assert not out.startswith("REFUSED")


def test_the_need_ends_on_one_card_per_item_with_its_matching_pr_and_a_summary(t):
    _all_in_cart(t)
    out, shown = _click(t, stage_tools.DONE_LABEL)
    final = next(ev for kind, ev in shown if kind == "nam_cards" and ev.get("group") == "final")
    assert final["title"] == "Your need: the matching PR for each item"
    assert [c["badge"] for c in final["cards"]] == ["Item 1 of 2", "Item 2 of 2"]
    valve = final["cards"][0]
    fields = dict((k, v) for k, v, _ in valve["fields"])
    assert fields["PR"] == "0010000002" and fields["PO"] == "4500000003"
    assert fields["Quantity"] == "4 NOS" and fields["Your answers"] == "screwed"
    assert valve["status"] == "PR + PO" and valve["subtitle"] == "valve"
    assert valve["note"].startswith("4 NOS of GATE VALVE 2IN as bought on PR 0010000002")
    assert "It matches what you chose: screwed." in valve["note"]
    assert "not a buyer" in out and "Then stop: ask for nothing" in out


def test_the_past_prs_and_pos_in_the_summary_are_no_claim_of_a_new_order(t):
    _all_in_cart(t)
    _click(t, stage_tools.DONE_LABEL)
    past = ("The valve last came from PR 0010000002, with purchase orders 4500000003. "
            "The thermowell was bought under PO 4500000004.")
    assert stage_tools.claims(t["_conn"], "chat-r", past, []) is None
    assert stage_tools.claims(t["_conn"], "chat-r", "The PR is confirmed and a purchase order "
                              "has been raised.", []) is not None


def test_the_pur_case_is_a_field_inside_the_cards_not_a_heading(t):
    _, shown = _all_in_cart(t)
    whole = next(ev for kind, ev in shown if kind == "nam_cards" and ev["title"] == "Your cart")
    assert dict((k, v) for k, v, _ in whole["cards"][0]["fields"])["PUR"] == "PUR-B"
    _, shown = _click(t, stage_tools.DONE_LABEL)
    final = next(ev for kind, ev in shown if kind == "nam_cards" and ev.get("group") == "final")
    card = final["cards"][0]
    assert dict((k, v) for k, v, _ in card["fields"])["PUR"] == "PUR-B"
    assert "PUR" not in card["title"] and "PUR" not in final["title"]
