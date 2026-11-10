"""The harness drives Need capture's follow-up questions, not the chat model.

Need capture (capture.py) already decides WHAT to ask, from the KG: it scans
every related past tender and builds one question that tells them apart
(followup.py: a model may propose it, the harness checks it against the
tenders, and the tenders' own facets are the fallback). Nothing there is
written per item.

What was left to the chat model was everything around it: noticing that "i
need tyres" is a need, calling nam_capture_need, showing its question with
ask_options and passing the officer's pick back. Gemma and Qwen did that;
Ministral answered "i need Bimetallic TG" with a rate table and asked
nothing. So the follow-ups depended on which model was loaded.

This module does those steps in code, before the model is called:

  a stated need ("i need X", "procure X")
      -> capture(X); its pending question is shown with ask_options
  a pick, "any" / "skip", or a short answer of their own to that question
      -> capture(answer=...); the next question, or the related tenders
  "stop asking"
      -> capture(skip_followups=True)

Anything else (a question, a lookup, "i need the PQC of ...") returns None
and the turn goes to the model exactly as before. So does a capture the KG
could not run: the harness never makes a turn worse than the model would.

One chat, several cases (chatstate.py keeps the chat's place in state.db):

  a new need stated while this chat's case is still going
      -> asked first: start a new case for it, or carry on with the case
         (or, with a follow-up open, take the words as its answer). The
         ongoing case is never dropped without the officer saying so.
  "resume NAM-2026-0012" / "back to NAM-2026-0012"
      -> that case is this chat's case again, and its open question or next
         step is asked
  a side question while a question is open
      -> the model answers it, and reminder() puts the open question back
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from nam_agent.nam import capture as nam_capture
from nam_agent.nam import chatstate, fill, guard, journey, orchestrator, scope, understand
from nam_agent.nam import followup as nam_followup
from nam_agent.nam import need as nam

# "i need", "we want to procure", "hi, please buy" -> what follows is the need.
_GREETING = r"(?:(?:hi+|hello|hey|dear)\b[\s,!.]*)?"
_WHO = (r"(?:(?:i|we)\s+(?:also\s+|now\s+|urgently\s+)?"
        r"(?:(?:need|require|want|would\s+like)\s+(?:to\s+)?|have\s+to\s+))")
_VERB = r"(?:(?:please\s+)?(?:procure|buy|purchase|order|source|get)\s+)"
_FILLER = re.compile(r"^(?:some|more|a|an|the|new|few|additional)\s+", re.IGNORECASE)

# What follows "i need" that is not an item: a lookup, a format, a pronoun.
# Those turns belong to the model and its KG tools.
_NOT_ITEM = {
    "it", "this", "that", "these", "those", "them", "all", "help", "you", "to",
    "previous", "prevouis", "past", "last", "earlier", "old", "pqc", "scc", "pc",
    "rate", "rates", "price", "prices", "list", "table", "details", "detail",
    "info", "information", "summary", "comparison", "compare", "report", "text",
    "pdf", "data", "answer", "nit", "same", "clause", "clauses",
    # "i want to <verb> ...": an action on the chat, not an item
    "narrow", "know", "see", "check", "understand", "ask", "find", "talk", "change",
    "update", "edit", "add", "remove", "modify", "download", "view", "open", "continue",
    "proceed", "go", "start", "use", "select", "pick", "choose", "generate", "draft",
    "create", "make", "give", "send", "write", "review", "verify",
}
_LOOKUP = re.compile(
    r"\?|\b(?:have|did|has)\s+(?:we|nrl)\b|\b(?:what|which|who|when|how|where|why|show|"
    r"list|compare|tell)\b|\b(?:precedent|past\s+tender|previous|historical)\b",
    re.IGNORECASE)

_SKIP = {"any", "skip", "any of these", "no preference",
         "doesnt matter", "doesn't matter", "either", "all", "all of them", "any one"}
# "I don't know" is not a skip (PR flow 1(g)): NAM takes NRL's usual answer
# and marks it a default
_UNKNOWN = {"not sure", "dont know", "don't know", "i dont know", "i don't know",
            "no idea", "not known", "unknown"}
# take the last answer back: the same question is asked again
_UNDO = {"undo", "go back", "back", "change my answer", "change my last answer",
         "undo my answer", "undo that", "previous question"}
_STOP = {"stop", "stop asking", "enough", "no more questions", "thats enough",
         "that's enough", "no more", "done"}
# the count an option's label carries (followup.option_label), dropped when a
# click is matched back to its option: "(14 tenders)",
# "(216 line-item records in 15 tenders)"
_COUNT = re.compile(r"\s*\((?:\d+\s+line[\s\u2011-]+item\s+records?(?:\s+in\s+)?)?"
                    r"(?:\d+\s+tenders?)?\)\s*$", re.IGNORECASE)
_MAX_FREE_ANSWER_WORDS = 8
MAX_ROWS = 10
# a question of the officer's own ("what is a ring joint?") is not an answer:
# the model answers it, and the open question is asked again after
_QUESTION = re.compile(r"\?\s*$|^\s*(?:what|why|how|which|who|whom|when|where|can|could|"
                       r"would|should|is|are|do|does|did|explain|tell me)\b", re.IGNORECASE)
# "i want to change something", "change my answer": the officer wants to give
# a value again (used when no model reads the message)
_CHANGE = re.compile(r"\b(?:change|chnage|chance|modify|edit|correct|update)\b", re.IGNORECASE)
# "choose the one that matches best", "which one fits our need?", "pick any":
# the officer asks NAM to choose (used when no model reads the message)
_RECOMMEND = re.compile(r"\b(?:choose|chose|pick|select|recommend|suggest)\b.*"
                        r"\b(?:best|match\w*|any|you|for me|one)\b|\bwhich\s+(?:one|tender)\b.*"
                        r"\b(?:match\w*|fit\w*|best|suit\w*)", re.IGNORECASE)
# "which tenders match the most", "what is the best match": answered from the
# case's own narrowing, never a table the model makes up
_BEST_MATCH = re.compile(r"\b(?:which|what)\b.*\b(?:tenders?|ones?|purs?)\b.*\b(?:match\w*|fit\w*|"
                         r"best|closest|suit\w*)|\b(?:best|closest|most)\s+(?:match\w*|tenders?|"
                         r"fit\w*)\b|\bmatch\w*\s+(?:the\s+)?most\b", re.IGNORECASE)
_CASE_ID = re.compile(r"\bNAM-\d{4}-\d{4,}\b", re.IGNORECASE)
_RESUME = re.compile(r"^\s*(?:please\s+)?(?:resume|continue(?:\s+with)?|go\s+back\s+to|back\s+to|"
                     r"switch\s+to|open|return\s+to)\b", re.IGNORECASE)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s:'-]", " ", _COUNT.sub("", text or "").lower()).split())


def stated_need(message: str) -> str | None:
    """The item when the message states something to procure, else None.
    NAM's output is the Purchase Request: a request for a NIT is not a need
    (NIT generation is not part of NAM), so it is the model's turn."""
    text = " ".join((message or "").split())
    if not text or _LOOKUP.search(text):
        return None
    m = re.match(rf"^{_GREETING}(?:{_WHO}{_VERB}?|{_VERB})(.+)$", text, re.IGNORECASE)
    if not m:
        return None
    item = m.group(1).strip(" .,!")
    while _FILLER.match(item):
        item = _FILLER.sub("", item, count=1)
    words = re.findall(r"[a-z0-9']+", item.lower())
    if not words or words[0] in _NOT_ITEM or _near_action(words[0]) or nam_capture.vague(item):
        return None
    return item


