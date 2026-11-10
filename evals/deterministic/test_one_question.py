"""DETERMINISTIC EVAL — the agent asks one question at a time, with choices.

Asked to procure pipes, the agent typed three questions in one message
(material, size and schedule, quantity) and the user had to answer them all in
free text. Now every missing detail is asked alone through ask_options, whose
choices the dashboard chat shows as buttons (plus "Other" for a typed answer),
and the next question comes only after the answer.
"""

from __future__ import annotations

import re
from pathlib import Path

from nam_agent.tools import clarify

ROOT = Path(__file__).resolve().parents[2]
RENDER = ROOT / "nam_agent" / "server" / "static" / "js" / "render.js"


def test_ask_options_is_one_question_with_choices():
    tool = clarify.make_tool()
    assert "ONE question" in tool.description and "never" in tool.input_schema[
        "properties"]["question"]["description"]
    seen = []
    out = tool.fn(question="Which material?", options=["Carbon steel", "Stainless steel"],
                  _notify=lambda kind, ev: seen.append((kind, ev)))
    assert seen == [("ask_options", {"question": "Which material?",
                                     "options": ["Carbon steel", "Stainless steel"]})]
    assert "has NOT answered yet" in out


def test_the_chat_shows_the_choices_as_buttons():
    js = RENDER.read_text(encoding="utf-8")
    assert 'ev.kind === "ask_options"' in js            # the streamed event is kept
    assert "ev.ask_options" in js                         # and the plain POST result
    assert re.search(r"^function pickOption\(", js, re.MULTILINE)   # global for onclick
    assert 'onclick: "pickOption(this)"' in js and "Other&hellip;" in js
    assert "${askRow(t)}" in js
    # once anything is sent, an old question shows without its buttons
    assert "m.ask.answered = true" in js and "if (t.ask.answered)" in js


def test_the_persona_says_one_question_at_a_time():
    from nam_agent.runtime.session import DEFAULT_SOUL

    assert "ask ONE question at a time, then stop and wait" in DEFAULT_SOUL
    assert "nam_ask" in DEFAULT_SOUL and "ask_options" in DEFAULT_SOUL
