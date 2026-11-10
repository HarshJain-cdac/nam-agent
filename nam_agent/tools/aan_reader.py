"""Reading an uploaded Administrative Approval Note (AAN), and every other
document the NIT intake accepts.

WHY THIS EXISTS
---------------
An AAN is a FORM: a Word document whose content lives almost entirely in
tables, as "Label: value" rows. Measured on a real one from the corpus
(PUR-INSTMNT-1000150) it has 5 paragraphs holding 62 characters — the title —
and 8 tables holding 3,733 characters, which is every field that matters:
File No., PR No., Value, Category, Nature of contract, Tender Type, Name of
proposal, justification, budget, PTR, payment terms, department.

The previous reader took `docx.Document(...).paragraphs` only. It therefore
handed the extractor 62 characters of title, the extractor found nothing in it,
and the wizard asked the user all 37 questions by hand — including the ones
sitting in the file they had just uploaded.

DETERMINISTIC FIRST, MODEL SECOND
---------------------------------
An AAN row is a label and a value in adjacent cells. That is not a job that
needs a language model, and a model is the wrong tool for it: it costs a call,
it can hallucinate a plausible PR number, and when it fails it fails silently.
So `extract_fields` matches labels directly, and the LLM is only asked about
the fields label-matching could not fill. Same order the KG's own classifier
uses (filename heuristics, then deterministic content rules, then LLM last),
for the same reason.

Labels are matched loosely — case-folded, punctuation and whitespace stripped —
because these forms are typed by people: "Tender Type :" and "Tender type" are
the same row, and one of them has a stray space before the colon in the real
document this was built against.
"""

from __future__ import annotations

import io
import re

# --------------------------------------------------------------------------
# Reading text out of the four formats the intake accepts.
# --------------------------------------------------------------------------


def _docx_rows(document) -> list[tuple[str, str]]:
    """Every table row as (label, value).

    python-docx repeats a merged cell's text once per underlying grid column,
    so a 2-column row in a table whose widest row has 4 columns comes back as
    ['Label','Label','Value','Value']. Collapsing consecutive duplicates
    restores what a reader sees.
    """
    rows: list[tuple[str, str]] = []
    for table in document.tables:
        for row in table.rows:
            cells: list[str] = []
            for cell in row.cells:
                text = cell.text.strip()
                if not cells or cells[-1] != text:
                    cells.append(text)
            cells = [c for c in cells if c]
            if len(cells) >= 2:
                rows.append((cells[0], " ".join(cells[1:])))
            elif len(cells) == 1:
                rows.append((cells[0], ""))
    return rows


def document_text(filename: str, raw: bytes) -> str:
    """Plain text for a document, INCLUDING table content.

    Tables are rendered as "Label: value" lines, which is both what the form
    means and the shape an extractor reads best.
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext == "pdf":
        from pypdf import PdfReader

        return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(raw)).pages)

    if ext == "docx":
        import docx

        document = docx.Document(io.BytesIO(raw))
        parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
        for label, value in _docx_rows(document):
            parts.append(f"{label}: {value}" if value else label)
        return "\n".join(parts)

    if ext in ("xlsx", "xlsm"):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
        return "\n".join(
            " | ".join(str(c) for c in row if c is not None)
            for ws in wb.worksheets
            for row in ws.iter_rows(values_only=True)
        )

    if ext == "doc":
        # Legacy Word. The intake advertises .docx/.pdf/.xlsx, so rather than
        # shell out to antiword here, say plainly what to do — a silent empty
        # extraction is what caused this whole class of bug.
        raise ValueError(
            "legacy .doc is not readable here — re-save the file as .docx and upload it again"
        )

    raise ValueError(f"unsupported file type '.{ext}' (accepted: .docx, .pdf, .xlsx)")


def document_rows(filename: str, raw: bytes) -> list[tuple[str, str]]:
    """(label, value) pairs when the format has them, else []."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext != "docx":
        return []
    import docx

    return _docx_rows(docx.Document(io.BytesIO(raw)))