def _near_action(word: str) -> bool:
    """True for a typo of an action word: "chance" or "chnage" for "change",
    "udpate" for "update". Only words of 5 letters or more, one edit away."""
    if len(word) < 5:
        return False
    return any(len(v) >= 5 and _one_edit(word, v) for v in _NOT_ITEM)


def _one_edit(a: str, b: str) -> bool:
    """True when a and b differ by one substitution, insertion, deletion or
    swap of two neighbouring letters."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                                  and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def open_need(conn: sqlite3.Connection, session_id: str, case_id: str = ""
              ) -> nam.NeedRecord | None:
    """The case open here (a screen's, else this chat's latest), when its
    follow-up question is still open."""
    case_id = case_id or session_case(conn, session_id)
    need = nam.load(conn, case_id) if case_id else None
    if need is None or need.stage != "capture" or not (need.followups or {}).get("pending"):
        return None
    return need


def answer_to(message: str, pending: dict, free: bool = True) -> tuple[str, str | None]:
    """("answer", value) / ("stop", None) / ("none", None) for a reply to the
    open question. An option's label or value picks it; "any" skips; a short
    reply of the officer's own (the screen's "Other") is used as the answer."""
    said = _norm(message)
    if not said:
        return "none", None
    options = pending.get("options") or []
    for o in options:
        # "In 2 months" picks "In 2 months (2027-01-02)": the part in
        # brackets is a detail of the label, not words the officer must type
        short = re.sub(r"\s*\([^)]*\)\s*$", "", o.get("label", ""))
        if said in (_norm(o.get("label", "")), _norm(o.get("value", "")), _norm(short)):
            return "answer", o["value"]
    if said in ("yes", "y") and options and _norm(options[0]["label"]).startswith("yes"):
        return "answer", options[0]["value"]
    if said in ("no", "n"):
        no = next((o for o in options if _norm(o.get("label", "")) == "no"), None)
        if no:
            return "answer", no["value"]
    if said in _SKIP:
        return "answer", ""
    if said in _UNKNOWN:
        return "answer", nam_followup.UNKNOWN
    if said in _STOP:
        return "stop", None
    if (free and len(said.split()) <= _MAX_FREE_ANSWER_WORDS and not _LOOKUP.search(message)
            and stated_need(message) is None and not wants_new(message)
            and not _QUESTION.search(message)):
        return "answer", message.strip()
    return "none", None


def _labels(pending: dict) -> list[str]:
    from nam_agent.tools import clarify

    # the chat adds its own "Other": a tender value "Others" would show it twice
    return clarify.clean_options(
        [nam_followup.option_label(o) for o in pending.get("options") or []])


def _table(related: list[dict]) -> str:
    rows = ["| # | Tender | Subject | Opened |", "|---:|---|---|---|"]
    for i, r in enumerate(related[:MAX_ROWS], 1):
        subject = " ".join(str(r.get("subject") or "").split())[:90].replace("|", "/")
        rows.append(f"| {i} | {r['case_key']} | {subject} | {r.get('bid_opening_date') or '-'} |")
    if len(related) > MAX_ROWS:
        rows.append(f"| | ... {len(related) - MAX_ROWS} more in the dossier | | |")
    return "\n".join(rows)


def all_items(items: list[dict]) -> list[str]:
    """The past tenders that carried every item of a request with several
    ("Supply of Temperature Gauge, Thermocouple and Thermowell spares" for
    all three): found for each item, before any answer narrowed them."""
    found = [x.get("found") for x in items]
    if len(found) < 2 or not all(found):
        return []
    common = set(found[0]).intersection(*map(set, found[1:]))
    return [k for k in found[0] if k in common]


def _per_item(out: dict, items: list[dict], hint: str) -> str:
    """The end of a request for several items: each item's own result, one
    final tender each where its questions got there, then every tender."""
    subject = {r["case_key"]: " ".join(str(r.get("subject") or "").split())[:80]
               for r in out["related"]}
    lines = [f"Your request has {len(items)} items; each was matched on its own."]
    every = all_items(items)
    if every:
        lines[0] += (" Bought together before, in one tender: "
                     + ", ".join(f"{k} ({subject[k]})" if subject.get(k) else k for k in every[:3])
                     + ".")
    for x in items:
        keys = [k for k in x.get("related") or [] if k]
        name = str(x.get("text") or "item")[:80]
        if len(keys) == 1:
            sub = subject.get(keys[0], "")
            lines.append(f"- {name}: final tender {keys[0]}" + (f" ({sub})" if sub else ""))
        elif keys:
            why = str(x.get("stopped") or "nothing left in them tells them apart")[:120]
            lines.append(f"- {name}: {len(keys)} tenders left ({', '.join(keys[:5])}); {why}")
        else:
            lines.append(f"- {name}: no past tender matched it")
    text = "\n".join(lines) + f"\n\n{_table(out['related'])}"
    return f"{text}\n\n{hint}" if hint else text


def _reply(out: dict, hint: str = "") -> tuple[str, dict | None]:
    """(reply, ask) for one capture result: the open question, or what was
    found and what NAM needs next. Every count is the harness's; `hint` is
    the journey's next step (journey.next_hint)."""
    f = out.get("followups") or {}
    item = out.get("item") or "this"
    # the counts of the item being asked about: with several items, `related`
    # holds every item's tenders, and "found 2; 4 match" mixed the two
    total = f.get("total_related", len(out["related"]))
    left = f.get("narrowed_to", len(out["related"]))
    # only answers the officer gave are "your answers"; values taken from
    # their first words narrow the tenders too, and say so
    inferred = f.get("inferred") or {}
    given = [a for a in f.get("answered") or [] if isinstance(a, dict)
             and not a.get("from_your_words") and a.get("id") not in inferred]
    found = (f"I found {total} past NRL tender(s) related to {item}"
             + ((f"; {left} match your answers so far." if given else
                 f"; {left} match what you described.") if left != total else "."))
    trail = [t for t in f.get("trail") or [] if isinstance(t, dict)]
    last = trail[-1] if trail else {}
    if (given and last.get("id") == given[-1].get("id") and not last.get("applied")
            and "no related tender matched" in str(last.get("note") or "")):
        # a value no past tender carries narrows nothing: say so, not silently
        found += (f" No past tender matched \"{last.get('answer')}\", so all {left} stay in "
                  "view; it is kept as your requirement.")
    pending = f.get("pending")
    entries = [a for a in f.get("answered") or [] if isinstance(a, dict)]
    answered = entries[-1] if entries else {}
    if answered.get("from_your_words"):
        # the last ones were taken from earlier words ("5 RM"): say the
        # officer's own answer, then what was taken
        own = [a for a in entries if not a.get("from_your_words")]
        taken = [a for a in entries[len(own) and entries.index(own[-1]) + 1:]
                 if a.get("from_your_words") and str(a.get("id") or "").startswith("d:")]
        answered = {**(own[-1] if own else answered), "taken": taken}
    if str(answered.get("id") or "").startswith("d:") or \
            str((pending or {}).get("id") or "").startswith("d:"):
        # a detail or the category: say what was noted, not the tender count again
        found = (f"Noted: {answered['answer']}." if answered.get("answer") else "")
        if answered.get("taken"):
            found += " From what you said earlier: " + ", ".join(
                f"{str(a.get('question') or a['id'])[:40].rstrip('?')}: {a.get('answer')}"
                for a in answered["taken"]) + "."
        default = answered.get("default")
        if found and default:
            # "I don't know": say the value is a default, and where it came from
            basis = default.get("basis") if isinstance(default, dict) else ""
            found = (f"Not known, so the default is used: {answered['answer']}"
                     + (f" ({basis})" if basis else "") + ".")
    ignored = str(f.get("ignored") or "")
    if " not taken: " in ignored:
        # the answer was refused: say why, never "Noted" for an earlier one
        found = f"Not taken: {ignored.split(' not taken: ', 1)[1]}."
    if pending:
        ask = {"question": pending["text"], "options": _labels(pending)}
        return f"{found}\n\n{pending['text']}".strip(), ask
    ready = out["readiness"]
    if not out["related"]:
        return f"{ready['reason']} {ready.get('ask') or ''}".strip(), None
    items = [x for x in f.get("items") or [] if isinstance(x, dict)]
    if scope.tender_only() and len(items) > 1:
        return _per_item(out, items, hint), None
    text = f"{found}\n\n{_table(out['related'])}"
    if scope.tender_only():
        # the answer the officer is after: the past tender that matches
        best = out["related"][0]
        subject = " ".join(str(best.get("subject") or "").split())[:90]
        if len(out["related"]) == 1:
            text += f"\n\nFinal tender: {best['case_key']}" + (f" ({subject})." if subject else ".")
        else:
            why = str(f.get("stopped") or "nothing left in them tells them apart")
            text += (f"\n\n{len(out['related'])} past tenders match what you gave ({why[:160]}). "
                     "The closest is listed first.")
        return f"{text}\n\n{hint}" if hint else text, None
    text += f"\n\nNeed {out['need_id']}: {ready['reason']}"
    if not ready["ready"] and ready.get("ask"):
        text += f"\n\n{ready['ask']}"
    elif hint:
        text += f"\n\n{hint}"
    return text, None


