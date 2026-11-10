"""Every NAM flow (plan section 3), in case-lifecycle order.

The intents are regexes over the lowercased system prompt, each with a
weight: 4 for a phrase only this flow uses, 3 for a strong signal, 2 for a
usual one, 1 for a hint. The router sums them per flow. The routing eval
(evals/deterministic/nam/test_nam_router.py) holds at least three phrasings
per flow, so a pattern changed here that breaks another flow's routing fails
there.

Roles are not here: they are policy/permissions.json, the one place who may
do what is written.
"""

from __future__ import annotations

from nam_agent.nam.playbooks import (
    ANY_STAGE,
    Flow,
    approval,
    ask,
    case_edit,
    case_status,
    checker,
    compliance,
    documents,
    enrich,
    estimate,
    handoff,
    materials,
    need_capture,
    need_classify,
    need_guided_form,
    need_replicate,
    package,
    plan,
    pr,
    precedent,
    quantity,
    review,
    uploads,
)

# "estimate", "costing", "rate analysis": the same words for all three
# estimate flows; the case's category decides between them (router.py)
_ESTIMATE = (r"\bestimat|\bcost(ing|s)?\b|\brate analysis\b|\brates?\b|\bpric(e|es|ing)\b", 2)

FLOWS: tuple[Flow, ...] = (
    Flow("need.capture", "Capture the need", ("F1", "F3", "F4", "F5", "F49"), ("S1",), (
        (r"\bassess(ing|ment)?\b.*\bneeds?\b", 3),
        (r"\bneed assessment\b", 3),
        (r"\b(capture|record|register|log|describe)\b.*\b(need|requirement|indent)\b", 3),
        (r"\b(new|raise|start|open)\b.*\b(need|requirement|indent|case)\b", 2),
        (r"\b(i|we) (need|want|require)\b", 2),
        (r"\bprocure(ment)?\b", 1),
    ), tools=("tenders", "resolve_reference"), result_schema="flow.need.capture",
        handler=need_capture.handle, needs_case=False),
    Flow("need.replicate", "Copy a past tender, PR or PO", ("F2",), ("S1",), (
        (r"\breplicat", 4),
        (r"\bcreate by copy\b", 4),
        (r"\b(copy|clone|duplicate)\b", 3),
        (r"\bsame as (the )?(last|previous|earlier)\b", 3),
        (r"\b(re-?use|repeat)\b.*\b(tender|pr|po|order|indent)\b", 3),
    ), tools=("resolve_reference", "tender_record", "documents"),
         result_schema="flow.need.replicate", handler=need_replicate.handle, needs_case=False),
    Flow("need.classify", "Confirm the category, procurement and tender type", ("F4",), ("S1",), (
        (r"\bclassif", 4),
        ((r"\b(confirm|set|choose|select|pick|change|correct)\b.*"
          r"\b(category|procurement type|tender type|sub-?type)\b"), 4),
        (r"\b(procurement|tender) type\b", 3),
        (r"\bsub-?type\b", 3),
        (r"\bcategor(y|ise|ize)\b", 2),
        (r"\b(capex|opex)\b", 2),
    ), result_schema="flow.need.classify", handler=need_classify.handle),
    Flow("need.guided_form", "Fill the guided form", ("F8",), ("S1", "S2"), (
        (r"\bguided form\b", 4),
        (r"\bno (similar|matching|match)\b", 4),
        (r"\bnone of (these|them|those)\b", 4),
        (r"\b(fill|use) (in )?(the |a )?form\b", 3),
        (r"\bmanual(ly)? (entry|form)\b", 3),
        (r"\bfrom scratch\b", 3),
    ), result_schema="flow.need.guided_form",
        handler=need_guided_form.handle, needs_case=False),
    Flow("precedent.search", "Find similar past tenders", ("F6",), ("S2",), (
        (r"\bsimilar\b.*\b(tenders?|cases?|prs?|purchases?|orders?)\b", 3),
        ((r"\b(find|search|look for|show|retrieve|fetch|list)\b.*"
          r"\b(precedents?|past|previous|historical|earlier)\b"), 3),
        (r"\bprecedents?\b", 2),
        (r"\btry another\b", 4),
        (r"\bmatch(es|ing)?\b", 1),
    ), tools=("tenders", "resolve_reference"), result_schema="flow.precedent.search",
        handler=precedent.search),
    Flow("precedent.select", "Select the precedent tender", ("F7",), ("S2",), (
        (r"\b(select|choose|pick|use)\b.*\b(tenders?|precedents?|cases?|references?)\b", 3),
        (r"\bgo with\b", 3),
        (r"\bselect(ed|ion)?\b", 1),
    ), tools=("resolve_reference", "tender_record"), result_schema="flow.precedent.select",
        handler=precedent.select),
    Flow("need.enrich", "Enrich from SAP and Aarohan", ("F9",), ("S2",), (
        (r"\benrich", 4),
        (r"\benterprise data\b", 4),
        (r"\bmissing (mandatory )?(fields?|info|information|data)\b", 3),
        (r"\b(sap|aarohan)\b.*\b(data|fields?|details?|history|records?)\b", 2),
        (r"\b(item|material|service) (code|master)\b", 2),
    ), tools=("enrichment", "resolve_reference"), result_schema="flow.need.enrich",
        handler=enrich.handle),
    Flow("quantity.finalise", "Finalise the quantity and scope", ("F10",), ("S3",), (
        (r"\bquantit(y|ies)\b", 3),
        (r"\bqty\b", 3),
        (r"\buom\b|\bunit of measure", 2),
        (r"\bfinali[sz]e\b.*\b(scope|requirement)\b", 2),
    ), tools=("tender_record", "quantity_history"), result_schema="flow.quantity.finalise",
        handler=quantity.finalise),
    Flow("material.suggest", "Suggest materials and specifications", ("F11",), ("S3",), (
        (r"\bsuggest\b.*\b(materials?|services?|specs?|specifications?|items?|grades?)\b", 3),
        (r"\brecommend\b.*\b(materials?|services?|items?|specs?|grades?)\b", 3),
        (r"\bwhich (material|item|grade)\b", 3),
    ), tools=("tenders", "tender_record"), result_schema="flow.material.suggest",
        handler=materials.handle),
    Flow("case.edit", "Edit the case", ("F12", "F42"), (ANY_STAGE,), (
        ((r"\b(edit|amend|modify|revise|correct|change|update)\b.*"
          r"\b(case|need|record|items?|rates?|specs?|details?|fields?)\b"), 3),
        (r"\breview screen\b", 4),
        (r"\bwhy (it|this) (was )?changed\b", 2),
    ), result_schema="flow.case.edit", handler=case_edit.handle),
    Flow("estimate.goods", "Estimate: Goods (item rates)", ("F13", "F16", "F48"), ("S4",), (
        _ESTIMATE,
        (r"\b(goods|items?|materials?) (rates?|estimates?|costing)\b", 3),
        (r"\b(po|rate contract|rc) rates?\b", 3),
        (r"\bvarn\b|\bcagr\b", 4),
        (r"\b(vendor|budgetary) quot", 2),
    ), tools=("price_history",), category="goods", result_schema="flow.estimate",
        handler=estimate.goods),
    Flow("estimate.wage", "Estimate: Manpower (wage rates)", ("F14", "F16"), ("S4",), (
        _ESTIMATE,
        (r"\b(wages?|manpower|labou?r|man-?days?|mdy)\b", 3),
        (r"\bminimum wage\b", 4),
        (r"\b(pf|esi|epf|edli)\b", 2),
    ), category="services", result_schema="flow.estimate", handler=estimate.wage),
    Flow("estimate.works", "Estimate: Works (SOR, DSR, rate analysis)", ("F15", "F16", "F47"),
         ("S4",), (
        _ESTIMATE,
        (r"\bworks? estimate\b", 4),
        (r"\b(sor|dsr|dar)\b", 3),
        (r"\bempanel", 4),
        (r"\bcivil\b", 2),
    ), tools=("price_history",), category="works", result_schema="flow.estimate",
        handler=estimate.works),
    Flow("docs.generate", "Generate the documents (BOQ, SOR, SCC, PQC, specification)",
         ("F17", "F18", "F19", "F20", "F21", "F22", "F23", "F25"), ("S5",), (
        ((r"\b(generate|draft|prepare|create|build|write|produce)\b.*"
          r"\b(documents?|docs|boq|sor|scc|pqc|sow|specs?|specifications?|qcbs)\b"), 3),
        (r"\b(boq|scc|pqc|sow|qcbs)\b", 2),
        (r"\btechnical spec", 2),
        (r"\bclauses?\b", 1),
    ), tools=("tender_record", "clauses", "suppliers"), result_schema="flow.docs.generate",
        handler=documents.generate),
    Flow("docs.review", "Review the drafts", ("F24",), ("S5",), (
        (r"\breview\b.*\b(drafts?|documents?|docs|clauses?|scc|pqc|boq|specs?)\b", 4),
        (r"^\s*(please )?review\b", 2),
        (r"\b(ambiguous|conflicting|contradictory) clauses?\b", 4),
        (r"\bproof ?read\b", 3),
    ), result_schema="flow.docs.review", handler=review.handle),
    Flow("compliance.build", "Build the compliance requirements", ("F26",), ("S5",), (
        (r"\bcompliances?\b", 3),
        (r"\bmse\b", 3),
        (r"\boem\b.*\b(requirements?|compliance|criteri)", 2),
        (r"\breservation\b", 2),
    ), tools=("boilerplate",), result_schema="flow.compliance.build",
        handler=compliance.handle),
    Flow("validate", "Validate the PR and documents", ("F30", "F31", "F32", "F33", "F34", "F46"),
         ("S5", "S6"), (
        (r"\bvalidat", 4),
        (r"\b(check|verify)\b.*\b(completeness|consistency|format|documents?|package|pr)\b", 3),
        (r"\bcross[- ]document\b", 4),
        (r"\b(procurement manual|circulars?)\b", 3),
        (r"\bdiscrepanc", 3),
        (r"\bchecker\b", 2),
    ), result_schema="flow.validate", handler=checker.handle),
    Flow("package.freeze", "Freeze the package", ("F35", "F36"), ("S6",), (
        (r"\bfreez", 4),
        (r"\b(assemble|finali[sz]e|lock|complete)\b.*\bpackage\b", 4),
    ), result_schema="flow.package.freeze", handler=package.freeze),
    Flow("pr.build", "Build the purchase requisition", ("F27",), ("S7",), (
        ((r"\b(build|prepare|draft|generate|create|make|raise)\b.*"
          r"\b(pr|purchase requisition)\b"), 3),
        (r"\bpr fields\b", 4),
        (r"\bpurchase requisition\b", 1),
    ), result_schema="flow.pr.build", handler=pr.build),
    Flow("pr.submit", "Submit the PR to SAP", ("F28", "F29"), ("S7", "S8"), (
        (r"\bsubmit\b.*\b(pr|purchase requisition)\b", 4),
        (r"\b(pr|purchase requisition)\b.*\bin sap\b|\bsap pr\b", 3),
        (r"\bcreate\b.*\bpr\b.*\bsap\b", 2),
        (r"\bone[- ]click\b", 4),
        (r"\bpr (number|no\b)", 3),
    ), result_schema="flow.pr.submit", handler=pr.submit),
    Flow("approval", "Route for approval (Aarohan / e-office)", ("F37", "F38", "F45"), ("S8",), (
        (r"\bapprov(al|e|er)\b", 1),
        (r"\b(ann|aan)\b", 3),
        (r"\badministrative approval\b", 4),
        (r"\b(route|send|forward|put up)\b.*\bfor approval\b", 4),
        (r"\b(aarohan|procurement|e-?office) file\b", 3),
        (r"\bdoa\b|\bdelegation of", 3),
    ), result_schema="flow.approval", handler=approval.handle),
    Flow("document.update", "Upload an edited document", ("F39",), ("S9", "S10"), (
        (r"\b(upload|attach)\b.*\b(edited|revised|updated|corrected|changed)\b", 4),
        (r"\bcorrigend", 4),
        (r"\bnew version\b", 3),
        (r"\bedited outside\b", 4),
        (r"\b(re-?upload|replace)\b.*\b(document|file)\b", 3),
    ), result_schema="flow.document.update", handler=uploads.handle),
    Flow("package.publish", "Push the documents to Aarohan", ("F40", "F50"),
         ("S8", "S9", "S10"), (
        (r"\b(push|upload|send|publish)\b.*\b(to|in|into|on) aarohan\b", 5),
        (r"\baarohan upload\b", 4),
    ), result_schema="flow.package.publish", handler=package.publish),
    Flow("handoff.nit", "Hand off to the NIT module", ("F41", "F43", "F44"), ("S10",), (
        (r"\bhand ?-?off\b|\bhand (it )?over\b", 4),
        (r"\bnit (module|team|stage|department)\b", 3),
        (r"\b(send|route|forward|pass|move)\b.*\b(nit|commercial)\b", 3),
        (r"\bcommercial (department|officer|team)\b", 2),
    ), result_schema="flow.handoff.nit", handler=handoff.handle),
    Flow("case.status", "Show the case status", ("F5",), (ANY_STAGE,), (
        (r"\bstatus\b", 3),
        (r"\bwhat stage\b", 4),
        (r"\bwhere (is|are)\b", 2),
        (r"\btrack(ing)?\b", 2),
        (r"\bprogress\b", 2),
        (r"\bhistory\b", 2),
    ), result_schema="flow.case.status", handler=case_status.handle),
    # the user's query as a to-do list, worked through step by step; each step
    # is one of the flows above, run and verified as its own call would be
    Flow("case.plan", "Make a to-do list and complete it step by step", ("F5",),
         (ANY_STAGE,), (
        (r"\b(to-?do|todo|checklist)\b", 4),
        (r"\bstep[- ]by[- ]step\b", 4),
        (r"\b(make|create|build|draw up|prepare) (a |the )?plan\b", 4),
        ((r"\b(complete|finish|run|do|process)\b.*\b(whole|entire|full|end[- ]to[- ]end|"
          r"all (the )?steps|everything)\b"), 4),
        (r"\b(take|bring|carry|drive|move)\b.*\b(up to|till|until|through|all the way)\b", 4),
    ), result_schema="flow.case.plan", handler=plan.handle, needs_case=False),
    # words that need no step: a question, a greeting, "approve it" (router
    # rule "words", intent.py); answered from the record and the KG, nothing
    # written
    Flow("case.ask", "Answer a question without a step", ("F5",), (ANY_STAGE,), (
        (r"\bwhat did we pay\b", 4),
        (r"\bwhat (is|are|does) (a |an |the )?(pqc|scc|gcc|boq|sor|dsr|dar|emd|mse|doa|oem)\b",
         4),
        (r"\b(explain|define|meaning of)\b", 4),
        (r"\b(show|list) (me )?(the )?past (tenders|prices)\b", 4),
    ), tools=("tenders", "price_history"), result_schema="flow.case.ask", handler=ask.handle,
        needs_case=False),
)
