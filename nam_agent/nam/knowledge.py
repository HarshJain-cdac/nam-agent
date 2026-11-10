"""Knowledge and retrieval (plan Step 13): where each fact of a case comes
from, how far it binds, and a check that none is uncited.

The sources NAM knows, and their authority:

  source               system   authority        connected
  past tenders         kg       precedent        yes (tenders, tender_record, enrichment)
  clause library       kg       precedent        yes: the clauses each past tender issued
                                                 (clauses) and NRL's standing NIT text
                                                 (boilerplate); no clause repository of
                                                 record
  past rates           kg       precedent        yes (price_history): medians of past NRL
                                                 tenders, never the latest rate (R7)
  NRL SOR              -        nrl_schedule     no: the officer types the rate with its
                                                 item reference (schedule=nrl_sor)
  DSR, DAR, APWD, ...  -        reference        no: typed the same way, always with the
                                                 non-NRL alert (R9)
  procurement manual   -        -                no: nothing is answered from it
  NAM policy files     policy/  policy           yes: rule percentages, thresholds, PR
                                                 fields, compliance list (several DRAFT)
  the officer          -        officer          a number or text typed by a named user

Every fact on a case carries one of these. A fact without one is uncited,
and the guardrail `citation` refuses the step that would store it
(guardrails._artifact). Never auto-select a precedent, never invent a rate:
those are the guardrails `precedent` and `rate_source`.

  sources()      the table above, for /v1/nam/health
  authority()    the authority of one fact's source
  facts()        every fact of a set of artifacts with its source and authority
  uncited()      the facts that have none
"""

from __future__ import annotations

from collections.abc import Iterator

AUTHORITIES = {
    "precedent": "a past NRL tender in the knowledge graph: evidence an officer weighs, "
                 "never a decision",
    "nrl_schedule": "an NRL SOR rate, typed by the officer with its item reference",
    "reference": "a DSR, DAR or other non-NRL schedule rate: reference data, not "
                 "NRL-approved (R9)",
    "policy": "a NAM policy file (nam_agent/nam/policy/): a rule percentage, a threshold, a "
              "proposed requirement; several are DRAFT",
    "officer": "typed by a named officer in a call, or decided by a department",
    "derived": "computed in code from other cited facts (a line's quantity times a rate, "
               "a rounding up)",
    "record": "the case's own Need Record and its history: what was decided on it, by whom",
}

SOURCES = (
    {"name": "past tenders", "system": "kg", "authority": "precedent", "connected": "kg",
     "tools": ["tenders", "tender_record", "enrichment", "documents", "suppliers"]},
    {"name": "clause library", "system": "kg", "authority": "precedent", "connected": "kg",
     "tools": ["clauses", "boilerplate"],
     "note": "the clauses past tenders issued and NRL's standing NIT text; no clause "
             "repository of record is connected"},
    {"name": "past rates", "system": "kg", "authority": "precedent", "connected": "kg",
     "tools": ["price_history", "quantity_history"],
     "note": "medians of past NRL tenders, never the latest rate (R7)"},
    {"name": "NRL SOR", "system": "", "authority": "nrl_schedule", "connected": "",
     "tools": [], "note": "not connected: the officer types the rate with its item "
                          "reference (schedule=nrl_sor, ref=...)"},
    {"name": "DSR, DAR and other schedules", "system": "", "authority": "reference",
     "connected": "", "tools": [],
     "note": "not connected: typed by the officer, always with the non-NRL alert (R9)"},
    {"name": "procurement manual and circulars", "system": "", "authority": "",
     "connected": "", "tools": [], "note": "not connected: nothing is answered from it"},
    {"name": "NAM policy files", "system": "policy", "authority": "policy",
     "connected": "always", "tools": [],
     "note": "guardrails, PR fields, guided forms, compliance list, DoA (several DRAFT)"},
)

_SCHEDULE = {"nrl_sor": "nrl_schedule"}


def sources(kg_configured: bool) -> list[dict]:
    out = []
    for s in SOURCES:
        on = s["connected"] == "always" or (s["connected"] == "kg" and kg_configured)
        out.append({k: v for k, v in s.items() if k != "connected"} | {"connected": on})
    return out


