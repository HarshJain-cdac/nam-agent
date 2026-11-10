"""The NIT (Notice Inviting Tender) for a case, built from NRL's template, as a PDF.

The template is nit-forge-ui/src/lib/nit-template.ts, ported row for row: the
same header, the same four sections in the same order, the same row labels
and the same standing-policy wording. That template was reconstructed from
one issued NRL notice (Goods, CPPP, 23.08.2024, PUR-SMT-1008283); one issued
NIT is precedent, not an approved blank, so the provenance prints on the
document itself.

The values come from the case (handoff.payload): the subject, the category,
the approval file, the contract period, the selected PQC clauses and the
items. A row the case does not answer prints [TO BE FILLED], never invented
text: a gap gets filled by a person, a plausible invention gets published.
A row the case leaves empty is suggested, when it can be, from the case's
past tenders in the KG (PQC and SCC wording, the past EMD as a reference),
printed blue with the tender it came from (suggestions()). The KG being down
leaves those rows as gaps; the NIT is still made.
The items go in an annexure with quantity and unit only; the estimate's rates
are internal and never printed.

A NIT is made only on the user's final yes. The chat tool works in two steps:
called without confirm=true it reports what the NIT will contain and has the
model ask "Generate the NIT now?" with ask_options; only after that preview
does confirm=true generate it. A case not yet handed off to NIT (S10) gets a
DRAFT banner instead of passing as the issued notice.

The PDF is written here with the standard library (Helvetica, A4), so no
dependency is added.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
import zlib

from nam_agent.tools.registry import Tool

TITLE = "Domestic Competitive Bidding - Notice Inviting Tender (NIT)"
PROVENANCE = (
    "Template reconstructed from an issued NRL notice: Tender No OC13000254/BIP "
    "(PUR-SMT-1008283), published 23.08.2024 on CPPP. One issued NIT is precedent, not an "
    "approved blank template: a Works, Services or GeM notice may legitimately differ.")
GAP = "[TO BE FILLED]"
YES = "Yes, generate the NIT"

_NOUN = {"Goods": "suppliers", "Works": "contractors", "Services": "contractors"}

# (label, field key) or (label, None, fixed text) or (label, derive function)
HEADER = (("Subject", "aan_name_of_proposal"), ("E-File Ref", "aan_file_number"),
          ("Tender No", "tender_no"))


def _period(f):
    value, unit = f.get("contract_period_value"), f.get("contract_period_unit")
    if not value:
        return None
    return f"The contract duration shall be {value}{' ' + unit if unit else ''}."


def _dlp(f):
    if f.get("dlp_applicable") == "No":
        return "Not Applicable"
    return f.get("dlp_value") or None


def _pre_bid(f):
    return "NA" if f.get("pre_bid_meeting_requirement") == "Not Applicable" else None


def _portal_text(site):
    return ("Only online offer shall be considered against the subject enquiry. For details, "
            f"please visit {site}. This Tender is being conducted in e-tendering mode and the "
            "Bid documents can be downloaded for ONLINE bidding by the bidders with Digital "
            "Signature Certificate.")


def _mode(f):
    portal = f.get("nit_publishing_portal")
    if portal == "GeM":
        return _portal_text("the Government e-Marketplace portal https://gem.gov.in")
    if portal == "CPPP":
        return _portal_text("our e-tendering portal http://eprocure.gov.in/eprocure/app")
    return None


SECTIONS = (
    ("Tender Schedule", (
        ("Tender Publish Date", "tender_publish_date"),
        ("Tender documents download start date & time", "tender_download_start"),
        ("Pre-Bid Meeting Date & Time", _pre_bid),
        ("Bid Submission start date & time", "bid_submission_start"),
        ("Bid Submission end date & time", "bid_submission_end"),
        ("Bid opening date & time (Technical Bid)", "bid_opening_date"),
    )),
    ("Tender Information Summary (TIS)", (
        ("The E-Bid to be submitted in Portal in Two Parts", None,
         "Part A - Techno-commercial / Unpriced Bid\nPart B - Price Bid"),
        ("Mode of submission", _mode),
        ("Contractual Period", _period),
        ("Earnest Money Deposit (EMD)", "emd"),
        ("Holiday Listing", None,
         ("Bidders who are on holiday list by NRL or MoPNG (Ministry of Petroleum & Natural "
         "Gas) will not be considered. Accordingly, the bidder shall submit a self-declaration "
         "as per the format enclosed with Tender annexures. It may be noted that if this "
         "declaration is found to be false, NRL shall have the right to reject the bid and/or "
         "take further action as deemed fit.")),
    )),
    ("Pre-Qualification Criteria (PQC)", (
        ("Pre-Qualification Criteria (PQC)", "pqc_text"),
        ("PTR (Past Track Record) relaxation for MSEs / Startups", "ptr_relaxation_mse_startup"),
        ("MSE Bidders", None,
         ("Purchase preference to Indian MSEs (Micro & Small Enterprises) shall be applicable "
         "as per the Govt. of India Policies. Refer details in ATC Annexure.")),
        ("PP-LC Policy", None, "Applicable"),
        ("Procurement from a bidder from a country which shares a Land Boundary with India",
         None,
         ("The Department of Expenditure (DOE) (Ministry of Finance) of the Govt. of India "
         "through OMs no. 6/18/2019-PPD dated 23rd July and 24th July 2020 has issued "
         "guidelines regarding procurement from bidders from a country or countries which "
         "share a land boundary with India. The detailed guidelines are available on the said "
         "OMs and the bidder shall comply with the same, including the registration "
         "requirement with the Competent Authority where applicable.")),
        ("Documents to be uploaded with Un-Priced / Techno-Commercial Bid", None,
         ("Documents in support of PQC.\n"
         "Complete Technical terms and conditions: duly sealed & signed on each page, as a "
         "token of acceptance.\n"
         "SOR enclosed along with Technical Specification mentioning Quoted/Unquoted against "
         "each item: duly sealed & signed on each page.\n"
         "Agreed Terms & Conditions (ATC) duly filled in, sealed and signed.")),
        ("Documents to be submitted with Price Bid", None,
         ('BoQ / Priced Bid in "XLS" format as per instructions provided in the e-tendering '
         "portal.")),
        ("Evaluation of Priced Offer", "method_of_evaluation"),
        ("General Instructions", None,
         ("NRL adopts a practice to verify documents submitted by the L1 (successful) bidder in "
         "support of the bidder's credentials against PQC. Authentication may be verified with "
         "the issuing authorities, by way of direct communication to NRL over official e-mail "
         "IDs or an original letter of authentication by post. Bidders are advised to ensure "
         "that documents submitted are authentic and verifiable.")),
    )),
    ("Commercial Terms", (
        ("Reimbursement", "reimbursement"),
        ("Consortium Agreement", "consortium_agreement"),
        ("Multiple Item Split", "multiple_item_split"),
        ("Contract Performance Security", "contract_performance_security"),
        ("Security Deposit", "security_deposit"),
        ("Advance Payment", "advance_payment"),
        ("Milestone Payment", "milestone_payment"),
        ("Pre-Bid Meeting Requirement", "pre_bid_meeting_requirement"),
        ("Defect Liability Period", _dlp),
        ("Rates Category", "rates_category"),
        ("Group / Item Structure", "group_structure"),
        ("L1 Evaluation Details", "l1_evaluation_details"),
    )),
)
CONTACTS = (("Technical Correspondence", "technical"), ("Commercial Correspondence", "commercial"))
CONTACT_ROWS = (("Name", "name"), ("Designation", "designation"), ("Email", "email"),
                ("Mobile", "mobile"))


# -- the case's values -------------------------------------------------------------

def fields_for(need) -> tuple[dict, dict]:
    """(the template's field values, the handoff data) for one case."""
    from nam_agent.nam import orchestrator
    from nam_agent.nam.playbooks import handoff

    wf = orchestrator.workflow(need)
    data = handoff.payload(need, wf)
    case_fields = {k: str(v).strip() for k, v in (data.get("fields") or {}).items()
                   if v not in (None, "")}
    category = (data.get("category") or (need.category or {}).get("value") or "").title()
    out = {"aan_name_of_proposal": data.get("subject") or "",
           "aan_file_number": (data.get("approval") or {}).get("file") or "",
           "tender_category": category}
    period = next((case_fields[k] for k in ("contract_period", "completion_period",
                                            "delivery_period") if case_fields.get(k)), "")
    out["contract_period_value"] = period or (data.get("term") or "")
    tender_type = str(data.get("tender_type") or "")
    if re.search(r"\bgem\b", tender_type, re.IGNORECASE):
        out["nit_publishing_portal"] = "GeM"
    elif re.search(r"\b(cppp|e-?tender\w*)\b", tender_type, re.IGNORECASE):
        out["nit_publishing_portal"] = "CPPP"
    docs = (orchestrator._artifact(wf, "documents") or {}).get("documents") or []
    pqc = [c["text"] for d in docs if d.get("kind") == "pqc"
           for c in d.get("clauses") or [] if c.get("selected") and c.get("text")]
    if pqc:
        out["pqc_text"] = "\n".join(f"{i}. {t}" for i, t in enumerate(pqc, 1))
    out.update({k: v for k, v in case_fields.items() if k not in out or not out[k]})
    return {k: v for k, v in out.items() if v}, data


# -- suggestions from the KG -----------------------------------------------------
# A row the case leaves empty can be suggested from NRL's past tenders: the
# case's selected precedents (or, until one is selected, its closest related
# tenders), read from the KG's /api/kg/case. The text is the past tender's own
# wording, and it prints as a suggestion with its source, never as this
# tender's answer: a past EMD is a reference, not this tender's EMD.

# row label -> (where to look, pattern a sentence must match)
_FROM_CLAUSES = {
    "Pre-Qualification Criteria (PQC)": ("pqc", None),
    "PTR (Past Track Record) relaxation for MSEs / Startups":
        ("pqc scc", (r"\b(mse|start-?ups?)\b.{0,120}\b(relax|exempt)|\b(relax|exempt)\w*\b.{0,120}"
                    r"\b(mse|start-?ups?)\b")),
    "Contractual Period": ("scc pqc", (r"\bcontract(ual)?\s+(period|duration)\b.{0,80}"
                                      r"\b(\d+|one|two|three|four|five|six)\s*\(?\w*\)?\s*"
                                      r"(years?|months?|days?)\b")),
    "Contract Performance Security": ("scc", r"\bperformance\s+(security|bank\s+guarantee)\b"),
    "Security Deposit": ("scc", r"\bsecurity\s+deposit\b"),
    "Advance Payment": ("scc", r"\b(advance\s+payment|mobili[sz]ation\s+advance)\b"),
    "Milestone Payment": ("scc", r"\b(milestone|terms\s+of\s+payment|payment\s+terms?)\b"),
    "Defect Liability Period": ("scc", r"\b(defect\s+liability|guarantee\s+period|warranty\s+period)\b"),
    "Consortium Agreement": ("scc pqc", r"\b(consortium|joint\s+venture)\b"),
    "Reimbursement": ("scc", r"\breimburs"),
    "Evaluation of Priced Offer": ("scc pqc", r"\b(evaluat\w+)\b.{0,80}\b(price|l-?1|lowest)\b"),
    "L1 Evaluation Details": ("scc pqc", r"\bl-?1\b|\blowest\s+(evaluated|bidder|price)"),
}
_KG_TTL = 600
_kg_cache: dict = {}


# "Please refer Tender documents." says where the clause is, not what it says
_POINTER = re.compile(r"^\W*(please\s+)?refer\b.{0,40}$", re.IGNORECASE | re.DOTALL)
_FILLER = re.compile(r"\bnoted\s*(&|and)\s*confirmed\b", re.IGNORECASE)


def _sentences(text: str, pattern: str) -> str:
    """The sentence that answers the row, in the past tender's words. A
    heading ("PAYMENT TERMS:") takes the text under it; a long run (a table
    row read as one line) is cut to the words around the match."""
    parts = [p.strip() for p in re.split(r"(?<=[.;])\s+|\n+", _FILLER.sub(" ", text))]
    parts = [re.sub(r"\s+", " ", p) for p in parts if p]
    for i, part in enumerate(parts):
        m = re.search(pattern, part, re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        if part.endswith(":") and i + 1 < len(parts):
            part = f"{part} {parts[i + 1]}"
            m = re.search(pattern, part, re.IGNORECASE | re.DOTALL)
        # a table row read as one line ("... above E DEFECT LIABILITY PERIOD: ...")
        # stops at the next lettered heading after the match
        nxt = re.search(r"\s[A-Z]\s+[A-Z][A-Z ,&/]{4,}:", part[m.end():])
        if nxt:
            part = part[:m.end() + nxt.start()]
        if m.start() > 60:                        # start at the words that answer the row
            part = "..." + part[part.rfind(" ", 0, m.start()) + 1:]
        if len(part) > 400:
            end = part.find(" ", 380)
            part = part[:end if end > 0 else None] + ("..." if end > 0 else "")
        if len(part) >= 20:
            return part[:500]
    return ""


def _none_said(value: str) -> bool:
    return re.fullmatch(r"(nil|na|n/?a|none|not\s+applicable|0+(\.0+)?|-)", value.strip(),
                        re.IGNORECASE) is not None


def _past_tenders(need, kg_url: str) -> list[dict]:
    """The precedents' records from the KG, best first; [] when it cannot be read."""
    from nam_agent.nam import kg_tools

    keys = list(need.selected_cases or need.related_cases or [])[:3]
    if not kg_url or not keys:
        return []
    import time

    cached = _kg_cache.get((kg_url, tuple(keys)))
    if cached and time.monotonic() - cached[0] < _KG_TTL:
        return cached[1]
    out, failed = [], False
    for key in keys:
        try:
            out.append(kg_tools._get(kg_url, "/api/kg/case", {"case_key": key, "clause_limit": 60,
                                                              "item_limit": 1}))
        except Exception:  # noqa: BLE001 - the KG down leaves gaps, never a failed NIT
            failed = True
    if not failed:          # an outage is not remembered: the next read tries again
        _kg_cache[(kg_url, tuple(keys))] = (time.monotonic(), out)
    return out


def suggestions(need, kg_url: str) -> dict[str, dict]:
    """row label -> {"text", "source"}, the first past tender that answers it."""
    found: dict[str, dict] = {}
    for past in _past_tenders(need, kg_url):
        key = past.get("case_key") or ""
        source = key + (f" (tender {past['tender_no']})" if past.get("tender_no") else "")
        for label, (kinds, pattern) in _FROM_CLAUSES.items():
            if label in found:
                continue
            for kind in kinds.split():
                clauses = [c.get("text") or "" for c in past.get(kind) or []
                           if len((c.get("text") or "").strip()) >= 20
                           and not _POINTER.search(c.get("text") or "")]
                if pattern is None:
                    seen, kept = set(), []
                    for c in clauses:
                        head = re.sub(r"\W+", " ", c.lower())[:80]
                        if head not in seen:
                            seen.add(head)
                            kept.append(c.strip())
                    text = ""
                    for c in kept[:6]:                    # whole clauses, up to ~1500 chars
                        if text and len(text) + len(c) > 1500:
                            break
                        text = f"{text}\n{c}" if text else c[:1500]
                else:
                    text = next((t for t in (_sentences(c, pattern) for c in clauses) if t), "")
                if text:
                    found[label] = {"text": text, "source": source}
                    break
        emd = str(past.get("emd_amount") or "").strip()
        if emd and "Earnest Money Deposit (EMD)" not in found:
            found["Earnest Money Deposit (EMD)"] = {
                "text": ("Nil" if _none_said(emd) else emd)
                + " in the past tender; set this tender's EMD", "source": source}
        pre = str(past.get("pre_bid_meeting_date") or "").strip()
        if pre and "Pre-Bid Meeting Requirement" not in found:
            found["Pre-Bid Meeting Requirement"] = {
                "text": "Not Applicable" if _none_said(pre) else f"Held in the past tender ({pre})",
                "source": source}
    return found


# -- the user's answers and edits ---------------------------------------------------
# What the user answers or changes in the chat (nit_set_field), or accepts from
# the KG on the sheet, is kept per case and row here, apart from the case
# record: the NIT's own entries (tender number, dates, contacts) are not part
# of the need. A stored value wins over the case and over a suggestion.

def ensure(conn: sqlite3.Connection) -> None:
    from nam_agent import db

    db.once(conn, "nam.nit", _setup, table="nam_nit_fields")


def _setup(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS nam_nit_fields (case_id TEXT NOT NULL, "
                 "row_id TEXT NOT NULL, value TEXT NOT NULL, source TEXT, "
                 "updated_at TEXT NOT NULL, PRIMARY KEY (case_id, row_id))")
    conn.commit()


def stored(conn, case_id: str) -> dict[str, dict]:
    if conn is None:
        return {}
    ensure(conn)
    return {r[0]: {"value": r[1], "source": r[2]} for r in conn.execute(
        "SELECT row_id, value, source FROM nam_nit_fields WHERE case_id = ?", (case_id,))}


def save(conn, case_id: str, row_id: str, value: str, source: str | None = None) -> None:
    from datetime import UTC, datetime

    ensure(conn)
    with conn:
        conn.execute("INSERT INTO nam_nit_fields VALUES (?, ?, ?, ?, ?) ON CONFLICT(case_id, "
                     "row_id) DO UPDATE SET value = excluded.value, source = excluded.source, "
                     "updated_at = excluded.updated_at",
                     (case_id, row_id, value.strip()[:4000], source,
                      datetime.now(UTC).isoformat(timespec="seconds")))


def _row_id(label: str, source) -> str:
    return source if isinstance(source, str) else re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def build(need, kg_url: str = "", conn=None) -> dict:
    """The NIT as plain data. Each asked row has an id, a kind and a value:
    filled (its text: from the case, or set by the user), suggested ({"text",
    "source"} from a past tender in the KG) or missing (None). Fixed rows are
    NRL's standing wording."""
    fields, data = fields_for(need)
    suggested = suggestions(need, kg_url)
    mine = stored(conn, need.need_id)

    def asked_row(label, row_id, value, hint_label=None):
        if row_id in mine:
            got = mine[row_id]
            return {"id": row_id, "label": label, "kind": "filled", "value": got["value"],
                    "source": got["source"] or "you", "fixed": False}
        if value:
            return {"id": row_id, "label": label, "kind": "filled", "value": value,
                    "source": "case", "fixed": False}
        hint = suggested.get(hint_label or label)
        if hint:
            return {"id": row_id, "label": label, "kind": "suggested", "value": hint,
                    "source": hint["source"], "fixed": False}
        return {"id": row_id, "label": label, "kind": "missing", "value": None,
                "source": None, "fixed": False}

    def row(spec):
        label, source = spec[0], spec[1]
        if source is None:
            return {"id": _row_id(label, None), "label": label, "kind": "fixed",
                    "value": spec[2], "source": "NRL standing wording", "fixed": True}
        value = source(fields) if callable(source) else fields.get(source)
        return asked_row(label, _row_id(label, source), value)

    category = fields.get("tender_category") or "Goods"
    sections = [{"title": title, "rows": [row(s) for s in rows]} for title, rows in SECTIONS]
    contacts = [{"title": title, "rows": [
        asked_row(label, f"{key}_contact_{k}", fields.get(f"{key}_contact_{k}"))
        for label, k in CONTACT_ROWS]} for title, key in CONTACTS]
    for block in contacts:
        for r in block["rows"]:
            r["full_label"] = f"{block['title']} - {r['label']}"
    header = [row(s) for s in HEADER]
    asked = [r for r in header + [r for s in sections + contacts for r in s["rows"]]
             if not r["fixed"]]
    items = [{"item": x.get("item") or "", "qty": x.get("qty"), "uom": x.get("uom") or ""}
             for x in data.get("items") or []]
    if not items and data.get("subject"):
        # no estimate yet: the need as stated is the one line, its quantity as given
        items = [{"item": data["subject"], "qty": need.quantity or None, "uom": ""}]
    missing = [r for r in asked if r["kind"] == "missing"]
    pending = [r for r in asked if r["kind"] == "suggested"]
    return {
        "case_id": need.need_id, "title": TITLE, "provenance": PROVENANCE,
        "handed_off": _handed_off(need),
        "intro": ("Numaligarh Refinery Limited (NRL) invites e-bids from competent and "
                  f"experienced Indian {_NOUN.get(category, 'suppliers')} for the following "
                  "under two bid system (Part-I: Techno-commercial part and Part-II: Price "
                  "Part) with sound technical and financial capabilities fulfilling the "
                  "Pre-Qualification Criteria (PQC) of the tender document."),
        "header": header, "sections": sections, "contacts": contacts, "items": items,
        "filled": sum(1 for r in asked if r["kind"] == "filled"),
        "suggested": len(pending), "total": len(asked),
        "missing": [r.get("full_label") or r["label"] for r in missing],
        "sources": sorted({r["source"] for r in pending}),
        # ready to issue: nothing missing and every suggestion accepted or changed
        "ready": not missing and not pending,
    }


def rows_of(nit: dict) -> list[dict]:
    """Every row the user can answer, in the order the notice prints them."""
    return [r for r in nit["header"] + [r for s in nit["sections"] + nit["contacts"]
                                        for r in s["rows"]] if not r["fixed"]]


def find_row(nit: dict, field: str) -> dict | None:
    """A row by its id or its label, as the user or the model names it."""
    want = re.sub(r"[^a-z0-9]+", " ", field.lower()).strip()
    rows = rows_of(nit)
    for r in rows:
        names = {r["id"].replace("_", " "), re.sub(r"[^a-z0-9]+", " ", r["label"].lower()).strip(),
                 re.sub(r"[^a-z0-9]+", " ", (r.get("full_label") or "").lower()).strip()}
        if want in names:
            return r
    hits = [r for r in rows if want and want in re.sub(r"[^a-z0-9]+", " ",
                                                        (r.get("full_label") or r["label"]).lower())]
    return hits[0] if len(hits) == 1 else None


def _handed_off(need) -> bool:
    from nam_agent.nam import orchestrator

    return "handoff_nit" in orchestrator.workflow(need)["actions"]


# -- the PDF -----------------------------------------------------------------------
# Helvetica's advance widths (per 1000 em) for printable ASCII, from its AFM.
_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
      *[556] * 10, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778,
      722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667,
      667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222,
      222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500,
      334, 260, 334, 584]
_SWAP = {"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"',
         "”": '"', "₹": "Rs.", "•": "-", " ": " ", "…": "..."}
PAGE_W, PAGE_H, MARGIN = 595, 842, 48


def _ascii(text: str) -> str:
    text = "".join(_SWAP.get(ch, ch) for ch in str(text))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return "".join(ch if 32 <= ord(ch) < 127 or ch == "\n" else " " for ch in text)


def _width(text: str, size: float, bold: bool = False) -> float:
    w = sum(_W[ord(ch) - 32] for ch in text if 32 <= ord(ch) < 127) * size / 1000
    return w * 1.08 if bold else w   # Helvetica-Bold runs about 8% wider


def _wrap(text: str, width: float, size: float, bold: bool = False) -> list[str]:
    lines = []
    for para in _ascii(text).split("\n"):
        line = ""
        for word in para.split(" "):
            while _width(word, size, bold) > width:          # a word wider than the cell
                cut = len(word)
                while cut > 1 and _width(word[:cut], size, bold) > width:
                    cut -= 1
                if line:
                    lines.append(line)
                    line = ""
                lines.append(word[:cut])
                word = word[cut:]
            trial = f"{line} {word}" if line else word
            if _width(trial, size, bold) <= width:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class _Pdf:
    """Pages of drawing operators, written out as a PDF 1.4 file."""

    def __init__(self):
        self.pages: list[list[str]] = []
        self.new_page()

    def new_page(self):
        self.pages.append([])
        self.y = PAGE_H - MARGIN

    def text(self, x, y, s, size=9, bold=False, grey=0.0, red=False, blue=False):
        colour = ("0.70 0.11 0.11 rg" if red else "0.11 0.31 0.85 rg" if blue
                  else f"{grey:.2f} g")
        self.pages[-1].append(f"BT {colour} /{'F2' if bold else 'F1'} {size} Tf "
                              f"{x:.1f} {y:.1f} Td ({_esc(s)}) Tj ET")

    def rect(self, x, y, w, h, fill=None):
        op = f"{fill:.2f} g {x:.1f} {y:.1f} {w:.1f} {h:.1f} re f" if fill is not None else \
            f"0.55 G 0.5 w {x:.1f} {y:.1f} {w:.1f} {h:.1f} re S"
        self.pages[-1].append(op)

    def need(self, h):
        if self.y - h < MARGIN + 20:
            self.new_page()

    def build(self) -> bytes:
        n = len(self.pages)
        objs = ["<< /Type /Catalog /Pages 2 0 R >>",
                "<< /Type /Pages /Kids [" + " ".join(f"{5 + 2 * i} 0 R" for i in range(n))
                + f"] /Count {n} >>",
                ("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                "/Encoding /WinAnsiEncoding >>"),
                ("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                "/Encoding /WinAnsiEncoding >>")]
        streams = {}
        for i, ops in enumerate(self.pages):
            ops = ops + [(f"BT 0.40 g /F1 8 Tf {PAGE_W - MARGIN - 60:.1f} 24 Td "
                         f"(Page {i + 1} of {n}) Tj ET")]
            objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                        f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                        f"/Contents {6 + 2 * i} 0 R >>")
            body = zlib.compress("\n".join(ops).encode("latin-1"))
            streams[len(objs)] = body
            objs.append(None)
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for num, obj in enumerate(objs, 1):
            offsets.append(len(out))
            if obj is None:
                body = streams[num - 1]
                out += (f"{num} 0 obj\n<< /Length {len(body)} /Filter /FlateDecode >>\n"
                        "stream\n").encode() + body + b"\nendstream\nendobj\n"
            else:
                out += f"{num} 0 obj\n{obj}\nendobj\n".encode()
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
        out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref}\n%%EOF\n").encode()
        return bytes(out)


def _table(pdf: _Pdf, widths, rows, header=None, size=9):
    """Rows of cells; a cell is text, None for a gap, or a KG suggestion
    {"text", "source"} (printed blue with its source). Splits across pages."""
    lead = size + 3
    x0 = MARGIN

    def shown(c):
        if c is None:
            return GAP
        if isinstance(c, dict):
            return f"{c['text']}\n(Suggested from {c['source']} in the KG; check before use.)"
        return c

    for r_i, cells in enumerate(([header] if header else []) + list(rows)):
        is_head = header is not None and r_i == 0
        wrapped = [_wrap(shown(c), w - 8, size, is_head or i == 0 and len(widths) == 2)
                   for i, (c, w) in enumerate(zip(cells, widths, strict=True))]
        start = 0
        total = max(len(w) for w in wrapped)
        while start < total:
            room = int((pdf.y - MARGIN - 20 - 6) // lead)
            if room < 1:
                pdf.new_page()
                continue
            take = min(room, total - start)
            h = take * lead + 6
            x = x0
            for i, (lines, w) in enumerate(zip(wrapped, widths, strict=True)):
                if is_head or (i == 0 and len(widths) == 2):
                    pdf.rect(x, pdf.y - h, w, h, fill=0.93)
                pdf.rect(x, pdf.y - h, w, h)
                for k, line in enumerate(lines[start:start + take]):
                    pdf.text(x + 4, pdf.y - 3 - size - k * lead, line, size,
                             bold=is_head or (i == 0 and len(widths) == 2),
                             red=cells[i] is None, blue=isinstance(cells[i], dict))
                x += w
            pdf.y -= h
            start += take


def _heading(pdf: _Pdf, title: str):
    pdf.need(60)
    pdf.y -= 10
    pdf.rect(MARGIN, pdf.y - 18, PAGE_W - 2 * MARGIN, 18, fill=0.15)
    pdf.text(MARGIN + 6, pdf.y - 13, _ascii(title), 10.5, bold=True, grey=1.0)
    pdf.y -= 18


def _para(pdf: _Pdf, text: str, size=9, bold=False, grey=0.0, center=False):
    width = PAGE_W - 2 * MARGIN
    for line in _wrap(text, width, size, bold):
        pdf.need(size + 4)
        x = MARGIN + (width - _width(line, size, bold)) / 2 if center else MARGIN
        pdf.text(x, pdf.y - size, line, size, bold, grey)
        pdf.y -= size + 4


def pdf(nit: dict) -> bytes:
    doc = _Pdf()
    full = PAGE_W - 2 * MARGIN
    _para(doc, "NUMALIGARH REFINERY LIMITED", 13, bold=True, center=True)
    _para(doc, nit["title"], 11, bold=True, center=True)
    if not nit.get("ready"):
        _para(doc, f"DRAFT - {len(nit['missing'])} entries missing, {nit.get('suggested', 0)} "
                   "suggestions not confirmed; not for issue.", 9, bold=True, grey=0.35,
              center=True)
    if not nit["handed_off"]:
        _para(doc, "The case is not yet handed off to the NIT module (S10).", 8, grey=0.35,
              center=True)
    doc.y -= 6
    _table(doc, (150, full - 150), [(r["label"], r["value"]) for r in nit["header"]])
    doc.y -= 8
    _para(doc, nit["intro"])
    for section in nit["sections"]:
        _heading(doc, section["title"])
        _table(doc, (170, full - 170), [(r["label"], r["value"]) for r in section["rows"]])
    _heading(doc, "Contacts")
    for block in nit["contacts"]:
        _table(doc, (170, full - 170), [(block["title"], "")] +
               [(r["label"], r["value"]) for r in block["rows"]])
    _heading(doc, "Annexure - Schedule of items")
    if nit["items"]:
        _table(doc, (30, full - 150, 60, 60),
               [(str(i), x["item"], "" if x["qty"] is None else f"{x['qty']:g}"
                 if isinstance(x["qty"], (int, float)) else str(x["qty"]), x["uom"])
                for i, x in enumerate(nit["items"], 1)],
               header=("#", "Item", "Qty", "Unit"))
    else:
        _table(doc, (full,), [(None,)])
    doc.y -= 10
    _para(doc, f"Entries answered from the case: {nit['filled']} of {nit['total']}; "
               f"suggested from past tenders in the KG (blue): {nit.get('suggested', 0)}; "
               f"marked {GAP}: {len(nit['missing'])}. Suggestions and gaps need a person to "
               "confirm or fill them before publication.", 8, grey=0.35)
    _para(doc, nit["provenance"], 8, grey=0.35)
    return doc.build()


# -- the chat tools ----------------------------------------------------------------
# generate_nit makes the NIT on the user's final yes and opens it beside the
# chat; nit_set_field writes an answer or an edit into one row. After each, the
# tool names the next entry to ask about, one at a time with ask_options, so
# the model never lists several questions or invents a value.

ACCEPT_ALL = "Accept all suggestions"
ONE_BY_ONE = "Review them one by one"
ACCEPT = "Accept the suggestion"
SKIP = "Skip for now"
STOP = "Stop asking"

# choices for rows whose answers are a known few (the screen adds "Other")
_CHOICES = {
    "pre_bid_meeting_requirement": ["Applicable", "Not Applicable"],
    "consortium_agreement": ["Consortium/JV bids not accepted", "Consortium/JV bids accepted"],
    "multiple_item_split": ["Split allowed (item-wise)", "Split not allowed"],
    "advance_payment": ["Not applicable", "Applicable"],
    "reimbursement": ["Not applicable"],
    "rates_category": ["Firm rates", "Variable rates"],
    "emd": ["Nil"],
    "nit_publishing_portal": ["CPPP", "GeM"],
}


def _summary(nit: dict) -> dict:
    return {"case_id": nit["case_id"], "filled": nit["filled"], "suggested": nit["suggested"],
            "missing": len(nit["missing"]), "total": nit["total"], "items": len(nit["items"]),
            "ready": nit["ready"], "draft": not nit["ready"]}


def _ask(question: str, options: list[str]) -> str:
    return (f"Now call ask_options with question {question!r} and options "
            f"{options!r}, then stop and wait for the answer. Do not list other questions.")


def next_step(nit: dict, skipped: set[str], asked_all: bool) -> str:
    """What the model does next: one question, or tell the user it is ready."""
    case = nit["case_id"]
    if nit["ready"]:
        return (f"The NIT for {case} is complete: every entry filled. Tell the user in one "
                "sentence that Download PDF is now active on the NIT beside the chat.")
    rows = [r for r in rows_of(nit) if r["kind"] != "filled" and r["id"] not in skipped]
    if nit["suggested"] and not asked_all:
        return (f"{nit['suggested']} entries have suggestions from past tenders in the KG "
                f"(blue on the sheet). " + _ask(
                    f"Accept the {nit['suggested']} suggestions from past tenders?",
                    [ACCEPT_ALL, ONE_BY_ONE])
                + f" For the reply, call nit_set_field(need_id={case!r}, "
                  "field='all_suggestions', value=<the option the user chose>).")
    if not rows:
        left = len(nit["missing"]) + nit["suggested"]
        return (f"{left} entries are still open but the user skipped them. Tell the user in one "
                "sentence they can fill any entry by asking, e.g. 'set EMD to Nil', and that "
                "Download draft works now.")
    r = rows[0]
    name = r.get("full_label") or r["label"]
    if r["kind"] == "suggested":
        text = r["value"]["text"][:300]
        return (f"Next entry: {name}. Past tender {r['value']['source']} says: \"{text}\". "
                + _ask(f"{name}: use the past tender's wording?", [ACCEPT, SKIP, STOP])
                + f" For a reply, call nit_set_field(need_id={case!r}, field={r['id']!r}, "
                  "value=<the user's words, or 'accept' or 'skip'>).")
    return (f"Next entry: {name} (not in the case or the KG). "
            + _ask(f"What is the {name}?", [*_CHOICES.get(r["id"], []), SKIP, STOP])
            + f" For a reply, call nit_set_field(need_id={case!r}, field={r['id']!r}, "
              "value=<the user's words, or 'skip'>). Never invent the value.")


def make_tools(conn: sqlite3.Connection, kg_url: str = "") -> list[Tool]:
    previewed: set[str] = set()     # a NIT is generated only after its preview was asked
    skipped: dict[str, set[str]] = {}
    asked_all: set[str] = set()

    def load(need_id: str):
        from nam_agent.nam import need as need_store

        return need_store.load(conn, need_id.strip()) if need_id.strip() else None

    def generate_nit(need_id: str = "", confirm: bool = False, _notify=None) -> str:
        need = load(need_id)
        if need is None:
            return (f"No case {need_id!r}. Give the case id (NAM-YYYY-NNNN) from this "
                    "conversation; never invent one.")
        nit = build(need, kg_url, conn)
        if not confirm or need.need_id not in previewed:
            previewed.add(need.need_id)
            return (f"NIT for {need.need_id} is ready to generate: {nit['filled']} of "
                    f"{nit['total']} entries filled, {nit['suggested']} suggested from past "
                    f"tenders in the KG ({', '.join(nit['sources']) or 'none'}), "
                    f"{len(nit['missing'])} missing. "
                    + _ask(f"Generate the NIT for {need.need_id} now?", [YES, "Not yet"])
                    + f" Call generate_nit with confirm=true only after the user picks {YES!r}.")
        skipped[need.need_id] = set()
        if _notify:
            _notify("nit", _summary(nit))
        return (f"Generated the NIT for {need.need_id}; it is open beside the chat. Do not "
                "repeat the document. " + next_step(nit, skipped[need.need_id],
                                                     need.need_id in asked_all))

    def nit_set_field(need_id: str = "", field: str = "", value: str = "", _notify=None) -> str:
        need = load(need_id)
        if need is None:
            return f"No case {need_id!r}; use the case id from this conversation."
        nit = build(need, kg_url, conn)
        said = (value or "").strip()
        if said == ONE_BY_ONE:
            asked_all.add(need.need_id)
        elif said == ACCEPT_ALL or (field in ("all_suggestions", "suggestions")
                                    and said.lower() in ("accept", ACCEPT_ALL.lower())):
            asked_all.add(need.need_id)
            for r in rows_of(nit):
                if r["kind"] == "suggested":
                    save(conn, need.need_id, r["id"], r["value"]["text"],
                         f"accepted from {r['value']['source']}")
        elif said == STOP:
            skipped.setdefault(need.need_id, set()).update(r["id"] for r in rows_of(nit))
        else:
            row = find_row(nit, field)
            if row is None:
                names = ", ".join(f"{r['id']} ({r.get('full_label') or r['label']})"
                                  for r in rows_of(nit))
                return f"No entry {field!r} in the NIT. The entries are: {names}."
            if not said:
                return f"Give the value for {row['label']}, or 'skip'."
            if said.lower() in ("skip", SKIP.lower()):
                skipped.setdefault(need.need_id, set()).add(row["id"])
            elif said.lower() in ("accept", ACCEPT.lower()):
                if row["kind"] != "suggested":
                    return f"{row['label']} has no suggestion to accept; ask the user for it."
                save(conn, need.need_id, row["id"], row["value"]["text"],
                     f"accepted from {row['value']['source']}")
            else:
                save(conn, need.need_id, row["id"], said, "you")
        nit = build(need, kg_url, conn)
        if _notify:
            _notify("nit", _summary(nit))
        return "Saved; the sheet beside the chat is updated. " + next_step(
            nit, skipped.get(need.need_id, set()), need.need_id in asked_all)

    YES_TEXT = "'" + YES + "'"
    return [
        Tool(
            name="generate_nit",
            description=(
                "Make the NIT (Notice Inviting Tender) for a NAM case from NRL's template; it "
                "opens beside the chat as a filled sheet. Only when the user asks for the NIT. "
                "First call it without confirm: it reports what is filled and tells you to ask "
                "the user to confirm. Call it with confirm=true only after the user answers "
                + YES_TEXT + ". Then follow what it says to ask next, one entry at a time."),
            input_schema={"type": "object", "properties": {
                "need_id": {"type": "string", "description": "The case id, NAM-YYYY-NNNN"},
                "confirm": {"type": "boolean",
                            "description": "true only after the user chose " + YES_TEXT}},
                "required": ["need_id"]},
            fn=generate_nit, wants_notify=True),
        Tool(
            name="nit_set_field",
            description=(
                "Fill or change one entry of a generated NIT: the user's answer to an entry "
                "you asked about, or a change they ask for ('set EMD to Nil', 'contract period "
                "2 years'). value is the user's own words, 'accept' to take the past tender's "
                "suggestion, or 'skip'. field 'all_suggestions' accepts every suggestion. "
                "Never invent a value."),
            input_schema={"type": "object", "properties": {
                "need_id": {"type": "string", "description": "The case id, NAM-YYYY-NNNN"},
                "field": {"type": "string",
                          "description": "The entry's id or its label, e.g. emd or "
                                         "'Earnest Money Deposit (EMD)'"},
                "value": {"type": "string", "description": "The value, 'accept' or 'skip'"}},
                "required": ["need_id", "field", "value"]},
            fn=nit_set_field, wants_notify=True),
    ]
