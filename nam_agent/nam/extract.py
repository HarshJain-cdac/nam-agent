"""Extraction in code, as the NAM skills specify it (nam_agent/nam/skills/):
what to take from a PQC, SCC, technical specification, BOQ table or
approval note, and how. Deterministic: the same text gives the same items,
every value is the text's own (never paraphrased, never converted), and a
field the text does not state is left out, never guessed.

  pqc(text)        criteria: kind, text, alternatives [(count, share%)],
                   share, amount, period, evidence          (nam-documents/pqc)
  scc(text)        clauses: term, text, figures [value, unit, as written],
                   class standard | contract_specific        (nam-documents/scc)
  spec(text)       clauses: standards, materials, ratings, tests (nam-documents/spec)
  boq(rows)        lines: item, qty, uom, rate, amount, code, sor_ref (nam-documents/boq)
  aan(text)        fields: subject, estimated value, budget, ... (nam-documents/aan)
  document(kind, text, rows)   the one for a document's kind, as
                   {"skill", "items"}, or None for other kinds

The text is untrusted document data; nothing here acts on what it says.
"""

from __future__ import annotations

import re

_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "single": 1}
_NUMBER = r"(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|single)"


def _n(word: str) -> float:
    word = word.lower()
    return float(_WORD_NUM[word]) if word in _WORD_NUM else float(word)


def _plain(x: float):
    return int(x) if float(x).is_integer() else x


def clauses(text: str) -> list[str]:
    """The text split into clauses: at lines and semicolons, with a line's
    leading number ("1.", "a)", "(ii)") dropped. A number in brackets inside
    a sentence ("seven (7) years") and a decimal point never split."""
    out = []
    for line in re.split(r"\n+", text or ""):
        for part in re.split(r";\s", line):
            part = re.sub(r"^\(?(?:[a-h]|i{1,3}|iv|v|vi{0,3}|\d{1,2})[.)]\s+", "",
                          part.strip())
            if len(part) >= 8 and not _heading(part):
                out.append(part)
    return out


def _heading(text: str) -> bool:
    """A title line ("Pre-qualification criteria", "A. Civil works"): a few
    words with no figure and nothing required of anyone."""
    return (len(text.split()) <= 6 and ":" not in text and not text.rstrip().endswith(".")
            and not re.search(r"\d|\b(shall|must|should|will|required|requires|to be)\b",
                              text, re.IGNORECASE))


# -- PQC (nam-documents/pqc) -----------------------------------------------------------------

_PQC_KINDS = (
    ("turnover", r"\bturn\s?over\b"),
    ("net_worth", r"\bnet\s?worth\b|\bsolvency\b"),
    ("experience", (r"\b(similar (works?|jobs?|orders?|supply|supplies|services?)|executed|"
                    r"supplied|completed|experience)\b")),
    ("oem", (r"\b(oem|original equipment manufacturer|manufacturer'?s? authori[sz]ation|"
             r"authori[sz]ed (dealer|distributor))\b")),
    ("registration", r"\b(gst|pan|epf|esic?|licen[cs]e|registration|registered)\b"),
    ("capacity", r"\b(machinery|equipment|plant and|manpower|facilit(y|ies)|capacity)\b"),
    ("relaxation", r"\b(relax\w*|startups?|start-ups?)\b"),
)
_ALT = re.compile(rf"\b{_NUMBER}\s+(?:\w+\s+){{0,3}}?(?:works?|orders?|supplies|jobs?|contracts?|"
                  rf"projects?)\b[^%;]{{0,80}}?(\d+(?:\.\d+)?)\s*(?:%|per\s?cent)", re.IGNORECASE)
_SHARE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|per\s?cent\b)", re.IGNORECASE)
_AMOUNT = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(lakhs?|lacs?|crores?|cr\.?)?",
                     re.IGNORECASE)
_PERIOD = re.compile(rf"\b(?:last|preceding|past|previous|immediately preceding)\s+{_NUMBER}\s*"
                     r"\(?\d*\)?\s*((?:financial\s+)?years?|months?)\b", re.IGNORECASE)
_EVIDENCE = re.compile(r"\b(work orders?|purchase orders?|completion certificates?|performance "
                       r"certificates?|audited (balance sheets?|accounts?|financial statements?)|"
                       r"balance sheets?|ca certificate|chartered accountant'?s? certificate|"
                       r"authori[sz]ation letter|itr|income tax returns?|registration "
                       r"certificates?)\b", re.IGNORECASE)


def _kind(text: str, kinds) -> str:
    for kind, pattern in kinds:
        if re.search(pattern, text, re.IGNORECASE):
            return kind
    return "other"


