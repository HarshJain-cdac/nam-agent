"""The document checks docs.review (F24) and validate (F32) share: what the
clauses say about each contract term, where two of them disagree, and wording
that leaves a term open. Read in code from the clause text, never by a model.

  terms()      per contract term (delivery, completion, warranty, liquidated
               damages, performance security, validity, defect liability,
               contract period), the first figure the sentence gives for it,
               as a period in days or a percentage
  conflicts()  the terms two selected clauses, or a clause and a
               contract-specific field, give different figures for
  vague()      wording that leaves a term to be decided later
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TERMS = {
    "delivery": r"\bdeliver(y|ed)\b",
    "completion": r"\bcomplet(ion|ed)\b",
    "warranty": r"\b(warranty|guarantee period)\b",
    "liquidated_damages": r"\bliquidated damages\b|\bl\.?d\.?\b",
    "performance_security": r"\b(performance (bank )?(guarantee|security)|pbg|security deposit)\b",
    "validity": r"\b(bid|offer) validity\b|\bvalid(ity)? for\b",
    "defect_liability": r"\bdefects? liability\b",
    "contract_period": r"\b(contract period|period of (the )?contract)\b",
}
# the contract-specific field (documents.FIELDS) each term must agree with
FIELD_TERM = {"delivery_period": "delivery", "completion_period": "completion",
              "contract_period": "contract_period"}

_FIGURE = re.compile(r"(\d+(?:\.\d+)?)\s*(%|(?:percent|per cent|days?|weeks?|months?|"
                     r"years?)\b)", re.IGNORECASE)
_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
_SENTENCE = re.compile(r"(?<!\d)\.(?!\d)|[;\n]")       # a decimal point does not end one
VAGUE = re.compile(r"\b(as (and when )?required|as per (the )?requirement|as directed|"
                   r"as applicable|suitable|approx(imately)?|etc|to be (decided|"
                   r"finali[sz]ed|confirmed)|tbd|tba|reasonable|if (necessary|required)|"
                   r"and/or|best quality|wherever possible)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Figure:
    term: str
    kind: str          # "days" or "percent"
    value: float
    text: str          # as written, "8 weeks"


def figure(text: str) -> tuple[str, float, str] | None:
    """(kind, value, as written) of the first period or percentage in text."""
    m = _FIGURE.search(text or "")
    if not m:
        return None
    number, unit = float(m.group(1)), m.group(2).lower()
    if unit.startswith(("%", "per")):
        return "percent", number, m.group(0)
    return "days", number * _DAYS[unit.rstrip("s")], m.group(0)


def terms(text: str) -> list[Figure]:
    """Each term's first figure, sentence by sentence."""
    out = []
    for sentence in _SENTENCE.split(text or ""):
        for term, pattern in TERMS.items():
            hit = re.search(pattern, sentence, re.IGNORECASE)
            if not hit:
                continue
            found = figure(sentence[hit.start():])
            if found:
                out.append(Figure(term, found[0], found[1], found[2]))
    return out


@dataclass(frozen=True)
class Place:
    doc: str           # the document kind, or "fields"
    ref: str           # "scc 2", "field delivery_period"
    text: str


def places(documents: list[dict], fields: dict) -> list[Place]:
    """Every selected clause of every required document, and the contract-
    specific fields, as places a term can be stated."""
    out = []
    for doc in documents:
        if doc.get("required") is False:
            continue
        for n, clause in enumerate(doc.get("clauses") or [], 1):
            if clause.get("selected"):
                out.append(Place(doc["kind"], f"{doc['kind']} {n}", clause["text"]))
    for key, value in (fields or {}).items():
        if key in FIELD_TERM:
            out.append(Place("fields", f"field {key}", f"{FIELD_TERM[key].replace('_', ' ')} "
                                                          f"{value}"))
    return out


def conflicts(found: list[Place]) -> list[dict]:
    """[{term, refs, says}] where two places give the same term different
    figures of the same kind. A field is read as its term ("delivery 8 weeks")."""
    seen: dict[tuple[str, str], list[tuple[Place, Figure]]] = {}
    for place in found:
        for f in terms(place.text):
            seen.setdefault((f.term, f.kind), []).append((place, f))
    out = []
    for (term, _kind), hits in seen.items():
        values = {f.value for _p, f in hits}
        if len(values) < 2:
            continue
        stated = "; ".join(f"{p.ref}: {f.text}" for p, f in hits[:6])
        out.append({"term": term, "refs": [p.ref for p, _f in hits][:20],
                    "says": f"{term.replace('_', ' ')} is stated differently: {stated}"[:300]})
    return out


def vague(text: str) -> str:
    """The words that leave a term open, or ""."""
    m = VAGUE.search(text or "")
    return m.group(0) if m else ""