def turn(conn: sqlite3.Connection, home: Path, kg_url: str, session_id: str, message: str,
         ask_model=None, awaiting: dict | None = None, fetch=None,
         case_id: str = "", intent_ask=None, history: list | None = None) -> dict | None:
    """Run this turn in code when it is a step of the flow. Returns {"reply",
    "ask", "tool_calls"} or None for the model.

    Exact words are read by rules: an option of the open question, "undo",
    "resume NAM-...", "line 3 rate 500", "next". Any other message, while a
    case is open or the words state a need, is read by the intent model with
    the case's context (understand.py, `intent_ask`, the chat's recent
    `history` as (role, text) pairs); the rules decide when no model answers.

    `awaiting` is the caller's per-chat memory of the question the harness
    asked last (chatstate.py); `fetch` is the KG reader (tests pass a stub)."""
    if not kg_url:
        return None
    message = guard.plain_ids(message)   # "resume NAM‑2026‑0012" typed with U+2011
    awaiting = awaiting if awaiting is not None else {}
    kw = {"fetch": fetch or nam._get}    # read at call time, so a test can swap it
    flow = _Flow(conn, home, kg_url, session_id, case_id, ask_model, awaiting, kw)
    if _norm(message) in _UNDO:
        out = flow.undo()
        if out is not None:
            return out
    for exact in (flow.switch_choice, flow.change_choice, flow.resume, flow.new_item,
                  flow.exact_answer, flow.exact_fill, flow.best_match):
        out = exact(message)
        if out is not None:
            return out
    pick = None
    if intent_ask is not None and (flow.case is not None or stated_need(message) is not None
                                   or wants_new(message)):
        pick = understand.read(intent_ask, flow.context(history), message)
    if pick is not None:
        return flow.act(pick, message)
    return flow.by_rules(message)


