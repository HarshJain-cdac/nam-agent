"""validate (FRS F30-F34, F46): the internal checker. It reads what the case
holds and gives one consolidated report with a status (S5-S6). Every check is
code; nothing here asks a model.

  format        (F30) every document is stored as a file, reads back with
                text, and a clause document has a clause or says why not
  completeness  (F31) the category's mandatory documents, the contract-
                specific fields, every line's quantity and unit, the PR's
                mandatory fields once it exists; New Items without a rate
  consistency   (F32) the BOQ and SOR against the estimate line by line, the
                estimate against the quantity, the documents built from the
                current estimate and precedents, and the same contract term
                stated alike everywhere (playbooks/checks.py; a conflict the
                drafter accepted in docs.review is not a failure)
  manual        (F33) no Procurement Manual or circulars are connected: said,
                not checked
  integrity     (F33) each stored file matches its hash, and the audit log's
                hash chain holds (audit.verify)
  versions      (F34) what changed since the last validation, and how far
                the clauses moved from the precedents
  review        docs.review run on these documents; compliance.build done

A failure fails the validation; a warning and a note are reported. The
report is also stored as a .docx (F46). The package can be frozen only on a
validation that passed after the documents last changed (guardrails R32).
Correct what failed and run validate again (R23).
"""

from __future__ import annotations

from nam_agent.nam import audit, guardrails, orchestrator, store
from nam_agent.nam import need as need_store
from nam_agent.nam.docs import store as doc_store
from nam_agent.nam.errors import NamError
from nam_agent.nam.identity import Caller
from nam_agent.nam.playbooks import Outcome, need_classify, review
from nam_agent.nam.playbooks import documents as documents_flow

_ORDER = {"fail": 0, "warn": 1, "info": 2}


def _item(text) -> str:
    return " ".join(str(text or "").lower().split())


def _by_rule(line: dict) -> bool:
    """A line the estimate's own rules made: the wage reimbursements (R13),
    each ending in its round-up to the policy step."""
    return any((c.get("source") or {}).get("rule") == "round_up"
               for c in line.get("components") or [])


class Report:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, check: str, severity: str, says: str, doc: str = "") -> None:
        self.items.append({"check": check, "severity": severity, "doc": doc, "says": says[:300]})


def _format(r: Report, ctx, caller, docs: list[dict]) -> None:
    for doc in docs:
        kind = doc["kind"]
        if not doc.get("doc_id"):
            r.add("format", "fail", f"the {kind} is not stored as a file", kind)
            continue
        try:
            body = doc_store.view(ctx, caller, doc["doc_id"], doc.get("version"))
            doc_store.content(ctx, caller, doc["doc_id"], doc.get("version"))
        except NamError as exc:
            sev_check = "integrity" if exc.code in ("internal", "integrity") else "format"
            r.add(sev_check, "fail", f"the {kind} file cannot be read ({exc.code}: "
                  f"{exc.message[:120]})", kind)
            continue
        if not any(p.get("usable") for p in body["content"]["pages"]):
            r.add("format", "fail", f"the {kind} file has no readable text", kind)
        latest = body["document"]["latest_version"]
        if doc.get("version") and latest > doc["version"]:
            r.add("versions", "warn", f"the {kind} has a newer version (v{latest}) than the "
                  f"one generated (v{doc['version']}): it was changed outside NAM", kind)
        if doc.get("required") is False:
            if not doc.get("required_why"):
                r.add("completeness", "fail", f"the {kind} is marked not required without a "
                      "reason", kind)
        elif "clauses" in doc and kind != "scope_of_work" \
                and not any(c["selected"] for c in doc["clauses"]):
            r.add("format", "fail", f"the {kind} has no clause", kind)
        elif kind in ("estimate", "boq", "sor") and not doc.get("rows"):
            r.add("format", "fail", f"the {kind} has no lines", kind)


