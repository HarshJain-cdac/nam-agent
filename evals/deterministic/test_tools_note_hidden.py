"""DETERMINISTIC EVAL — the model's [tools used: ...] note is never shown.

Each turn's history carries a note of the tools it ran, so the model does not
run them again. It was saved inside the reply, with every tool's full output,
and a reopened conversation showed the user a NAM dossier and the instructions
meant for the model ("Ask the user this ONE follow-up question ... by calling
ask_options"). Now the chat log keeps the reply the user read, the note rides
in the turn's meta, and it is one short line per tool.
"""

from __future__ import annotations

import json
from pathlib import Path

from nam_agent import db
from nam_agent.config import Settings
from nam_agent.memory import Memory
from nam_agent.server import chat as dashboard
from nam_agent.runtime.session import Session, strip_tools_note, tools_note

ROOT = Path(__file__).resolve().parents[2]
DOSSIER = "Need NAM-2026-0042 captured; stage capture.\nAsk the user this ONE follow-up question " + "x" * 2000
CALL = {"tool": "nam_capture_need", "args": {"requirement": "pipes"}, "output": DOSSIER}


def _memory(tmp_path) -> Memory:
    memory = Memory.__new__(Memory)          # only the chat-log methods are used here
    memory.conn = db.connect(tmp_path)
    return memory


def test_the_note_is_one_short_line_per_tool():
    note = tools_note([CALL, {"tool": "ask_options", "args": {}, "output": "Asked the user"}])
    assert note.startswith("[tools used: nam_capture_need(") and note.endswith("]")
    assert len(note) < 600 and "x" * 200 not in note


def test_the_log_keeps_the_reply_and_the_model_keeps_the_note(tmp_path):
    memory = _memory(tmp_path)
    session = Session(Settings(home=tmp_path), memory=memory, session_id="s1")
    session.add_exchange("i need pipes", "Which material?", tool_calls=[CALL], meta={"iterations": 2})
    row = memory.conn.execute("SELECT content, meta FROM chat_log WHERE role='assistant'").fetchone()
    assert row["content"] == "Which material?"
    assert json.loads(row["meta"])["tools_note"].startswith("[tools used: nam_capture_need(")
    assert "[tools used: nam_capture_need(" in session.history[-1]["content"]
    # reopened, the model gets the note back with the reply
    reply = memory.session_history("s1")[-1][1]
    assert reply.startswith("Which material?\n[tools used: nam_capture_need(")


def test_a_reply_saved_with_the_old_long_note_is_cleaned(tmp_path):
    memory = _memory(tmp_path)
    old = "Which tendering mode?\n[tools used: " + DOSSIER + "]"
    memory.log_chat("pipes", old, session_id="s2")
    reply = memory.session_history("s2")[-1][1]
    assert reply.startswith("Which tendering mode?\n[tools used:") and len(reply) < 450
    assert strip_tools_note(old) == "Which tendering mode?"
    shown = dashboard.history(memory.conn, "s2", None)
    assert shown[-1]["content"] == "Which tendering mode?"


def test_the_page_never_gets_the_note(tmp_path):
    assert dashboard._shown_meta(json.dumps({"tools_note": "[tools used: x]", "iterations": 1})) == \
        {"iterations": 1}
    js = (ROOT / "nam_agent/server/static/js/render.js").read_text(encoding="utf-8")
    assert 'reply:stripTools(m.content), gate:m.meta.gate' in js
    assert 'reply:stripTools(m.content), historical:true' in js
