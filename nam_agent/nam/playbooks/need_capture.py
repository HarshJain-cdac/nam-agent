"""need.capture (FRS F1, F3, F4, F5, F49): the stated need in, every related
past tender in the KG out, with the follow-up questions that narrow them.

The work itself is nam_agent/nam/capture.py. This file only maps a nam.v1 request
onto it and its output onto the response:

  new case     the last user message is the need ("We need 20 gate valves")
  case_id      the last user message refines the need, unless the need
               already holds those words (a backend that resends the whole
               conversation must not append the same sentence twice)
  answer       answers the open question; the messages are not read as new
               need text, so an answer is never counted twice
  skip         options.skip_questions ends the follow-up questions
  undo         options.undo (a question id) takes that answer, and every
               answer after it, back, and asks the same question again
  category     options.category, the officer's pick before typing (PR flow 1(a))

Status: needs_input while a question is open or the KG found nothing to build
from, ok once NAM can move on: to need.classify while the category,
procurement type or tender type is not confirmed (the S1 gate, F4), else to
the references step (precedent.search).
"""

from __future__ import annotations

from nam_agent.nam import capture, llm
from nam_agent.nam import need as need_store
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome

# The free-text question asked when the need names no item, or when nothing
# in the KG can be built from yet. Its answer is added to the need.
DETAIL = "detail"


def _last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return (msg.get("content") or "").strip()
    return ""


def _already_in(raw_text: str, text: str) -> bool:
    parts = {p.strip().lower() for p in (raw_text or "").split(";")}
    return text.strip().lower() in parts


def _question(pending: dict) -> dict:
    return {
        "id": str(pending["id"]),
        "text": pending["text"],
        "why": pending.get("why") or "",
        "source": pending.get("source") if pending.get("source") in ("model", "tenders")
        else "harness",
        "options": [{"label": str(o.get("label") or o.get("value")),
                     "value": str(o.get("value") or ""),
                     "count": int(o.get("count") or 0)}
                    for o in pending.get("options") or []],
        # False for the category and the details: they do not narrow the tenders
        "narrows": bool(pending.get("narrows", True)),
    }


def handle(request: dict, ctx) -> Outcome:
    case_id = request.get("case_id") or ""
    answer = request.get("answer")
    options = request.get("options") or {}
    skip = bool(options.get("skip_questions"))
    undo = str(options.get("undo") or "")           # take an answer back, ask it again
    category = str(options.get("category") or "")   # the officer's pick before typing
    text = _last_user_text(request["messages"])

    if case_id:
        need = need_store.load(ctx.conn, case_id)
        if need is None:
            raise NamError("not_found", f"no case {case_id}")
        if answer and answer["question_id"] == DETAIL:
            text, answer = answer["value"], None     # the free-text answer is more need
        elif answer or skip or undo or _already_in(need.raw_text, text):
            text = ""
    elif not text:
        raise NamError("bad_request", "the last user message is empty: state the need")

    out = capture.capture(ctx.conn, ctx.home, ctx.kg_url, text, need_id=case_id,
                          fetch=ctx.fetch, answer=answer, skip_followups=skip,
                          ask_model=llm.asker(ctx, "followup"), undo=undo, category=category)
    if not out.get("need_id"):
        raise NamError("bad_request", out.get("error") or "need capture failed")
    case_id = out["need_id"]

    error = out.get("error") or ""
    if error and "names an item" not in error:
        # unreachable or not configured: not the same as "nothing found"
        raise NamError("kg_unavailable", "The knowledge graph could not be searched. "
                       "The case is saved; retry the same call.", case_id=case_id)

    need = need_store.load(ctx.conn, case_id)
    followups = out["followups"]
    ready = out["readiness"]
    warnings = [error] if error else []     # the need names no item
    if followups.get("ignored"):
        warnings.append(followups["ignored"])
    semantic = out.get("semantic") or {}
    if out.get("semantic") is not None and semantic.get("pending"):
        warnings.append("The meaning search is still running: these are the literal matches; "
                        "its tenders join on the next step.")
    elif out.get("semantic") is not None and not semantic.get("on"):
        warnings.append("Semantic search was unavailable: related tenders are literal "
                        "matches only.")

    questions = ()
    if followups.get("pending"):
        questions = (_question(followups["pending"]),)
    elif not out["next_step"]:
        questions = ({"id": DETAIL, "source": "harness", "options": [],
                      "text": ready.get("ask") or "Describe the need in more detail.",
                      "why": ready.get("reason") or ""},)

    result = {
        "requirement": need.raw_text,
        "item": out.get("item") or "",
        "classification": {
            "category": need.category, "subtype": need.subtype,
            "procurement_type": need.procurement_type, "tender_type": need.tender_type,
            "location": need.location, "term": need.term, "quantity": need.quantity,
        },
        "ask_about": out["ask_about"],
        "readiness": ready,
        "related": out["related"],
        "incidental": out["incidental"],
        "semantic": out.get("semantic"),
        "tenders_in_graph": out.get("tenders_in_graph"),
        "followups": {k: v for k, v in followups.items() if k not in ("pending", "ignored")},
        "dossier": out["dossier"],
    }
    return Outcome(
        case_id=case_id,
        status="ok" if out["next_step"] else "needs_input",
        result=result,
        questions=questions,
        citations=tuple({"source": "kg", "ref": f"tender:{r['case_key']}",
                         "authority": "precedent"} for r in out["related"]),
        warnings=tuple(warnings),
        next=(("need.classify",) if need.unresolved else ("precedent.search",))
        if out["next_step"] else (),
    )