def _completeness(r: Report, need, wf, docs_art: dict, est: dict, qty: dict) -> None:
    category = need_classify.route(need)["category"]
    have = {d["kind"] for d in docs_art["documents"]}
    for kind in guardrails.policy()["mandatory_documents"].get(category, []):
        if kind not in have:
            r.add("completeness", "fail", f"the mandatory {kind} is missing", kind)
    for key in documents_flow.FIELDS.get(category, ()):
        if not (docs_art.get("fields") or {}).get(key):
            r.add("completeness", "fail", f"the contract-specific field {key} is not given")
    for i, line in enumerate(qty.get("lines") or [], 1):
        if line.get("qty") is None or not line.get("uom"):
            r.add("completeness", "fail", f"quantity line {i} has no quantity or unit")
    new = [i for i, x in enumerate(est["lines"], 1) if x.get("rate") is None]
    if new:
        r.add("completeness", "warn", f"line(s) {', '.join(map(str, new[:20]))} are New Items "
              "without a rate: get quotations before the tender")
    for gate in ("quantity", "estimate"):
        if gate not in wf["gates"]:
            r.add("completeness", "fail", f"the {gate} approval is not given")
    for gate in ("clauses", "documents"):
        if gate not in wf["gates"]:
            r.add("completeness", "info", f"the {gate} approval is still to be given")
    pr = orchestrator._artifact(wf, "pr")
    if pr and pr.get("missing"):
        r.add("completeness", "fail", "the PR lacks " + ", ".join(pr["missing"][:8]), "pr")


def _consistency(r: Report, need, wf, docs_art: dict, est: dict, qty: dict,
                 review_art: dict) -> None:
    by_kind = {d["kind"]: d for d in docs_art["documents"]}
    for kind in ("boq", "sor"):
        rows = (by_kind.get(kind) or {}).get("rows")
        if rows is None:
            continue
        if len(rows) != len(est["lines"]):
            r.add("consistency", "fail", f"the {kind} has {len(rows)} line(s), the estimate "
                  f"{len(est['lines'])}", kind)
            continue
        for n, (row, line) in enumerate(zip(rows, est["lines"], strict=True), 1):
            for key in ("item", "qty", "uom", "rate"):
                a, b = row.get(key), line.get(key)
                if (_item(a) != _item(b)) if key in ("item", "uom") else a != b:
                    r.add("consistency", "fail", f"{kind} line {n}: {key} is {a!r}, the "
                          f"estimate's is {b!r}", kind)
        total = by_kind[kind].get("total")
        if kind == "boq" and total != est.get("total"):
            r.add("consistency", "fail", f"the BOQ total {total} is not the estimate's "
                  f"{est.get('total')}", kind)
    made_from = {d["metadata"]["estimate_version"] for d in docs_art["documents"]}
    current = wf["artifacts"]["estimate"]["at_version"]
    if made_from - {current}:
        r.add("consistency", "fail", "the documents were built from an earlier estimate: run "
              "docs.generate again")
    wanted = {_item(x["item"]): x for x in qty.get("lines") or []}
    for n, line in enumerate(est["lines"], 1):
        q = wanted.get(_item(line["item"]))
        if q is None and _by_rule(line):
            r.add("consistency", "info", f"estimate line {n} ({line['item'][:60]}) is added by "
                  "the wage rules (R13), not by the quantity")
        elif q is None:
            r.add("consistency", "fail", f"estimate line {n} ({line['item'][:60]}) is not in the "
                  "approved quantity")
        elif q.get("qty") != line.get("qty") or _item(q.get("uom")) != _item(line.get("uom")):
            r.add("consistency", "fail", f"estimate line {n}: {line.get('qty')} "
                  f"{line.get('uom')}, the approved quantity says {q.get('qty')} {q.get('uom')}")
    if list(docs_art.get("precedents") or []) != list(need.selected_cases):
        r.add("consistency", "fail", "the documents were built from other precedents than the "
              "ones now selected: run docs.generate again")
    accepted = {f["id"] for f in review_art.get("findings") or [] if f.get("accepted")}
    for f in review.code_findings(docs_art["documents"], docs_art.get("fields") or {}):
        if f["kind"] == "conflict":
            if f["id"] in accepted:
                r.add("consistency", "info", f"accepted in review: {f['says']}")
            else:
                r.add("consistency", "fail", f["says"])


