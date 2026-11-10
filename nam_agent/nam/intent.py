"""What a user's message asks for when the call names no NAM step
("flowless" work), read in code.

The backend's call sites often send one generic system prompt ("Procurix NAM
assistant") and let the user's words carry the request. The router asks
this module what the message is, and takes a flow only when one is needed:

  kind()       the kind of message:
                 question   "what did we pay for gaskets?", "why PUR-4?"
                 show       "show me past tenders for gaskets"
                 approve    "approve it": an approval is never given from
                            words (plan D6); the pending one is shown
                 proceed    "go ahead", "next", "ok do it"
                 greeting   "hello"
                 ""         anything else: a need, or a change to the case
  answer_for() a change written in words, as the edit the step takes:
                 quantity.finalise  "make it 35 nos", "the quantity of
                                    line 2 should be 40"   -> "lines"
                 estimate.*         "the rate for line 1 is 520"  -> "rates"
                 pr.build           "the cost centre is 4100, plant NR01"
                                                           -> "fields"
               None when the words are not understood: never a guess
  edit_like()  words that look like such a change (a number with a field
               or a change verb), so a step that applied none of it can say
               so instead of reporting done (verify.py, check message_applied)
"""

from __future__ import annotations

import re

_QUESTION = re.compile(
    r"^\s*(what|what's|whats|why|which|who|whom|whose|when|where|how|is|are|was|were|"
    r"does|do|did|has|have|had|should|will|would)\b", re.IGNORECASE)
_REQUEST = re.compile(r"^\s*(can|could|would|will) you\b|\bplease\b", re.IGNORECASE)
_SHOW = re.compile(r"^\s*(show|list|find|tell|give|fetch|display|search)\b( me)?\b",
                   re.IGNORECASE)
_APPROVE = re.compile(r"^\s*(i\s+)?(approve|approved|sign off|signed off|confirm|confirmed|"
                      r"accept|accepted)\b(\s+(it|this|that|the \w+))?\s*[.!]?\s*$",
                      re.IGNORECASE)
_PROCEED = re.compile(r"^\s*(go ahead|go on|next|next step|proceed|continue|carry on|"
                      r"move on|ok(ay)?(,)?( (go|do it|proceed|continue))?|yes(,)? (go|do it|"
                      r"proceed|continue)|do it|let'?s (go|proceed|continue)|keep going|"
                      r"resume)\b[\s.!]*$", re.IGNORECASE)
_GREETING = re.compile(r"^\s*(hi|hello|hey|good (morning|afternoon|evening)|namaste)\b[\s.!]*$",
                       re.IGNORECASE)


def kind(message: str) -> str:
    text = (message or "").strip()
    if not text:
        return ""
    if _GREETING.match(text):
        return "greeting"
    if _APPROVE.match(text):
        return "approve"
    if _PROCEED.match(text):
        return "proceed"
    if _REQUEST.search(text):
        return ""                       # "could you prepare the PR": a request
    if _QUESTION.match(text) or text.endswith("?"):
        return "question"
    if _SHOW.match(text):
        return "show"
    return ""


# -- changes written in words ---------------------------------------------------------

_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"(nos|no|numbers|units|pcs|pieces|sets|kg|kgs|m|mtr|metres|meters|ltr|litres|cum|sqm|" \
        r"mdy|mandays|days|months|ls|lot)"
_LINE = r"(?:line|item|row)\s*(?:no\.?\s*)?(\d{1,4})"
_QTY_WORDS = r"(?:quantity|qty|count|number)"
_TO = r"(?:should be|must be|to be|to|=|:|is|of|at|as)?"
_CHANGE = re.compile(r"\b(make|set|change|update|increase|decrease|reduce|raise|revise|"
                     r"correct|should be|must be|instead)\b", re.IGNORECASE)


