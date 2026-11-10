"""NAM's outputs, as the PR Creation Flow names them, as Excel sheets.

  requirement   1(h) the completed requirement per item: every detail with
                its value and where it came from (the officer, their own
                words, or a default NAM filled in), and 1(i) the matching
                past tenders in the flow's order
  estimate      2(f) the estimate sheet: S.No, code, description, quantity,
                unit price, total, basis, source (with the last price, its
                year, the escalation and the rate check's result)
  pr            3(a) the filled PR: every field with its value and where it
                came from (policy/pr_fields.json), and the items with their
                valuation price

Every value is read from the case record; nothing is computed here that the
harness did not already decide. A sheet whose stage has not produced its
data yet is refused with the reason (OutputError), never filled with blanks.
NIT generation is not part of NAM: its output ends with the Purchase Request.
"""

from __future__ import annotations

from nam_agent.nam import capture, orchestrator, trace
from nam_agent.nam.docs.xlsx import xlsx

# each output: its title and what the PR Creation Flow says it holds
KINDS = {
    "requirement": ("Requirement sheet", (
        "1(h): the completed requirement per item, every detail with where it came "
        "from, and the matching past tenders, ranked")),
    "estimate": ("Estimate sheet", (
        "2(f): S.No, code, description, quantity, unit price, total, basis and source "
        "of each line, with the rate check")),
    "pr": ("Purchase Request", (
        "3(a): every PR field with its value and where it came from, and the items "
        "with their valuation price, ready for SAP")),
}


class OutputError(Exception):
    """The case has not reached the data this output is made of."""


def _art(need, name: str) -> dict:
    return orchestrator._artifact(orchestrator.workflow(need), name) or {}


def _pred(p: dict | None) -> tuple[str, str]:
    p = p or {}
    if not p.get("value"):
        return "", "not set"
    return str(p["value"]), ("confirmed by the officer" if p.get("confident")
                             else "suggested by NAM, not confirmed")


def _answers(state: dict) -> list[tuple[str, str, str]]:
    """(question, answer as shown, taken from) for one item's state."""
    questions = state.get("questions") or {}
    asked = state.get("asked") or {}
    defaults = state.get("defaults") or {}
    inferred = state.get("inferred") or {}
    out = []
    for qid, value in (state.get("answers") or {}).items():
        if qid == "d:category":
            continue
        shown = capture._label(asked.get(qid), value) or "any"
        if qid in defaults:
            source = f"default: {defaults[qid].get('basis', '')}"
        elif qid in inferred:
            source = "from the officer's own words"
        else:
            source = "officer"
        out.append((str(questions.get(qid, qid)), shown, source))
    return out


def requirement(need) -> list[tuple[str, list[list]]]:
    f = need.followups or {}
    if not f:
        raise OutputError("need capture has not run on this case")
    rows: list[list] = [["Item", "Detail", "Value", "Taken from"]]
    rows.append(["The need", "In the officer's words", need.raw_text.split(";")[0].strip(),
                 "officer"])
    for label, field in (("Category", "category"), ("Sub-type", "subtype"),
                         ("Procurement type", "procurement_type"),
                         ("Tender type", "tender_type")):
        value, source = _pred(getattr(need, field))
        rows.append(["The need", label, value, source])
    items = f.get("items") or []
    current = int(f.get("item") or 0)
    blocks = ([(x["text"], f if i == current else x) for i, x in enumerate(items)]
              if items else [("The need", f)])
    for name, state in blocks:
        for question, answer, source in _answers(state):
            rows.append([name, question, answer, source])
    for key, value in (f.get("details") or {}).items():
        if not any(r[1].lower().startswith(key.replace("_", " ")) for r in rows[1:]):
            rows.append(["The need", key.replace("_", " ").capitalize(), value, "officer"])
    related = {}
    for x in [*[i.get("related") or [] for i in items], (f.get("scan") or {}).get("related") or []]:
        for r in x:
            related.setdefault(r["case_key"], r)
    order = f.get("ranked") or list(need.related_cases)
    tenders = [["Rank", "PUR", "Tender no", "Subject", "Opened", "Estimated value",
                "Awarded to", "Matching items"]]
    for n, key in enumerate([k for k in order if k in related], 1):
        r = related[key]
        tenders.append([n, key, r.get("tender_no") or "", r.get("subject") or "",
                        r.get("bid_opening_date") or "", r.get("estimated_value") or "",
                        ", ".join(r.get("awarded_to") or []),
                        "; ".join((r.get("matching_items") or [])[:5])])
    sheets = [("Requirement", rows)]
    if len(tenders) > 1:
        sheets.append(("Matching tenders", tenders))
    if need.selected_cases:
        sheets.append(("Precedent", [["Selected precedent"], *[[k] for k in
                                                                need.selected_cases]]))
    return sheets