class _Flow:
    """One turn's view of the chat's place in the flow, and the steps it can
    take. Every step is the harness's: capture, fill, a case switch."""

    def __init__(self, conn, home, kg_url, session_id, case_id, ask_model, awaiting, kw):
        self.conn, self.home, self.kg_url = conn, home, kg_url
        self.session_id, self.case_id = session_id, case_id
        self.ask_model, self.awaiting, self.kw = ask_model, awaiting, kw
        self.waiting = awaiting.get(session_id)
        self.case = nam.load(conn, case_id or session_case(conn, session_id) or "")
        self.need = open_need(conn, session_id, case_id)
        self.pending = self.need.followups["pending"] if self.need is not None else None
        # a chat with no case of its own sees the case another chat left open
        # a moment ago (a new chat started mid-procurement): its question is
        # the context the answer is read against, and the case joins this
        # chat only when the officer's message answers it
        self.borrowed = ""
        if self.case is None and not case_id:
            other = recent_open_case(conn, session_id)
            if other is not None:
                self.case = self.need = other
                self.pending = other.followups["pending"]
                self.borrowed = other.need_id

    # -- exact words: rules ------------------------------------------------------------

    def undo(self) -> dict | None:
        self.awaiting.pop(self.session_id, None)
        last = _last_answered(self.conn, self.session_id, self.case_id)
        if last is None:
            return None
        need, qid = last
        out = nam_capture.capture(self.conn, self.home, self.kg_url, "", need.need_id,
                                  self.session_id, undo=qid, ask_model=self.ask_model, **self.kw)
        return _done(self.conn, out, {"need_id": need.need_id, "undo": qid}, self.awaiting,
                     self.session_id)

    def switch_choice(self, message: str) -> dict | None:
        return _switch_turn(self.conn, self.home, self.kg_url, self.session_id, message,
                            self.case_id, self.ask_model, self.awaiting, self.kw)

    def change_choice(self, message: str) -> dict | None:
        """A pick from "What do you want to change?" (ask_change)."""
        waiting = self.waiting if isinstance(self.waiting, dict) else {}
        if waiting.get("field") != "change":
            return None
        targets = waiting.get("targets") or {}
        target = next((t for t in targets if _norm(t) == _norm(message)), None)
        if target is None:
            return None
        return self.change(target, targets[target])

    def resume(self, message: str) -> dict | None:
        return _resume_turn(self.conn, self.session_id, message, self.awaiting)

    def exact_answer(self, message: str) -> dict | None:
        """An option of the open follow-up question, "skip", "I don't know",
        "stop asking", "what next"; a free answer only without a model."""
        if self.pending is None:
            return None
        kind, value = answer_to(message, self.pending, free=False)
        early = _early_values(message)
        if kind != "answer" and self.pending.get("id") == "d:quantity" and "quantity" in early:
            # "5 RM" to "How many?": 5 is the answer, RM is kept for the unit
            if "uom" in early:
                self._keep_early({"uom": early["uom"]})
            return self.answer(early["quantity"])
        if kind != "answer" and _plain_value(message, self.pending):
            kind, value = "answer", " ".join(message.split())
        if kind != "answer" and self.pending.get("narrows"):
            early = _early_values(message)
            if early:
                return self.note_early(early)
        if kind == "stop":
            return self.stop()
        if kind == "answer":
            return self.answer(value)
        if fill._norm(message) in fill._NEXT:
            return self.ask_open()
        return None

    def exact_fill(self, message: str) -> dict | None:
        """An option of the harness's open question, an edit stated outright
        ("line 3 rate 450"), or "next": the fill step, by rules."""
        if self.case is None or (self.case.followups or {}).get("pending"):
            return None
        waiting = self.waiting if isinstance(self.waiting, dict) else None
        if waiting and waiting.get("field") == "redescribe":
            # a click on one of the KG's nearest items: the need, said again
            picked = next((o for o in waiting.get("options") or [] if _norm(o) == _norm(message)),
                          None)
            return self.describe(_COUNT.sub("", picked).strip()) if picked else None
        options = [_norm(o) for o in (self._fill_question() or {}).get("options") or []]
        exact = (_norm(message) in options or fill._explicit(self.case, message) is not None
                 or fill._norm(message) in fill._NEXT)
        return self.fill(message) if exact else None

    # -- the steps ----------------------------------------------------------------------

    def answer(self, value: str) -> dict:
        need, pending = self.need, self.pending
        if self.borrowed:
            chatstate.put(self.conn, self.session_id, "case", self.borrowed)
        out = nam_capture.capture(self.conn, self.home, self.kg_url, "", need.need_id,
                                  self.session_id, answer={"question_id": pending["id"],
                                                           "value": value},
                                  ask_model=self.ask_model, **self.kw)
        done = _done(self.conn, out, {"need_id": need.need_id, "question_id": pending["id"],
                                      "answer": value}, self.awaiting, self.session_id)
        if done is not None and self.borrowed:
            done["reply"] = (f"Continuing {need.need_id} ({need.raw_text[:60]}) from your "
                             f"earlier chat.\n\n{done['reply']}")
        return done

    def _keep_early(self, early: dict) -> None:
        need = nam.load(self.conn, self.need.need_id)
        need.followups = {**(need.followups or {}),
                          "early": {**((need.followups or {}).get("early") or {}), **early}}
        nam.save(self.conn, need, self.session_id, reason="values given before their questions")

    def note_early(self, early: dict) -> dict:
        """Values given before their question ("5", "RM", "in 2 months" while
        a question about the tenders is open): kept for their own questions,
        which take them without asking, and the open question is asked again."""
        need = nam.load(self.conn, self.need.need_id)
        state = dict(need.followups or {})
        state["early"] = {**(state.get("early") or {}), **early}
        need.followups = state
        nam.save(self.conn, need, self.session_id, reason="values given before their questions")
        names = {"quantity": "quantity", "uom": "unit", "required_by": "date needed"}
        said = ", ".join(f"{v} as the {names.get(k, k)}" for k, v in early.items())
        text = f"Noted {said}, for later.\n\n{self.pending['text']}"
        ask = {"question": self.pending["text"], "options": _labels(self.pending)}
        return _turn_reply(text, ask, "nam_capture_need", {"need_id": need.need_id,
                                                           "early": early})

    def stop(self) -> dict:
        out = nam_capture.capture(self.conn, self.home, self.kg_url, "", self.need.need_id,
                                  self.session_id, skip_followups=True,
                                  ask_model=self.ask_model, **self.kw)
        return _done(self.conn, out, {"need_id": self.need.need_id, "stop": True},
                     self.awaiting, self.session_id)

    def recommend(self) -> dict | None:
        """NAM's pick for the open question, from the case's data, with why;
        the officer confirms it with one click (or picks another)."""
        if self.pending is not None:
            question, labels = self.pending, _labels(self.pending)
            pick = fill.recommend(self.need, self.pending)
        else:
            item = self._fill_item()
            if item is None:
                return None
            question, labels = item["question"], fill._ask(item)["options"]
            pick = fill.recommend(self.case, item["question"])
        if pick is None:
            # no data in the case ranks these options: the model answers with
            # the case in view (its advice), and the question comes back after
            return None
        label, why = pick
        first = next((x for x in labels if _norm(x).startswith(_norm(label))), label)
        ordered = [first, *[x for x in labels if x != first]]
        text = f"I recommend {label}. {why}\n\n{question['text']}"
        return _turn_reply(text, {"question": question["text"], "options": ordered},
                           "nam_recommend", {"recommended": label})

    def best_match(self, message: str) -> dict | None:
        """"Which tender matches best?" once the questions have ended: each
        item's tenders left, from the case's own record. Every tender left
        carries every answer given for its item (the questions removed the
        rest), so none is said to match "more" than another."""
        case = self.case
        if case is None or self.pending is not None or not _BEST_MATCH.search(message):
            return None
        state = case.followups or {}
        results = [x for x in state.get("item_results") or [] if isinstance(x, dict)]
        if not results:
            results = [{"text": case.raw_text, "related": list(case.related_cases or []),
                        "stopped": str(state.get("stopped") or "")}]
        subject: dict[str, str] = {}
        scans = [state.get("scan") or {}] + [x.get("scan") or {} for x in
                                              state.get("items") or [] if isinstance(x, dict)]
        for scan in scans:
            for r in scan.get("related") or []:
                if isinstance(r, dict) and r.get("case_key"):
                    subject.setdefault(r["case_key"],
                                       " ".join(str(r.get("subject") or "").split())[:80])
        stored = [x for x in state.get("items") or [] if isinstance(x, dict)]
        lines = []
        for i, x in enumerate(results):
            keys = [k for k in x.get("related") or [] if k]
            current = i == (state.get("item") or 0) or i >= len(stored)
            own = (state.get("answers") if current else stored[i].get("answers")) or {}
            given = ", ".join(str(v) for k, v in own.items()
                              if not str(k).startswith("d:"))[:160]
            name = str(x.get("text") or case.raw_text)[:80]
            by = f" (your answers: {given})" if given else ""
            if len(keys) == 1:
                sub = subject.get(keys[0], "")
                lines.append(f"- {name}: {keys[0]}" + (f" ({sub})" if sub else "")
                             + f" is the one past tender that carries every answer you gave{by}.")
            elif keys:
                lines.append(f"- {name}: {len(keys)} tenders carry every answer you gave{by}: "
                             + ", ".join(f"{k} ({subject.get(k, '')})".replace(" ()", "")
                                         for k in keys[:6])
                             + ". Your answers do not rank one above another; describe "
                               "what you need further to narrow them.")
            else:
                lines.append(f"- {name}: no past tender matched it.")
        every = all_items(results)
        if every:
            lines.append("- Every item together: " + ", ".join(
                f"{k} ({subject[k]})" if subject.get(k) else k for k in every[:3])
                + " carried all of them in one tender.")
        text = ("How the tenders match, from the questions you answered:\n" + "\n".join(lines))
        return _turn_reply(text, None, "nam_best_match", {"case_id": case.need_id})

    def _fill_item(self) -> dict | None:
        waiting = self.waiting if isinstance(self.waiting, dict) else {}
        if self.case is None or not waiting.get("field"):
            return None
        return next((x for x in fill.needs(self.case)
                     if (x["question"].get("id") or x.get("field")) == waiting["field"]), None)

    def ask_open(self) -> dict:
        ask = {"question": self.pending["text"], "options": _labels(self.pending)}
        return _turn_reply(self.pending["text"], ask, "nam_capture_need",
                           {"need_id": self.need.need_id})

    def fill(self, message: str) -> dict | None:
        """fill.turn on the chat's case; nothing to do on a case with no past
        tender asks for the need in other words instead of repeating the hint."""
        case = self.case
        if case is None or (case.followups or {}).get("pending"):
            return None
        if scope.tender_only() and fill._norm(message) not in fill._NEXT:
            return None                 # no later step to fill yet (scope.py)
        waiting = self.waiting if isinstance(self.waiting, dict) else None
        filled = fill.turn(self.conn, self.home, self.kg_url, case, message, waiting)
        if filled is None:
            return None
        reply, ask, state = filled
        if ask is None and not state and _no_precedent(case):
            return self.ask_redescribe()
        if state:
            self.awaiting[self.session_id] = state
        else:
            self.awaiting.pop(self.session_id, None)
        return _turn_reply(reply, ask, "nam_fill", {"need_id": case.need_id})

    def ask_redescribe(self) -> dict:
        case = self.case
        near = self.near_items(case.raw_text)
        self.awaiting[self.session_id] = {"field": "redescribe", "options": near,
                                          "text": "Describe the item another way"}
        text = (f"No past NRL tender matched \"{case.raw_text[:80]}\" in {case.need_id}, so "
                "there is nothing to build it from yet. "
                + ("Items NRL has bought whose names are closest to it are below: pick one, "
                   "or describe the item another way." if near else
                   "Describe the item another way: its other name, its material or grade, "
                   "or the equipment it is for."))
        ask = {"question": text, "options": near, "field": "redescribe"} if near else None
        return _turn_reply(text, ask, "nam_fill", {"need_id": case.need_id})

    def near_items(self, text: str) -> list[str]:
        """What the officer most likely meant, when nothing matched their
        words: the model proposes item names (a typo fixed, an abbreviation
        written out), and only the ones the KG finds tenders for are offered,
        each with its count. [] with no model or nothing found."""
        if self.ask_model is None:
            return []
        from nam_agent.nam.followup import _json_object

        prompt = ("A procurement officer at NRL (an oil refinery) asked to procure: "
                  + guard.wrap(text[:200], "user", "need")
                  + "\nNo past tender matched those words. Give up to 5 item names they most "
                    "likely meant (fix spelling, write out abbreviations), each a few words. "
                    'Reply with only JSON: {"items": ["...", "..."]}')
        try:
            reply = _json_object(self.ask_model(
                "You suggest what a refinery buyer most likely meant. " + guard.RULE, prompt))
        except Exception:  # noqa: BLE001 - a suggestion less, never a failed turn
            return []
        names = [" ".join(str(x).split())[:60] for x in (reply or {}).get("items") or []
                 if isinstance(x, str) and x.strip()][:5]

        def found(name: str) -> int:
            try:
                rows = self.kw["fetch"](self.kg_url, "/api/kg/tenders",
                                        {"query": name, "limit": 50}).get("tenders") or []
            except Exception:  # noqa: BLE001
                return 0
            return len(rows)

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            counts = list(pool.map(found, names))
        out, seen = [], set()
        for name, n in sorted(zip(names, counts), key=lambda x: -x[1]):
            if n and _norm(name) not in seen:
                seen.add(_norm(name))
                out.append(f"{name} ({n} tender{'s' if n != 1 else ''})")
        return out

    def describe(self, detail: str) -> dict | None:
        """More words about the case's need: the need is refined and the
        tenders are scanned again (capture's own refine)."""
        case = self.case
        if case is None or case.stage not in ("capture", "references", "describe"):
            return None
        self.awaiting.pop(self.session_id, None)
        said = detail
        if not case.related_cases:
            # nothing matched the old words: the new ones replace them, so the
            # scan is not still held to a word no tender has
            state = dict(case.followups or {})
            items = [dict(x) for x in state.get("items") or []]
            if items:
                items[int(state.get("item") or 0)]["text"] = detail
            case.raw_text = detail
            case.followups = {**state, "items": items, "scan": None, "pending": None}
            nam.save(self.conn, case, self.session_id,
                     reason="described again: no past tender matched the earlier words")
            detail = ""
        out = nam_capture.capture(self.conn, self.home, self.kg_url, detail, case.need_id,
                                  self.session_id, ask_model=self.ask_model, **self.kw)
        done = _done(self.conn, out, {"need_id": case.need_id, "detail": said},
                     self.awaiting, self.session_id)
        after = nam.load(self.conn, case.need_id)
        if after is not None and not after.related_cases and done is not None \
                and not done.get("ask"):
            # still nothing matched: the next words describe it again
            self.awaiting[self.session_id] = {"field": "redescribe"}
        return done

    def new_need(self, item: str, message: str) -> dict:
        """A new procurement: asked about first while a case is going; with no
        item named, "What do you want to procure?" comes before any case."""
        ongoing = _ongoing_case(self.conn, self.session_id, self.case_id)
        if ongoing is not None:
            return _ask_switch(self.conn, self.session_id, item, ongoing, message)
        if not item:
            return _ask_new(self.awaiting, self.session_id)
        return _start(self.conn, self.home, self.kg_url, self.session_id, item,
                      self.ask_model, self.awaiting, self.kw)

    def new_item(self, message: str) -> dict | None:
        """The answer to "What do you want to procure?": the case opens."""
        waiting = self.waiting if isinstance(self.waiting, dict) else {}
        if waiting.get("field") != "new_item" or _norm(message) in _UNDO | _STOP:
            return None
        item = stated_need(message) or " ".join(message.split()).strip(" .,!")
        if nam_capture.vague(item) or _QUESTION.search(message):
            return None
        return _start(self.conn, self.home, self.kg_url, self.session_id, item,
                      self.ask_model, self.awaiting, self.kw)

    def changeable(self) -> dict[str, str]:
        """{label: target} of what the officer can give again in the case."""
        case = self.case
        if case is None:
            return {}
        out: dict[str, str] = {}
        state = case.followups or {}
        questions = state.get("questions") or {}
        answered = list(state.get("answers") or {})
        for item in state.get("items") or []:
            answered += [q for q in item.get("answers") or {} if q not in answered]
        for qid in answered:
            text = str((state.get("asked") or {}).get(qid, {}).get("text")
                       or questions.get(qid) or qid)
            out[f"Answer to: {text[:70]}"] = f"q:{qid}"
        for field in ("procurement_type", "tender_type"):
            if (getattr(case, field) or {}).get("value"):
                out[field.replace("_", " ").capitalize()] = f"f:{field}"
        return out

    def ask_change(self) -> dict:
        targets = self.changeable()
        if not targets:
            return _turn_reply("Nothing in this case has been given yet, so there is nothing "
                               "to change.", None, "nam_case_change", {})
        self.awaiting[self.session_id] = {"field": "change", "targets": targets}
        text = f"What do you want to change in {self.case.need_id}?"
        ask = {"question": text, "options": list(targets), "field": "change"}
        return _turn_reply(text, ask, "nam_case_change", {"case_id": self.case.need_id})

    def change(self, label: str, target: str) -> dict | None:
        """Give a value again: a follow-up answer is taken back and its
        question asked again (later answers with it); a classification field
        is asked again with its options."""
        case = self.case
        kind, _, key = target.partition(":")
        if kind == "q":
            self.awaiting.pop(self.session_id, None)
            out = nam_capture.capture(self.conn, self.home, self.kg_url, "", case.need_id,
                                      self.session_id, undo=key, ask_model=self.ask_model,
                                      **self.kw)
            return _done(self.conn, out, {"need_id": case.need_id, "undo": key},
                         self.awaiting, self.session_id)
        item = fill.change_item(case, key)
        if item is None:
            return None
        ask = fill._ask(item)
        self.awaiting[self.session_id] = {"field": key, "change": True}
        return _turn_reply(ask["question"], ask, "nam_case_change", {"case_id": case.need_id,
                                                                      "field": key})

    def change_to(self, target: str, value: str) -> dict | None:
        """Give a new value at once: "200 mm" for the diameter recorded as
        15 mm. The old answer is taken back and the new one answers the same
        question (an option when it is one, else the officer's words)."""
        case = self.case
        kind, _, key = target.partition(":")
        if kind == "q":
            nam_capture.capture(self.conn, self.home, self.kg_url, "", case.need_id,
                                self.session_id, undo=key, ask_model=self.ask_model,
                                **self.kw)
            again = nam.load(self.conn, case.need_id)
            pending = (again.followups or {}).get("pending") if again else None
            if not pending or pending.get("id") != key:
                return None
            option = next((o["value"] for o in pending.get("options") or []
                           if _norm(o.get("label", "")) == _norm(value)
                           or _norm(o.get("value", "")) == _norm(value)), value)
            out = nam_capture.capture(self.conn, self.home, self.kg_url, "", case.need_id,
                                      self.session_id, answer={"question_id": key,
                                                               "value": option},
                                      ask_model=self.ask_model, **self.kw)
            return _done(self.conn, out, {"need_id": case.need_id, "question_id": key,
                                          "answer": option, "changed": True},
                         self.awaiting, self.session_id)
        item = fill.change_item(case, key)
        if item is None:
            return None
        self.awaiting[self.session_id] = {"field": key, "change": True}
        self.waiting = self.awaiting[self.session_id]
        return self.fill(value)

    def _fill_question(self) -> dict | None:
        waiting = self.waiting if isinstance(self.waiting, dict) else {}
        if self.case is None or not waiting.get("field"):
            return None
        if waiting.get("change"):
            item = fill.change_item(self.case, waiting["field"])
        else:
            item = next((x for x in fill.needs(self.case)
                         if (x["question"].get("id") or x.get("field")) == waiting["field"]),
                        None)
        return fill._ask(item) if item else None

    # -- the model's reading ------------------------------------------------------------

    def context(self, history: list | None) -> dict:
        case = self.case
        out: dict = {"turns": [(r, t) for r, t in (history or [])][-understand.TURNS:]}
        if case is not None:
            out["case"] = {"id": case.need_id, "need": case.raw_text, "where": _where(case),
                           "next": _hint(self.conn, case.need_id)}
            if self.borrowed:
                out["case"]["where"] = ("left open in the officer's earlier chat; this is a "
                                        "new chat. " + str(out["case"]["where"]))
            out["changeable"] = list(self.changeable())
        if self.pending is not None:
            out["question"] = {"text": self.pending["text"], "options": _labels(self.pending),
                               "narrows": bool(self.pending.get("narrows"))}
        elif chatstate.get(self.conn, self.session_id, "switch"):
            sw = chatstate.get(self.conn, self.session_id, "switch")
            out["question"] = {"text": "Start a new case, or carry on with the open one?",
                               "options": _switch_options(sw["item"], case) if case else []}
        elif isinstance(self.waiting, dict) and self.waiting.get("field") == "redescribe":
            out["question"] = {"text": "No past tender matched the need: describe the item "
                                       "another way (pick one of the items NRL has bought, or "
                                       "say it in other words)",
                               "options": list(self.waiting.get("options") or [])}
        else:
            q = self._fill_question()
            if q:
                out["question"] = {"text": q["question"], "options": q["options"]}
        return out

    def act(self, pick: dict, message: str) -> dict | None:
        """Do what the model's checked reading says; None hands the turn to
        the model's own reply (a question, small talk)."""
        intent = pick["intent"]
        waiting = self.waiting if isinstance(self.waiting, dict) else {}
        if intent in ("answer", "describe") and waiting.get("field") == "redescribe" \
                and self.case is not None:
            return self.describe(pick.get("value") or pick.get("detail") or message)
        if intent == "answer":
            if self.pending is not None:
                value = next((o["value"] for o in self.pending.get("options") or []
                              if _norm(o.get("label", "")) == _norm(pick["value"])
                              or _norm(o.get("value", "")) == _norm(pick["value"])),
                             pick["value"])
                return self.answer(value)
            sw = self.switch_choice(pick["value"])
            if sw is not None:
                return sw
            return self.fill(pick["value"])
        if intent == "new_need":
            return self.new_need(pick["item"], message)
        if intent == "describe":
            return self.describe(pick["detail"])
        if intent == "change":
            targets = self.changeable()
            if pick.get("target") in targets:
                if pick.get("value"):
                    out = self.change_to(targets[pick["target"]], pick["value"])
                    if out is not None:
                        return out
                return self.change(pick["target"], targets[pick["target"]])
            return self.ask_change() if self.case is not None else None
        if intent == "recommend":
            return self.recommend()
        if intent == "next":
            if self.pending is not None:
                return self.ask_open()
            if scope.tender_only() and fill._norm(message) not in fill._NEXT:
                # nothing comes after the tender yet: "get the PR of PUR-..."
                # is the model's, with the KG's tools, not the flow's "next"
                return None
            return self.fill("next")
        if intent == "resume":
            return self.resume(f"resume {pick['case_id']}")
        if intent == "undo":
            return self.undo()
        if intent == "stop" and self.need is not None:
            return self.stop()
        return None                                # a question or small talk: the model's

    # -- no model: the rules ------------------------------------------------------------

    def by_rules(self, message: str) -> dict | None:
        if self.pending is not None:
            kind, value = answer_to(message, self.pending)
            if kind == "answer":
                return self.answer(value)
        waiting = self.waiting if isinstance(self.waiting, dict) else {}
        if waiting.get("field") == "redescribe" and stated_need(message) is None \
                and not _QUESTION.search(message):
            if fill._norm(message) in fill._NEXT | fill._YES or len(_norm(message)) < 3:
                return self.ask_redescribe()            # "ok" is not a description
            return self.describe(message)
        if _RECOMMEND.search(message) and (self.pending is not None or self._fill_item()):
            out = self.recommend()
            if out is not None:
                return out
        if _CHANGE.search(message) and self.case is not None:
            return self.ask_change()
        out = self.fill(message)
        if out is not None:
            return out
        # not an answer to it: the model's turn, and the question stays open
        # (reminder() asks it again after the model's reply)
        item = stated_need(message)
        if item is None:
            return self.new_need("", message) if wants_new(message) else None
        return self.new_need(item, message)


