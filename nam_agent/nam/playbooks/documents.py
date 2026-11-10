"""docs.generate (FRS F17-F23, F25): the case's documents, drafted from its
approved estimate and its selected precedents, for the drafter to check and
approve at the clauses and documents gates (S5).

Which documents: the category's mandatory list (policy/guardrails.json):
Goods estimate, BOQ, specification, PQC, SCC; Services scope of work in
place of the specification; Works the SOR as well.

  estimate, BOQ, SOR   from the approved estimate, line by line (R19): the BOQ
                       is made from the finished estimate, never the other
                       way. A New Item has no rate yet; past awardees of the
                       item are listed as names to ask for a quotation
                       (suppliers: precedent, never an approved vendor list).
  SCC, PQC, spec       every clause of every selected precedent, verbatim,
                       combined pointwise (an identical clause once) (R16).
                       The closest other tenders' clauses (the clauses tool)
                       are offered as suggestions, not included until the
                       drafter keeps them. Each clause is classed standard or
                       contract-specific by its words (F22, R17), or left
                       unclassified when the words do not say.
  scope of work        the guided form's scope, or the need as stated, with
                       the precedents' specification clauses

The drafter's edits are one answer per document, in the format of lines.py:
  answer {question_id: "scc", value: "3: keep=no, why=repeats clause 1;
          add: text=..., why=...; all: required=no, why=..."}
  answer {question_id: "fields", value: "all: delivery_period=8 weeks"}
Removing or adding a clause needs its reason (R27).

The contract-specific fields (F23) the category needs come from the case
(location, period) or the officer. Until they are all there the documents
are drafts: saved, but not complete, and the gates are not put.

Each document is also stored as a .docx in the case's documents
(docs/store.py), a new version whenever its content changes (F25), with its
metadata and a summary of where its clauses came from.

There is no master SCC/PQC repository connected, so R18 (no mandatory clause
omitted) cannot be checked here: every response says so.
"""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime

from nam_agent.nam import guardrails, orchestrator, store, tools
from nam_agent.nam import need as need_store
from nam_agent.nam.docs import render
from nam_agent.nam.docs import store as doc_store
from nam_agent.nam.errors import NamError
from nam_agent.nam.identity import Caller
from nam_agent.nam.playbooks import Outcome, lines, need_classify

CLAUSE_DOCS = {"scc": "scc", "pqc": "pqc", "specification": "tech_spec"}
TITLES = {"estimate": "Cost estimate", "boq": "Bill of quantities", "sor": "Schedule of rates",
          "scc": "Special conditions of contract", "pqc": "Pre-qualification criteria",
          "specification": "Technical specification", "scope_of_work": "Scope of work"}
FIELDS = {"goods": ("delivery_period", "delivery_location"),
          "services": ("contract_period", "location"),
          "works": ("completion_period", "location")}
SUGGEST = 5
_STANDARD = re.compile(r"\b(sub-?contract\w*|statutory|schedule of rates|\bsor\b|price "
                       r"vari\w+|pvc|tax(es)?|gst|dut(y|ies)|liquidated damages|force "
                       r"majeure|arbitration|insurance|warranty|guarantee)\b", re.IGNORECASE)
_SPECIFIC = re.compile(r"\b(contract period|period of contract|scope|key (man ?power|"
                       r"personnel)|delivery (period|schedule|within)|completion (period|"
                       r"time|within)|location|site|quantit(y|ies))\b", re.IGNORECASE)


def classify(text: str) -> tuple[str, str]:
    """(standard | contract_specific | unclassified, the words that decided)."""
    specific, standard = _SPECIFIC.search(text), _STANDARD.search(text)
    if specific and not standard:
        return "contract_specific", specific.group(0)
    if standard and not specific:
        return "standard", standard.group(0)
    if specific and standard:
        return "contract_specific", f"{specific.group(0)} (also {standard.group(0)})"
    return "unclassified", ""


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


# -- clause documents ----------------------------------------------------------------

