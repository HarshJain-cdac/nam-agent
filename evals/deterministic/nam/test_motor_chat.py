"""DETERMINISTIC EVAL: what went wrong in an officer's "i want to procure motor" chat.

  - Plant "P1", storage location "S1" and a justification were set on the PR
    by the model; the officer gave only the date.
  - The date was offered as three made-up dates with buttons.
  - The reply named a tool ("recorded with nam_need(category=...)"), wrote
    SAP's internal keys ("SAP:motor dismantling & erection:"), and typed out
    the 55 materials nam_related read, with reasons of its own.
  - Two records were left, a question about them was refused, and nothing was
    shown: no cards, no buttons.
  - The past PR and PO the item follows were nowhere on the cart or the PR.
  - "motor" as goods: SAP held only service lines, and nobody said so.
"""

from __future__ import annotations

import sqlite3

import pytest

from evals.deterministic.nam.test_cart_flow import JOB, SAP, URL
from evals.deterministic.nam.test_pr_flow import _kg
from nam_agent.nam import case, chatstate, narrow, stage_tools
from nam_agent.tools import clarify


@pytest.fixture
def t(tmp_path):
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(SAP)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    tools["_conn"] = conn
    token = chatstate.enter("chat-m")         # the harness's own steps: no officer message
    yield tools
    chatstate.leave(token)


def _in_cart(t):
    t["nam_need"].fn(category="goods", need=JOB, items=["valve"])
    t["nam_answer"].fn(item="valve", answer="screwed")
    t["nam_pick"].fn(item="valve", key="V-2", quantity=4)
    chatstate.mark_asked("")
    t["nam_cart"].fn(answer="done")


def _said(message):
    """A new officer turn with these words."""
    return chatstate.enter("chat-m", message)


def test_a_pr_field_the_officer_never_gave_is_refused(t):
    _in_cart(t)
    token = _said("raise the PR, needed 2026-12-01")
    try:
        out = t["nam_pr"].fn(item="valve", action="set", delivery_date="2026-12-01",
                             plant="P1", storage_location="S1",
                             justification="Replacement of old motor")
    finally:
        chatstate.leave(token)
    assert out.startswith("REFUSED: The officer has not given Plant, Storage location, "
                          "Justification: a PR field is the officer's")
    token = _said("PR: by 1st December, plant 1100, store 0001, the old valve is leaking")
    try:
        out = t["nam_pr"].fn(item="valve", action="set", delivery_date="2026-12-01",
                             plant="1100", storage_location="0001",
                             justification="Old valve is leaking")
    finally:
        chatstate.leave(token)
    assert not out.startswith("REFUSED") and "| Plant | 1100 |" in out


def test_made_up_choices_for_a_pr_field_are_refused():
    tool = clarify.make_tool()
    out = tool.fn(question="What is the date needed (YYYY-MM-DD)?",
                  options=["2026-12-01", "2026-12-15", "2026-12-31"])
    assert out.startswith("That is a PR field the officer gives in their own words")
    shown = []
    out = tool.fn(question="Do you want one common PR for these?", options=["Yes", "No"],
                  _notify=lambda k, e: shown.append(e))
    assert shown and out.startswith("Asked the user")


def test_tool_names_internal_keys_and_the_evidence_never_reach_the_officer():
    data = {"items": [{"name": "motor", "candidates": [
        {"key": "SAP:motor dismantling & erection:", "code": "",
         "description": "Motor dismantling & erection"}]}],
        "related": {"codes": {"SAP:greasing:": "Greasing",
                              "SAP:dismantling steel work:": "Dismantling steel work"}}}
    reply = ("NAM-2026-0014 has been opened for the need motor.\n\n"
             "(Your choice will be recorded with `nam_need(category=...)`.)\n\n"
             "| # | Item | Why |\n|---|---|---|\n| 1 | Dismantling steel work | Often needed |\n"
             "| 2 | Greasing | Routine |\n\n"
             "Which record? **SAP:motor dismantling & erection:**")
    out = stage_tools.plain_words(reply, data, [{"tool": "nam_related"}])
    assert "nam_need" not in out and "Greasing" not in out and "SAP:" not in out
    assert out.startswith("NAM-2026-0014 has been opened")
    assert out.endswith("Which record? Motor dismantling & erection")


def test_with_a_few_records_left_the_harness_shows_them_when_the_question_was_refused(t):
    t["nam_need"].fn(category="goods", need=JOB, items=["valve"])
    t["nam_answer"].fn(item="valve", answer="cs")              # V-1 and V-3 left
    shown = []
    emit = chatstate.set_emitter(lambda kind, ev: shown.append((kind, ev)))
    try:
        assert stage_tools.reminder(t["_conn"], "chat-m") is None
    finally:
        chatstate.reset_emitter(emit)
    groups = [ev for kind, ev in shown if kind == "nam_cards"]
    assert groups and groups[0]["group"] == "records"