_UNITS = {"m", "rm", "mtr", "mtrs", "meter", "meters", "metre", "metres", "nos", "no",
          "each", "set", "sets", "kg", "kgs", "ltr", "litre", "liter", "lot", "pcs", "pc",
          "rmt", "km", "ton", "tonne", "mt"}
_LATER = re.compile(r"^\s*(?:in|within)\s+(\d+)\s+(day|week|month)s?\s*$", re.IGNORECASE)


def _early_values(message: str) -> dict:
    """{detail key: value} for words that plainly answer a detail question
    asked later: a number ("5", "5 RM"), a unit ("RM"), a time ("in 2 months").
    Empty when the words are anything else."""
    from datetime import date, timedelta

    text = " ".join((message or "").split())
    later = _LATER.match(text)
    if later:
        n, unit = int(later.group(1)), later.group(2).lower()
        days = n * {"day": 1, "week": 7, "month": 30}[unit]
        return {"required_by": (date.today() + timedelta(days=days)).isoformat()}
    m = re.fullmatch(r"([\d][\d,]*(?:\.\d+)?)\s*([a-zA-Z]{1,6})?", text)
    if m:
        out = {"quantity": m.group(1).replace(",", "")}
        if m.group(2) and m.group(2).lower() in _UNITS:
            out["uom"] = m.group(2)
        elif m.group(2):
            return {}
        return out
    if text.lower() in _UNITS:
        return {"uom": text}
    return {}