def _clauses(records: dict, kind: str, suggested: dict | None, before: dict) -> list[dict]:
    """The precedents' clauses, combined pointwise, then the suggestions;
    the drafter's earlier decisions kept (by the clause's words)."""
    decided = {_norm(c["text"]): c for c in before.get("clauses") or []}
    out, seen = [], set()

    def put(text: str, source: str, selected: bool) -> None:
        key = _norm(text)
        if not key or key in seen:
            return
        seen.add(key)
        cls, basis = classify(text)
        clause = {"text": text, "source": source, "class": cls, "basis": basis,
                  "selected": selected, "why": ""}
        if key in decided:
            clause["selected"], clause["why"] = decided[key]["selected"], decided[key]["why"]
        out.append(clause)

    for key, record in records.items():
        for c in record["clauses"][kind]:
            put(c["text"], key, True)
    for c in (suggested or {}).get("clauses") or []:
        put(c["text"], f"suggested from {c['cite']['case_key']}", False)
    for c in before.get("clauses") or []:
        if c["source"] == "officer":
            put(c["text"], "officer", c["selected"])
            out[-1]["why"] = c["why"]
    return out


def _edit_clauses(doc: dict, text: str) -> None:
    for e in lines.parse(text, {"keep", "why", "text", "required"}):
        why = e.fields.get("why", "").strip()
        if e.target == "all":
            if "required" not in e.fields:
                raise lines.LineError("all: takes required=yes or required=no")
            if e.fields["required"] not in ("yes", "no"):
                raise lines.LineError("required is yes or no")
            if e.fields["required"] == "no" and not why:
                raise lines.LineError(f"saying the {doc['kind']} is not required needs why=...")
            doc["required"] = e.fields["required"] == "yes"
            doc["required_why"] = why
            continue
        if e.target == "add":
            body = e.fields.get("text", "").strip()
            if len(body) < 5 or not why:
                raise lines.LineError("add: needs text=... and why=...")
            cls, basis = classify(body)
            doc["clauses"].append({"text": body[:20000], "source": "officer", "class": cls,
                                   "basis": basis, "selected": True, "why": why[:300]})
            continue
        if e.remove:
            raise lines.LineError("a clause is dropped with keep=no and why=..., so the "
                                  "reason is kept")
        if e.index >= len(doc["clauses"]):
            raise lines.LineError(f"the {doc['kind']} has no clause {e.target}")
        clause = doc["clauses"][e.index]
        if "keep" in e.fields:
            if e.fields["keep"] not in ("yes", "no"):
                raise lines.LineError("keep is yes or no")
            keep = e.fields["keep"] == "yes"
            if keep != clause["selected"] and not why:
                raise lines.LineError(f"clause {e.target}: changing it needs why=... (R27)")
            clause["selected"], clause["why"] = keep, why[:300]


# -- table documents -----------------------------------------------------------------

def _rows(est: dict, kind: str) -> list[dict]:
    out = []
    for n, x in enumerate(est["lines"], 1):
        row = {"n": n, "item": x["item"], "material_code": x.get("material_code"),
               "qty": x["qty"], "uom": x.get("uom"), "rate": x.get("rate"),
               "amount": x.get("amount")}
        if kind == "estimate":
            src = x.get("source") or {}
            row["basis"] = x["basis"]
            row["source"] = (f"{src.get('tool')} {src.get('evidence')}" if src.get("tool")
                             else f"officer {src.get('officer')}" if src.get("officer")
                             else "components" if x.get("components") else "none")
        if kind == "sor":
            row["schedule"] = x.get("schedule")
            row["reference"] = ((x.get("source") or {}).get("quote") or "")[:200]
        out.append(row)
    return out


# -- rendering and storing -----------------------------------------------------------

