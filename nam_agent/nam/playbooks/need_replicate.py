"""need.replicate (FRS F2): create by copy. The officer names a past tender;
a new case opens as an exact copy of it, with that tender as the selected
precedent.

  1. The reference: the answer to the `reference` question, else a PUR case
     id or tender number in the last user message or the system prompt.
  2. resolve_reference. Nothing found, or several tenders sharing a base
     number: needs_input, and no case is opened. The officer picks; the
     harness never does (R2). To answer, send the same system prompt with
     answer {question_id: "reference", value: <case key>}.
  3. tender_record and documents: the tender's fields, every BOQ line with
     the rate it recorded, its clauses verbatim, its files. All of it is read
     before the case opens, so a KG failure leaves no half-made case.
  4. The case opens. Its category is the tender's recorded work category when
     that is one of NAM's, else classified from the subject like a typed need.
     The tender is the selected precedent: the officer named it.
  5. The copy is the `replica` artifact: each field with its source, the BOQ
     with its rates marked historical (the estimate proposes the latest, R7),
     the clauses, SOR references, awardees and files. Nothing is merged in
     from a second tender.

A PR or PO number needs the SAP connector, which is not there yet: it is
asked about, never looked up somewhere else.
"""

from __future__ import annotations

import re

from nam_agent.nam import need as need_store
from nam_agent.nam import router, tools
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome

REFERENCE = "reference"
_SAP_ONLY = ("pr", "po")
# a reference typed on its own: PUR-..., or a tender number like OC01000435/PRV
_BARE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_./-]{2,119})\s*$")
# one inside a sentence: a PUR case id, or letters then digits (a tender number)
_IN_TEXT = re.compile(r"(?<![\w/-])((?:[A-Za-z]{1,4}-)?PUR-[A-Za-z0-9-]*\d[A-Za-z0-9-]*"
                      r"|[A-Za-z]{1,4}-?\d{6,}(?:/[A-Za-z0-9]+)?)", re.IGNORECASE)
_WORK_CATEGORY = {"goods": need_store.GOODS, "works": need_store.WORKS,
                  "work": need_store.WORKS, "services": need_store.SERVICES,
                  "service": need_store.SERVICES, "manpower": need_store.SERVICES,
                  "manpower / services": need_store.SERVICES}


def _last_user(request: dict) -> str:
    for msg in reversed(request.get("messages") or []):
        if msg.get("role") == "user":
            return (msg.get("content") or "").strip()
    return ""


def _reference(request: dict) -> tuple[str, list[str]]:
    """(the tender reference, the SAP references seen instead)."""
    answer = request.get("answer") or {}
    if answer.get("question_id") == REFERENCE:
        return str(answer.get("value") or "").strip(), []
    text = _last_user(request)
    sap = []
    for source in (text, request["system"]):
        found = router.refs(source)
        if found.get("tender"):
            return found["tender"], []
        sap += [f"{k.upper()} {found[k]}" for k in _SAP_ONLY if found.get(k)]
        named = _IN_TEXT.search(source)
        if named and not sap:
            return named.group(1), []
    bare = _BARE.match(text)
    if bare and not sap and any(c.isdigit() for c in bare.group(1)):
        return bare.group(1), []
    return "", sap


def _ask(text: str, why: str, *, options=(), result=None, warnings=()) -> Outcome:
    question = {"id": REFERENCE, "text": text, "why": why, "source": "harness",
                "options": list(options)}
    return Outcome(case_id=None, status="needs_input",
                   result=result or {"reference": None, "candidates": [], "copied": None},
                   questions=(question,), warnings=tuple(warnings))


def _category(record: dict, subject: str) -> dict:
    recorded = (record["fields"].get("work_category") or "").strip()
    label = _WORK_CATEGORY.get(recorded.lower())
    if label:
        return need_store.Prediction(label, f"the tender's work category: {recorded}",
                                     True).as_dict()
    return need_store.classify(subject)["category"]