_VALUE = re.compile(r"^\s*[\d.,]+\s*[a-zA-Z%°\"']{0,6}\s*$")


def _plain_value(message: str, pending: dict) -> bool:
    """True when the words are plainly a value for an open detail question
    ("5", "200 mm", "RM", "Nos"): a number with or without a unit, or one
    or two plain words for a question that takes the officer's own answer.
    Such a reply is the answer; no model is asked what it means (a small
    model once read "5" as a request to change the diameter)."""
    if pending.get("narrows") or pending.get("closed"):
        return False
    if _VALUE.match(message or ""):
        return True
    words = (message or "").split()
    return (0 < len(words) <= 2 and not _QUESTION.search(message)
            and stated_need(message) is None and not wants_new(message)
            and _norm(message) not in _UNDO | _STOP | _SKIP
            and not _CHANGE.search(message) and fill._norm(message) not in fill._NEXT)


def _no_precedent(case) -> bool:
    """True while the case is still being described and no past tender
    matched it: the flow cannot go on until the need is put another way."""
    wf = orchestrator.workflow(case)
    return (str(wf.get("stage") or "S1") in ("S1", "S2") and not case.related_cases
            and not case.selected_cases and not fill.needs(case))


def _start(conn, home, kg_url, session_id, item, ask_model, awaiting, kw) -> dict | None:
    """A new case for the stated item; it becomes this chat's case."""
    awaiting.pop(session_id, None)
    out = nam_capture.capture(conn, home, kg_url, item, session_id=session_id,
                              ask_model=ask_model, **kw)
    if out.get("need_id"):
        chatstate.put(conn, session_id, "case", out["need_id"])
        if not out.get("related") and not (out.get("followups") or {}).get("pending") \
                and not out.get("error"):
            # nothing matched: the items the KG holds nearest to the words
            flow = _Flow(conn, home, kg_url, session_id, out["need_id"], ask_model, awaiting, kw)
            if flow.case is not None:
                turn = flow.ask_redescribe()
                turn["tool_calls"].insert(0, {
                    "tool": "nam_capture_need", "args": {"requirement": item},
                    "output": f"Need {out['need_id']} captured; stage {out.get('stage')}. "
                              "0 related tender(s)."})
                return turn
    return _done(conn, out, {"requirement": item}, awaiting, session_id)


