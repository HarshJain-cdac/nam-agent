"""DETERMINISTIC EVAL — the harness asks Need capture's follow-up questions,
whatever model is loaded.

With Ministral, "i need Bimetallic TG 0-200 °C" got a rate table and no
question: the follow-ups ran only when the model chose to call
nam_capture_need. Now a stated need, and each answer to the open question,
run in code (nam_agent/nam/drive.py). The questions themselves still come from
the KG tenders (followup.py), not from a script. Proved here with a model
that never calls a tool.
"""

from __future__ import annotations

import sqlite3

import pytest
from nam_fakes import fake_kg

from evals.helpers import ScriptedClient, make_agent, response, text_block
from nam_agent.nam import drive, followup
from nam_agent.nam import need as nam

# The officers' own messages from the chat log.
NEEDS = {
    "i need Bimetallic TG 0-200 °C": "Bimetallic TG 0-200 °C",
    "i need tyres": "tyres",
    "i want to procure tyres": "tyres",
    "i want to buy oil 10000 liters": "oil 10000 liters",
    "hii, procure binding wires": "binding wires",
    "we have to procure more gaskets": "gaskets",
}
NOT_NEEDS = [
    "i need all the pqc of cili maintaince", "i need pc from electrical maintanence",
    "i need the prevouis work on PORTABLE MULTIGAS DETECTOR", "i need it in text",
    "i want to compare the PQC and SCC for both", "have we bought gaskets before?",
    "i want to narrow down my requirements", "we have pipes in store",
    "what did we pay for gaskets", "yes now give the final NiT",
    # NIT generation is not part of NAM: a NIT request is not a need
    "draft a NiT for the PORTABLE MULTIGAS DETECTOR", "I want to generate a NiT",
    "generate a NIT toprocure",
]


def test_a_stated_need_is_recognised_and_a_lookup_is_not():
    for message, expected in NEEDS.items():
        assert drive.stated_need(message) == expected, message
    for message in NOT_NEEDS:
        assert drive.stated_need(message) is None, message


def _turn(conn, tmp_path, message, fetch, awaiting=None):
    return drive.turn(conn, tmp_path, "http://kg", "chat-1", message, fetch=fetch,
                      awaiting=awaiting)


def test_a_need_gets_a_question_built_from_the_tenders(tmp_path):
    conn, fetch = sqlite3.connect(":memory:"), fake_kg()
    out = _turn(conn, tmp_path, "i need gaskets", fetch)
    assert out["ask"] and len(out["ask"]["options"]) >= 2
    assert [c["tool"] for c in out["tool_calls"]] == ["nam_capture_need", "ask_options"]
    assert out["ask"]["question"] in out["reply"] and "6 past NRL tender(s)" in out["reply"]
    # the options name what the tenders contain, with the harness's counts
    assert any("tenders)" in o for o in out["ask"]["options"])
    assert drive.open_need(conn, "chat-1") is not None
    assert drive.open_need(conn, "another-chat") is None


def test_a_clicked_option_is_applied_and_asking_moves_on(tmp_path):
    conn, fetch = sqlite3.connect(":memory:"), fake_kg()
    first = _turn(conn, tmp_path, "i need gaskets", fetch)
    pick = first["ask"]["options"][0]                      # exactly as the button sends it
    out = _turn(conn, tmp_path, pick, fetch)
    assert out is not None and out["tool_calls"][0]["args"]["question_id"]
    need = nam.load(conn, out["tool_calls"][0]["args"]["need_id"])
    assert next(iter(need.followups["answers"].values())) not in ("", pick)   # the option's value
    assert "match your answers" in out["reply"] or out["ask"] is None