def _blocks(doc: dict, fields: dict) -> list[tuple]:
    meta = doc["metadata"]
    blocks = [("p", (f"Case {meta['case_id']}; version of {meta['generated_at']}; "
                    f"by {meta['by']}"))]
    if doc.get("required") is False:
        return [*blocks, ("p", f"Not required: {doc.get('required_why') or ''}")]
    if fields:
        blocks += [("h", "Contract-specific fields"),
                   ("table", [["field", "value"], *[[k, v] for k, v in fields.items()]])]
    if doc.get("text"):
        blocks += [("h", "Scope"), ("p", doc["text"])]
    if doc.get("rows"):
        head = list(doc["rows"][0].keys())
        blocks += [("h", "Lines"), ("table", [head, *[[r[k] for k in head]
                                                     for r in doc["rows"]]])]
        if doc.get("total") is not None:
            blocks.append(("p", f"Total: Rs {doc['total']:,.2f} (provisional until approved)"))
    kept = [c for c in doc.get("clauses") or [] if c["selected"]]
    if kept:
        blocks.append(("h", "Clauses"))
        blocks += [("p", f"{i}. {c['text']}") for i, c in enumerate(kept, 1)]
    sel = meta["selection"]
    blocks += [("h", "Clause selection"),
               ("p", (f"From the selected precedents: {sel['precedent']}; kept from "
                     f"suggestions: {sel['suggested_kept']}; added by the officer: "
                     f"{sel['officer']}; dropped: {sel['dropped']}."))]
    return blocks


def store_docx(ctx, case_id: str, kind: str, title: str, blocks: list[tuple], before: dict,
               warnings: list[str], flow: str = "docs.generate") -> tuple[str | None, int | None]:
    """Render and store one generated document in the case's documents: a
    new version of `before` (its doc_id, version) or a new document.
    Returns (doc_id, version); a store refusal is a warning, keeping before."""
    actor = store.current()
    caller = Caller(tenant=actor.tenant, user=actor.user, roles=tuple(actor.roles))
    data = render.docx(f"{title}: {case_id}", blocks)
    name = f"{case_id}-{kind}.docx"
    reason = f"generated by {flow} ({kind})"
    try:
        if before.get("doc_id"):
            out = doc_store.new_version(ctx, caller, before["doc_id"], filename=name, data=data,
                                        expected=before.get("version") or 1, reason=reason,
                                        run_id=actor.run_id, generated=True)
        else:
            out = doc_store.upload(ctx, caller, case_id, filename=name, data=data, kind=kind,
                                   reason=reason, run_id=actor.run_id, generated=True)
        return out["document"]["doc_id"], out["document"]["doc_version"]
    except NamError as exc:
        warnings.append(f"{kind}: the .docx was not stored ({exc.code}: {exc.message[:120]})")
        return before.get("doc_id"), before.get("version")


def _store(ctx, case_id: str, doc: dict, fields: dict, before: dict,
           warnings: list[str]) -> None:
    doc["doc_id"], doc["version"] = store_docx(ctx, case_id, doc["kind"], TITLES[doc["kind"]],
                                               _blocks(doc, fields), before, warnings)


# -- the flow -----------------------------------------------------------------------

