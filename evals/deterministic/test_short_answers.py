"""DETERMINISTIC EVAL — "yes", "ok", "go" continue the conversation.

The officer was drafting a NIT; NAM harness asked "Should I generate the NIT?" and
the officer said "ok". The intent node read "ok" as a greeting and answered
with the canned hello, so the flow restarted (0 iterations, no history). Now
a short answer after NAM harness has replied goes to the full loop with the chat so
far; only before the first reply is it small talk. And a skill name called as
a tool ('procurement/nit-generation') gets a hint naming the real tools.
"""

from __future__ import annotations

from pathlib import Path

from nam_agent.tools import intent
from nam_agent.tools.registry import Tool, ToolRegistry

ROOT = Path(__file__).resolve().parents[2]

# The officer's own replies from the chat that lost track.
ANSWERS = ["yes", "ok", "proced", "go", "yes , contract period of 1 year",
           "limited tender", "advance", "sure", "okay go ahead"]


def test_short_answers_mid_conversation_reach_the_loop():
    for msg in ANSWERS:
        assert intent.classify(msg, in_conversation=True)[0] != intent.GREETING, msg


def test_greetings_still_short_circuit():
    assert intent.classify("hi")[0] == intent.GREETING
    assert intent.classify("ok")[0] == intent.GREETING          # nothing to answer yet
    assert intent.classify("thanks!", in_conversation=True)[0] == intent.GREETING
    assert intent.classify("hello there", in_conversation=True)[0] == intent.GREETING


def test_the_app_passes_the_chat_state():
    src = (ROOT / "nam_agent/app.py").read_text(encoding="utf-8")
    assert "intent_rules.classify(user_message, in_conversation)" in src


def test_a_skill_called_as_a_tool_gets_a_hint():
    reg = ToolRegistry()
    reg.register(Tool("nam_capture_need", "d", {"type": "object", "properties": {}},
                      lambda: "x"))
    out = reg.execute("procurement/nit-generation", {})
    assert "unknown tool" in out and "not tools" in out
    # NIT generation is not NAM's: no hint sends the model to a NIT tool
    assert "generate_nit" not in out
