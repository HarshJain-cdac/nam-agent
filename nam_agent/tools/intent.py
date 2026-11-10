"""The intent node — what kind of message is this, decided before the loop runs.

WHY THIS IS NOT AN LLM CALL
---------------------------
The obvious design is to ask a small model "what does the user want?". On this
deployment that is the wrong trade. Measured against the inference endpoint
this NAM harness talks to, one call costs between 1.6s and 21s — it is a shared vLLM
and the spread is queue time, not compute. An LLM classifier would therefore
put a whole extra queue wait in front of every message, and "Hii" would get
SLOWER, not faster.

So the obvious cases are decided here, from the text, in microseconds:

    "Hii"                  -> greeting   (answer now, no model, no tools)
    "thanks!"              -> greeting
    "yes" / "ok" / "go"    -> greeting only before the NAM harness's first reply; after
                              it, an answer: full loop with the chat so far
    "generate a NIT"       -> nit        (the wizard owns this turn)
    "what did we pay for   -> kg         (knowledge-graph lookup; the diagram
     gaskets last time?"                  lights its KG block for this)
    anything else          -> task       (the full loop, unchanged)

Only `greeting` short-circuits. `nit` and `kg` are labels: they steer nothing
on their own, they tell the harness diagram and the trace what this turn is
about, and they are the signal the KG block lights on. Everything that is not
a confident match falls through to the full loop exactly as before — this node
can remove latency, never capability.

That is the same order the rest of this project uses when it has a choice:
deterministic rules first, a model only for what rules genuinely cannot do
(see nam_agent/tools/aan_reader.py, and the KG's own doc_classifier.py).
"""

from __future__ import annotations

import re

GREETING = "greeting"
NIT = "nit"
KG = "kg"
TASK = "task"

# A greeting is short AND made only of greeting words. Both halves matter:
# "hi, can you cancel my 3pm?" starts with a greeting and is not one, and
# length alone would misread a short real request like "book Friday 9am".
_GREETING_WORDS = {
    "hi", "hii", "hiii", "hey", "heya", "hello", "helo", "hola", "yo",
    "namaste", "greetings", "morning", "afternoon", "evening",
    "thanks", "thank", "thankyou", "thx", "ty", "cheers",
    "ok", "okay", "okey", "k", "cool", "nice", "great", "good", "fine",
    "bye", "goodbye", "cya", "later", "night",
    "sup", "howdy", "yes", "no", "yeah", "yep", "nope", "sure",
    "you", "u", "there", "a", "the", "day", "to", "very", "much", "so",
    "and", "how", "are", "doing", "is", "it", "going", "im", "i", "am",
}
_MAX_GREETING_WORDS = 6

# Words that answer the assistant rather than open a conversation. "yes",
# "ok", "go" after "Should I generate the NIT?" is the officer's answer: it
# must reach the loop with the chat so far, never the canned hello (that
# reset the NIT flow mid-way). Only before the first reply is it small talk.
_ANSWER_WORDS = {
    "ok", "okay", "okey", "k", "cool", "nice", "great", "good", "fine",
    "yes", "no", "yeah", "yep", "nope", "sure", "go", "proceed", "proced",
    "continue", "alright", "done", "right", "correct", "y", "n",
}

# A knowledge-graph question: it is asking about what NRL has done before.
# Matched as whole words so "priceless" is not a price question.
_KG_PATTERNS = [
    r"\b(precedent|past tender|previous tender|earlier tender|historical)\b",
    r"\b(have|did|has) (we|nrl) (ever )?(bought|procured|purchased|tendered|issued)\b",
    r"\b(what|how much) (did|do|does) (we|nrl|it) (last )?(pay|paid|cost|costs)\b",
    r"\b(last|previous|historical|past) (price|rate|cost)\b",
    r"\b(price|rate|cost) (reference|history|history for|for)\b",
    r"\b(pqc|scc)\b",
    r"\b(pre.?qualification criteria|special contract conditions)\b",
    r"\b(who|which firm|which vendor|which bidder) (bid|bids|bid on|won|was awarded)\b",
    r"\b(bidder|vendor|supplier|firm)s? (for|who|that)\b",
    r"\bsimilar (tender|item|spec|work)\b",
    r"\bwhat has nrl\b",
]

# Starting or driving the NIT wizard.
_NIT_PATTERNS = [
    r"\bnit\b",
    r"\bnotice inviting tender\b",
    r"\b(generate|create|draft|raise|make|prepare) (a |an |the )?(new )?tender\b",
    r"\bannexure\b",
    r"\baan\b",
    r"\badmin(istrative)? approval note\b",
]

_WORD = re.compile(r"[a-z0-9']+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def is_greeting(message: str) -> bool:
    words = _words(message)
    if not words or len(words) > _MAX_GREETING_WORDS:
        return False
    return all(w in _GREETING_WORDS for w in words)


def _matches(message: str, patterns: list[str]) -> str | None:
    low = message.lower()
    for pattern in patterns:
        found = re.search(pattern, low)
        if found:
            return found.group(0)
    return None


def is_answer(message: str) -> bool:
    """A short reply with a yes/ok/go word in it: an answer to the assistant."""
    words = _words(message)
    return bool(words) and len(words) <= _MAX_GREETING_WORDS and bool(set(words) & _ANSWER_WORDS)


def classify(message: str, in_conversation: bool = False) -> tuple[str, str]:
    """(intent, reason). Never raises, never calls a model, never blocks.
    `in_conversation`: the assistant has already replied in this chat, so a
    short "yes" / "ok" / "go" answers it and goes to the full loop."""
    text = (message or "").strip()
    if not text:
        return TASK, "empty message"
    if in_conversation and is_answer(text):
        return TASK, "a short answer to the assistant — full loop with the chat so far"
    if is_greeting(text):
        return GREETING, "greeting or acknowledgement only"
    # NIT before KG: "what PQC did we use in the last NIT" is a NIT turn that
    # will consult the graph, and the wizard must own it either way.
    hit = _matches(text, _NIT_PATTERNS)
    if hit:
        return NIT, f"matched '{hit}'"
    hit = _matches(text, _KG_PATTERNS)
    if hit:
        return KG, f"matched '{hit}'"
    return TASK, "no rule matched — full loop"


# Replies to a greeting. Plain, varied only by what was said, and deliberately
# not model-written: the whole point is that this turn costs no network at all.
_GREETING_REPLY = (
    "Hello. Tell me what you need to procure and I will take it through Need "
    "Assessment: the questions, the estimate and the Purchase Request. I can also "
    "look up what NRL has tendered before. What do you need?"
)
_THANKS_REPLY = "Anytime. Say the word when you want the next one."
_BYE_REPLY = "Goodbye."

_THANKS = {"thanks", "thank", "thankyou", "thx", "ty", "cheers"}
_BYE = {"bye", "goodbye", "cya", "later", "night"}


def greeting_reply(message: str) -> str:
    words = set(_words(message))
    if words & _BYE:
        return _BYE_REPLY
    if words & _THANKS:
        return _THANKS_REPLY
    return _GREETING_REPLY