def authority(source, schedule: str | None = None) -> str:
    """The authority of a fact's source, or "" when it has none.

    source  an estimate source dict ({tool, evidence, path}, {officer, quote},
            {rule, pct, of}, {line, component}), or a clause or requirement
            source string (a case key, "suggested from PUR-4", "officer",
            "policy", "department")
    """
    if isinstance(source, dict):
        if source.get("rule") == "escalation":
            # a past price brought up to date: the fact is the price it started from
            return authority(source.get("base"), schedule)
        if source.get("tool") and source.get("evidence"):
            return "precedent"
        if source.get("officer"):
            if schedule:
                return _SCHEDULE.get(schedule, "reference")
            return "officer"
        if source.get("rule") == "round_up" or "line" in source:
            return "derived"
        if source.get("rule"):
            return "policy"
        return ""
    text = str(source or "").strip()
    if not text:
        return ""
    if text in ("officer", "department"):
        return "officer"
    if text == "policy":
        return "policy"
    return "precedent"                    # a tender: "PUR-4", "suggested from PUR-4", ...


def _estimate(est: dict) -> Iterator[dict]:
    for n, line in enumerate(est.get("lines") or [], 1):
        if line.get("rate") is None:
            continue                      # a New Item states no rate
        where = f"estimate line {n}"
        comps = line.get("components") or []
        if comps:
            for j, comp in enumerate(comps, 1):
                yield {"fact": f"{where} component {j} ({str(comp.get('name'))[:60]})",
                       "value": comp.get("amount"), "source": comp.get("source"),
                       "authority": authority(comp.get("source"), line.get("schedule"))}
            continue
        yield {"fact": f"{where} rate ({str(line.get('item'))[:60]})", "value": line["rate"],
               "source": line.get("source"),
               "authority": authority(line.get("source"), line.get("schedule"))}


def _documents(docs: dict) -> Iterator[dict]:
    for doc in docs.get("documents") or []:
        for n, clause in enumerate(doc.get("clauses") or [], 1):
            if clause.get("selected"):
                yield {"fact": f"{doc.get('kind')} clause {n}", "value": clause.get("text"),
                       "source": clause.get("source"),
                       "authority": authority(clause.get("source"))}
        if doc.get("kind") == "estimate":
            for row in doc.get("rows") or []:
                if row.get("rate") is not None:
                    yield {"fact": f"estimate document line {row.get('n')}",
                           "value": row["rate"], "source": row.get("source"),
                           "authority": "derived" if row.get("source") else ""}


def _compliance(comp: dict) -> Iterator[dict]:
    for n, req in enumerate(comp.get("requirements") or [], 1):
        yield {"fact": f"compliance requirement {n}", "value": req.get("text"),
               "source": req.get("source"), "authority": authority(req.get("source"))}
        for c in req.get("issued") or []:
            yield {"fact": f"compliance requirement {n} issued wording",
                   "value": c.get("text"), "source": c.get("case_key"),
                   "authority": authority(c.get("case_key"))}


def _materials(mat: dict) -> Iterator[dict]:
    for n, s in enumerate(mat.get("suggestions") or [], 1):
        yield {"fact": f"material suggestion {n}", "value": s.get("item"),
               "source": s.get("source"), "authority": authority(s.get("source"))}


_FACTS = {"estimate": _estimate, "documents": _documents, "compliance": _compliance,
          "materials": _materials}


def facts(artifacts: dict) -> list[dict]:
    """Every cited fact of these artifacts ({name: value}), with its source
    and authority ("" when it has none)."""
    out = []
    for name, value in artifacts.items():
        walk = _FACTS.get(name)
        if walk and isinstance(value, dict):
            out += list(walk(value))
    return out


def uncited(artifacts: dict) -> list[str]:
    """The facts without a source: each as "where: value"."""
    return [f"{f['fact']}: {str(f['value'])[:80]}" for f in facts(artifacts)
            if not f["authority"]]
