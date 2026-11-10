"""need.classify (FRS F4, R1, R28): the officer confirms the case's category,
procurement type and tender type, one question at a time, and is told what
the category decides.

  1. An answer to one of the questions below is checked against its closed
     vocabulary (need.apply_update) and stored as the officer's: confident,
     basis "you set this". An answer outside the vocabulary is bad_request.
     value '' skips the question for now; the next one is asked.
  2. The next question is the first field not confidently settled, in the
     order category, procurement type, tender type. Its options are the
     whole vocabulary, the predicted value first and marked as a suggestion
     with the words that suggested it. A tender type is never settled from
     words alone (need.classify): the officer always answers it.
  3. Once the three are confirmed (the S1 gate, FRS F4), the sub-type is
     offered as one more question. It is not part of the gate, but it picks
     the guided form for Works (F8: New or Repetitive Works).
  4. The route (R1): the estimate flow the category takes, the documents
     its package must hold (policy/guardrails.json), the guided form.

Every change is saved with the field and the value in its reason, so the
history shows who set what (F42). The flow belongs to S1. Run on a case past
S1 it is rework, like any earlier stage's flow (orchestrator.admit): the
stages after S1 are settled again and their approvals given again. After the
freeze it is refused.
"""

from __future__ import annotations

from nam_agent.nam import guardrails
from nam_agent.nam import need as need_store
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome

FIELDS = ("category", "procurement_type", "tender_type")       # the S1 gate (F4)
SUBTYPE = "subtype"
QUESTIONS = (*FIELDS, SUBTYPE)

_ASK = {
    "category": ("Which category is this procurement?",
                 "The category decides the estimate, the documents and the checks (R1)."),
    "procurement_type": ("Which type of procurement is it?",
                         ("The type of procurement is part of the classification NAM must "
                          "confirm before the case moves on (F4).")),
    "tender_type": ("Which tender type applies?",
                    ("A tender type follows value thresholds and OEM or proprietary status: "
                     "the officer decides it, never NAM (F4).")),
    SUBTYPE: ("Which sub-type is it?",
              "The sub-type picks the guided form and the rules for the category (R28)."),
}
_ESTIMATE = {"goods": "estimate.goods", "services": "estimate.wage",
             "works": "estimate.works"}


def _vocabulary(need, name: str) -> tuple[str, ...]:
    if name == "category":
        return need_store.CATEGORIES
    if name == "procurement_type":
        return need_store.PROCUREMENT_TYPES
    if name == "tender_type":
        return need_store.TENDER_TYPES
    return need_store.SUBTYPES.get((need.category or {}).get("value"), ())


def _settled(need, name: str) -> bool:
    pred = getattr(need, name) or {}
    return bool(pred.get("value")) and bool(pred.get("confident"))


def _question(need, name: str) -> dict:
    text, why = _ASK[name]
    pred = getattr(need, name) or {}
    guess = pred.get("value") if not pred.get("confident") else None
    vocabulary = _vocabulary(need, name)
    order = [guess, *(v for v in vocabulary if v != guess)] if guess in vocabulary \
        else list(vocabulary)
    options = [{"label": (f"{v} (suggested: \"{pred.get('basis') or ''}\")"[:200]
                          if v == guess else v), "value": v, "count": 0} for v in order]
    if guess in vocabulary:
        why = f"{why} Suggested from the words \"{pred.get('basis') or ''}\"; confirm or change it."
    return {"id": name, "text": text, "why": why[:500], "source": "harness",
            "options": options}


def _next_open(need, after: str = "") -> str | None:
    """The first field not settled, starting after a skipped one."""
    order = list(FIELDS)
    if after in order:
        i = order.index(after) + 1
        order = order[i:] + order[:i - 1]
    for name in order:
        if not _settled(need, name):
            return name
    if after != SUBTYPE and _vocabulary(need, SUBTYPE) and not _settled(need, SUBTYPE):
        return SUBTYPE
    return None


def _guided_form(category: str | None, subtype: str | None) -> str | None:
    if category == "goods":
        return "Goods"
    if category == "services":
        return "Services"
    if category == "works" and subtype in ("New", "Repetitive"):
        return f"{subtype} Works"
    return None


def route(need) -> dict:
    """What the category decides (R1). Empty values until it is confirmed."""
    cat = need_store.category_id((need.category or {}).get("value")) \
        if _settled(need, "category") else None
    sub = (need.subtype or {}).get("value") if _settled(need, SUBTYPE) else None
    return {"category": cat,
            "estimate_flow": _ESTIMATE.get(cat or ""),
            "mandatory_documents": list(guardrails.policy()["mandatory_documents"].get(cat, []))
            if cat else [],
            "guided_form": _guided_form(cat, sub)}


def handle(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")

    past_s1 = (need.workflow or {}).get("stage", "S1") != "S1"
    answer = request.get("answer") or {}
    asked = answer.get("question_id") if answer.get("question_id") in QUESTIONS else ""
    changed = ""
    if asked and answer.get("value"):
        value = answer["value"]
        if asked == "category":                        # an id or the label
            value = need_store.category_label(value) or value
        if not (_settled(need, asked) and getattr(need, asked)["value"] == value):
            error = need_store.apply_update(need, {asked: value})
            if error:
                raise NamError("bad_request", error)
            need_store.save(ctx.conn, need, reason=f"classification: {asked} set to {value}")
            changed = asked
    elif answer and not asked:
        raise NamError("bad_request", f"need.classify asks only {', '.join(QUESTIONS)}; "
                       f"'{answer.get('question_id')}' is not one of them")

    skipped = asked if asked and not answer.get("value") else ""
    confirmed = all(_settled(need, f) for f in FIELDS)
    # a skipped field that is the only one open is asked again
    nxt = _next_open(need, after=skipped) or (skipped if not confirmed else None)
    warnings = []
    if skipped and skipped in FIELDS:
        warnings.append(f"{skipped.replace('_', ' ')} is still open: the case cannot leave S1 "
                        "until the category, procurement type and tender type are confirmed.")
    if changed and past_s1:
        warnings.append(f"The {changed.replace('_', ' ')} changed after S1: the estimate, "
                        "documents and approvals that depend on it are rework.")
    if nxt == SUBTYPE:
        warnings.append("The sub-type is optional for the S1 gate; it picks the guided form "
                        "and the category's rules (R28).")

    if not confirmed:
        follow = ()
    elif need.selected_cases:
        follow = ("quantity.finalise",)
    elif need.stage == "references":
        follow = ("precedent.search",)
    else:
        follow = ("need.capture",)
    return Outcome(
        case_id=case_id,
        status="ok" if confirmed else "needs_input",
        result={
            "classification": {name: getattr(need, name) for name in QUESTIONS},
            "confirmed": confirmed,
            "open": [f for f in FIELDS if not _settled(need, f)],
            "changed": changed or None,
            "route": route(need),
        },
        questions=(_question(need, nxt),) if nxt else (),
        warnings=tuple(warnings),
        next=follow,
    )
