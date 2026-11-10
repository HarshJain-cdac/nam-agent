"""package.freeze (FRS F35, F36) and package.publish (F40, F50).

package.freeze (S6)
  Assembles the Need Assessment package from what validation passed: every
  generated document at the version validated, the compliance requirements
  and the validation report when stored, and every other file on the case as
  an attachment. It runs only on a validation that passed after the documents
  last changed. The freeze itself is the stage machine's irreversible action
  (freeze_package, confirmed by a functional reviewer); after it the package
  and the stages before it no longer change.

package.publish (S8-S10)
  The list of files to upload to the Aarohan procurement file, in the order
  of FRS F50 (SCC, PQC, BOQ, QCBS file, technical specification or scope of
  work, then the rest), at their latest versions. There is no Aarohan
  connector: nothing is sent, the list says so, and the reference of the file
  uploaded by hand is recorded:
    answer {question_id: "aarohan_ref", value: "NRL/MAT/2026/123"}.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from nam_agent.nam import guardrails, orchestrator, store
from nam_agent.nam import need as need_store
from nam_agent.nam.docs import store as doc_store
from nam_agent.nam.errors import NamError
from nam_agent.nam.identity import Caller
from nam_agent.nam.playbooks import Outcome

AAROHAN_REF = "aarohan_ref"
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._-]{2,59}$")
_KIND = re.compile(r"^[a-z][a-z_]{1,40}$")
F50_ORDER = ("scc", "pqc", "boq", "qcbs", "specification", "scope_of_work", "sor", "estimate",
             "compliance")


def _caller() -> Caller:
    actor = store.current()
    return Caller(tenant=actor.tenant, user=actor.user, roles=tuple(actor.roles))


def contents(ctx, need, wf: dict) -> list[dict]:
    """The package's files: the generated documents at the version
    validated, then compliance and the validation report, then every other
    file of the case (attachments, at their latest version)."""
    docs_art = orchestrator._artifact(wf, "documents") or {}
    out = [{"kind": d["kind"], "doc_id": d.get("doc_id"), "version": d.get("version")}
           for d in docs_art.get("documents") or []]
    for name, kind in (("compliance", "compliance"), ("validation", "validation_report")):
        art = orchestrator._artifact(wf, name) or {}
        ref = art.get("report") if name == "validation" else art
        if ref and ref.get("doc_id"):
            out.append({"kind": kind, "doc_id": ref["doc_id"], "version": ref.get("version")})
    have = {d["doc_id"] for d in out if d["doc_id"]}
    listing = doc_store.listing(ctx, _caller(), need.need_id)["documents"]
    for doc in listing:
        if doc["doc_id"] not in have:
            kind = doc["kind"] if _KIND.match(doc["kind"] or "") else "attachment"
            out.append({"kind": kind, "doc_id": doc["doc_id"], "version": doc["latest_version"]})
    return out[:200]


def freeze(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    if request.get("answer"):
        raise NamError("bad_request", "package.freeze asks nothing")
    wf = orchestrator.workflow(need)
    stale = guardrails._validated(wf)
    if stale:
        raise NamError("stage_gate", f"the package is assembled only from a passed, current "
                       f"validation: {stale[0].says}", case_id=case_id,
                       details=[b.says for b in stale])
    stored = orchestrator._artifact(wf, "package") or {}
    files = contents(ctx, need, wf)
    lacking = guardrails._mandatory(need, {"documents": files})
    value = {"summary": f"{len(files)} file(s) for {need.need_id}"[:300], "documents": files,
             "precedents": list(need.selected_cases),
             "validation_version": wf["artifacts"]["validation"]["at_version"]}
    frozen = wf.get("frozen")
    return Outcome(
        case_id=case_id, status="ok",
        result={"documents": files, "frozen": bool(frozen),
                "frozen_at_version": (frozen or {}).get("at_version"),
                "lacking": [b.says for b in lacking]},
        warnings=tuple(b.says for b in lacking), next=("pr.build",) if frozen else (),
        artifacts={"package": value} if value != stored and not frozen else {})


def publish(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    wf = orchestrator.workflow(need)
    package = orchestrator._artifact(wf, "package")
    if not package or not wf.get("frozen"):
        raise NamError("stage_gate", "only a frozen package is uploaded (package.freeze)",
                       case_id=case_id)
    answer = request.get("answer") or {}
    if answer and answer.get("question_id") != AAROHAN_REF:
        raise NamError("bad_request", f"package.publish asks only '{AAROHAN_REF}'")
    stored = orchestrator._artifact(wf, "published") or {}
    warnings: list[str] = []
    caller = _caller()
    items = []
    for d in package["documents"]:
        version = d.get("version")
        if d.get("doc_id"):
            try:
                version = doc_store.view(ctx, caller, d["doc_id"])["document"]["latest_version"]
            except NamError as exc:
                warnings.append(f"{d['kind']}: {exc.message[:120]}")
        items.append({"kind": d["kind"], "doc_id": d.get("doc_id"), "version": version})
    rank = {k: i for i, k in enumerate(F50_ORDER)}
    items.sort(key=lambda x: rank.get(x["kind"], len(rank)))
    if (need.subtype or {}).get("value") == "Consultancy" and \
            not any(x["kind"] == "qcbs" for x in items):
        warnings.append("A QCBS consultancy needs its QCBS file in the Aarohan upload (F50): "
                        "upload it to the case as kind qcbs.")
    ref = stored.get("aarohan_ref")
    by, at = stored.get("by"), stored.get("at")
    value_given = str(answer.get("value") or "").strip()
    if value_given:
        if not _REF.match(value_given):
            warnings.append(f"'{value_given[:40]}' is not an Aarohan file reference (3 to 60 "
                            "letters, digits and / . _ -).")
        elif value_given != ref:
            ref, by = value_given, store.current().user
            at = datetime.now(UTC).isoformat(timespec="seconds")
    value = {"summary": (f"{len(items)} file(s) for Aarohan"
                         + (f", uploaded as {ref}" if ref else ", not sent"))[:300],
             "items": items, "sent": False, "aarohan_ref": ref, "by": by, "at": at}
    questions = ()
    if not ref:
        questions = ({"id": AAROHAN_REF, "source": "harness", "options": [],
                      "text": "Upload these files to the Aarohan procurement file, then give "
                              "its reference.",
                      "why": "There is no Aarohan connector: the upload is by hand (F40)."},)
    return Outcome(
        case_id=case_id, status="ok", questions=questions, warnings=tuple(warnings),
        result={"items": items, "sent": False, "aarohan_ref": ref,
                "aarohan": "not sent: no Aarohan connector"},
        next=("handoff.nit",) if wf["stage"] == "S10" else (),
        artifacts={"published": value} if value != stored else {})
