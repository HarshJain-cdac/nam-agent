"""need.guided_form (FRS F8, R1, R28): when no past tender fits, the officer
describes the need through the guided form for its category: Goods,
Services, New Works or Repetitive Works (Diagram B).

  1. The case: the one given, or a new one opened from the last user message
     ("Fill the form from scratch: AMC of the cooling towers").
  2. The form: need.classify's route decides it. Until the category is
     confirmed the first question is the category, and for Works until it is
     known to be New or Repetitive, that.
  3. The fields are policy/guided_forms.json, asked one at a time in the
     form's order by nam_agent/tools/flow_engine.py: a field is mandatory, or
     mandatory only after another answer (a material code once the item is
     Codified), or NA once another answer rules it out. Only the next field
     still needing an answer is asked.
  4. An answer is checked before it is stored: an option from the list, a
     positive number, a YYYY-MM-DD date. A wrong one is asked again with the
     reason and stores nothing. value '' skips an optional field (NA); a
     mandatory field skipped is asked again.
  5. The answers are the `guided_form` artifact, saved after every answer so
     the form resumes across calls. Once every field is settled it is
     complete: S1 counts the need as captured and S2 takes it in place of a
     precedent. The sub-type, location, quantity and contract period also go
     onto the Need Record, as the officer's.

A change of category starts the new category's form, keeping the answers to
the fields both forms share.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path

from nam_agent.nam import need as need_store
from nam_agent.nam import orchestrator
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome, need_classify
from nam_agent.tools import flow_engine

POLICY = Path(__file__).resolve().parents[1] / "policy" / "guided_forms.json"
WORKS_KIND = "works_kind"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@cache
def policy() -> dict:
    return json.loads(POLICY.read_text(encoding="utf-8"))


def questions(form: str) -> list[dict]:
    """The form's fields, in order, in flow_engine's question shape."""
    fields = policy()["fields"]
    return [{"key": key, **fields[key]} for key in policy()["forms"][form]]


def _last_user(request: dict) -> str:
    for msg in reversed(request.get("messages") or []):
        if msg.get("role") == "user":
            return (msg.get("content") or "").strip()
    return ""


def _typed(q: dict, value: str) -> tuple[str, str | None]:
    """(the value as stored, an error) for a typed field."""
    if value == "NA" or q["type"] not in ("number", "date"):
        return value, None
    if q["type"] == "number":
        try:
            number = Decimal(value.replace(",", ""))
        except InvalidOperation:
            number = None
        if number is None or not number.is_finite() or number <= 0:
            return value, f"'{value}' is not a positive number for '{q['label']}'."
        return format(number.normalize(), "f"), None
    try:
        day = date.fromisoformat(value) if _DATE.match(value) else None
    except ValueError:
        day = None
    if day is None:
        return value, f"'{value}' is not a date as YYYY-MM-DD for '{q['label']}'."
    return day.isoformat(), None


def _ask(q: dict, form: str, answers: dict) -> dict:
    required = flow_engine.is_required(q, answers)
    why = (f"Required on the {form} form." if required
           else f"Optional on the {form} form: send '' to skip it.")
    return {"id": q["key"], "text": q["label"], "why": why, "source": "harness",
            "options": [{"label": o, "value": o, "count": 0} for o in q.get("options") or []]}


def _carried(artifact: dict | None, form: str) -> dict:
    """The answers so far: this form's, or the shared fields of another."""
    if not artifact:
        return {}
    keys = set(policy()["forms"][form])
    return {k: v for k, v in (artifact.get("answers") or {}).items()
            if artifact.get("form") == form or k in keys}


def _onto_need(need, q: dict, value: str, answers: dict) -> None:
    if value == "NA":
        return
    if q.get("subtype"):
        error = need_store.apply_update(need, {"subtype": value})
        if error:
            raise NamError("internal", f"the form's {q['key']} options are not sub-types: {error}")
    target = q.get("need")
    if target == "quantity":
        need.quantity = f"{value} {answers.get('uom') or ''}".strip()
    elif target == "term":
        need.term = f"{value} months"
    elif target == "location":
        need.location = value
    if q["key"] == "uom" and answers.get("quantity") not in (None, "NA"):
        need.quantity = f"{answers['quantity']} {value}"
    if q["key"] == "description" and value.lower() not in need.raw_text.lower():
        need.raw_text = f"{need.raw_text}; {value}" if need.raw_text else value


