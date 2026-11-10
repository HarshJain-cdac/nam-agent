"""DETERMINISTIC EVAL — one chat, an ongoing procurement, and what the
officer says in the middle of it.

The chat keeps its place in the flow (nam_agent/nam/chatstate.py, in state.db):
  - a new need stated while a case is going is asked about first (start a new
    case, or carry on); the ongoing case is never dropped silently, and
    "resume NAM-..." goes back to it at its open question;
  - a side question gets the model's answer and the open question back;
  - the open question survives a restart;
"""

from __future__ import annotations

import sqlite3

import pytest
from nam_fakes import fake_kg

from evals.helpers import ScriptedClient, make_agent, response, text_block
from nam_agent.nam import chatstate, drive, fill
from nam_agent.nam import need as nam

SID = "chat-1"


@pytest.fixture()
def chat(tmp_path, monkeypatch):
    monkeypatch.setattr(nam, "_get", fake_kg())
    conn = sqlite3.connect(":memory:")

    def say(message, awaiting=None, case_id=""):
        return drive.turn(conn, tmp_path, "http://kg", SID, message, fetch=fake_kg(),
                          awaiting=chatstate.Awaiting(conn) if awaiting is None else awaiting,
                          case_id=case_id)

    return conn, say


def _cases(conn):
    return [r[0] for r in conn.execute("SELECT case_id FROM nam_cases ORDER BY case_id")]


def test_a_new_procurement_mid_capture_is_asked_about_not_started(chat):
    conn, say = chat
    first = say("i need gaskets")
    case = first["tool_calls"][0]["output"].split()[1]
    question = first["ask"]["question"]
    out = say("i need gate valves")
    assert out["ask"]["field"] == "switch" and case in out["reply"]
    assert out["ask"]["options"][:2] == ["Start a new case for gate valves", f"Continue {case}"]
    assert _cases(conn) == [case]                         # nothing started yet
    back = say(f"Continue {case}")
    assert back["ask"]["question"] == question           # the same open question, again
    assert _cases(conn) == [case]


def test_starting_the_new_case_keeps_the_old_one_and_resume_goes_back(chat):
    conn, say = chat
    first = say("i need gaskets")
    old = first["tool_calls"][0]["output"].split()[1]
    question = first["ask"]["question"]
    say("i need gate valves")
    out = say("Start a new case for gate valves")
    new = [c for c in _cases(conn) if c != old]
    assert len(new) == 1 and drive.session_case(conn, SID) == new[0]
    assert "gate valves" in nam.load(conn, new[0]).raw_text
    assert nam.load(conn, old).followups["pending"]["text"] == question   # untouched
    # no past tender has gate valves here: the new case asks for them in other
    # words, not for a category (2026-11-05: the category was asked of a need
    # nothing matched)
    assert "No past NRL tender matched" in out["reply"]
    back = say(f"resume {old}")
    assert drive.session_case(conn, SID) == old
    assert back["ask"]["question"] == question and old in back["reply"]


def test_a_new_procurement_after_capture_is_asked_about_too(chat):
    conn, say = chat
    first = say("i need gaskets")
    case = first["tool_calls"][0]["output"].split()[1]
    say("stop asking")
    awaiting = chatstate.Awaiting(conn)
    for value in ("OPEX", "Open"):                        # classified: the case moves on
        say("what next", awaiting)
        say(value, awaiting)
    out = say("we need to procure safety helmets")
    assert out["ask"]["field"] == "switch"
    assert out["ask"]["options"] == ["Start a new case for safety helmets",
                                     "Continue NAM-2026-0001"]
    say("no, carry on")
    assert _cases(conn) == [case]


def test_a_new_procurement_with_no_item_asks_what_before_any_case(chat):
    # live, 2026-11-03: "procure something else" became an item ("esle"), the
    # switch offered 'Use "procresomeething esle" as the answer', and the new
    # case asked "How many esle are needed?"
    conn, say = chat
    first = say("i need gaskets")
    case = first["tool_calls"][0]["output"].split()[1]
    out = say("i want to procresomeething esle")
    assert out["ask"]["options"] == ["Start a new procurement", f"Continue {case}"]
    assert "esle" not in out["reply"]
    out = say("Start a new procurement")
    assert out["reply"].startswith("What do you want to procure?") and _cases(conn) == [case]
    out = say("steel pipes")
    new = [c for c in _cases(conn) if c != case]
    assert len(new) == 1 and nam.load(conn, new[0]).raw_text == "steel pipes"
    assert drive.session_case(conn, SID) == new[0]


def test_something_else_with_no_case_open_asks_what_to_procure(chat):
    conn, say = chat
    out = say("i want to procure something else")
    assert out["reply"].startswith("What do you want to procure?") and _cases(conn) == []


def test_the_open_question_survives_a_restart(chat):
    conn, say = chat
    say("i need gaskets")
    say("stop asking")
    out = say("what next")
    assert out["ask"]["field"] == "procurement_type"
    # a restart: a fresh NAM harness builds a fresh mapping over the same state.db
    out = say(fill.nam.PROCUREMENT_TYPES[0], awaiting=chatstate.Awaiting(conn))
    case = nam.load(conn, drive.session_case(conn, SID))
    assert case.procurement_type["value"] == fill.nam.PROCUREMENT_TYPES[0]
    assert out["ask"]["field"] == "tender_type"


def test_a_question_of_the_officers_own_is_not_taken_as_the_answer(chat):
    conn, say = chat
    say("i need gaskets")
    assert say("what is a ring joint gasket?") is None    # the model's turn
    back = drive.reminder(conn, SID, "", chatstate.Awaiting(conn))
    assert back is not None and back[1]["options"]


def test_after_a_side_question_the_chat_goes_back_to_the_open_question(tmp_path, monkeypatch):
    monkeypatch.setattr(nam, "_get", fake_kg())
    client = ScriptedClient([])
    nam_agent = make_agent(tmp_path, client=client, kg_url="http://kg", nam_chat="driver")
    first = nam_agent.respond("i need gaskets")
    asked = next(c for c in first.tool_calls if c["tool"] == "ask_options")["args"]
    client._script += [response([text_block('{"intent": "question"}')]),   # intent reading
                       response([text_block("A ring joint gasket seals high-pressure flanges.")])]
    seen = []
    out = nam_agent.respond("what is a ring joint gasket?",
                       observer=lambda kind, ev: seen.append((kind, ev)))
    assert out.reply.startswith("A ring joint gasket seals")
    assert f"Back to the case: {asked['question']}" in out.reply
    assert ("ask_options", next(c["args"] for c in out.tool_calls
                                if c["tool"] == "ask_options")) in seen
