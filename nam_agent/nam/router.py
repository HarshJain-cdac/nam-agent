"""Which NAM flow a call runs, decided in code from the short system prompt
and the case's stage (plan section 3, "Routing").

In this order, the first rule that decides wins:

  answer     the caller answered the router's own "which step?" question
  explicit   the prompt names a flow id ("run estimate.goods")
  resume     the caller answered the open question of the flow that asked it
  boundary   the prompt asks for work after the NIT handoff (R33): refused
  intent     the phrasings in playbooks/catalog.py, scored; a tie is broken
             by the case's stage, then by its category (Goods -> estimate.goods,
             Services -> estimate.wage, Works -> estimate.works)
  continue   "continue", "next step": the next flow for the case's stage
  words      no intent in the prompt ("flowless" work, intent.py): the last
             user message read for what it is. A question, a greeting, "show
             me ..." with no case, or "approve it" is answered without a step
             (case.ask); "go ahead" continues the to-do list or the stage's
             next step; a change in words ("make it 35 nos") goes to the step
             of the case's stage that takes it; a tender named with "use" or
             "instead" is a precedent selection
  message    the same scoring as the prompt's, on the last user message
  need       no case, and the message states something to procure: need
             capture opens the case
  refused    no case, and nothing in the call is about procurement
  model      still ambiguous: one model call picks from a closed list of flow
             ids, and an answer outside that list is ignored
  otherwise  needs_input: a question offering the candidate flows

Everything but the model step is deterministic, so the routing eval can pin
every phrasing to its flow.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field

from nam_agent.nam import guard, intent, llm, playbooks, store
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Flow

log = logging.getLogger("nam_agent.nam.router")

FLOW_QUESTION = "flow"          # the id of the router's own question

# NAM ends at the handoff to NIT (R33): creating the NIT, publishing the
# tender, bids, evaluation and award belong to the modules after it.
_BOUNDARY = (
    (r"\b(create|draft|prepare|generate|publish|issue|float|write|make)\b.*"
     r"\bnit\b(?!\s*(module|team|stage|department))"),
    r"\b(evaluate|open|compare|rank)\b.*\bbids?\b|\bbid (evaluation|opening)\b",
    r"\baward\b.*\b(contract|tender|order|l1|vendor|bidder)s?\b",
    r"\bpublish\b.*\btender\b",
    r"\bnegotiat|\breverse auction\b",
)
_OFF_TOPIC = (r"\b(poem|joke|song|story|weather|recipe|cricket|football|movie|horoscope|"
              r"stock price|news|translate|essay)\b")
_NAM_WORDS = (r"\b(procure\w*|purchas\w*|tenders?|indents?|needs?|requirements?|requisition|pr|"
              r"po|estimat\w*|boq|sor|dsr|scc|pqc|specs?|specifications?|vendors?|suppliers?|"
              r"materials?|items?|quantit\w*|qty|goods|works|services?|manpower|contracts?|nam|"
              r"nit|aarohan|sap|cases?|request|approvals?|package|documents?|clauses?|rates?)\b")
_CONTINUE = r"\b(continue|proceed|next step|go on|resume|carry on|move on)\b"

# the flow "continue" means at each stage (Step 9's orchestrator refines it)
STAGE_NEXT = {"S1": "need.capture", "S2": "precedent.search", "S3": "quantity.finalise",
              "S4": "estimate", "S5": "docs.generate", "S6": "validate", "S7": "pr.build",
              "S8": "approval", "S9": "document.update", "S10": "handoff.nit"}

_REFS = {
    "case_id": r"\b(nam-\d{4}-\d{4,8})\b",
    "request": r"\brequest\s*(?:#|no\.?|number)?\s*(\d{1,12})\b",
    "tender": r"\b(pur-[a-z0-9]+-\d{4,}|[a-z]{2}\d{8}/[a-z]{2,6})\b",
    "pr": r"\bpr\s*(?:#|no\.?|number)?\s*(\d{8,12})\b",
    "po": r"\bpo\s*(?:#|no\.?|number)?\s*(\d{8,12})\b",
}


@dataclass(frozen=True)
class CaseState:
    stage: str = "S1"
    category: str = ""              # goods | services | works | ""
    last_flow: str = ""             # the flow that last wrote the case
    open_question: str = ""         # the id of the question it left open
    unclassified: bool = False      # category, procurement or tender type not confirmed
    form: str = ""                  # the guided form: "" none, "open" half-filled, "complete"
    plan: bool = False              # a to-do list (case.plan) with steps still to do


@dataclass(frozen=True)
class Route:
    flow: str | None                # None: no flow runs, see status
    by: str                         # which rule decided (the module docstring)
    why: str
    status: str = ""                # needs_input | refused, when flow is None
    candidates: tuple[str, ...] = ()
    refs: dict = field(default_factory=dict)


def refs(system: str) -> dict:
    """The identifiers in a system prompt: case, request, tender, PR, PO."""
    text = system.lower()
    out = {}
    for name, pattern in _REFS.items():
        found = re.search(pattern, text)
        if found:
            out[name] = found.group(1).upper() if name in ("case_id", "tender") else found.group(1)
    return out


def case_state(conn: sqlite3.Connection, case_id: str) -> CaseState:
    row = store.case_row(conn, case_id)
    if row is None:
        return CaseState()
    need = row["record"]
    category = (need.get("category") or {})
    pending = (need.get("followups") or {}).get("pending") or {}
    unclassified = any(not (need.get(f) or {}).get("value") or not (need.get(f) or {}).get(
        "confident") for f in ("category", "procurement_type", "tender_type"))
    return CaseState(stage=row["stage"], category=str(category.get("id") or ""),
                     last_flow=store.last_flow(conn, case_id),
                     open_question=str(pending.get("id") or ""), unclassified=unclassified,
                     form=_form(need), plan=_plan_open(need))


def _plan_open(need: dict) -> bool:
    todo = (((need.get("workflow") or {}).get("artifacts") or {}).get("todo") or {})
    items = (todo.get("value") or {}).get("items") or []
    return any(i.get("status") != "done" for i in items)


def _form(need: dict) -> str:
    form = (((need.get("workflow") or {}).get("artifacts") or {}).get("guided_form") or {})
    if not form.get("value"):
        return ""
    return "complete" if form["value"].get("complete") else "open"


def _last_user(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return msg.get("content") or ""
    return ""


def _scores(flows: dict[str, Flow], text: str) -> dict[str, int]:
    return {fid: s for fid, f in flows.items() if (s := playbooks.intent_score(f, text))}


def _by_category(flows: dict[str, Flow], ids, category: str) -> str | None:
    matching = [i for i in ids if flows[i].category and flows[i].category == category]
    return matching[0] if len(matching) == 1 and all(flows[i].category for i in ids) else None


def _decide(flows, scores: dict[str, int], case: CaseState | None, by: str,
            found: dict) -> Route | None:
    """The winner of a scoring, or None when nobody scored. A tie that the
    stage and the category cannot break is returned as candidates."""
    if not scores:
        return None
    best = max(scores.values())
    top = sorted(i for i, s in scores.items() if s == best)
    if len(top) == 1:
        return Route(top[0], by, f"the prompt matches {top[0]} (score {best})", refs=found)
    if case is not None:
        here = [i for i in top if flows[i].at(case.stage)]
        if len(here) == 1:
            return Route(here[0], "stage", f"{', '.join(top)} tie; the case is at {case.stage}",
                         refs=found)
        chosen = _by_category(flows, top, case.category)
        if chosen:
            return Route(chosen, "category", f"{', '.join(top)} tie; the case's category is "
                         f"{case.category}", refs=found)
    return Route(None, "none", f"the prompt could mean {', '.join(top)}", status="needs_input",
                 candidates=tuple(top), refs=found)


def route(request: dict, case: CaseState | None, ask_model=None) -> Route:
    flows = playbooks.registry()
    system = request["system"].lower()
    found = refs(request["system"])

    answer = request.get("answer") or {}
    if answer.get("question_id") == FLOW_QUESTION:
        if answer.get("value") not in flows:
            raise NamError("bad_request", f"the answer to '{FLOW_QUESTION}' must be one of the "
                           "offered flow ids")
        return Route(answer["value"], "answer", "the caller chose the flow", refs=found)

    named = sorted({m for m in re.findall(r"\b[a-z]+(?:\.[a-z_]+)+\b", system) if m in flows})
    if len(named) == 1:
        return Route(named[0], "explicit", f"the prompt names {named[0]}", refs=found)

    if case is not None and case.plan and re.search(_CONTINUE, system):
        return Route("case.plan", "resume", "continues the case's to-do list", refs=found)

    if (answer and case is not None and case.open_question
            and answer.get("question_id") == case.open_question and case.last_flow in flows):
        return Route(case.last_flow, "resume", f"answers {case.last_flow}'s open question "
                     f"'{case.open_question}'", refs=found)

    for pattern in _BOUNDARY:
        if re.search(pattern, system):
            return Route(None, "none", "NAM ends at the handoff to the NIT module (R33): "
                         "creating the NIT, publishing the tender, bids, evaluation and award "
                         "are done after it.", status="refused", refs=found)

    scores = _scores(flows, system)
    if re.search(_OFF_TOPIC, system) and max(scores.values(), default=0) < 3:
        return _refused(found)
    decided = _decide(flows, scores, case, "intent", found)

    if decided is None and case is not None and re.search(_CONTINUE, system):
        nxt = _next_for(case, flows)
        if nxt:
            return Route(nxt, "continue", f"the case is at {case.stage}; next is {nxt}",
                         refs=found)

    message = _last_user(request["messages"]).lower()
    if decided is None:
        decided = _words(message, case, flows, found)
    if decided is None and not re.search(_OFF_TOPIC, message):
        decided = _decide(flows, _scores(flows, message), case, "message", found)
        if case is None and decided is not None and decided.flow \
                and flows[decided.flow].needs_case:
            decided = None              # no case yet: only a step that opens one
    if decided is None and re.search(_OFF_TOPIC, message) \
            and max(_scores(flows, message).values(), default=0) < 3:
        return _refused(found)
    if decided is None and case is None and message.strip() and not re.search(_OFF_TOPIC,
                                                                                  message) \
            and (re.search(_NAM_WORDS, message) or re.search(_STATED, message)):
        decided = Route("need.capture", "need", "no case, and the message states a need",
                        refs=found)

    if decided is None:
        text = system + " " + _last_user(request["messages"]).lower()
        if case is None and not re.search(_NAM_WORDS, text):
            # nothing names a case or anything procurement: not a NAM call
            return _refused(found)
        stage = case.stage if case is not None else "S1"
        pool = tuple(i for i, f in flows.items()
                     if (f.at(stage) if case is not None else not f.needs_case))
        decided = Route(None, "none", "the prompt names no NAM step", status="needs_input",
                        candidates=pool, refs=found)

    if decided.flow is None and decided.status == "needs_input" and ask_model is not None:
        picked = _ask(ask_model, request["system"], case, decided.candidates, flows)
        if picked:
            who = getattr(ask_model, "last", "") or "model"
            return Route(picked, "model", f"the model ({who[:80]}) chose {picked} from "
                         f"{', '.join(decided.candidates)}", refs=found)
    return decided


_STATED = (r"\b\d+\s*(nos|no|numbers|units|pcs|pieces|sets|kg|kgs|m|mtr|metres|ltr|lot|"
           r"mandays|months)\b|\b(urgent|urgently|required|require|want|buy)\b")
_TENDER_WORD = r"\bpur-[a-z0-9-]+\b|\b[a-z]{2}\d{8}/[a-z]{2,6}\b"
_SELECT = r"\b(use|select|choose|pick|take|go with|prefer)\b|\binstead\b"


def _next_for(case: CaseState, flows) -> str:
    """The flow "continue" means at the case's stage."""
    nxt = STAGE_NEXT.get(case.stage, "")
    if case.stage in ("S1", "S2") and case.form == "open":
        nxt = "need.guided_form"          # the half-filled form, where it stopped
    elif case.stage == "S1" and case.unclassified and not case.open_question \
            and (case.form == "complete" or case.last_flow in (
                "need.capture", "need.replicate", "need.classify")):
        nxt = "need.classify"             # capture or the form is done; the classification is not
    if nxt == "estimate":
        nxt = _by_category(flows, [i for i in flows if i.startswith("estimate.")],
                           case.category) or ""
    return nxt if nxt in flows else ""