def handle(request: dict, ctx) -> Outcome:
    if request.get("case_id"):
        raise NamError("bad_request", "need.replicate opens a new case: send no case_id")
    ref, sap = _reference(request)
    if not ref:
        warnings = [(f"{', '.join(sap)}: copying a PR or PO needs the SAP connector, which is "
                    "not connected; give the tender it was raised for")] if sap else []
        return _ask("Which past tender should the new case copy? Give its PUR case id or "
                    "tender number.", "Create by copy needs the exact record to copy.",
                    warnings=warnings)

    found = tools.call(ctx, "resolve_reference", {"ref": ref})
    matches = found["matches"]
    candidates = [{"case_key": m["cite"]["case_key"], "tender_no": m["cite"]["tender_no"],
                   "job_name": m["cite"]["job_name"], "match": m["match"]} for m in matches]
    reference = {"ref": ref, "tier": found["tier"],
                 "case_key": candidates[0]["case_key"] if len(candidates) == 1 else None}
    if not candidates:
        return _ask(f"No past tender in the knowledge graph matches {ref}. Check the number, "
                    "or give the PUR case id.", "Nothing is copied from a guess.",
                    result={"reference": reference, "candidates": [], "copied": None},
                    warnings=found["warnings"])
    if found["ambiguous"]:
        options = [{"label": f"{c['tender_no'] or c['case_key']}: {c['job_name'] or ''}"[:200],
                    "value": c["case_key"], "count": 0} for c in candidates]
        return _ask(f"{ref} matches {len(candidates)} past tenders. Which one should be "
                    "copied?", "Tenders can share a base number across years; the officer "
                    "picks, never the harness.", options=options,
                    result={"reference": reference, "candidates": candidates, "copied": None},
                    warnings=found["warnings"])

    key = reference["case_key"]
    record = tools.call(ctx, "tender_record", {"case_key": key})
    if not record["found"]:
        return _ask(f"{ref} resolved to {key}, but the knowledge graph holds no record for "
                    "it. Give another tender.", "Nothing is copied from an empty record.",
                    result={"reference": reference, "candidates": candidates, "copied": None})
    files = tools.call(ctx, "documents", {"case_key": key})

    subject = record["fields"].get("job_name") or key
    need = need_store.create(ctx.conn, subject)
    need.category = _category(record, subject)
    if need.subtype.get("value") not in need_store.SUBTYPES.get(need.category["value"], ()):
        need.subtype = need_store.Prediction().as_dict()
    need.selected_cases = [key]
    need.stage = "selected"
    need_store.save(ctx.conn, need, reason=f"replicated from tender {key} (reference {ref})")

    endpoint = record["source"]["endpoint"]
    fields = {name: {"value": value, "source": f"kg {endpoint} {key}"}
              for name, value in record["fields"].items()}
    replica = {
        "summary": (f"copy of {key} ({record['fields'].get('tender_no') or 'no tender number'}): "
                    f"{len(record['boq'])} BOQ lines, {len(record['clauses']['pqc'])} PQC, "
                    f"{len(record['clauses']['scc'])} SCC, "
                    f"{len(record['clauses']['tech_spec'])} specification clauses")[:300],
        "precedent": key,
        "reference": {"ref": ref, "tier": found["tier"]},
        "source": record["source"],
        "fields": fields,
        "boq": [{**line, "historical": True} for line in record["boq"]],
        "clauses": record["clauses"],
        "sor_references": record["sor_references"],
        "awarded_to": record["awarded_to"],
        "documents": files["documents"],
        "best_nit": files["best_nit"],
        "complete": record["complete"],
    }
    counts = {"boq": len(record["boq"]), **{k: len(v) for k, v in record["clauses"].items()},
              "sor_references": len(record["sor_references"]),
              "documents": len(files["documents"])}
    warnings = [*record["warnings"], *files["warnings"]]
    if any(line.get("unit_price") or line.get("estimated_rate") for line in record["boq"]):
        warnings.append(f"The BOQ rates are {key}'s, as recorded: historical. The estimate "
                        "step proposes the latest rate for each line (R7).")
    if not need.category["value"]:
        warnings.append("The category could not be read from the tender; set it with "
                        "case.edit before the case can move on.")
    return Outcome(
        case_id=need.need_id, status="ok" if need.category["value"] else "needs_input",
        result={"reference": reference, "candidates": candidates, "copied": {
            "case_key": key, "tender_no": record["fields"].get("tender_no"),
            "subject": subject, "counts": counts, "complete": record["complete"],
            "classification": {"category": need.category, "subtype": need.subtype}}},
        citations=({"source": "kg", "ref": f"tender:{key}", "authority": "precedent"},),
        warnings=tuple(warnings),
        next=("need.classify",) if need.unresolved else ("case.edit",),
        artifacts={"replica": replica},
    )
