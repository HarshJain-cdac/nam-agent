"""case.edit (FRS F12, F42): the edit screen for the case's own fields, with
the reason for every change, at any stage before the freeze.

  answer {question_id: "edit", value: "all: location=Plant 2, why=the unit moved"}

The fields are the Need Record's: location, term, quantity (as stated) and
the sub-type. Every change needs why=... (F42): it is the reason written to
the case history, with the old and new value, and the audit row of the
call. A field another flow owns is changed there: the category and types in
need.classify, the lines in quantity.finalise, the rates in the estimate,
the clauses in docs.generate. Without an answer the flow shows the fields
and the history of every change made to the case, by whom and why.
"""

from __future__ import annotations

from nam_agent.nam import need as need_store
from nam_agent.nam import orchestrator, store
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome, lines

EDIT = "edit"
FIELDS = ("location", "term", "quantity", "subtype")
ELSEWHERE = {"category": "need.classify", "procurement_type": "need.classify",
             "tender_type": "need.classify", "lines": "quantity.finalise",
             "qty": "quantity.finalise", "rate": "the estimate flow",
             "clauses": "docs.generate", "precedent": "precedent.select"}


def _current(need) -> dict:
    return {"location": need.location, "term": need.term, "quantity": need.quantity,
            "subtype": (need.subtype or {}).get("value")}


def handle(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    answer = request.get("answer") or {}
    if answer and answer.get("question_id") != EDIT:
        raise NamError("bad_request", f"case.edit asks only '{EDIT}'")
    warnings: list[str] = []
    changes = []
    given = str(answer.get("value") or "").strip()
    if given:
        try:
            edits = lines.parse(given, {*FIELDS, *ELSEWHERE, "why"})
            if any(e.target != "all" for e in edits):
                raise lines.LineError("case fields are set with all: name=value, why=...")
            fields = {k: v.strip() for e in edits for k, v in e.fields.items()}
            why = fields.pop("why", "")
            owned = [k for k in fields if k in ELSEWHERE]
            if owned:
                raise lines.LineError(f"{owned[0]} is changed in {ELSEWHERE[owned[0]]}")
            if len(why) < 3:
                raise lines.LineError("every change needs its reason: why=... (F42)")
            before = _current(need)
            patch = {k: v[:300] for k, v in fields.items()}
            error = need_store.apply_update(need, patch)
            if error:
                raise lines.LineError(error)
            after = _current(need)
            changes = [{"field": k, "from": before[k], "to": after[k]} for k in patch
                       if before[k] != after[k]]
            if changes:
                said = "; ".join(f"{c['field']} {c['from']!r} -> {c['to']!r}" for c in changes)
                need_store.save(ctx.conn, need, reason=f"case.edit: {said} because {why}"[:500])
                wf = orchestrator.workflow(need)
                if orchestrator._artifact(wf, "documents"):
                    warnings.append("The documents keep the fields they were generated with: "
                                    "run docs.generate again to carry the change.")
        except lines.LineError as exc:
            warnings.append(str(exc))
    log = [{"case_version": v["case_version"], "at": v["at"], "user": v["by"]["user"],
            "flow": v["flow"], "reason": v["reason"][:500]}
           for v in store.versions(ctx.conn, case_id)][-100:]
    return Outcome(
        case_id=case_id, status="ok",
        result={"fields": _current(need), "changed": changes, "history": log},
        questions=({"id": EDIT, "source": "harness", "options": [],
                    "text": "Change a field with 'all: location=..., why=...'.",
                    "why": "Every change is kept with its reason (F42)."},) if not given else (),
        warnings=tuple(warnings))