# the step of each stage that takes a change written in words (intent.answer_for)
_EDITS_AT = {"S3": "quantity.finalise", "S4": "estimate", "S7": "pr.build"}


def _words(message: str, case: CaseState | None, flows, found: dict) -> Route | None:
    """The user's words, when the prompt named no step (intent.py)."""
    if re.search(_OFF_TOPIC, message):
        return None                     # not NAM: refused below
    kind = intent.kind(message)
    if kind in ("question", "greeting", "approve") or (kind == "show" and case is None):
        return Route("case.ask", "words", f"the message is a {kind}: answered without a step",
                     refs=found)
    if kind == "proceed":
        if case is None:
            return Route("case.ask", "words", "nothing to go on with: there is no case",
                         refs=found)
        if case.plan:
            return Route("case.plan", "words", "continues the case's to-do list", refs=found)
        nxt = _next_for(case, flows)
        if nxt:
            return Route(nxt, "words", f"go ahead: the case is at {case.stage}; next is {nxt}",
                         refs=found)
    if case is not None and (found.get("tender") or re.search(_TENDER_WORD, message)) \
            and re.search(_SELECT, message):
        return Route("precedent.select", "words", "the message names a tender to use",
                     refs=found)
    if case is not None and intent.edit_like(message):
        target = _EDITS_AT.get(case.stage, "")
        if target == "estimate":
            target = _by_category(flows, [i for i in flows if i.startswith("estimate.")],
                                  case.category) or ""
        if target in flows:
            return Route(target, "words", f"a change in words, to the step of {case.stage} "
                         f"that takes it ({target})", refs=found)
    return None