def _versions(r: Report, docs_art: dict, before: dict) -> dict:
    now = {d["kind"]: d.get("version") for d in docs_art["documents"] if d.get("version")}
    for kind, version in now.items():
        old = before.get(kind)
        if old and old != version:
            r.add("versions", "info", f"the {kind} changed since the last validation (v{old} "
                  f"to v{version})", kind)
    for d in docs_art["documents"]:
        sel = d["metadata"]["selection"]
        if sel.get("dropped") or sel.get("officer") or sel.get("suggested_kept"):
            r.add("versions", "info", f"the {d['kind']} differs from its precedents: "
                  f"{sel.get('dropped', 0)} clause(s) dropped, {sel.get('officer', 0)} added by "
                  f"the officer, {sel.get('suggested_kept', 0)} taken from other tenders",
                  d["kind"])
    return now


def handle(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    if request.get("answer"):
        raise NamError("bad_request", "validate asks nothing: correct what failed in its flow "
                       "and validate again")
    wf = orchestrator.workflow(need)
    docs_art = orchestrator._artifact(wf, "documents")
    est = orchestrator._artifact(wf, "estimate")
    if not docs_art or not est:
        raise NamError("stage_gate", "there is nothing to validate yet: the estimate and the "
                       "documents come first (docs.generate)", case_id=case_id)
    qty = orchestrator._artifact(wf, "quantity") or {}
    review_art = orchestrator._artifact(wf, "review") or {}
    compliance = orchestrator._artifact(wf, "compliance")
    stored = orchestrator._artifact(wf, "validation") or {}
    actor = store.current()
    caller = Caller(tenant=actor.tenant, user=actor.user, roles=tuple(actor.roles))

    r = Report()
    _format(r, ctx, caller, docs_art["documents"])
    _completeness(r, need, wf, docs_art, est, qty)
    _consistency(r, need, wf, docs_art, est, qty, review_art)
    r.add("manual", "info", "No Procurement Manual or circulars are connected: checking the "
          "documents against them (F33) is for the reviewer.")
    chain = audit.verify(ctx.conn)
    if not chain["ok"]:
        r.add("integrity", "fail", chain["says"])
    if not docs_art.get("complete"):
        r.add("completeness", "fail", "the documents are drafts (docs.generate)")
    if not review_art:
        r.add("review", "warn", "the drafts were not reviewed (docs.review)")
    elif review_art.get("documents_version") != wf["artifacts"]["documents"]["at_version"]:
        r.add("review", "warn", "the review is of an earlier version of the documents: run "
              "docs.review again")
    if not compliance:
        r.add("review", "warn", "the compliance requirements are not built (compliance.build)")
    elif not compliance.get("complete"):
        r.add("review", "warn", "the compliance requirements are not all decided")
    versions = _versions(r, docs_art, stored.get("versions") or {})

    items = sorted(r.items, key=lambda x: _ORDER[x["severity"]])[:500]
    fails = sum(x["severity"] == "fail" for x in items)
    status = "fail" if fails else "pass"
    summary = (f"validation {status}: {fails} failure(s), "
               f"{sum(x['severity'] == 'warn' for x in items)} warning(s)")
    value = {"status": status, "summary": summary[:300],
             "findings": [f"{x['severity']}: {x['says']}"[:300] for x in items
                          if x["severity"] != "info"],
             "checks": items, "versions": versions,
             "documents_version": wf["artifacts"]["documents"]["at_version"],
             "report": stored.get("report")}
    core = {k: v for k, v in value.items() if k != "report"}
    changed = core != {k: v for k, v in stored.items() if k != "report"}
    warnings: list[str] = []
    if changed:
        blocks = [("p", f"Case {case_id}: {summary} (FRS F46).")]
        blocks.append(("table", [["check", "severity", "document", "finding"],
                                 *[[x["check"], x["severity"], x["doc"], x["says"]]
                                   for x in items]]))
        before = stored.get("report") or {}
        doc_id, version = documents_flow.store_docx(ctx, case_id, "validation_report",
                                                    "Validation report", blocks, before,
                                                    warnings, flow="validate")
        value["report"] = {"doc_id": doc_id, "version": version} if doc_id else None
    stage = wf["stage"]
    nxt = ("package.freeze",) if status == "pass" and stage == "S6" else \
        () if status == "pass" else ("docs.generate",)
    return Outcome(
        case_id=case_id, status="ok",
        result={"status": status, "summary": summary, "checks": items,
                "failures": fails, "report": value["report"]},
        warnings=tuple(warnings), next=nxt,
        artifacts={"validation": value} if changed else {})