def pqc(text: str) -> list[dict]:
    out = []
    for clause in clauses(text):
        item: dict = {"kind": _kind(clause, _PQC_KINDS), "text": clause[:2000]}
        alts = [{"count": _plain(_n(c)), "share": _plain(float(s))}
                for c, s in _ALT.findall(clause)]
        if alts:
            item["alternatives"] = alts
        elif (m := _SHARE.search(clause)):
            item["share"] = _plain(float(m.group(1)))
        if (m := _AMOUNT.search(clause)):
            item["amount"] = {"value": m.group(1).replace(",", ""),
                              "unit": (m.group(2) or "rupees").lower().rstrip(".")}
        if (m := _PERIOD.search(clause)):
            item["period"] = {"value": _plain(_n(m.group(1))), "unit": m.group(2).lower()}
        evidence = sorted({e.lower() for e, *_ in _EVIDENCE.findall(clause)})
        if evidence:
            item["evidence"] = evidence
        out.append(item)
    return out


# -- SCC (nam-documents/scc) -----------------------------------------------------------------

_SCC_TERMS = (
    ("liquidated_damages", r"\bliquidated damages\b|\bl\.?d\.?\b|\bpenalty\b"),
    ("performance_security", (r"\bperformance (bank )?(guarantee|security)\b|\bpbg\b|"
                              r"\bsecurity deposit\b")),
    ("defect_liability", r"\bdefects? liability\b"),
    ("warranty", r"\bwarranty\b|\bguarantee period\b"),
    ("contract_period", r"\bcontract period\b|\bperiod of (the )?contract\b"),
    ("completion", r"\bcomplet(ion|ed)\b"),
    ("delivery", r"\bdeliver(y|ed)\b"),
    ("payment", r"\bpayment\b"),
    ("price_basis", (r"\b(firm|fixed|variable) price\b|\bprices? (shall|will) (be|remain)\b|"
                     r"\bescalation\b|\bfor destination\b")),
    ("validity", r"\b(bid|offer) validity\b|\bvalid(ity)? for\b"),
    ("inspection", r"\binspection\b|\btpi\b"),
)
_FIG = re.compile(r"(\d+(?:\.\d+)?)\s*(?:\(\s*\w+\s*\)\s*)?(%|(?:per\s?cent|days?|weeks?|months?|"
                  r"years?)\b)(\s+per\s+(?:week|day|month)\b)?", re.IGNORECASE)
_WORD_FIG = re.compile(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|twelve|"
                       r"eighteen|twenty-four|thirty|sixty|ninety)\s*\(\s*(\d+)\s*\)",
                       re.IGNORECASE)


def figures(text: str) -> list[dict]:
    """Every figure with its unit, as written: "eight (8) weeks" once, as 8."""
    plain = _WORD_FIG.sub(lambda m: m.group(2), text or "")
    out = []
    for m in _FIG.finditer(plain):
        unit = m.group(2).lower().replace(" ", "")
        unit = "%" if unit in ("%", "percent") else unit.rstrip("s")
        per = (m.group(3) or "").strip().lower()
        out.append({"value": _plain(float(m.group(1))), "unit": unit + (f" {per}" if per else ""),
                    "text": m.group(0).strip()})
    for m in _AMOUNT.finditer(plain):
        out.append({"value": m.group(1).replace(",", ""),
                    "unit": (m.group(2) or "rupees").lower().rstrip("."),
                    "text": m.group(0).strip()})
    return out[:10]


def scc(text: str) -> list[dict]:
    out = []
    for clause in clauses(text):
        found = figures(clause)
        out.append({"term": _kind(clause, _SCC_TERMS), "text": clause[:2000],
                    "figures": found,
                    "class": "contract_specific" if found else "standard"})
    return out


# -- technical specification (nam-documents/spec) ---------------------------------------

_STANDARD = re.compile(r"\b(ASME|API|ASTM|IS|BS|EN|IEC|ISO|DIN|ANSI|MSS|NACE|IEEE|IBR|OISD)"
                       r"[\s-]*([A-Z]?\d[\dA-Z.:/-]*(?:\s*(?:Part|Pt\.?)\s*\d+)?)")
_MATERIAL = re.compile(r"\b(SS\s?\d{3}L?|CS|carbon steel|stainless steel|graphite|PTFE|"
                       r"A105|A182\s?F\d+L?|A106|A216\s?WCB|duplex|inconel|monel|cast iron|"
                       r"GI|MS|HDPE|PVC|copper|aluminium)\b", re.IGNORECASE)
_RATING = re.compile(r"(\d+(?:\.\d+)?)\s*(inch|in\b|\"|mm|nb|kv|v\b|kw|hp|bar|kg/cm2|psi|"
                     r"°c|deg ?c)|\b(?:class|pn|#)\s*(\d+)", re.IGNORECASE)
_TESTS = re.compile(r"\b(hydro(static)? test|fat|sat|third[- ]party inspection|tpi|"
                    r"mtc(\s*3\.[12])?|test certificates?|radiography|ultrasonic|pmi)\b",
                    re.IGNORECASE)