# -- one chat, several cases ----------------------------------------------------------

def _ongoing_case(conn, session_id: str, case_id: str = "") -> nam.NeedRecord | None:
    """This chat's case while NAM is not done with it (its PR not yet sent):
    a new need stated now must not silently replace it."""
    need = nam.load(conn, case_id or session_case(conn, session_id) or "")
    if need is None:
        return None
    wf = orchestrator.workflow(need)
    if wf.get("closed"):
        return None
    stage_no = int(str(wf.get("stage") or "S1")[1:] or 1)
    return need if stage_no <= int(journey.end_stage()[1:]) else None


def _where(need: nam.NeedRecord) -> str:
    """"NAM-2026-0012 (gaskets, step 1k: The quantities finalised)"."""
    try:
        j = journey.build(need)
        step = next((s for s in j["steps"] if s["id"] == j["step"]), None)
        at = f"step {step['id']}: {step['name']}" if step else \
            f"{j['stage']['id']}: {j['stage']['name']}"
    except Exception:  # noqa: BLE001 - the place is a courtesy, the id is enough
        at = "in progress"
    return f"{need.need_id} ({need.raw_text[:60]}, {at})"


def _switch_options(item: str, need: nam.NeedRecord) -> list[str]:
    start = f"Start a new case for {item}" if item else "Start a new procurement"
    return [start, f"Continue {need.need_id}"]


def _ask_switch(conn, session_id: str, item: str, need: nam.NeedRecord, message: str) -> dict:
    """Ask before a new need replaces the case under way."""
    chatstate.put(conn, session_id, "switch", {"item": item, "case_id": need.need_id,
                                               "message": message[:300]})
    what = f"a new case for {item}" if item else "a new procurement"
    question = (f"{_where(need)} is still going. Start {what}, or continue with "
                f"{need.need_id}? The ongoing case stays saved either way.")
    ask = {"question": question, "options": _switch_options(item, need), "field": "switch"}
    return _turn_reply(question, ask, "nam_case_switch",
                       {"item": item, "ongoing": need.need_id})


def _switch_turn(conn, home, kg_url, session_id, message, case_id, ask_model, awaiting,
                 kw) -> dict | None:
    """The officer's answer to "start a new case or carry on?", or None."""
    sw = chatstate.get(conn, session_id, "switch")
    if not sw:
        return None
    chatstate.put(conn, session_id, "switch", None)
    need = nam.load(conn, sw["case_id"])
    said = _norm(message)
    labels = [_norm(o) for o in _switch_options(sw["item"], need)] if need else []
    choice = labels.index(said) if said in labels else None
    if choice is None:
        if re.match(r"^(?:start|new|yes|start a new case|a new case|new case)\b", said):
            choice = 0
        elif re.match(r"^(?:continue|carry on|keep|no|go on|stay)\b", said):
            choice = 1
    if choice is None or need is None:
        return None                      # not an answer to it: read the message as it is
    if choice == 0:
        if not sw["item"]:
            return _ask_new(awaiting, session_id)
        return _start(conn, home, kg_url, session_id, sw["item"], ask_model, awaiting, kw)
    chatstate.put(conn, session_id, "case", need.need_id)
    return _carry_on(conn, need, awaiting, session_id, f"Carrying on with {_where(need)}.")


def _ask_new(awaiting, session_id: str) -> dict:
    """A new procurement with no item named yet: ask for it first. The next
    message (what to procure) opens the case (_Flow.new_item)."""
    awaiting[session_id] = {"field": "new_item"}
    text = "What do you want to procure? Name the item (for example: steel pipes, gate valves)."
    return _turn_reply(text, None, "nam_new_procurement", {})


def wants_new(message: str) -> bool:
    """True when the words ask for a procurement but name no item: "i want to
    procure something else"."""
    text = " ".join((message or "").split())
    m = re.match(rf"^{_GREETING}(?:{_WHO}{_VERB}?|{_VERB})(.+)$", text, re.IGNORECASE)
    if m:
        return nam_capture.vague(m.group(1))
    return bool(re.match(r"^(?:start\s+)?(?:a\s+)?new\s+(?:procurement|case|need|request)\b",
                         text, re.IGNORECASE))


