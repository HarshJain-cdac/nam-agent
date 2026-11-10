"""DETERMINISTIC EVAL — a procurement does not loop when the chat changes hands.

Live, 2026-11-05: "procure bimetaliic" opened NAM-2026-0129 and asked its
category; the officer's "Goods" came in a new chat, which had no case, so the
model took the turn, opened a case named "Goods", asked options of its own
("Pipe, Valve, Chemical, Equipment"), and every answer opened another case.
Now: a case the model's tool opens is the chat's; a new chat sees the case
another chat left open, and an answer to its question carries it on; a need
nothing matched offers the items the KG holds nearest to it, never a
question about its category.
"""

from __future__ import annotations

import sqlite3

from nam_fakes import fake_kg

from nam_agent.nam import capture, chatstate, drive
from nam_agent.nam import need as nam

KG = "http://kg"


def _turn(conn, tmp_path, session, message, fetch, awaiting):
    return drive.turn(conn, tmp_path, KG, session, message, fetch=fetch, awaiting=awaiting)


def test_a_case_the_model_opens_is_the_chats_and_its_answer_goes_to_the_harness(tmp_path,
                                                                                monkeypatch):
    conn, fetch = sqlite3.connect(":memory:"), fake_kg()
    real = capture.capture
    monkeypatch.setattr(capture, "capture", lambda *a, **k: real(*a, **{**k, "fetch": fetch}))
    tool = capture.make_tool(conn, tmp_path, KG)
    token = chatstate.enter("chat-A")
    try:
        out = tool.fn(requirement="i need gaskets")
    finally:
        chatstate.leave(token)
    case_id = chatstate.get(conn, "chat-A", "case")
    assert case_id and case_id in out
    assert "3-5 likely answers" not in out
    need = nam.load(conn, case_id)
    option = need.followups["pending"]["options"][0]["label"]
    nxt = _turn(conn, tmp_path, "chat-A", option, fetch, {})
    assert nxt is not None and nxt["tool_calls"][0]["tool"] == "nam_capture_need"
    assert nxt["tool_calls"][0]["args"]["need_id"] == case_id      # the same case, answered


def test_a_new_chat_carries_on_the_case_another_chat_left_open(tmp_path):
    conn, fetch, awaiting = sqlite3.connect(":memory:"), fake_kg(), {}
    first = _turn(conn, tmp_path, "chat-A", "i need gaskets", fetch, awaiting)
    case_id = chatstate.get(conn, "chat-A", "case")
    option = first["ask"]["options"][0]
    out = _turn(conn, tmp_path, "chat-B", option, fetch, awaiting)
    assert out["reply"].startswith(f"Continuing {case_id} (")
    assert chatstate.get(conn, "chat-B", "case") == case_id
    assert out["tool_calls"][0]["args"]["need_id"] == case_id
    # a chat whose message is not an answer does not take the case on
    assert _turn(conn, tmp_path, "chat-C", "hello there", fetch, awaiting) is None
    assert chatstate.get(conn, "chat-C", "case") is None


def test_a_new_chat_does_not_see_a_case_idle_past_the_chat_window(tmp_path, monkeypatch):
    conn, fetch, awaiting = sqlite3.connect(":memory:"), fake_kg(), {}
    first = _turn(conn, tmp_path, "chat-A", "i need gaskets", fetch, awaiting)
    monkeypatch.setenv("NAM_SESSION_IDLE_MINUTES", "0")
    assert drive.recent_open_case(conn, "chat-B") is None
    out = _turn(conn, tmp_path, "chat-B", first["ask"]["options"][0], fetch, awaiting)
    assert out is None or not out["reply"].startswith("Continuing")


def test_a_need_nothing_matched_offers_what_the_kg_finds_for_the_models_guesses(tmp_path):
    # "bimetaliic": the model proposes what was meant, and only names the KG
    # finds tenders for are offered, each with its count; no category question
    def model(system, user):
        return '{"items": ["spiral wound gasket", "bicycle", "gaskets"]}'

    conn, awaiting = sqlite3.connect(":memory:"), {}
    out = drive.turn(conn, tmp_path, KG, "chat-A", "i need to procure bimetaliic",
                     fetch=fake_kg(), awaiting=awaiting, ask_model=model)
    assert "category" not in out["reply"].lower()
    options = out["ask"]["options"]
    assert options and all("tender" in o for o in options)
    assert not any("bicycle" in o for o in options)           # the KG has none: not offered
    case_id = chatstate.get(conn, "chat-A", "case")
    again = drive.turn(conn, tmp_path, KG, "chat-A", options[0], fetch=fake_kg(),
                       awaiting=awaiting, ask_model=model)
    assert again is not None
    assert "(" not in nam.load(conn, case_id).raw_text      # the name, not its count
    assert "gasket" in nam.load(conn, case_id).raw_text


def test_with_no_suggestions_it_asks_in_plain_words(tmp_path):
    conn, awaiting = sqlite3.connect(":memory:"), {}
    out = _turn(conn, tmp_path, "chat-A", "i need to procure bimetaliic", fake_kg(), awaiting)
    assert out["ask"] is None and "Describe the item another way" in out["reply"]
