"""handoff.nit (FRS F41, F43, F44): the package and the case's structured data
for the NIT module and the Commercial Department (S10), so nothing captured
in NAM is typed in again.

The handoff carries:
  documents   every file of the frozen package at its latest version (an
              edit uploaded in S9 included), and the files added since
  data        the case: category, sub-type, procurement and tender type,
              location, term, the contract-specific fields, the items with
              quantity, unit, rate and basis, the estimated value
              (provisional), the compliance requirements that apply, the
              precedents
  references  the PR (Procurix.ai number, SAP PR), the approval (file,
              authority, who approved), the Aarohan upload reference

It is assembled only when R32 holds: every approval given, validation
passed, the mandatory documents present, nothing awaited, the package
frozen (guardrails.py). The handoff itself is the stage machine's
irreversible action (handoff_nit, confirmed by the Commercial Department);
after it the case is closed. There is no NIT connector: the handoff is
recorded, and the NIT module reads it from the case.
"""

from __future__ import annotations

from nam_agent.nam import need as need_store
from nam_agent.nam import orchestrator
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome, need_classify


def value(prediction) -> str | None:
    return (prediction or {}).get("value")


def payload(need, wf: dict) -> dict:
    est = orchestrator._artifact(wf, "estimate") or {}
    docs_art = orchestrator._artifact(wf, "documents") or {}
    package = orchestrator._artifact(wf, "package") or {}
    uploads = orchestrator._artifact(wf, "uploads") or {}
    compliance = orchestrator._artifact(wf, "compliance") or {}
    approval = orchestrator._artifact(wf, "approval_file") or {}
    published = orchestrator._artifact(wf, "published") or {}
    ref = orchestrator._artifact(wf, "pr_reference") or {}
    pr = (wf["actions"].get("submit_pr") or {}).get("result") or {}
    approved = wf["actions"].get("approve") or {}
    newer = {u["doc_id"]: u["to_version"] for u in uploads.get("updates") or []}
    documents = [{**d, "version": newer.get(d.get("doc_id"), d.get("version"))}
                 for d in package.get("documents") or []]
    documents += [{"kind": a["kind"], "doc_id": a["doc_id"], "version": a["version"]}
                  for a in uploads.get("added") or []]
    route = need_classify.route(need)
    return {
        "case_id": need.need_id,
        "subject": need.raw_text.split(";")[0].strip()[:300],
        "category": route["category"], "subtype": value(need.subtype),
        "procurement_type": value(need.procurement_type), "tender_type": value(need.tender_type),
        "location": need.location or None, "term": need.term or None,
        "fields": dict(docs_art.get("fields") or {}),
        "items": [{"item": x["item"][:300], "material_code": x.get("material_code"),
                   "qty": x.get("qty"), "uom": x.get("uom"), "rate": x.get("rate"),
                   "amount": x.get("amount"), "basis": x.get("basis")}
                  for x in est.get("lines") or []][:2000],
        "estimated_value": est.get("total"), "estimate_provisional": True,
        "compliance": [r["text"][:2000] for r in compliance.get("requirements") or []
                       if r.get("apply")][:100],
        "precedents": list(need.selected_cases),
        "documents": documents[:200],
        "pr": {"pr_number": pr.get("pr_number"), "sap_pr": ref.get("sap_pr")},
        "approval": {"file": approval.get("aarohan_file") or approval.get("file_number"),
                     "authority": approval.get("authority"),
                     "approved_by": approved.get("user"), "approved_at": approved.get("at")},
        "aarohan_ref": published.get("aarohan_ref"),
    }


def handle(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    if request.get("answer"):
        raise NamError("bad_request", "handoff.nit asks nothing")
    wf = orchestrator.workflow(need)
    frozen = (wf.get("frozen") or {}).get("at_version")
    if not frozen:
        raise NamError("stage_gate", "the package is not frozen", case_id=case_id)
    stored = orchestrator._artifact(wf, "handoff") or {}
    data = payload(need, wf)
    made = {"summary": (f"{len(data['documents'])} file(s) and {len(data['items'])} item(s) "
                         f"to NIT")[:300], "package_version": frozen, "payload": data}
    done = wf["actions"].get("handoff_nit")
    warnings = [] if data["aarohan_ref"] else [
        "The files are not recorded as uploaded to Aarohan (package.publish)."]
    return Outcome(
        case_id=case_id, status="ok",
        result={"package_version": frozen, "handed_off": bool(done), "data": data,
                "nit": "recorded: no NIT connector; the NIT module reads the handoff from "
                       "the case"},
        warnings=tuple(warnings),
        artifacts={"handoff": made} if made != stored and not done else {})
