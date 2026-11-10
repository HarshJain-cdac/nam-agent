"""DETERMINISTIC EVAL — a question's choices never show "Other" twice.

The chat adds its own "Other" button (an answer in your own words) under
every ask_options question. A model that also listed "Others" or "Other
(please specify)" made the same choice appear twice on every question.
"""

from __future__ import annotations

from nam_agent.tools import clarify


def test_the_models_other_and_repeats_are_dropped_the_rest_kept_in_order():
    assert clarify.clean_options(["Open", "Limited", "Others", "Other (please specify)",
                                  "open", " ", "Otherwise noted"]) == [
        "Open", "Limited", "Otherwise noted"]


def test_the_question_sent_to_the_screen_carries_no_other():
    sent = []
    tool = clarify.make_tool()
    tool.fn(question="Which tender type?", options=["Open", "Other", "Limited", "OTHERS"],
            _notify=lambda kind, ev: sent.append(ev))
    assert sent == [{"question": "Which tender type?", "options": ["Open", "Limited"]}]