def _refused(found: dict) -> Route:
    return Route(None, "none", "This is outside the Need Assessment Module: NAM assesses "
                 "procurement needs up to the handoff to NIT.", status="refused", refs=found)


_MODEL_SYSTEM = ("You route one request inside the Need Assessment Module (NAM) of a "
                 "procurement system. Reply with exactly one flow id from the list and nothing "
                 "else. If none fits, reply none. " + guard.RULE)


def _ask(ask_model, system: str, case: CaseState | None, candidates, flows) -> str | None:
    """One model call over a closed list. Anything but one of its ids is None."""
    if len(candidates) < 2:
        return None
    listing = "\n".join(f"- {i}: {flows[i].title}" for i in candidates)
    user = (f"Request:\n{guard.wrap(system[:500], 'caller', 'system')}\n"
            f"Case stage: {case.stage if case else 'no case yet'}\nFlows:\n{listing}")
    def pick(reply: str) -> str | None:
        words = (reply or "").strip().split()
        word = words[0].strip("`'\".,:;").lower() if words else ""
        if word == "none":
            return None
        if word not in candidates:
            raise llm.Rejected(f"'{word[:40]}' is not one of: {', '.join(candidates)}, none")
        return word

    try:
        return llm.checked(ask_model, _MODEL_SYSTEM, user, pick)
    except llm.Rejected as exc:
        log.warning("routing model answers were refused: %s", str(exc)[:200])
        return None
    except Exception as exc:  # noqa: BLE001 - no model answer: the caller is asked instead
        log.warning("routing model call failed: %s", str(exc)[:200])
        return None


def question(route_: Route) -> dict:
    """The needs_input question that offers the candidate flows."""
    flows = playbooks.registry()
    return {"id": FLOW_QUESTION, "source": "harness",
            "text": "Which step should NAM run?", "why": route_.why[:300],
            "options": [{"label": flows[i].title, "value": i} for i in route_.candidates[:10]]}