def test_skip_stop_and_unrelated_messages(tmp_path):
    conn, fetch = sqlite3.connect(":memory:"), fake_kg()
    _turn(conn, tmp_path, "i need gaskets", fetch)
    pending = drive.open_need(conn, "chat-1").followups["pending"]
    assert drive.answer_to("any", pending) == ("answer", "")
    # "I don't know" is not a skip: NAM takes NRL's usual answer (PR flow 1(g))
    assert drive.answer_to("i don't know", pending) == ("answer", followup.UNKNOWN)
    assert drive.answer_to("what did we pay last time?", pending) == ("none", None)
    out = _turn(conn, tmp_path, "stop asking", fetch)
    assert "| PUR-1 |" in out["reply"]
    # then the classification, as the harness's own question with its options
    assert out["ask"]["field"] in ("procurement_type", "tender_type")
    assert set(out["ask"]["options"]) >= {"CAPEX", "OPEX"} or "Open" in out["ask"]["options"]
    assert drive.open_need(conn, "chat-1") is None
    assert _turn(conn, tmp_path, "what did we pay last time?", fetch) is None   # the model's


def test_a_nit_request_is_not_nams_and_no_question_about_a_nit_is_asked(tmp_path):
    conn, fetch, awaiting = sqlite3.connect(":memory:"), fake_kg(), {}
    assert _turn(conn, tmp_path, "generate a NIT", fetch, awaiting) is None
    assert not awaiting


def test_a_kg_that_cannot_run_leaves_the_turn_to_the_model(tmp_path):
    conn = sqlite3.connect(":memory:")
    assert _turn(conn, tmp_path, "i need gaskets", fake_kg(down=True)) is None


@pytest.fixture
def silent_model():
    """A model that never calls a tool: only says "Noted." when asked."""
    return ScriptedClient([response([text_block("Noted.")]) for _ in range(20)])


def test_the_follow_up_does_not_depend_on_the_model(tmp_path, monkeypatch, silent_model):
    monkeypatch.setattr(nam, "_get", fake_kg())
    nam_agent = make_agent(tmp_path, client=silent_model, kg_url="http://kg", nam_chat="driver")
    asked = []
    result = nam_agent.respond("i need gaskets", observer=lambda k, ev: asked.append(ev)
                          if k == "ask_options" else None)
    assert result.iterations == 0 and asked and asked[0]["options"]
    assert [c["tool"] for c in result.tool_calls] == ["nam_capture_need", "ask_options"]
    # the officer's click goes back to the capture, again without the model
    nxt = nam_agent.respond(asked[0]["options"][0])
    assert nxt.iterations == 0 and nxt.tool_calls[0]["args"].get("question_id")


def test_an_option_is_picked_by_its_words_without_the_bracketed_detail():
    # live, 2026-11-03: "In 2 months" was not taken for "In 2 months (2027-01-02)"
    pending = {"options": [{"label": "In 1 month (2026-12-03)", "value": "2026-12-03"},
                           {"label": "In 2 months (2027-01-02)", "value": "2027-01-02"}]}
    assert drive.answer_to("In 2 months", pending) == ("answer", "2027-01-02")
    assert drive.answer_to("in 2 months (2027-01-02)", pending) == ("answer", "2027-01-02")


def test_a_refused_answer_says_why_not_noted():
    out = {"followups": {"pending": {"id": "d:required_by", "text": "By when?", "options": []},
                         "answered": [{"id": "d:uom", "answer": "RM"}],
                         "ignored": "answer to d:required_by not taken: give a date"},
           "related": [], "item": "pipes"}
    reply, _ = drive._reply(out)
    assert reply.startswith("Not taken: give a date.") and "Noted: RM" not in reply


def test_an_answer_no_tender_carries_is_said_not_silently_kept():
    # live, 2026-11-03: "200 mm" for CPVC pipes matched none of the 15 tenders
    # and the reply only repeated "I found 15 ..."
    out = {"followups": {"pending": {"id": "q2", "text": "Water supply or drainage?",
                                     "options": []},
                         "answered": [{"id": "q1", "answer": "200 mm"}],
                         "trail": [{"id": "q1", "answer": "200 mm", "before": 15, "after": 15,
                                    "applied": False, "note": "no related tender matched "
                                                              "this, so the list was kept"}],
                         "total_related": 15},
           "related": [{}] * 15, "item": "cpvc pipes"}
    reply, _ = drive._reply(out)
    assert "related to cpvc pipes" in reply
    assert 'No past tender matched "200 mm", so all 15 stay in view' in reply