def _resume_turn(conn, session_id: str, message: str, awaiting) -> dict | None:
    """"resume NAM-2026-0012": that case is this chat's again, or None."""
    m = _CASE_ID.search(message or "")
    if not m or not _RESUME.match(message):
        return None
    need = nam.load(conn, m.group(0).upper())
    if need is None:
        return _turn_reply(f"There is no case {m.group(0).upper()}.", None,
                           "nam_case_resume", {"case_id": m.group(0).upper()})
    chatstate.put(conn, session_id, "case", need.need_id)
    awaiting.pop(session_id, None)
    return _carry_on(conn, need, awaiting, session_id, f"Back to {_where(need)}.")


def _carry_on(conn, need: nam.NeedRecord, awaiting, session_id: str, said: str) -> dict:
    """The case's open question, else its next need, asked again."""
    back = reminder(conn, session_id, need.need_id, awaiting, force=True)
    if back is None:
        return _turn_reply(f"{said} {_hint(conn, need.need_id)}".strip(), None,
                           "nam_case_resume", {"case_id": need.need_id})
    text, ask = back
    return _turn_reply(f"{said}\n\n{text}", ask, "nam_case_resume", {"case_id": need.need_id})


def reminder(conn, session_id: str, case_id: str = "", awaiting=None, force: bool = False
             ) -> tuple[str, dict] | None:
    """(text, ask) for the question this chat's case has open, or None: the
    flow's place, put back after a side question so it is never lost. With
    `force`, the case's next need is asked even when no question was open."""
    need = open_need(conn, session_id, case_id)
    if need is not None:
        pending = need.followups["pending"]
        return pending["text"], {"question": pending["text"], "options": _labels(pending)}
    waiting = (awaiting or {}).get(session_id) if awaiting is not None else None
    case = nam.load(conn, case_id or session_case(conn, session_id) or "")
    if case is None or not (force or isinstance(waiting, dict)):
        return None
    todo = fill.needs(case)
    if not todo:
        return None
    ask = fill._ask(todo[0])
    if awaiting is not None:
        awaiting[session_id] = {"field": ask["field"]}
    return todo[0]["question"]["text"], ask


def _done(conn: sqlite3.Connection, out: dict, args: dict, awaiting: dict | None = None,
          session_id: str = "") -> dict | None:
    """The turn's reply for a capture result, or None for the model."""
    error = str(out.get("error") or "").lower()
    if error and not out.get("related") and (
            not out.get("need_id") or "unreachable" in error or "not configured" in error):
        return None            # the KG could not run: the model handles the turn
    reply, ask = _reply(out, _hint(conn, out.get("need_id")))
    need = nam.load(conn, out["need_id"]) if out.get("need_id") else None
    if ask is None and need is not None and awaiting is not None and session_id:
        todo = fill.needs(need)
        if todo:
            # the next thing the case needs (the classification first) is the
            # harness's question too, with its options, never the model's prose
            ask = fill._ask(todo[0])
            reply = f"{reply}\n\n{todo[0]['question']['text']}"
            awaiting[session_id] = {"field": ask["field"]}
    calls = [{"tool": "nam_capture_need", "args": args,
              "output": f"Need {out.get('need_id')} captured; stage {out.get('stage')}. "
                        f"{len(out.get('related') or [])} related tender(s)."}]
    if ask:
        calls.append({"tool": "ask_options", "args": ask,
                      "output": f"Asked the user: \"{ask['question']}\". Waiting for the answer."})
    return {"reply": reply, "ask": ask, "tool_calls": calls}


def _hint(conn: sqlite3.Connection, need_id: str | None) -> str:
    """The journey's next step for the case, or "" (never breaks a reply)."""
    need = nam.load(conn, need_id) if need_id else None
    if need is None:
        return ""
    if scope.tender_only():
        return scope.END
    try:
        return journey.next_hint(journey.build(need))
    except Exception:  # noqa: BLE001 - a hint is a courtesy, the reply stands without it
        return ""


def _last_answered(conn: sqlite3.Connection, session_id: str, case_id: str = ""
                   ) -> tuple[nam.NeedRecord, str] | None:
    """The case's last answered question, while it is in capture."""
    case_id = case_id or session_case(conn, session_id)
    need = nam.load(conn, case_id) if case_id else None
    if need is None or need.stage not in ("capture", "references"):
        return None
    state = need.followups or {}
    answers = list(state.get("answers") or {})
    if not answers:
        done = [x for x in state.get("items") or [] if x.get("answers")]
        answers = list(done[-1]["answers"]) if done else []
    return (need, answers[-1]) if answers else None


def entry_kind(message: str) -> str:
    """"need" when the words state something to procure, else "chat".

    For a screen whose box is for stating a need (the Need Assessment entry):
    a greeting, thanks, a question or a lookup goes to the NAM harness's chat, never to
    a KG search for the word "Hii". A stated need ("i need X"), or words that
    name an item, start Need capture."""
    from nam_agent.tools import intent

    if stated_need(message) is not None:
        return "need"
    if intent.is_greeting(message) or _LOOKUP.search(message) \
            or _norm(message) in _UNDO | _STOP | _SKIP:
        return "chat"
    return "need" if nam_capture.head_term(message) else "chat"


def recent_open_case(conn: sqlite3.Connection, session_id: str) -> nam.NeedRecord | None:
    """The case another chat left with its question open, within the chat's
    idle window (NAM_SESSION_IDLE_MINUTES, the window a chat itself is kept
    open for), or None."""
    import os

    minutes = int(os.getenv("NAM_SESSION_IDLE_MINUTES", "60") or 60)
    if minutes <= 0:
        return None
    try:
        rows = conn.execute(
            "SELECT case_id FROM nam_cases WHERE COALESCE(session_id, '') != ? "
            "AND updated_at >= strftime('%Y-%m-%d %H:%M:%f', 'now', ?) "
            "ORDER BY updated_at DESC LIMIT 3", (session_id or "", f"-{minutes} minutes")).fetchall()
    except sqlite3.Error:
        return None
    for (case_id,) in rows:
        need = nam.load(conn, case_id)
        if need is not None and need.stage == "capture" and (need.followups or {}).get("pending"):
            return need
    return None


def session_case(conn: sqlite3.Connection, session_id: str) -> str | None:
    """The case this chat is working on (chatstate "case": the one it opened
    or resumed last), else the one it touched last, whatever its stage, or
    None."""
    if not session_id:
        return None
    chosen = chatstate.get(conn, session_id, "case")
    if chosen and nam.load(conn, str(chosen)) is not None:
        return str(chosen)
    try:
        row = conn.execute("SELECT case_id FROM nam_cases WHERE session_id = ? "
                           "ORDER BY updated_at DESC, case_id DESC LIMIT 1",
                           (session_id,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _turn_reply(reply: str, ask: dict | None, tool: str, args: dict) -> dict:
    calls = [{"tool": tool, "args": args, "output": reply.split("\n")[0][:200]}]
    if ask:
        calls.append({"tool": "ask_options", "args": ask,
                      "output": f"Asked the user: \"{ask['question']}\". Waiting for the answer."})
    return {"reply": reply, "ask": ask, "tool_calls": calls}
