"""ask_options — the agent's way of asking a clarifying question with a fixed
set of choices, instead of guessing, before it calls a tool that CREATES or
GENERATES something on an underspecified request.

This tool doesn't touch state.db or the filesystem. "Running" it means
notifying the observer with the question + options — the SAME event pipe
that already carries gate/tool/text events out through respond()'s observer,
into dashboard.chat_stream's emit, and over SSE to the frontend. No new
plumbing, no change to the loop in agent.py: the model just doesn't call a
create/generate tool this turn, which is already guardrail 1's normal
end-of-turn case.
"""

from __future__ import annotations

import re

from nam_agent.tools.registry import Tool

# The screen adds its own "Other" (an answer in your own words), so a model's
# "Other" / "Others (please specify)" would show the same choice twice.
_OTHER = re.compile(r"^\W*others?\b", re.IGNORECASE)


def clean_options(options: list) -> list[str]:
    """The model's choices without its "Other", empties and repeats, in order."""
    out, seen = [], set()
    for o in options or []:
        text = " ".join(str(o).split())
        if text and not _OTHER.match(text) and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def make_tool() -> Tool:
    def ask_options(question: str = "", options: list[str] | None = None, _notify=None) -> str:
        from nam_agent.nam import chatstate

        if chatstate.asked():
            # nam_ask already showed this turn's question, its options checked
            # against the records: a second ask would replace them
            return (f"The officer already has the question \"{chatstate.asked()}\" with "
                    "options checked against the records. Do not ask again: stop and wait.")
        if chatstate.case_open():
            return ("A NAM case is open in this chat: ask with nam_ask, whose options come "
                    "from the records. A detail the records do not split on (a quantity, a "
                    "date) is asked in plain words, without options. Nothing was shown.")
        options = clean_options(options or [])
        if not question or not options:
            return ("ask_options needs both a question and at least two options. "
                     "Please call it again with both.")
        if _notify:
            _notify("ask_options", {"question": question, "options": options})
        if chatstate.current():
            chatstate.mark_asked(question)   # shown: nothing asks it again this turn
        # This string becomes the tool_result the model sees. It must make the
        # model stop, not treat the placeholder as an answer to relay or a
        # green light to immediately call the create/generate tool anyway.
        return (
            f"Asked the user: \"{question}\" with options: {', '.join(options)}. "
            "The user has NOT answered yet. Reply now with a brief sentence "
            "confirming you're waiting — do NOT call any other tool this turn."
        )

    return Tool(
        name="ask_options",
        description=(
            "Ask the user ONE question with choices they can click. Use it whenever you need a "
            "detail the user has not given (material, size, quantity, a date, which tender), and "
            "BEFORE calling a tool that creates or generates something (e.g. "
            "create_event) on an underspecified request — always prefer this over guessing or "
            "over typing several questions. One question per call, 3-5 likely answers as "
            "options (the screen adds 'Other' for a typed answer). After calling it, stop and "
            "wait; ask the next question only after the user has answered."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "One question, one sentence; never several at once"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-6 short, likely answers the user can pick from",
                },
            },
            "required": ["question", "options"],
        },
        fn=ask_options,
        wants_notify=True,
    )