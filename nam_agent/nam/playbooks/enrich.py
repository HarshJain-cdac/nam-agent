"""need.enrich (FRS F9): what the enterprise record says about the need,
before the PR (S2).

SAP and Aarohan are not connected, so the enterprise data is the knowledge
graph's: the recorded fields of the selected precedents, or of the closest
related tenders while none is selected (department, category, estimated
value, EMD, bid type, PR number, awardees). An officer can name another
tender to read: answer {question_id: "ref", value: "PUR-12"}. A tender read
here is shown, never selected: only precedent.select selects (R2).

The fields are suggestions for the PR and the documents, each with the
tender it came from; the EMD and estimated value are history, never the
case's own (R7). What the SAP field list (E3) needs and the KG does not hold
is listed as missing, for pr.build to ask.
"""

from __future__ import annotations

from nam_agent.nam import need as need_store
from nam_agent.nam import orchestrator, tools
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome

REF = "ref"
MAX_SOURCES = 5
SAP_ONLY = ("cost_centre", "plant", "purchasing_group", "material_group", "gl_account")


def handle(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    answer = request.get("answer") or {}
    if answer and answer.get("question_id") != REF:
        raise NamError("bad_request", f"need.enrich asks only '{REF}'")
    wf = orchestrator.workflow(need)
    stored = orchestrator._artifact(wf, "enrichment") or {}
    warnings: list[str] = [("SAP and Aarohan are not connected: the enterprise data is the "
                           "knowledge graph's record of past tenders.")]
    keys = list(need.selected_cases) or list(need.related_cases)[:MAX_SOURCES]
    extra = list(stored.get("named") or [])
    given = str(answer.get("value") or "").strip()
    if given:
        found = tools.call(ctx, "resolve_reference", {"ref": given[:120]})
        matches = [m["cite"]["case_key"] for m in found["matches"]]
        if not matches:
            warnings.append(f"'{given[:40]}' names no tender in the knowledge graph.")
        elif found["ambiguous"]:
            warnings.append(f"'{given[:40]}' names {len(matches)} tenders "
                            f"({', '.join(matches[:5])}): give the exact one.")
        elif matches[0] not in extra:
            extra.append(matches[0])
    sources = []
    for key in list(dict.fromkeys([*keys, *extra]))[:MAX_SOURCES + len(extra)]:
        out = tools.call(ctx, "enrichment", {"case_key": key})
        if not out.get("cite"):
            warnings.append(f"{key}: no record in the knowledge graph")
            continue
        sources.append({"case_key": key, "selected": key in need.selected_cases,
                        "fields": {k: v for k, v in out["fields"].items() if v is not None},
                        "awarded_to": out["awarded_to"][:10]})
    value = {"summary": f"{len(sources)} tender record(s) read"[:300], "sources": sources,
             "named": extra, "missing": list(SAP_ONLY)}
    return Outcome(
        case_id=case_id, status="ok",
        result={"sources": sources, "missing": list(SAP_ONLY),
                "sap": "not connected: these fields are asked in pr.build"},
        warnings=tuple(warnings),
        citations=tuple({"source": "kg", "ref": f"tender:{s['case_key']}",
                         "authority": "precedent"} for s in sources),
        next=("precedent.select",) if not need.selected_cases else ("quantity.finalise",),
        artifacts={"enrichment": value} if value != stored else {})