_RESULT = {"ALR": "Cleared", "Regular": "Cleared", "AHR": "AHR - flagged",
           "manual_review": "Manual review", "new_item": "New item"}


def estimate(need) -> list[tuple[str, list[list]]]:
    est = _art(need, "estimate")
    if not est.get("lines"):
        raise OutputError("there is no estimate yet: it is prepared at S4")
    wf = orchestrator.workflow(need)
    sources = {e["line"]: e for e in trace._line_evidence(est)}
    rows = [["S.No", "Code", "Description", "Quantity", "Unit", "Unit price", "Total",
             "Basis", "Source", "Last price", "Year", "Escalation", "Result", "Decision"]]
    for n, line in enumerate(est["lines"], 1):
        src = line.get("source") or {}
        last = line.get("last_price") or {}
        comp = line.get("comparison") or {}
        decision = (line.get("decision") or {}).get("value") or ""
        rows.append([
            n, line.get("material_code") or "", line["item"], line.get("qty"),
            line.get("uom") or "", line.get("rate") if line.get("rate") is not None else "",
            line.get("amount") if line.get("amount") is not None else "",
            line.get("basis") or "", (sources.get(n) or {}).get("source", ""),
            last.get("rate", ""), last.get("year") or "",
            (f"+{src.get('pct')}% a year x {src.get('years')}" if src.get("rule") == "escalation"
             else ""),
            _RESULT.get(comp.get("flag", ""), comp.get("flag", "")) if comp else "",
            decision])
    rows.append(["", "", "Total excl. GST", "", "", "", est.get("total") or 0, "", "", "",
                 "", "", "", ""])
    status = [["Estimate", "Status"],
              ["Approved (locked)" if "estimate" in wf["gates"] else "Not approved yet",
               est.get("summary", "")]]
    return [("Estimate", rows), ("Status", status)]


def pr(need) -> list[tuple[str, list[list]]]:
    from nam_agent.nam.playbooks import pr as pr_flow

    art = _art(need, "pr")
    if not art:
        raise OutputError("the PR is not filled yet: it is prepared at S7 (Prepare the PR)")
    spec = pr_flow.policy()["header"]
    officer = art.get("officer_fields") or {}
    taken = {"case": "the case (need identification)", "estimate": "the approved estimate",
             "officer": "the officer"}
    header = [["PR field", "Value", "Taken from", "Mandatory"]]
    for key, rule in spec.items():
        value = (art.get("header") or {}).get(key)
        source = "officer" if key in officer else rule.get("from", "")
        header.append([rule.get("label", key), "" if value is None else value,
                       taken.get(source, source), "yes" if rule.get("mandatory") else ""])
    items = [["Item no", "Material code", "Short text / description", "Quantity", "Unit",
              "Valuation price", "Amount"]]
    for n, x in enumerate(art.get("items") or [], 1):
        items.append([n, x.get("material_code") or "", x.get("description") or "",
                      x.get("qty"), x.get("uom") or "",
                      x.get("rate") if x.get("rate") is not None else "",
                      x.get("amount") if x.get("amount") is not None else ""])
    ref = _art(need, "pr_reference")
    status = [["Purchase Request", "Status"],
              ["Mandatory fields missing", ", ".join(art.get("missing") or []) or "none"],
              ["Submitted", "yes" if ref else "not yet"],
              ["PR number", (ref.get("pr_number") or ref.get("sap_pr") or "")
               if ref else "not yet: no SAP connector, enter the PR in SAP"]]
    return [("PR header", header), ("PR items", items), ("Status", status)]


_BUILD = {"requirement": requirement, "estimate": estimate, "pr": pr}


def build(need, kind: str) -> tuple[str, bytes]:
    """(file name, .xlsx bytes) of one output, or OutputError."""
    if kind not in _BUILD:
        raise OutputError(f"the outputs are {', '.join(KINDS)}")
    sheets = _BUILD[kind](need)
    return f"{need.need_id}-{kind}.xlsx", xlsx(sheets)


def available(need) -> list[dict]:
    """Each output, whether it can be made yet, and why not."""
    out = []
    for kind, (title, says) in KINDS.items():
        try:
            _BUILD[kind](need)
            out.append({"kind": kind, "title": title, "says": says, "ready": True, "why": ""})
        except OutputError as exc:
            out.append({"kind": kind, "title": title, "says": says, "ready": False,
                        "why": str(exc)})
    return out