def _qty(message: str, lines: int) -> str | None:
    text = message.lower()
    m = re.search(rf"{_LINE}\D{{0,30}}?{_QTY_WORDS}?\s*{_TO}\s*{_NUM}\s*{_UNIT}?\b", text) \
        or re.search(rf"{_QTY_WORDS}\s*(?:of|for)\s*{_LINE}\s*{_TO}\s*{_NUM}\s*{_UNIT}?\b", text)
    if m and re.search(_QTY_WORDS + r"|" + _UNIT + r"\b|" + _CHANGE.pattern, text):
        return f"{m.group(1)}: qty={m.group(2)}"      # the unit stays the line's
    if lines != 1:
        return None                     # which line? never guessed
    m = re.search(rf"(?:{_QTY_WORDS}\s*{_TO}|\b(?:make|set|change|update)\s+it\s*(?:to)?|"
                  rf"\bit\s+(?:should|must)\s+be)\s*{_NUM}\s*{_UNIT}?\b", text) \
        or re.search(rf"^\s*{_NUM}\s*{_UNIT}\s*[.!]?\s*$", text)
    if not m:
        return None
    return f"1: qty={m.group(1)}"


def _rate(message: str, lines: int) -> str | None:
    text = message.lower().replace(",", "")
    m = re.search(rf"\brate\s*(?:for|of|on)?\s*{_LINE}\s*{_TO}\s*(?:rs\.?|inr|₹)?\s*{_NUM}\b",
                  text) \
        or re.search(rf"{_LINE}\D{{0,20}}?\brate\s*{_TO}\s*(?:rs\.?|inr|₹)?\s*{_NUM}\b", text)
    if m:
        return f"{m.group(1)}: rate={m.group(2)}"
    if lines == 1:
        m = re.search(rf"\b(?:the\s+)?rate\s*{_TO}\s*(?:rs\.?|inr|₹)?\s*{_NUM}\b", text)
        if m:
            return f"1: rate={m.group(1)}"
    return None


_PR_FIELDS = {"cost_centre": r"cost\s*cent(?:re|er)", "plant": r"plant",
              "purchasing_group": r"purchas(?:ing|e)\s*group", "material_group": r"material\s*group",
              "gl_account": r"gl\s*account", "delivery_date": r"delivery\s*date",
              "justification": r"justification"}


def _pr(message: str) -> str | None:
    found = []
    for key, words in _PR_FIELDS.items():
        m = re.search(rf"\b{words}\s*(?:is|=|:|should be|to)?\s*([A-Za-z0-9][\w./-]*(?: [\w./-]+)?)",
                      message, re.IGNORECASE)
        if m and m.group(1).lower() not in ("is", "the", "should", "to"):
            value = m.group(1).strip().rstrip(".,;")
            if key != "justification":
                value = value.split()[0]
            found.append(f"{key}={value}")
    return "all: " + ", ".join(found) if found else None


ANSWER_IDS = {"quantity.finalise": "lines", "estimate.goods": "rates", "estimate.wage": "rates",
              "estimate.works": "rates", "pr.build": "fields"}


def answer_for(flow_id: str, message: str, lines: int) -> dict | None:
    """The change the message writes in words, as the step's own answer
    ({question_id, value}), or None. `lines`: how many lines the case has,
    so "make it 35" is read only when there is one."""
    qid = ANSWER_IDS.get(flow_id)
    if qid is None or not (message or "").strip():
        return None
    if flow_id == "quantity.finalise":
        value = _qty(message, lines)
    elif flow_id.startswith("estimate."):
        value = _rate(message, lines)
    else:
        value = _pr(message)
    return {"question_id": qid, "value": value} if value else None


def edit_like(message: str) -> bool:
    """Words that change something on the case: a number with a change verb
    or a field name. A step that applied none of it must say so."""
    text = (message or "").lower()
    if kind(text) in ("question", "greeting", "approve", "proceed", "show"):
        return False
    has_number = bool(re.search(r"\d", text))
    fields = re.search(rf"{_QTY_WORDS}|\brate\b|\bprice\b|\bunit\b|{_UNIT}\b|"
                       + "|".join(_PR_FIELDS.values()), text)
    return has_number and bool(_CHANGE.search(text) or fields)