def test_the_cart_and_the_pr_carry_the_past_pr_and_its_po(t):
    shown = []
    emit = chatstate.set_emitter(lambda kind, ev: shown.append((kind, ev)))
    try:
        t["nam_need"].fn(category="goods", need=JOB, items=["valve"])
        t["nam_answer"].fn(item="valve", answer="screwed")
        out = t["nam_pick"].fn(item="valve", key="V-2", quantity=4)
    finally:
        chatstate.reset_emitter(emit)
    assert "past PR 0010000002, its PO 4500000003" in out           # for the summary
    whole = next(ev for kind, ev in shown if kind == "nam_cards" and ev["title"] == "Your cart")
    fields = dict((k, v) for k, v, _ in whole["cards"][0]["fields"])
    assert fields["PR"] == "0010000002" and fields["PO"] == "4500000003"
    chatstate.mark_asked("")
    t["nam_cart"].fn(answer="done")
    out = t["nam_pr"].fn(item="valve", action="show")
    assert "| Based on past PR | 0010000002 (PO 4500000003) |" in out
    from nam_agent.nam import cards

    card = cards.pr("valve", case.item(case.for_session(t["_conn"], "chat-m"), "valve")["pr"])
    fields = dict((k, v) for k, v, _ in card["cards"][0]["fields"])
    assert fields["Based on PR"] == "0010000002" and fields["Its PO"] == "4500000003"


def test_goods_for_which_sap_holds_only_service_lines_is_said_plainly(t):
    out = t["nam_need"].fn(category="goods", need="stud bolts", items=["bolt"])
    assert ("The need is goods, but for 'bolt' SAP holds only service lines, such as "
            "'Seal plate work'") in out


def test_suggestions_for_goods_are_materials_not_service_lines(tmp_path):
    line = lambda code, desc, kind: {"description": desc, "codes": {code: desc} if code else {},  # noqa: E731
                                     "lines": [{"kind": kind, "doc_no": "1", "date": "2024-01-01",
                                                "material": code or None, "pr_no": "10000077",
                                                "pur_case_id": "PUR-Z"}]}
    sap = {"10000077": {"results": [line("M-1", "MOTOR 15KW", "PO"),
                                    line("C-1", "MOTOR COUPLING", "PO"),
                                    line("", "Greasing", "SERVICE")]},
           "motor": {"results": [line("M-1", "MOTOR 15KW", "PO")]}}
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(sap)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    token = chatstate.enter("chat-z", "goods: a motor")
    try:
        tools["nam_need"].fn(category="goods", need="a motor", items=["motor"])
        out = tools["nam_related"].fn()
    finally:
        chatstate.leave(token)
    assert "MOTOR COUPLING" in out and "Greasing" not in out
    assert narrow.unit({"code": "", "lines": [{"kind": "SERVICE"}]}) == "service line"


def test_a_record_named_like_the_needs_own_is_marked_for_the_agent_to_judge(tmp_path):
    line = lambda code, desc: {"description": desc, "codes": {code: desc},  # noqa: E731
                               "lines": [{"kind": "PO", "doc_no": "1", "date": "2024-01-01",
                                          "material": code, "pr_no": "10000088",
                                          "pur_case_id": "PUR-Y"}]}
    sap = {"10000088": {"results": [line("TG-1", "TG Bimetallic U100"), line("TG-9", "TG Capillary U600"),
                                    line("PG-1", "PG 0-16 kg/cm2")]},
           "temperature gauge": {"results": [line("TG-1", "TG Bimetallic U100")]}}
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(sap)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    token = chatstate.enter("chat-y")
    try:
        tools["nam_need"].fn(category="goods", need="temperature gauge", items=["temperature gauge"])
        out = tools["nam_related"].fn(case_id="NAM-2026-0001")      # an extra argument is ignored
    finally:
        chatstate.leave(token)
    assert "| TG-9 | TG Capillary U600 | 1 of 1 | 0010000088 | temperature gauge |" in out
    assert "| PG-1 | PG 0-16 kg/cm2 | 1 of 1 | 0010000088 | - |" in out


def test_a_code_as_a_name_or_a_look_alike_of_the_needs_own_is_no_suggestion(tmp_path):
    line = lambda code, desc: {"description": desc, "codes": {code: desc},  # noqa: E731
                               "lines": [{"kind": "PO", "doc_no": "1", "date": "2024-01-01",
                                          "material": code, "pr_no": "10000088",
                                          "pur_case_id": "PUR-Y"}]}
    sap = {"10000088": {"results": [line("TG-1", "TG Bimetallic U100"), line("TG-9", "TG Capillary U600"),
                                    line("TW-1", "Thermowell SS316 U320")]},
           "temperature gauge": {"results": [line("TG-1", "TG Bimetallic U100")]},
           "thermowell": {"results": [line("TW-1", "Thermowell SS316 U320")]}}
    conn = sqlite3.connect(":memory:")
    call, _ = _kg(sap)
    tools = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call)}
    token = chatstate.enter("chat-y")
    try:
        tools["nam_need"].fn(category="goods", need="temperature gauge", items=["temperature gauge"])
        tools["nam_related"].fn()
        q = "Which go forward?"
        out = tools["nam_choose"].fn(question=q, suggest=[{"name": "TG-9", "why": "x", "keys": ["TG-9"]}])
        assert "is a material code" in out
        out = tools["nam_choose"].fn(question=q, suggest=[
            {"name": "capillary gauge", "why": "bought with it", "keys": ["TG-9"]}])
        assert "named like 'temperature gauge', already in the need" in out
        out = tools["nam_choose"].fn(question=q, suggest=[
            {"name": "thermowell", "why": "the gauge sits in a thermowell", "keys": ["TW-1"]}])
        assert out.startswith('Asked: "Which go forward?"')
    finally:
        chatstate.leave(token)
    data = case.for_session(conn, "chat-y")
    reply = "- **TG-9** - a gauge for 0-600\n- **TW-1** - a thermowell\n\nWhich go forward?"
    assert stage_tools.plain_words(reply, data, [{"tool": "nam_related"}]) == "Which go forward?"