def generate(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    wf = orchestrator.workflow(need)
    est = orchestrator._artifact(wf, "estimate")
    if not est or "estimate" not in wf.get("gates", {}):
        raise NamError("stage_gate", "the estimate is not approved (gate estimate)",
                       case_id=case_id)
    route = need_classify.route(need)
    category = route["category"]
    stored = orchestrator._artifact(wf, "documents") or {}
    before = {d["kind"]: d for d in stored.get("documents") or []}
    answer = request.get("answer") or {}
    asked = answer.get("question_id") or ""
    if asked and asked not in (*CLAUSE_DOCS, "fields"):
        raise NamError("bad_request", f"docs.generate asks only fields, "
                       f"{', '.join(CLAUSE_DOCS)}")
    warnings: list[str] = [("No master SCC/PQC repository is connected: that no mandatory "
                           "clause is omitted (R18) is for the drafter to check.")]

    records = {}
    for key in need.selected_cases:
        record = tools.call(ctx, "tender_record", {"case_key": key})
        if record["found"]:
            records[key] = record
            if not record["complete"]:
                warnings.append(f"{key} is larger than one KG record: not all its clauses "
                                "were read")
        else:
            warnings.append(f"{key}: no record in the knowledge graph; none of its clauses "
                            "were used")
    query = need_store.search_query(need)[:200]
    kinds = list(guardrails.policy()["mandatory_documents"].get(category, []))
    form = (orchestrator._artifact(wf, "guided_form") or {}).get("answers") or {}

    docs = []
    now = datetime.now(UTC).isoformat(timespec="seconds")
    actor = store.current()
    for kind in kinds:
        doc = {"kind": kind, "title": TITLES.get(kind, kind), "doc_id": None, "version": None,
               "required": before.get(kind, {}).get("required", True),
               "required_why": before.get(kind, {}).get("required_why", "")}
        if kind in ("estimate", "boq", "sor"):
            doc["rows"] = _rows(est, kind)
            doc["total"] = est.get("total")
        elif kind == "scope_of_work":
            scope = form.get("scope_of_work") if form.get("scope_of_work") not in (None, "NA") \
                else need.raw_text
            doc["text"] = (scope or "")[:20000]
            doc["clauses"] = _clauses(records, "tech_spec", None, before.get(kind, {}))
        else:
            suggested = tools.call(ctx, "clauses", {"requirement": query or need.raw_text[:200],
                                                    "kind": CLAUSE_DOCS[kind],
                                                    "limit": SUGGEST}) if len(query) >= 2 \
                else None
            doc["clauses"] = _clauses(records, CLAUSE_DOCS[kind], suggested,
                                      before.get(kind, {}))
        docs.append(doc)

    fields = dict(stored.get("fields") or {})
    for key, value in (("location", need.location), ("delivery_location", need.location),
                       ("contract_period", need.term)):
        if value and key in FIELDS.get(category, ()) and key not in fields:
            fields[key] = value
    try:
        if asked == "fields":
            edits = lines.parse(str(answer.get("value") or ""), set(FIELDS[category]))
            if any(e.target != "all" for e in edits):
                raise lines.LineError("fields are set with all: name=value")
            for e in edits:
                fields.update({k: v.strip()[:300] for k, v in e.fields.items() if v.strip()})
        elif asked:
            at = next((i for i, d in enumerate(docs) if d["kind"] == asked), None)
            if at is None or "clauses" not in docs[at]:
                raise lines.LineError(f"this case has no {asked} to edit")
            draft = copy.deepcopy(docs[at])        # all of the answer, or none of it
            _edit_clauses(draft, str(answer.get("value") or ""))
            docs[at] = draft
    except lines.LineError as exc:
        warnings.append(str(exc))

    for doc in docs:
        clauses = doc.get("clauses") or []
        doc["metadata"] = {
            "case_id": case_id, "generated_at": now, "by": actor.user,
            "sources": sorted(records) or [], "estimate_version": (wf["artifacts"]
                                                                   ["estimate"]["at_version"]),
            "selection": {"precedent": sum(c["selected"] for c in clauses
                                           if c["source"] in records),
                          "suggested_kept": sum(c["selected"] for c in clauses
                                                if c["source"].startswith("suggested")),
                          "officer": sum(c["source"] == "officer" for c in clauses),
                          "dropped": sum(not c["selected"] for c in clauses
                                         if not c["source"].startswith("suggested"))}}
        if "clauses" in doc and doc["required"] and not any(c["selected"] for c in clauses) \
                and doc["kind"] != "scope_of_work":
            warnings.append(f"The {doc['kind']} has no clause: keep a suggestion or add one, "
                            "or say it is not required.")

    missing = [f for f in FIELDS.get(category, ()) if not fields.get(f)]
    empty = [d["kind"] for d in docs if "clauses" in d and d["required"]
             and d["kind"] != "scope_of_work" and not any(c["selected"] for c in d["clauses"])]
    complete = not missing and not empty
    if (need.subtype or {}).get("value") == "Consultancy":
        warnings.append("A consultancy evaluated by QCBS needs a qualification matrix in the "
                        "PQC (R29): add its criteria and marks as clauses.")
    new_items = [r for r in (docs[0].get("rows") or []) if r["rate"] is None] if docs else []
    vendors = []
    for row in new_items[:5]:
        found = tools.call(ctx, "suppliers", {"requirement": row["item"][:200], "limit": 5})
        if found["suppliers"]:
            vendors.append({"line": row["n"], "item": row["item"],
                            "past_awardees": [s["name"] for s in found["suppliers"]]})

    value = {"summary": (f"{len(docs)} document(s) for {category}"
                         + ("" if complete else ", draft"))[:300],
             "category": category, "complete": complete, "fields": fields,
             "documents": docs, "precedents": list(need.selected_cases)}
    if _core(value) != _core(stored):
        for doc in docs:
            old = before.get(doc["kind"], {})
            if old.get("doc_id") and _same(doc, old) and fields == (stored.get("fields") or {}):
                # this document did not change: its file and metadata stay
                doc.update(doc_id=old["doc_id"], version=old["version"],
                           metadata=old["metadata"])
            else:
                _store(ctx, case_id, doc, fields, old, warnings)
        changed = True
    else:
        for doc in docs:
            doc["doc_id"] = before.get(doc["kind"], {}).get("doc_id")
            doc["version"] = before.get(doc["kind"], {}).get("version")
            doc["metadata"] = before.get(doc["kind"], {}).get("metadata", doc["metadata"])
        changed = False

    questions = ()
    if missing:
        questions = ({"id": "fields", "source": "harness", "options": [],
                      "text": f"Give the contract-specific fields: "
                              f"{', '.join(missing)} (all: {missing[0]}=...).",
                      "why": "They go into the documents as the officer gives them (F23)."},)
    elif empty:
        questions = ({"id": empty[0], "source": "harness", "options": [],
                      "text": f"The {empty[0]} has no clause. Keep a suggestion "
                              f"('2: keep=yes, why=...'), add one, or 'all: required=no, "
                              "why=...'.", "why": "A required document cannot be empty."},)
    return Outcome(
        case_id=case_id, status="ok" if complete else "needs_input",
        result={"category": category, "complete": complete, "missing_fields": missing,
                "documents": [_summary(d) for d in docs], "vendors_to_ask": vendors},
        questions=questions, warnings=tuple(warnings),
        citations=tuple({"source": "kg", "ref": f"tender:{k}", "authority": "precedent"}
                        for k in records),
        next=("docs.review",) if complete else (),
        artifacts={"documents": value} if changed else {},
    )


def _core(value: dict) -> dict:
    """The documents without when, by whom and where they were stored."""
    return {**{k: v for k, v in value.items() if k != "documents"},
            "documents": [{k: v for k, v in d.items()
                           if k not in ("metadata", "doc_id", "version")}
                          for d in value.get("documents") or []]}


def _same(doc: dict, old: dict) -> bool:
    """One document unchanged: its content, and what it was made from."""
    def core(d):
        meta = {k: v for k, v in (d.get("metadata") or {}).items()
                if k not in ("generated_at", "by")}
        return {**{k: v for k, v in d.items() if k not in ("metadata", "doc_id", "version")},
                "metadata": meta}
    return core(doc) == core(old)


def _summary(doc: dict) -> dict:
    clauses = doc.get("clauses") or []
    return {"kind": doc["kind"], "title": doc["title"], "doc_id": doc["doc_id"],
            "version": doc["version"], "required": doc["required"],
            "lines": len(doc.get("rows") or []),
            "clauses": [{"n": i, "text": c["text"][:2000], "source": c["source"],
                         "class": c["class"], "selected": c["selected"]}
                        for i, c in enumerate(clauses, 1)][:200],
            "selection": doc["metadata"]["selection"]}
