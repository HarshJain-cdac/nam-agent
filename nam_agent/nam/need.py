"""Need Assessment Module — the need record, its classification, and the
retrieval of similar past cases.

This implements the front half of the NAM FRS (Procurix.ai, v4.0): §3.1.1
Procurement Need Capture and Classification, §3.1.2 the conversational prefill,
and the step the whole module turns on —

    "NAM retrieves and presents a list of previous, similar tenders/PRs
     relevant to the requirement. The user reviews the retrieved list and
     selects the relevant previous tender(s) ... the estimates, BOQ, SCC, PQC
     and Technical Specification are subsequently generated based on the
     tender(s) selected by the user, RATHER THAN BY AI SELECTION ALONE."

That last clause is a design constraint, not a preference. NAM ranks and
presents; the officer chooses; nothing downstream may be built from a case the
officer did not pick. `NeedRecord.selected_cases` is the only thing generation
is allowed to read, and it is empty until somebody selects something.

WHERE THE DATA COMES FROM
-------------------------
The FRS names SAP and Aarohan. Neither is reachable here, and nothing in this
module pretends otherwise: every field is either read from the knowledge graph
(156 historical tenders with their BOQ lines, PQC, SCC and specifications),
derived from the officer's own words, or left unset. A field NAM could not
source is reported as unsourced rather than filled with a plausible value —
see `NeedRecord.unresolved`.

CLASSIFICATION IS RULE-BASED
----------------------------
§3.1.2 wants the chatbot to predict Category, Type of Procurement and Tender
Type from free text, and to "ask a clarifying follow-up question only for
whichever field it could not confidently predict". That is a routing decision
over a small closed vocabulary, which rules do exactly and cheaply — and on
this deployment one model call costs between 1.6s and 21s of queue. So the
rules run first and the module reports its own confidence; what they cannot
decide is what the officer gets asked.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime

from nam_agent.nam import kg_mcp

_TIMEOUT = 240.0

# --------------------------------------------------------------------------
# The closed vocabularies, exactly as the FRS and the UI define them.
# --------------------------------------------------------------------------

GOODS = "Goods"
SERVICES = "Manpower / Services"
WORKS = "Works"
CATEGORIES = (GOODS, SERVICES, WORKS)

# Stable ids beside the display labels. The UI carries the category on the case
# and keys its own logic off "goods" / "services" / "works"
# (nit-forge-ui/src/lib/nam-steps.ts), so the API sends both: the id to switch
# on and the label to print. A label is a display decision and will change; an
# id that travels between the two halves of the system must not.
CATEGORY_ID = {GOODS: "goods", SERVICES: "services", WORKS: "works"}
ID_TO_CATEGORY = {v: k for k, v in CATEGORY_ID.items()}


def category_id(label: str | None) -> str | None:
    return CATEGORY_ID.get(label or "")


def category_label(ident: str | None) -> str | None:
    return ID_TO_CATEGORY.get(ident or "")

SUBTYPES = {
    GOODS: ("Codified", "Uncodified"),
    SERVICES: ("Repetitive (AMC, manpower)", "Consultancy", "Non-consultancy"),
    WORKS: ("Repetitive", "New", "Capital / Revenue",
            "Within impanelment", "Outside impanelment"),
}

PROCUREMENT_TYPES = ("CAPEX", "OPEX", "Technical Quantity-and-Rate Estimate",
                     "MRO-Triggered", "Emergency")

TENDER_TYPES = ("Open", "Limited", "OEM", "Proprietary", "Nomination", "SPOT", "GeM")

# Rules. Each is (regex, value); the first match wins, and a requirement that
# matches nothing leaves the field unset for the officer to answer.
_CATEGORY_RULES = [
    ((r"\b(amc|annual maintenance|manpower|housekeeping|security|catering|"
      r"transport(ation)?|consultanc(y|ies)|service contract|deployment|"
      r"outsourc\w+|staffing|horticulture|garden\w*|cleaning)\b"), SERVICES),
    ((r"\b(civil work|erection|construction|painting|fabrication|installation|"
      r"commissioning|renovation|repair work|laying|piling|road work|building)\b"), WORKS),
    ((r"\b(supply of|procurement of|purchase of|spares?|valves?|gaskets?|pipes?|"
      r"cables?|transformers?|panels?|instruments?|chemicals?|consumables?|"
      r"fittings?|material|equipment)\b"), GOODS),
]

_SUBTYPE_RULES = [
    ((r"\b(amc|annual maintenance|rate contract|arc|repetitive|recurring|renewal)\b"),
     {SERVICES: "Repetitive (AMC, manpower)", WORKS: "Repetitive"}),
    (r"\bconsultanc(y|ies)\b", {SERVICES: "Consultancy"}),
]

_PROCUREMENT_RULES = [
    (r"\b(emergency|urgent|breakdown|immediate|disaster)\b", "Emergency"),
    (r"\b(capex|capital|new project|expansion|modernisation|modernization)\b", "CAPEX"),
    ((r"\b(mro|maintenance, repair|spares?|consumables?|overhaul|shutdown|rta)\b"),
     "MRO-Triggered"),
    (r"\b(opex|operational|recurring|amc|annual maintenance|rate contract|arc)\b", "OPEX"),
]

# Tender type words are only read as tender types in a tendering phrase.
#
# The bare words collide with ordinary procurement text: "Numaligarh Refinery
# Limited" is NRL's own legal name and sits at the end of a large share of tender
# subjects, so a bare `limited` rule typed nearly every one of them a Limited
# Tender. "Open area painting" became an Open Tender the same way. A tender type
# is a policy decision with value thresholds behind it; reading one off a
# company name is worse than not reading one at all.
_TENDER_RULES = [
    (r"\b(gem|government e-?marketplace)\b", "GeM"),
    (r"\b(oem|original equipment manufacturer)\b", "OEM"),
    (r"\b(proprietar(y|ies)|single[- ]source)\b", "Proprietary"),
    (r"\bnomination (basis|tender)\b", "Nomination"),
    (r"\bspot (purchase|tender|quotation)\b", "SPOT"),
    (r"\b(limited tender|limited tendering|lte)\b", "Limited"),
    (r"\b(open tender|open tendering|ote)\b", "Open"),
]

# Periods the officer may state: "12 months", "2 years", "3 yr"
_TERM = re.compile(r"\b(\d{1,3})\s*(day|week|month|year|yr)s?\b", re.IGNORECASE)
# Units take an optional plural: "34 positions" and "12 sets" are how officers
# write quantities, and the singular-only form silently dropped them.
_QTY = re.compile(r"\b(\d[\d,]*\.?\d*)\s*(nos?|each|kg|mt|ltr|litre|l|m|mtr|metre|"
                  r"sqm|cum|set|unit|position|person|manpower|nos\.)s?\b", re.IGNORECASE)
_PLANT = re.compile(r"\b(plant[\s-]?\d+|block[\s-]?[a-z0-9]+|unit[\s-]?\d+|"
                    r"substation[\s-]?\d+|ss[\s-]?\d+|township|refinery|guwahati|"
                    r"siliguri|numaligarh)\b", re.IGNORECASE)


def _first_rule(text: str, rules) -> tuple[str | None, str]:
    """(value, the words that decided it). The evidence travels with the value
    so the UI can show WHY a field was predicted, and the officer can judge it."""
    low = text.lower()
    for pattern, value in rules:
        found = re.search(pattern, low)
        if found:
            return value, found.group(0)
    return None, ""


@dataclass
class Prediction:
    value: str | None = None
    basis: str = ""          # the words in the requirement that decided it
    confident: bool = False  # False -> the chatbot asks about this one

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class NeedRecord:
    """One procurement need, from free text to selected precedent."""

    need_id: str
    created_at: str
    raw_text: str
    category: dict = field(default_factory=lambda: Prediction().as_dict())
    subtype: dict = field(default_factory=lambda: Prediction().as_dict())
    procurement_type: dict = field(default_factory=lambda: Prediction().as_dict())
    tender_type: dict = field(default_factory=lambda: Prediction().as_dict())
    location: str = ""
    term: str = ""
    quantity: str = ""
    # Case keys the OFFICER selected. Generation reads only this.
    selected_cases: list[str] = field(default_factory=list)
    # describe | matching | selected, and from Need capture (nam_capture.py):
    # capture (still gathering) | references (enough known to move on)
    stage: str = "describe"
    # Every related past tender Need capture found. Found, not selected: the
    # officer still picks from these, and generation reads selected_cases only.
    related_cases: list[str] = field(default_factory=list)
    readiness: dict = field(default_factory=dict)
    # Need capture's follow-up questions (nam_followup.py): the last scan,
    # the officer's answers in the order given, and whether they are done.
    followups: dict = field(default_factory=dict)
    # The stage machine's state (nam_agent/nam/orchestrator.py): stage, gates
    # passed, actions done, the flows' artifacts, the pending action.
    workflow: dict = field(default_factory=dict)
    # The store's version of this record when it was read (0: never saved).
    # Not part of the record: nam_agent/nam/store.py keeps it, and refuses a save
    # made against an older version (stale_version).
    version: int = 0

    @property
    def unresolved(self) -> list[str]:
        """Fields NAM has not CONFIDENTLY settled — exactly what to ask about.

        Confidence, not presence. A keyword suggestion for tender type carries a
        value but is never confident, and must still be put to the officer;
        treating "a value exists" as "decided" is how a company name became a
        tender type nobody was asked to confirm.
        """
        out = []
        for name in ("category", "procurement_type", "tender_type"):
            pred = getattr(self, name)
            if not pred.get("value") or not pred.get("confident"):
                out.append(name)
        return out


def new_need_id(conn: sqlite3.Connection) -> str:
    """NAM-YYYY-NNNN. The FRS's Unique File Number: one per need, threading the
    whole flow so a case can be tracked end to end. Drawn atomically
    (nam_agent/nam/store.py), so two needs opened at once never share one."""
    from nam_agent.nam import store

    return store.next_case_id(conn)


def classify(text: str) -> dict:
    """Predict category, sub-type, procurement type and tender type from free
    text, and say which ones were not decided. No model call."""
    category, cat_basis = _first_rule(text, _CATEGORY_RULES)

    subtype_value, sub_basis = None, ""
    for pattern, by_category in _SUBTYPE_RULES:
        found = re.search(pattern, text.lower())
        if found and category in by_category:
            subtype_value, sub_basis = by_category[category], found.group(0)
            break

    procurement, proc_basis = _first_rule(text, _PROCUREMENT_RULES)
    tender, tender_basis = _first_rule(text, _TENDER_RULES)

    term = _TERM.search(text)
    qty = _QTY.search(text)
    plant = _PLANT.search(text)

    return {
        "category": {**Prediction(category, cat_basis, bool(category)).as_dict(),
                     "id": category_id(category)},
        "subtype": Prediction(subtype_value, sub_basis, bool(subtype_value)).as_dict(),
        "procurement_type": Prediction(procurement, proc_basis, bool(procurement)).as_dict(),
        # Tender type is genuinely a policy call (value thresholds, OEM status).
        # A word match is a suggestion, never a confident answer.
        "tender_type": Prediction(tender, tender_basis, False).as_dict(),
        "location": plant.group(0) if plant else "",
        "term": term.group(0) if term else "",
        "quantity": qty.group(0) if qty else "",
    }


# --------------------------------------------------------------------------
# Similar cases — the screen this module exists for
# --------------------------------------------------------------------------


_SCAFFOLD = {
    "for", "of", "the", "and", "at", "to", "in", "on", "with", "a", "an",
    "supply", "procurement", "purchase", "annual", "rate", "contract", "new",
    "nrl", "numaligarh", "refinery", "work", "works", "job", "service",
    "services", "need", "require", "required", "requirement", "want", "please",
}


def match_strength(requirement: str, case: dict, summary: dict) -> tuple[int, str]:
    """A percentage the card can show, and a sentence saying what it means.

    Deliberately NOT called "similarity". The embedding score is a different
    quantity, is only available when the semantic lane is up, and reporting a
    lexical overlap under that name would overstate what the number knows.
    This is: how much of what you asked for does this tender actually contain,
    computed from two things the graph can prove —

        term coverage   how many of the requirement's substantive words appear
                        in the tender's subject
        line-item depth whether the tender bought the thing, or merely mentions
                        it once in a subject line

    Both are explainable to a procurement officer, which a cosine distance is
    not, and neither can be produced when the data is absent.
    """
    wanted = {w for w in re.findall(r"[a-z]{3,}", (requirement or "").lower())
              if w not in _SCAFFOLD}
    if not wanted:
        return 0, "nothing substantive to match on"

    subject = str(summary.get("job_name") or case.get("job_name") or "").lower()

    def present(word: str) -> bool:
        """The word, or its singular/plural twin, in the subject.

        Without this, a search for "gaskets" scored "Procurement of Gasket" as
        matching none of the requirement's terms — the one tender whose entire
        subject IS the requirement. The full-text index does not stem and
        neither did this.
        """
        forms = {word}
        if word.endswith("ies"):
            forms.add(word[:-3] + "y")
        elif word.endswith("es") and word[-3:-2] in ("s", "x", "h", "z"):
            forms.add(word[:-2])
        elif word.endswith("s") and not word.endswith("ss"):
            forms.add(word[:-1])
        else:
            forms.add(word + "s")
            forms.add(word + "es")
        return any(f in subject for f in forms)

    covered = {w for w in wanted if present(w)}
    coverage = len(covered) / len(wanted)

    hits = case.get("matching_item_count", 0) or 0
    # A tender with many matching line items really bought this; one with a
    # single mention may only reference it. Saturates quickly on purpose —
    # 20 matching lines is already conclusive, 1275 is not 60x better.
    depth = min(hits, 20) / 20

    score = round(100 * (0.6 * coverage + 0.4 * depth))
    parts = []
    if covered:
        parts.append(f"subject matches {len(covered)} of {len(wanted)} terms "
                     f"({', '.join(sorted(covered)[:4])})")
    else:
        parts.append("subject matches none of your terms")
    parts.append(f"{hits} matching line item(s)" if hits else "no matching line items")
    return score, "; ".join(parts)


def search_query(need: NeedRecord) -> str:
    """What to actually search the graph for.

    NOT the officer's whole sentence. "AMC for housekeeping at Plant-1 for 12
    months" carries three different kinds of information: what is being
    procured (housekeeping, AMC), where (Plant-1) and for how long (12 months).
    Only the first kind belongs in a search — the location and the term are
    captured as their own fields, and feeding them to a literal index searches
    for tenders whose SUBJECT says "plant" and "months", which is every tender
    and therefore no tender.

    Stripping them is what turns 30 loose matches into the handful that
    actually procured the thing.
    """
    text = need.raw_text or ""
    for captured in (need.location, need.term, need.quantity):
        if captured:
            text = re.sub(re.escape(captured), " ", text, flags=re.IGNORECASE)
    # drop connective words and any bare number left behind by the removals
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{1,}", text)
             if w.lower() not in _SCAFFOLD]
    return " ".join(words[:6]).strip() or (need.raw_text or "").strip()


def _get(kg_url: str, path: str, params: dict) -> dict:
    """An old /api/kg/<name> read: from the MCP server's tools, or, when that
    server has no such tool (it now serves SAP's records alone), from the
    tender knowledge graph's own API (NAM_KG_API_URL), which answers these
    paths natively."""
    try:
        return kg_mcp.legacy_get(kg_url, path, params)
    except RuntimeError as exc:
        if "Unknown tool" not in str(exc) and "no MCP equivalent" not in str(exc):
            raise
        from nam_agent.nam import kg_tools

        api = kg_tools.kg_api(os.getenv("NAM_KG_API_URL", "http://127.0.0.1:8077"), "")
        if not api or kg_tools.is_mcp(api):
            raise
        return kg_tools._get(api, path, params)


def _term_months(text: str) -> int | None:
    found = _TERM.search(text or "")
    if not found:
        return None
    n = int(found.group(1))
    unit = found.group(2).lower()
    return {"day": n // 30, "week": n // 4, "month": n,
            "year": n * 12, "yr": n * 12}.get(unit)


def similar_cases(need: NeedRecord, kg_url: str, limit: int = 5) -> dict:
    """Ranked past tenders for this need, shaped for the Similar-cases cards.

    Every number on a card is read from the graph. `similarity` is the KG's own
    item-embedding score where the semantic lane is up, and is reported as
    absent rather than invented when it is not — a card claiming "78% similar"
    off a literal word match would be a worse lie than no percentage at all.
    """
    query = search_query(need)
    if not query:
        return {"cases": [], "note": "Nothing to search on yet."}

    try:
        found = _get(kg_url, "/api/kg/tenders", {"query": query, "limit": limit})
    except Exception as exc:  # noqa: BLE001
        return {"cases": [], "error": f"Knowledge graph unreachable: {str(exc)[:120]}"}

    rows = found.get("tenders") or []
    if not rows:
        return {"cases": [], "note": f"No past tender matches '{query}'.",
                "terms": found.get("terms") or []}

    # One batched call for what the cards display.
    keys = [r["case_key"] for r in rows][:limit]
    summaries: dict[str, dict] = {}
    try:
        data = _get(kg_url, "/api/kg/case_summary", {"case_keys": ",".join(keys)})
        summaries = {c["case_key"]: c for c in data.get("cases", [])}
    except Exception:  # noqa: BLE001 - cards without counts beat no cards
        pass

    # The semantic score, where the lane is up. Read once for the whole list.
    similarity: float | None = None
    try:
        prec = _get(kg_url, "/api/kg/precedent", {"requirement": query, "limit": 1})
        similarity = prec.get("best_similarity")
    except Exception:  # noqa: BLE001
        similarity = None

    wanted_months = _term_months(need.term)
    # Order by how much of the requirement each tender actually contains.
    rows = sorted(rows[:limit],
                  key=lambda r: -match_strength(query, r,
                                                summaries.get(r["case_key"], {}))[0])
    cases = []
    for rank, row in enumerate(rows):
        key = row["case_key"]
        summary = summaries.get(key, {})
        strength, basis = match_strength(query, row, summary)
        flags: list[str] = []

        # Only flags that can be computed. A card never warns about something
        # the graph did not tell us.
        if summary.get("boq_lines", 0) == 0:
            flags.append("No BOQ lines on record")
        if not summary.get("pqc_clauses") and not summary.get("scc_clauses"):
            flags.append("No PQC or SCC on record")
        if need.category.get("value") == SERVICES and summary.get("department") and \
                "maintenance" not in str(summary["department"]).lower() and \
                row.get("matching_item_count", 0) == 0:
            flags.append("Matched on subject only, not line items")
        if wanted_months and summary.get("bid_opening_iso"):
            pass  # contract term is not on the tender node; no flag invented

        cases.append({
            "case_key": key,
            "tender_no": summary.get("tender_no") or row.get("tender_no"),
            "subject": summary.get("job_name") or row.get("job_name"),
            "department": summary.get("department"),
            "estimated_value": summary.get("estimated_value"),
            "bid_opening_date": summary.get("bid_opening_date"),
            "awarded_to": summary.get("awarded_to") or [],
            "counts": {
                "boq_lines": summary.get("boq_lines", 0),
                "pqc_clauses": summary.get("pqc_clauses", 0),
                "scc_clauses": summary.get("scc_clauses", 0),
                "spec_clauses": summary.get("spec_clauses", 0),
                "sor_references": summary.get("sor_references", 0),
            },
            "matching_items": row.get("matching_items", [])[:6],
            "matching_item_count": row.get("matching_item_count", 0),
            # Rank is real ordering. Similarity is only present when the
            # semantic lane produced one.
            "rank": rank + 1,
            # What the card shows as a percentage, with its own explanation.
            "match_strength": strength,
            "match_basis": basis,
            # The embedding score, present only when the semantic lane produced
            # one and only for the case it was computed against.
            "semantic_similarity": round(similarity, 3) if rank == 0 and similarity else None,
            "flags": flags,
        })

    return {
        "cases": cases,
        "total_matches": found.get("count", len(cases)),
        "search_query": query,
        "terms": found.get("terms") or [],
        "note": ("Ranked by how much of this requirement each tender actually "
                 "procured. Pick the one to start from — nothing is generated "
                 "until you do."),
    }


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

# The record lives in nam_agent/nam/store.py (nam_cases, with every version kept).
# Rows written to nam_needs before that are copied there when it first opens.

def save(conn: sqlite3.Connection, need: NeedRecord, session_id: str = "",
         reason: str = "") -> None:
    """Save the need as its next version. `reason` says why, for its history.
    A need read before someone else saved it is refused (stale_version)."""
    from nam_agent.nam import store

    record = asdict(need)
    record.pop("version")
    stage = (need.workflow or {}).get("stage") or store.stage_of(need.stage)
    need.version = store.write(conn, need.need_id, record, expected=need.version, stage=stage,
                               session_id=session_id,
                               reason=reason)


def load(conn: sqlite3.Connection, need_id: str) -> NeedRecord | None:
    from nam_agent.nam import store

    found = store.load(conn, need_id)
    if found is None:
        return None
    record, version = found
    record.pop("version", None)
    return NeedRecord(**record, version=version)


def recent(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """The "Your drafts" list on the landing screen: the acting tenant's
    cases only (the dashboard's are the tenant "local")."""
    from nam_agent.nam import store

    out = []
    for rec, updated in store.recent(conn, store.current().tenant, limit):
        out.append({
            "need_id": rec.get("need_id"),
            "raw_text": (rec.get("raw_text") or "")[:80],
            "category": (rec.get("category") or {}).get("value"),
            "stage": rec.get("stage"),
            "selected": len(rec.get("selected_cases") or []),
            "updated_at": updated,
        })
    return out


def create(conn: sqlite3.Connection, text: str, session_id: str = "") -> NeedRecord:
    """A need from one sentence — classified, with a file number, ready to match."""
    predicted = classify(text)
    need = NeedRecord(
        need_id=new_need_id(conn),
        created_at=datetime.now().isoformat(timespec="seconds"),
        raw_text=text.strip(),
        category=predicted["category"],
        subtype=predicted["subtype"],
        procurement_type=predicted["procurement_type"],
        tender_type=predicted["tender_type"],
        location=predicted["location"],
        term=predicted["term"],
        quantity=predicted["quantity"],
    )
    save(conn, need, session_id, reason="opened from the stated need")
    if session_id:
        # the case this chat now works on (drive.session_case)
        from nam_agent.nam import chatstate

        chatstate.put(conn, session_id, "case", need.need_id)
    return need


def case_detail(case_key: str, kg_url: str) -> dict:
    """One past tender in full — the read behind "Start with this" / "Compare".

    Returns the clause TEXT and the BOQ lines, not counts: the officer has
    already chosen, and what they get now is the material the case will be
    built from.
    """
    try:
        return _get(kg_url, "/api/kg/case", {"case_key": case_key})
    except Exception as exc:  # noqa: BLE001
        return {"case_key": case_key, "error": f"Knowledge graph unreachable: {str(exc)[:120]}"}


def compare(case_keys: list[str], kg_url: str) -> dict:
    """Two or three past tenders side by side, for the Compare button.

    Only the attributes that decide a choice: what it was for, how much of a
    document set it carries, what it was worth, who won it. The clause text is
    deliberately left out — comparing forty clauses in a modal helps nobody;
    comparing how many there are, and against which subject, does.
    """
    rows = []
    for key in case_keys[:3]:
        detail = case_detail(key, kg_url)
        if detail.get("error"):
            rows.append({"case_key": key, "error": detail["error"]})
            continue
        rows.append({
            "case_key": key,
            "tender_no": detail.get("tender_no"),
            "subject": detail.get("job_name"),
            "department": detail.get("department"),
            "work_category": detail.get("work_category"),
            "estimated_value": detail.get("estimated_value"),
            "emd_amount": detail.get("emd_amount"),
            "bid_type": detail.get("bid_type"),
            "bid_opening_date": detail.get("bid_opening_date"),
            "awarded_to": detail.get("awarded_to") or [],
            "counts": detail.get("counts") or {},
            "sor_references": detail.get("sor_references") or [],
        })
    # Which fields actually differ, so the UI can mark them rather than leaving
    # the officer to diff four columns by eye.
    differing = []
    for attribute in ("department", "work_category", "bid_type", "emd_amount"):
        values = {str(r.get(attribute)) for r in rows if "error" not in r}
        if len(values) > 1:
            differing.append(attribute)
    return {"cases": rows, "differing_fields": differing}


def selected_material(need: NeedRecord, kg_url: str) -> dict:
    """Everything the officer's SELECTED tenders contribute, merged.

    This is the only door between matching and generation. It reads
    `need.selected_cases` and nothing else — a case the officer did not pick
    contributes no clause, no BOQ line and no rate, however well it scored.
    That is the FRS rule ("rather than by AI selection alone") expressed as
    code rather than as an instruction.
    """
    if not need.selected_cases:
        return {"selected": [], "pqc": [], "scc": [], "tech_spec": [], "boq": [],
                "note": "No tender selected. Nothing can be built from precedent yet."}

    merged: dict = {"selected": list(need.selected_cases),
                    "pqc": [], "scc": [], "tech_spec": [], "boq": [], "sources": []}
    for key in need.selected_cases:
        detail = case_detail(key, kg_url)
        if detail.get("error"):
            merged["sources"].append({"case_key": key, "error": detail["error"]})
            continue
        merged["sources"].append({
            "case_key": key, "tender_no": detail.get("tender_no"),
            "subject": detail.get("job_name"), "counts": detail.get("counts") or {},
        })
        # Every clause and line carries the tender it came from. Merged material
        # with no attribution is exactly how a clause from one tender ends up
        # cited to another.
        for section in ("pqc", "scc", "tech_spec"):
            for clause in detail.get(section) or []:
                merged[section].append({
                    "text": clause.get("text", ""),
                    "summary": clause.get("summary", ""),
                    "case_key": key,
                    "tender_no": detail.get("tender_no"),
                    "subject": detail.get("job_name"),
                })
        for line in detail.get("boq") or []:
            merged["boq"].append({**line, "case_key": key,
                                   "tender_no": detail.get("tender_no")})

    for section in ("pqc", "scc", "tech_spec"):
        merged[section] = _dedupe_clauses(merged[section])
    merged["counts"] = {k: len(merged[k]) for k in ("pqc", "scc", "tech_spec", "boq")}
    return merged


def _dedupe_clauses(clauses: list[dict]) -> list[dict]:
    """Drop duplicates and near-duplicates, keeping the first citation.

    Selecting two similar past tenders otherwise produces a PQC section that
    says the same thing twice under two different tender numbers.
    """
    seen: list[str] = []
    out = []
    for clause in clauses:
        text = (clause.get("text") or "").strip()
        if len(text) < 40:
            continue
        key = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()[:400]
        if any(key.startswith(k[:200]) or k.startswith(key[:200]) for k in seen):
            continue
        seen.append(key)
        out.append(clause)
    return out


# --------------------------------------------------------------------------
# Replicate from a reference (FRS 3.1.2, UI step 2)
# --------------------------------------------------------------------------

# A bare run of digits is how SAP numbers PRs and POs (0010002456,
# 4500128776). Those live in SAP, which this module does not reach, so a
# reference of that shape is answered with exactly that rather than searched.
_SAP_NUMBER = re.compile(r"^\d{8,12}$")


def lookup_reference(ref: str, kg_url: str) -> dict:
    """What a reference the officer typed names, and what it can carry over.

    Returns `kind` — "tender" (resolved from the graph), "sap" (a PR/PO number,
    unanswerable here) or "unknown" — so the UI can say precisely why a lookup
    came back empty instead of a generic "not found".
    """
    text = (ref or "").strip()
    if not text:
        return {"ref": ref, "kind": "unknown", "matches": [],
                "note": "Enter a tender number, PR number or PO number."}

    if _SAP_NUMBER.match(text.replace(" ", "")):
        return {
            "ref": text, "kind": "sap", "matches": [],
            "note": ("That looks like a SAP PR or PO number. SAP is not connected to "
                     "this system, so it cannot be looked up here. A tender number "
                     "(e.g. OC01000435/PRV or PUR-MECHMNT-1008357) resolves from the "
                     "tender repository."),
        }

    try:
        found = _get(kg_url, "/api/kg/resolve", {"ref": text})
    except Exception as exc:  # noqa: BLE001
        return {"ref": text, "kind": "unknown", "matches": [],
                "error": f"Knowledge graph unreachable: {str(exc)[:120]}"}

    matches = found.get("matches") or []
    if not matches:
        return {"ref": text, "kind": "unknown", "matches": [],
                "note": f"No past tender matches '{text}'."}

    result = {
        "ref": text,
        "kind": "tender",
        "tier": found.get("tier"),
        "ambiguous": bool(found.get("ambiguous")),
        "matches": matches,
        "record": None,
    }
    # Only an unambiguous match is opened. Two tenders sharing a base number
    # are shown side by side for the officer to choose — picking one for them
    # would be AI selection by the back door.
    if not result["ambiguous"]:
        detail = case_detail(matches[0]["case_key"], kg_url)
        if not detail.get("error"):
            result["record"] = {
                "case_key": detail.get("case_key"),
                "tender_no": detail.get("tender_no"),
                "subject": detail.get("job_name"),
                "department": detail.get("department"),
                "work_category": detail.get("work_category"),
                "estimated_value": detail.get("estimated_value"),
                "emd_amount": detail.get("emd_amount"),
                "bid_type": detail.get("bid_type"),
                "bid_opening_date": detail.get("bid_opening_date"),
                "pr_no": detail.get("pr_no"),
                "counts": detail.get("counts") or {},
                "awarded_to": detail.get("awarded_to") or [],
            }
    return result


def replicate(conn: sqlite3.Connection, case_key: str, kg_url: str,
              session_id: str = "") -> NeedRecord | None:
    """A new need built from one named past tender — "create by copy".

    The officer typed this tender's number and asked to replicate it, so it is
    selected precedent from the start: that is an explicit human choice, which
    is exactly what the FRS gate requires, not an AI pick. The subject is the
    tender's own, and is classified the same way a typed requirement is.
    """
    detail = case_detail(case_key, kg_url)
    if detail.get("error") or not detail.get("job_name"):
        return None
    need = create(conn, str(detail["job_name"]), session_id)
    need.selected_cases = [case_key]
    need.stage = "selected"
    save(conn, need, session_id, reason=f"replicated from tender {case_key}")
    return need


def apply_update(need: NeedRecord, patch: dict) -> str | None:
    """Apply an officer's answers to a need. Returns an error, or None.

    Every value is checked against its closed vocabulary before it is stored:
    a category outside the three paths routes the case nowhere, and a stored
    sentence where "Open" belonged would reach the NIT as the tender type.
    An officer's answer is recorded as confident — that is why they were asked.

    Processed category first, so a sub-type sent in the same patch is judged
    against the NEW path rather than the one being replaced.
    """
    you = {"basis": "you set this", "confident": True}

    if "category" in patch:
        value = patch["category"]
        label = category_label(value) or (value if value in CATEGORIES else None)
        if label is None:
            return f"unknown category '{value}' — one of {', '.join(CATEGORY_ID.values())}"
        need.category = {"value": label, **you, "id": category_id(label)}
        # A sub-type belongs to one path. Switching Works to Goods kept
        # "Repetitive", which is not a Goods sub-type, and the record then
        # described a case that cannot exist.
        current = (need.subtype or {}).get("value")
        if current and current not in SUBTYPES.get(label, ()) and "subtype" not in patch:
            need.subtype = {"value": None, "basis": "", "confident": False}

    if "subtype" in patch:
        path = (need.category or {}).get("value")
        allowed = SUBTYPES.get(path, ()) if path else tuple(v for vs in SUBTYPES.values() for v in vs)
        if patch["subtype"] not in allowed:
            return (f"'{patch['subtype']}' is not a sub-type of {path}" if path
                    else f"'{patch['subtype']}' is not a known sub-type")
        need.subtype = {"value": patch["subtype"], **you}

    for key, vocabulary in (("procurement_type", PROCUREMENT_TYPES),
                            ("tender_type", TENDER_TYPES)):
        if key in patch:
            if patch[key] not in vocabulary:
                return f"'{patch[key]}' is not a valid {key.replace('_', ' ')}"
            setattr(need, key, {"value": patch[key], **you})

    for key in ("location", "term", "quantity"):
        if key in patch:
            setattr(need, key, str(patch[key] or "").strip())
    return None