def _pre_question(need, form: str | None) -> dict | None:
    """The question that picks the form, while it is not picked."""
    if not need_classify._settled(need, "category"):
        return need_classify._question(need, "category")
    if form is None:                                  # Works, New or Repetitive not known
        return {"id": WORKS_KIND, "text": "Is this new work or repetitive work?",
                "why": "New and Repetitive Works have different forms (F8, R28).",
                "source": "harness",
                "options": [{"label": v, "value": v, "count": 0} for v in ("New", "Repetitive")]}
    return None


def handle(request: dict, ctx) -> Outcome:
    case_id = request.get("case_id") or ""
    if case_id:
        need = need_store.load(ctx.conn, case_id)
        if need is None:
            raise NamError("not_found", f"no case {case_id}")
    else:
        text = _last_user(request)
        if not text:
            raise NamError("bad_request", "the last user message is empty: say what is needed")
        need = need_store.create(ctx.conn, text)
        case_id = need.need_id

    answer = request.get("answer") or {}
    asked, value = answer.get("question_id") or "", str(answer.get("value") or "").strip()
    warnings: list[str] = []
    form = need_classify.route(need)["guided_form"]
    artifact = orchestrator._artifact(orchestrator.workflow(need), "guided_form")
    answers = _carried(artifact, form) if form else {}
    before = dict(answers)
    retry = ""

    if asked in ("category", WORKS_KIND):
        patch = {"category": value} if asked == "category" else {"subtype": value}
        error = (None if value in ("New", "Repetitive") else f"'{value}' is not New or "
                 "Repetitive") if asked == WORKS_KIND else None
        error = error or need_store.apply_update(need, patch)
        if error:
            warnings.append(error)
            retry = asked
        else:
            need_store.save(ctx.conn, need, reason=f"guided form: {asked} set to {value}")
            form = need_classify.route(need)["guided_form"]
            answers = _carried(artifact, form) if form else {}
            before = None                                    # the form itself changed
    elif asked:
        fields = {q["key"]: q for q in questions(form)} if form else {}
        if asked not in fields:
            raise NamError("bad_request", f"'{asked}' is not a field of the "
                           f"{form or 'unpicked'} form")
        q = fields[asked]
        stored = flow_engine.normalize(value)
        error = flow_engine.validate_answer(q, stored, answers)
        if not error:
            stored, error = _typed(q, stored)
        if error:
            warnings.append(error)
            retry = asked
        else:
            answers[asked] = stored
            _onto_need(need, q, stored, answers)
            need_store.save(ctx.conn, need, reason=f"guided form: {asked}")

    pre = _pre_question(need, form)
    if pre is not None:
        return Outcome(case_id=case_id, status="needs_input",
                       result={"form": None, "answers": {}, "missing": [], "complete": False},
                       questions=(pre,), warnings=tuple(warnings))

    form_questions = questions(form)
    # a refused answer is asked again; else the next field (filling ruled-out NAs)
    nxt = next((q for q in form_questions if q["key"] == retry), None) \
        or flow_engine.next_question(form_questions, answers)
    missing = [q["key"] for q in form_questions
               if flow_engine.is_required(q, answers) and answers.get(q["key"]) in (None, "NA")
               and not flow_engine.is_na_forced(q, answers)]
    complete = nxt is None and not missing
    value_out = {"form": form, "answers": answers, "missing": missing, "complete": complete}
    changed = answers != before or (artifact or {}).get("complete") != complete \
        or (artifact or {}).get("form") != form

    if complete:
        follow = ("need.classify",) if need.unresolved else ("quantity.finalise",)
    else:
        follow = ()
    return Outcome(
        case_id=case_id,
        status="ok" if complete else "needs_input",
        result=value_out,
        questions=(_ask(nxt, form, answers),) if nxt else (),
        warnings=tuple(warnings),
        next=follow,
        artifacts={"guided_form": value_out} if changed else {},
    )