# --------------------------------------------------------------------------
# Label -> field. One AAN row can answer a question in BOTH flows (the NIT
# flow's aan_* pre-fill fields and the AAN sub-flow's aan_q_* fields), so a
# label maps to a list of keys and fills every one of them.
# --------------------------------------------------------------------------
AAN_LABELS: dict[str, list[str]] = {
    "file no": ["aan_file_number", "aan_q_file_no"],
    "pr no": ["aan_pr_number", "aan_q_pr_no"],
    "value rs": ["aan_q_value"],
    "value": ["aan_q_value"],
    "category": ["aan_category", "aan_q_category"],
    "nature of contract": ["aan_q_nature_of_contract"],
    "tender type": ["aan_tender_type_extracted", "aan_q_tender_type"],
    "name of proposal": ["aan_name_of_proposal", "aan_q_name_of_proposal"],
    "justificationreason for procurement": ["aan_q_justification"],
    "justification reason for procurement": ["aan_q_justification"],
    "urgency": ["aan_q_urgency"],
    "state reasons": ["aan_q_urgency_reason"],
    "wbs": ["aan_q_wbs"],
    "cost center": ["aan_q_cost_center"],
    "cost centre": ["aan_q_cost_center"],
    "internal order": ["aan_q_internal_order"],
    "type of budget": ["aan_q_type_of_budget"],
    "basis of estimate": ["aan_q_basis_of_estimate"],
    "payment terms": ["aan_q_payment_terms"],
    "contract closing is required": ["aan_q_contract_closing_required"],
    "pr monitored by maint planning": ["aan_q_pr_monitored_by_maint_planning"],
    "additional remarks if any": ["aan_q_additional_remarks"],
    "engineerincharge": ["aan_q_engineer_in_charge"],
    "engineer in charge": ["aan_q_engineer_in_charge"],
    "approving authority": ["aan_q_approving_authority"],
    "department": ["aan_q_department"],
    "reason compulsory ptrpartial ptr": ["aan_q_ptr_reason"],
    "reason compulsory ptr partial ptr": ["aan_q_ptr_reason"],
    "contract period": ["aan_q_contract_period"],
    "contract duration": ["aan_q_contract_duration"],
    "defect liability period": ["aan_q_defect_liability_period"],
    "delivery period": ["aan_q_contract_period"],
    "reasons if item cannot be split to multiparty": ["aan_q_reasons_cannot_split"],
    "prebid meet required": ["aan_q_pre_bid_meet_required"],
    "pre bid meet required": ["aan_q_pre_bid_meet_required"],
    "pqc for open tenders": ["pqc_text"],
    "pre qualification criteria": ["pqc_text"],
}

# Values that mean "this row was left blank on the form". Treated as absent
# rather than as an answer, so the wizard still asks.
_EMPTY = {"", "-", "na", "n/a", "nil", "none", "not applicable",
          "option will appear on selection", "refer annexure"}

# Answers the flows validate against a fixed option list. A form says "Open
# Tender" where the dropdown says "Open"; an unmapped value would be rejected
# by flow_engine.validate_answer and re-asked, which is the bug this whole
# module exists to stop.
_NORMALIZE: dict[str, dict[str, str]] = {
    "aan_tender_type_extracted": {},   # free text in the NIT flow
    "aan_q_tender_type": {
        "open tender": "Open", "open": "Open",
        "limited tender": "Limited", "limited": "Limited",
        "oem": "OEM", "proprietary": "Proprietary",
        "emergency": "Emergency", "single tender": "Proprietary",
    },
    "aan_q_category": {
        "supply": "Goods", "goods": "Goods", "purchase": "Goods",
        "works": "Works", "work": "Works",
        "services": "Services", "service": "Services",
    },
    "aan_category": {},                # free text in the NIT flow
    "aan_q_nature_of_contract": {
        "amc": "AMC", "rate contract": "Rate Contract",
        "amc /rate contract": "AMC", "amc/rate contract": "AMC",
    },
    "aan_q_type_of_budget": {
        "revenue": "Revenue", "capital": "Capital",
    },
    "aan_q_department": {
        "maintenance instrumentation": "Instrumentation Maintenance",
        "instrumentation maintenance": "Instrumentation Maintenance",
        "maintenance mechanical": "Mechanical", "mechanical": "Mechanical",
        "maintenance civil": "Civil", "civil": "Civil",
        "operations": "Operations", "operation": "Operations",
        "inspection": "Inspection",
        "corporate affairs": "Corporate Affairs",
    },
}

_YES_NO_FIELDS = {
    "aan_q_urgency", "aan_q_contract_closing_required",
    "aan_q_pr_monitored_by_maint_planning", "aan_q_pre_bid_meet_required",
    "aan_q_ptr_applicable", "aan_q_labour_component_applicable",
    "aan_q_security_deposit_applicable", "aan_q_power_water_free",
    "aan_q_nrl_will_issue_material", "aan_q_has_reference_order",
    "aan_q_has_previous_reference", "aan_q_can_split_multi_party",
}


def _fold(label: str) -> str:
    """'Tender Type :' and 'tender-type' fold to the same key."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", label.lower())).strip()


def _normalize(key: str, value: str) -> str | None:
    text = " ".join(value.split())
    if text.lower() in _EMPTY:
        return None
    if key in _YES_NO_FIELDS:
        head = text.lower()
        if head.startswith("y"):
            return "Yes"
        if head.startswith("n"):
            return "No"
        return None
    table = _NORMALIZE.get(key)
    if table:
        mapped = table.get(text.lower())
        if mapped:
            return mapped
        # An unmapped value would be rejected by the flow's option check and
        # re-asked anyway; leaving it out asks ONE question instead of showing
        # the user a value that then gets refused.
        return None
    return text


def extract_fields(filename: str, raw: bytes, text: str = "") -> dict[str, str]:
    """Fields read straight off the form, no model involved.

    Works from (label, value) rows when the upload is a .docx, and falls back
    to "Label: value" lines for a PDF or a spreadsheet.
    """
    pairs = document_rows(filename, raw)
    if not pairs:
        pairs = [
            (line.split(":", 1)[0], line.split(":", 1)[1])
            for line in (text or "").splitlines()
            if ":" in line
        ]

    out: dict[str, str] = {}
    for label, value in pairs:
        keys = AAN_LABELS.get(_fold(label))
        if not keys:
            continue
        for key in keys:
            if key in out:
                continue
            cleaned = _normalize(key, value)
            if cleaned:
                out[key] = cleaned
    return out
