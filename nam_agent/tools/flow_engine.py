"""Generic declarative intake-flow engine — pure data in, pure logic out, no
LLM calls and no side effects. Deterministic on purpose: with 30-80
mandatory/conditional questions per flow, letting the model freelance the
branching (skip a field, ask two at once, accept an NA that should have been
rejected) is where these things go wrong in practice. The model's job is
narrower and better suited to it: decide WHEN to start a flow, phrase the
question naturally, parse the user's free-text reply into a plain value, and
use judgment on anything genuinely ambiguous. Which field is next, and
whether "NA" is actually allowed right now, is decided here, from data,
every time, the same way.

A question is a plain dict:
    {
        "key": "urgency_reason",              # storage key / template placeholder name
        "label": "State reason for urgency",  # shown to the user
        "type": "text",                       # text | dropdown | radio | email | mobile | number | file
        "options": [...],                     # for dropdown / radio
        "mandatory": True,                    # unconditionally required
        "mandatory_if": {"field": "urgency", "equals": "Yes"},  # OR conditionally required
        "na_if": {"field": "urgency", "equals": "No"},          # auto-fills "NA", never asked
    }
Exactly one of "mandatory" / "mandatory_if" is normally set. "na_if" is only
needed on fields whose NA-ness is itself conditional on another answer; a
field that's simply optional just omits "mandatory" and accepts NA freely.
"""

from __future__ import annotations

from typing import Any

Question = dict[str, Any]
Flow = list[Question]

_NA_WORDS = {"NA", "N/A", "NONE", "SKIP", ""}


def _condition_met(cond: dict | None, answers: dict) -> bool:
    if cond is None:
        return False
    val = answers.get(cond["field"])
    if "equals" in cond:
        return val == cond["equals"]
    if "not_equals" in cond:
        return val is not None and val != cond["not_equals"]
    if "in" in cond:
        return val in cond["in"]
    return False


def is_required(q: Question, answers: dict) -> bool:
    if q.get("mandatory_if") is not None:
        return _condition_met(q["mandatory_if"], answers)
    return bool(q.get("mandatory"))


def is_na_forced(q: Question, answers: dict) -> bool:
    return _condition_met(q.get("na_if"), answers)


def normalize(value: str) -> str:
    v = (value or "").strip()
    return "NA" if v.upper() in _NA_WORDS else v


def validate_answer(q: Question, value: str, answers: dict) -> str | None:
    """Returns an error message to relay to the model, or None if accepted."""
    v = normalize(value)
    if v == "NA":
        if is_required(q, answers) and not is_na_forced(q, answers):
            return (f"'{q['label']}' is required based on the answers so far, so it can't be "
                     f"left as NA — please provide the actual value.")
        return None
    if q["type"] in ("dropdown", "radio") and q.get("options") and v not in q["options"]:
        return f"'{value}' isn't one of the allowed choices for '{q['label']}': {', '.join(q['options'])}."
    if q["type"] == "email" and ("@" not in v or "." not in v.split("@")[-1]):
        return f"'{value}' doesn't look like a valid email for '{q['label']}'."
    return None


def next_question(flow: Flow, answers: dict) -> Question | None:
    """First question that still needs an answer, walking the flow in order
    and auto-filling any field whose na_if condition is already true. Mutates
    `answers` in place only to fill those auto-NA fields (deterministic, no
    side effect a human would need to see asked-and-answered)."""
    for q in flow:
        key = q["key"]
        current = answers.get(key)
        if current is not None:
            # Re-open a field that was accepted as NA earlier if the answers
            # since then make it mandatory now (e.g. user changed an earlier
            # Yes/No that this field's requirement depends on).
            if current == "NA" and is_required(q, answers) and not is_na_forced(q, answers):
                return q
            continue
        if is_na_forced(q, answers):
            answers[key] = "NA"
            continue
        return q
    return None


def to_event(q: Question, answers: dict) -> dict:
    """Shape sent to the frontend via the 'ask_field' notify event."""
    return {
        "key": q["key"],
        "question": q["label"],
        "type": q["type"],
        "options": q.get("options", []),
        "mandatory": is_required(q, answers),
    }