def spec(text: str) -> list[dict]:
    out = []
    for clause in clauses(text):
        item: dict = {"text": clause[:2000]}
        stds = [f"{a} {b}".strip() for a, b in _STANDARD.findall(clause)]
        if stds:
            item["standards"] = list(dict.fromkeys(stds))
        mats = [m.upper() if len(m) <= 6 else m.lower() for m in _MATERIAL.findall(clause)]
        if mats:
            item["materials"] = list(dict.fromkeys(mats))
        ratings = [m.group(0).strip() for m in _RATING.finditer(clause)]
        if ratings:
            item["ratings"] = ratings[:10]
        tests = [m.group(0) for m in _TESTS.finditer(clause)]
        if tests:
            item["tests"] = list(dict.fromkeys(t.lower() for t in tests))
        if re.search(r"\b(make|brand)\b", clause, re.IGNORECASE) and \
                not re.search(r"\bor equivalent\b", clause, re.IGNORECASE):
            item["brand_without_equivalent"] = True
        out.append(item)
    return out


# -- BOQ (nam-documents/boq) -----------------------------------------------------------------

_COLUMNS = {"item": r"descr|item|particular|work|material",
            "qty": r"^qty|quantit", "uom": r"unit|uom",
            "rate": r"rate|unit price", "amount": r"amount|total|value",
            "material_code": r"code|material no", "sor_ref": r"sor|dsr|schedule|ref"}


def _decimal(cell: str) -> str | None:
    text = re.sub(r"(?i)rs\.?|inr|₹|\s", "", cell or "").replace(",", "")
    return text if re.fullmatch(r"\d+(\.\d+)?", text) else None


def boq(rows: list[list[str]]) -> list[dict]:
    """One line per table row under the header row (the row naming the
    description and the quantity)."""
    header, cols = None, {}
    out = []
    for r, row in enumerate(rows or []):
        cells = [str(c or "").strip() for c in row]
        if header is None:
            found = {}
            for key, pattern in _COLUMNS.items():
                for i, c in enumerate(cells):
                    if i not in found.values() and re.search(pattern, c, re.IGNORECASE):
                        found[key] = i
                        break
            if "item" in found and "qty" in found:
                header, cols = r, found
            continue
        qty = _decimal(cells[cols["qty"]]) if cols["qty"] < len(cells) else None
        item = cells[cols["item"]] if cols["item"] < len(cells) else ""
        if qty is None or not item or re.search(r"(?i)\b(total|gst|tax)\b", item):
            continue                       # a heading, a note or a total
        line = {"row": r + 1, "item": item[:300], "qty": qty}
        for key in ("uom", "material_code", "sor_ref"):
            if key in cols and cols[key] < len(cells) and cells[cols[key]]:
                line[key] = cells[cols[key]][:60]
        for key in ("rate", "amount"):
            if key in cols and cols[key] < len(cells):
                value = _decimal(cells[cols[key]])
                if value is not None:
                    line[key] = value
        out.append(line)
    return out


# -- approval note (nam-documents/aan) -------------------------------------------------------

_AAN_FIELDS = {
    "file_number": r"file\s*(?:no\.?|number)",
    "subject": r"subject|sub\.",
    "department": r"department|deptt?\.?",
    "justification": r"justification|need for|purpose",
    "estimated_value": r"estimated (?:cost|value)|estimate",
    "budget": r"budget(?:\s*head)?|wbs",
    "period": r"(?:delivery|completion|contract) period",
    "tender_type": r"(?:mode|type) of tender(?:ing)?|tender type",
    "approver": r"competent authority|approving authority|approved by",
    "date": r"date(?:d)?",
}


def aan(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        for field_name, label in _AAN_FIELDS.items():
            m = re.match(rf"\s*(?:\d+[.)]\s*)?(?:{label})\s*[:\-–]\s*(.+)", line, re.IGNORECASE)
            if m and not any(x["field"] == field_name for x in out):
                out.append({"field": field_name, "value": m.group(1).strip()[:500]})
                break
    return out


KINDS = {"pqc": ("nam-documents/pqc", pqc), "scc": ("nam-documents/scc", scc),
         "specification": ("nam-documents/spec", spec),
         "aan": ("nam-documents/aan", aan), "ann": ("nam-documents/aan", aan)}


def document(kind: str, text: str, rows: list[list[str]] | None = None) -> dict | None:
    """What the skill for a document's kind says to extract, or None."""
    if kind in ("boq", "estimate", "sor"):
        return {"skill": "nam-documents/boq", "items": boq(rows or [])[:500]}
    if kind in KINDS:
        skill, fn = KINDS[kind]
        return {"skill": skill, "items": fn(text)[:300]}
    return None
