"""NAM step 1, Need capture: one sentence in, every related past tender out.

The officer states a need ("i need to procure pipes"). This step scans the
whole knowledge graph for it, sorts what it finds by how related it really is,
writes a compact account into the need's dossier (see nam_dossier.py), and
decides whether NAM now knows enough to move on to the References step.

WHAT "SCAN THE WHOLE GRAPH" MEANS HERE
--------------------------------------
Two lanes, run in parallel:

  lexical   /api/kg/tenders walks every Tender -> Item edge in the graph and
            returns each tender whose subject or line items name the thing.
            Complete, literal, about a second.
  semantic  /api/kg/precedent, the embedding search. It finds what the words
            miss ("tubes" for "pipes") and takes ~15s. When it is down, the
            lexical lane still gives a complete literal answer, and the dossier
            says the semantic lane was off.

WHAT COUNTS AS RELATED
----------------------
The lexical lane searches for the HEAD of the need (the thing itself: "pipes"
in "carbon steel pipes for RTA"), not every word. Searching every word matches
"steel" in "stainless steel valves". Each tender found then falls in one tier:

  direct       its subject names the thing: it was a procurement OF it
  line items   it bought the thing as 3+ line items, or the semantic lane
               matched one of its items: it procured it inside a larger job
  incidental   1-2 line items mention it and nothing else does. Listed by
               number in the dossier so nothing is hidden, never shown as related

READY FOR REFERENCES
--------------------
NAM moves on when at least one related tender carries BOQ lines AND clauses
(PQC, SCC or specs): that is material the next steps can be built from. A need
with no related tender, or only related tenders that hold nothing, stays in
capture, and the response says what to add.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from nam_agent.nam import dossier as nam_dossier
from nam_agent.nam import followup as nam_followup
from nam_agent.nam import guard
from nam_agent.nam import need as nam
from nam_agent.nam import scope as nam_scope
from nam_agent.nam.futures import ready
from nam_agent.tools.registry import Tool

# Words that say HOW something is bought, not WHAT. Added to nam's scaffold
# for this step: "procure" left in a literal query matches "Procurement of" in
# nearly every subject, and "AMC" as the head of "AMC for housekeeping" would
# search for the contract form instead of the service.
_GENERIC = nam._SCAFFOLD | {
    "procure", "procuring", "buy", "buying", "purchase", "purchasing", "order",
    "amc", "arc", "tender", "tenders", "item", "items", "some", "our", "we",
    "urgent", "basis", "quantity", "get", "looking", "like", "would",
    # vague nouns: "i need something" names no item and must be asked about
    "something", "anything", "thing", "things", "stuff", "material", "materials",
    "equipment", "goods",
    # units after a quantity: "oil 10000 liters" names oil, not liters
    "nos", "units", "unit", "pcs", "pieces", "liters", "litres", "ltrs", "ltr", "kgs",
    "meters", "metres", "mtrs", "mtr", "sets", "each", "numbers", "tons", "tonnes",
    # the document being asked for, not the item: "draft a NIT for valves"
    "nit", "nits",
}
# The first of these words ends the phrase that names the thing:
# "pipes | for RTA", "housekeeping | at Plant-1". "of" is not one of them,
# because in "supply of pipes" the thing comes after it. ";" is the join a
# refinement adds, so "procure pipes; 6 inch carbon steel" keeps "pipes".
_BOUNDARY = re.compile(r"\b(?:for|at|in|with|during|to|from|under|within|by|as)\b|;",
                       re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z][A-Za-z-]{2,}")

LINE_ITEM_MIN = 3       # line items needed to count as procured, not mentioned
SUMMARY_CAP = 100       # related tenders summarised (the API takes 20 per call);
                        # the follow-up questions need department and date on each
_TIERS = ("exact items", "direct", "line items")
SEMANTIC_WAIT_S = 5.0   # how long a scan waits for the meaning lane (the KG's can take ~35s)
# the meaning lane's calls still running, by kg_url|query, merged when done
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="nam-scan")
_LATE: dict = {}


def _strip_captured(need: nam.NeedRecord) -> str:
    """The requirement minus location, term and quantity, which are fields
    of their own and are not what is being procured."""
    text = need.raw_text or ""
    for captured in (need.location, need.term, need.quantity):
        if captured:
            text = re.sub(re.escape(captured), " ", text, flags=re.IGNORECASE)
    return text


def search_terms(text: str) -> list[str]:
    """The substantive words of a requirement, in order, lower case."""
    return [w.lower() for w in _WORD.findall(text or "") if w.lower() not in _GENERIC]


def head_term(text: str) -> str:
    """The thing being procured: the last substantive word of the first phrase
    that has one. "i need to procure pipes" -> "pipes" (the first phrase,
    "i need", names nothing); "carbon steel pipes for RTA" -> "pipes"."""
    for segment in _BOUNDARY.split(text or ""):
        words = search_terms(segment)
        if words:
            return words[-1]
    return ""


# Words that say "a new procurement" without naming what: "something else",
# "another one", "some other item". A need made of these names no item, so no
# case is opened for it (the officer is asked what to procure).
_VAGUE = {"something", "else", "other", "another", "anything", "thing", "things", "stuff",
          "item", "items", "one", "ones", "new", "different", "more", "some", "any", "else's",
          "procure", "procurement", "buy", "purchase", "order", "it", "this", "that"}


def vague(text: str) -> bool:
    """True when the words name no item: every word is one of _VAGUE, a typo
    of one ("somthing", "esle"), or a run-together form ("procresomeething")."""
    from nam_agent.nam.drive import _one_edit

    words = re.findall(r"[a-z']+", (text or "").lower())
    if not words:
        return True

    def empty(w: str) -> bool:
        return (w in _VAGUE or "thing" in w or w.startswith(("procur", "some"))
                or (len(w) >= 4 and any(_one_edit(w, v) for v in _VAGUE if len(v) >= 4)))
    return all(empty(w) for w in words)


def _plain_number(value: str) -> str:
    """"20.0" and "20" are one quantity: a number is written without trailing
    zeros; anything else is returned trimmed."""
    value = " ".join((value or "").split())
    if re.fullmatch(r"\d+\.\d+", value):
        value = value.rstrip("0").rstrip(".")
    return value


def _said_in_full(option: dict, words: str) -> bool:
    """True when the officer's words carry every part of the option, numbers
    and units included. search_terms drops "15" and "mm", so "15 mm CPVC
    pipe" once looked said by "CPVC pipes" and 15 mm was recorded as the
    officer's own answer."""
    def tokens(text: str) -> set[str]:
        return set(re.findall(r"[a-z]+|\d+(?:\.\d+)?", (text or "").lower()))
    said = set().union(*({t} | (_forms(t) if t.isalpha() else set()) for t in tokens(words)))
    need = (tokens(option.get("value")) | tokens(option.get("label"))) - _GENERIC - {
        "of", "the", "a", "an", "and", "for", "with", "in", "to"}
    # every word of the option, in one of its forms ("pipe" by "pipes")
    return bool(need) and all(t in said or (t.isalpha() and _forms(t) & said) for t in need)


# What a tender's subject says it buys. The KG's search rows carry no
# category, and a need for temperature gauges (Goods) also matches the
# installation and maintenance contracts whose line items name gauges.
_KIND_WORDS = {
    nam.GOODS: {"supply", "supplies", "procurement", "purchase", "spares", "spare", "material",
                "materials", "items", "equipment", "goods", "consumables"},
    nam.SERVICES: {"maintenance", "assistance", "installation", "operation", "services",
                   "service", "hiring", "manpower", "amc", "repair", "calibration", "jobs",
                   "o&m", "overhauling", "inspection"},
    nam.WORKS: {"construction", "civil", "works", "renovation", "fabrication", "laying",
                "painting", "building", "erection", "demolition"},
}


def tender_kind(subject: str) -> str:
    """Goods, Services or Works by the words of a tender's subject, or ""
    when they do not say (or say two equally)."""
    words = re.findall(r"[a-z&]+", (subject or "").lower())
    scores = {k: sum(w in v for w in words) for k, v in _KIND_WORDS.items()}
    best = max(scores.values())
    top = [k for k, v in scores.items() if v == best]
    return top[0] if best and len(top) == 1 else ""


def fit_category(entries: list[dict], category: str) -> list[dict]:
    """The tenders whose subject fits the confirmed category, and those it
    cannot tell; all of them when none fits (never an empty list)."""
    if not category:
        return entries
    kept = [e for e in entries if tender_kind(e.get("subject", "")) in ("", category)]
    return kept or entries


def _forms(word: str) -> set[str]:
    """A word and its singular/plural twin, the same crude rule the KG uses."""
    forms = {word}
    if word.endswith("ies"):
        forms.add(word[:-3] + "y")
    elif word.endswith("es") and word[-3:-2] in ("s", "x", "h", "z"):
        forms.add(word[:-2])
    elif word.endswith("s") and not word.endswith("ss"):
        forms.add(word[:-1])
    else:
        forms |= {word + "s", word + "es"}
    return forms


def coverage(terms: list[str], subject: str) -> float:
    """Share of the requirement's terms that the tender's subject contains."""
    if not terms:
        return 0.0
    low = (subject or "").lower()
    return sum(1 for t in terms if any(f in low for f in _forms(t))) / len(terms)


def classify_tenders(lexical: list[dict], semantic: list[dict],
                     terms: list[str], exact: bool = False) -> tuple[list[dict], list[dict]]:
    """(related, incidental) from the two lanes' rows. Pure: no network.

    `exact`: the KG found line items that are the whole item ("CPVC pipes",
    its item_match.exact). Then only the tenders carrying such lines are
    related, read from those lines; a tender that only mentions "pipes" is
    incidental, whatever its subject says."""
    by_key: dict[str, dict] = {}
    qualifiers = [t for t in terms[:-1]] if len(terms) > 1 else []
    for row in lexical:
        key = row.get("case_key")
        if not key:
            continue
        count = row.get("matching_item_count", 0) or 0
        lines = [str(i) for i in row.get("exact_items") or []]
        if exact:
            tier, why = (("exact items", f"{len(lines)} line item(s) are the item asked for")
                         if lines else ("incidental", "names the kind of item, not this one"))
            by_key[key] = {
                "case_key": key, "tender_no": row.get("tender_no"),
                "subject": row.get("job_name") or "", "tier": tier, "why": why,
                "matching_item_count": len(lines) if lines else count,
                "matching_items": [x[:200] for x in (lines or row.get("matching_items") or [])[:3]],
                "items_sample": [x[:160] for x in (lines or row.get("matching_items") or [])[:8]],
                "semantic": False,
            }
            continue
        if row.get("matched_by_name"):
            tier, why = "direct", "subject names it"
        elif count >= LINE_ITEM_MIN:
            tier, why = "line items", f"{count} line items name it"
        else:
            tier, why = "incidental", f"{count} line item(s) mention it"
        by_key[key] = {
            "case_key": key, "tender_no": row.get("tender_no"),
            "subject": row.get("job_name") or "", "tier": tier, "why": why,
            "matching_item_count": count,
            "matching_items": (row.get("matching_items") or [])[:3],
            # the longer sample the follow-up questions read variants from
            "items_sample": [str(i)[:160] for i in (row.get("matching_items") or [])[:8]],
            "semantic": False,
        }

    for hit in semantic:
        key = hit.get("pur_case_id")
        items = hit.get("matched_items") or []
        if not key or not items:
            continue   # a nearest neighbour that matched no item is not related
        if exact and qualifiers and not any(
                all(q in str(i).lower() for q in qualifiers) for i in items):
            continue   # near in meaning, but none of its items is the item asked for
        entry = by_key.get(key)
        if entry:
            entry["semantic"] = True
            if entry["tier"] == "incidental":
                entry["tier"], entry["why"] = "line items", "semantic match on its items"
            continue
        by_key[key] = {
            "case_key": key, "tender_no": hit.get("tender_no"),
            "subject": hit.get("job_name") or "", "tier": "line items",
            "why": "semantic match on its items", "matching_item_count": len(items),
            "matching_items": [str(i)[:120] for i in items[:3]],
            "items_sample": [str(i)[:160] for i in items[:8]], "semantic": True,
        }

    for entry in by_key.values():
        entry["coverage"] = round(coverage(terms, entry["subject"]), 2)
    related = sorted((e for e in by_key.values() if e["tier"] in _TIERS),
                     key=lambda e: (_TIERS.index(e["tier"]), -e["coverage"],
                                    -e["matching_item_count"], e["case_key"]))
    incidental = sorted((e for e in by_key.values() if e["tier"] == "incidental"),
                        key=lambda e: e["case_key"])
    return related, incidental


def readiness(related: list[dict]) -> dict:
    """Whether NAM knows enough to move to References, and why."""
    if not related:
        return {"ready": False, "material_cases": [],
                "reason": "No past tender in the graph procured this.",
                "ask": ("Describe the need another way: the item's other name, its "
                        "material or grade, or the equipment it is for.")}
    material = [r["case_key"] for r in related
                if (r.get("counts") or {}).get("boq_lines", 0) > 0
                and sum((r.get("counts") or {}).get(k, 0)
                        for k in ("pqc_clauses", "scc_clauses", "spec_clauses")) > 0]
    if not material:
        return {"ready": False, "material_cases": [],
                "reason": (f"{len(related)} related tender(s) found, but none carries both "
                           "BOQ lines and clauses to build from."),
                "ask": "Add detail (size, grade, the plant or job it is for) to widen the match."}
    return {"ready": True, "material_cases": material,
            "reason": (f"{len(material)} related tender(s) carry BOQ lines and clauses: "
                       f"{', '.join(material[:5])}"
                       + (f" and {len(material) - 5} more." if len(material) > 5 else ".")),
            "ask": ""}


def _summaries(keys: list[str], kg_url: str, fetch) -> dict[str, dict]:
    chunks = [keys[i:i + 20] for i in range(0, len(keys), 20)]

    def one(chunk):
        try:
            return fetch(kg_url, "/api/kg/case_summary", {"case_keys": ",".join(chunk)})
        except Exception:  # noqa: BLE001 - rows without counts beat no rows
            return {}

    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(chunks))) as pool:
        for data in pool.map(one, chunks):
            out.update({c["case_key"]: c for c in data.get("cases", [])})
    return out


def scan(need: nam.NeedRecord, kg_url: str, fetch=nam._get, text: str | None = None) -> dict:
    """Both lanes over the whole graph, then tiers and summaries. `text` is
    one item of a request with several; else the whole need."""
    text = text or _strip_captured(need)
    terms = search_terms(text)
    head = head_term(text)
    if not head:
        return {"head": "", "terms": terms, "error": "Nothing in the requirement names an item."}
    query = " ".join(terms[:6])

    def call(path, params):
        try:
            return fetch(kg_url, path, params), None
        except Exception as exc:  # noqa: BLE001
            return {}, str(exc)[:160]

    # the meaning lane can take half a minute on the KG; it is waited for a
    # few seconds, then the officer gets the literal matches and its tenders
    # join on the next step (merge_late)
    key = f"{kg_url}|{query}"
    sem_f = _LATE.pop(key, None) or _POOL.submit(
        call, "/api/kg/precedent", {"requirement": query, "limit": 10})
    # the whole item first ("cpvc pipes"): the KG marks the line items that
    # are that item (exact_items). Only when none is, the head word alone
    # ("pipes"), as before, so a rare wording never loses its tenders.
    phrase = " ".join(terms[:6]) if len(terms) > 1 else head
    lex_f = _POOL.submit(call, "/api/kg/tenders", {"query": phrase, "limit": 100})
    health_f = _POOL.submit(call, "/health", {})
    # the KG's item check reads every line item for the whole item, NRL's
    # abbreviations included (TG temperature gauge): the tenders that bought
    # it, when the tender search does not mark them ("temperature gauge" once
    # fell back to all 18 tenders naming any "gauge", wire-gauge shutters too)
    items_f = _POOL.submit(call, "/api/kg/items", {"query": phrase, "limit": 40}) \
        if phrase != head else None
    (lexical, lex_err), (health, _) = lex_f.result(), health_f.result()
    exact = phrase != head and int(((lexical or {}).get("item_match") or {}).get("exact") or 0) > 0
    if items_f is not None and not exact and not lex_err:
        try:
            checked, _err = items_f.result(timeout=ITEMS_WAIT_S)
        except FutureTimeout:
            checked = {}
        bought = item_tenders(checked, lexical.get("tenders") or [])
        if bought:
            lexical, exact = {**lexical, "tenders": bought, "count": len(bought)}, True
    if phrase != head and not exact and not lex_err:
        lexical, lex_err = call("/api/kg/tenders", {"query": head, "limit": 100})
    late = False
    try:
        semantic, sem_err = sem_f.result(timeout=SEMANTIC_WAIT_S)
    except FutureTimeout:
        semantic, sem_err, late = {}, None, True
        _LATE[key] = sem_f

    if lex_err and (sem_err or late):
        return {"head": head, "terms": terms,
                "error": f"Knowledge graph unreachable: {lex_err}"}

    prefetch_quantities(kg_url, query, fetch)    # for the quantity questions later
    lexical_tenders = lexical.get("tenders") or []
    related, incidental = _tiered(lexical_tenders, semantic.get("tenders") or [], terms,
                                  kg_url, fetch, exact)
    return {
        "head": head, "terms": terms, "query": query,
        "searched": sorted(lexical.get("terms") or [head]),
        "tenders_in_graph": health.get("tenders"),
        "lexical_matches": lexical.get("count", 0),
        "lexical_error": lex_err,
        "lexical_tenders": lexical_tenders,
        "exact": exact,
        "semantic": {"on": not late and not sem_err and not semantic.get("retrieval_error"),
                     "pending": late,
                     "best_similarity": semantic.get("best_similarity"),
                     "error": sem_err or semantic.get("retrieval_error")},
        "related": related, "incidental": incidental,
    }


ITEMS_WAIT_S = 20     # the KG's item check, waited for this long (it takes about 1 s)


def item_tenders(checked: dict, lexical: list[dict]) -> list[dict]:
    """The tenders whose line items ARE the item, from the KG's item check
    (/api/kg/items), as tender rows with their exact lines; [] when it found
    none at all (or could not be had). Rows at another size count too: the
    item was bought, and the size is asked about after."""
    if not isinstance(checked, dict) or checked.get("answer") not in ("yes", "similar"):
        return []
    rows = (checked.get("exact") or []) if checked.get("answer") == "yes" \
        else (checked.get("similar") or [])
    lines: dict[str, list[str]] = {}
    for r in rows:
        if isinstance(r, dict) and r.get("case_key") and r.get("item"):
            lines.setdefault(r["case_key"], []).append(str(r["item"]))
    if not lines:
        return []
    by_key = {t.get("case_key"): t for t in lexical if isinstance(t, dict)}
    info = checked.get("tenders") or {}
    order = [k for k in by_key if k in lines] + [k for k in lines if k not in by_key]
    out = []
    for key in order:
        row = dict(by_key.get(key) or {})
        t = info.get(key) or {}
        row.update(case_key=key, exact_items=lines[key],
                   tender_no=row.get("tender_no") or t.get("tender_no"),
                   job_name=row.get("job_name") or t.get("job_name") or "")
        out.append(row)
    return out


def _tiered(lexical: list[dict], semantic: list[dict], terms: list[str], kg_url: str,
            fetch, exact: bool = False) -> tuple[list[dict], list[dict]]:
    """The tiers of both lanes' tenders, the related ones with their summaries."""
    related, incidental = classify_tenders(lexical, semantic, terms, exact)
    summaries = _summaries([r["case_key"] for r in related[:SUMMARY_CAP]], kg_url, fetch)
    for entry in related:
        s = summaries.get(entry["case_key"])
        if s:
            entry["tender_no"] = s.get("tender_no") or entry["tender_no"]
            entry.update({
                "department": s.get("department"),
                "estimated_value": s.get("estimated_value"),
                "bid_opening_date": s.get("bid_opening_iso") or s.get("bid_opening_date"),
                "awarded_to": s.get("awarded_to") or [],
                "counts": {k: s.get(k, 0) for k in ("boq_lines", "pqc_clauses",
                                                    "scc_clauses", "spec_clauses")},
            })
    return related, incidental


def merge_late(result: dict | None, kg_url: str, fetch=nam._get) -> dict | None:
    """The scan with the meaning lane's tenders added, once they have come
    in; None while they have not (or there were none to wait for)."""
    if not result or not (result.get("semantic") or {}).get("pending"):
        return None
    key = f"{kg_url}|{result.get('query', '')}"
    fut = _LATE.get(key)
    if fut is None:                 # a restart lost it: the literal matches stand
        return {**result, "semantic": {**result["semantic"], "pending": False,
                                       "error": "the meaning search did not finish"}}
    if not fut.done():
        return None
    _LATE.pop(key, None)
    semantic, sem_err = ready(fut, ({}, "the meaning search did not finish"))
    related, incidental = _tiered(result.get("lexical_tenders") or [],
                                  semantic.get("tenders") or [], result.get("terms") or [],
                                  kg_url, fetch, bool(result.get("exact")))
    return {**result, "related": related, "incidental": incidental,
            "semantic": {"on": not sem_err and not semantic.get("retrieval_error"),
                         "pending": False, "best_similarity": semantic.get("best_similarity"),
                         "error": sem_err or semantic.get("retrieval_error")}}


def _render_followups(followups: dict | None) -> list[str]:
    """The questions asked so far, each answer, and how far it narrowed the list."""
    if not followups or not (followups.get("trail") or followups.get("pending")
                             or followups.get("stopped")):
        return []
    out = ["### Follow-up questions", ""]
    if followups.get("trail"):
        out += ["| # | Question | Answer | Tenders | Note |", "|---:|---|---|---|---|"]
        for i, step in enumerate(followups["trail"], 1):
            out.append(f"| {i} | {_cell(step.get('question') or step['id'], 90)} | "
                       f"{_cell(step.get('answer'), 40)} | "
                       f"{step['before']} -> {step['after']} | {_cell(step['note'], 60)} |")
        out.append("")
    if followups.get("pending"):
        out += [f"**Asking now:** {followups['pending']['text']}", ""]
    elif followups.get("stopped"):
        out += [f"**Stopped asking:** {followups['stopped']}", ""]
    return out


def _cell(value, width: int = 60) -> str:
    return str(value if value not in (None, "") else "-").replace("|", "/").replace("\n", " ")[:width]


def render(need: nam.NeedRecord, result: dict, ready: dict,
           followups: dict | None = None) -> str:
    """The capture step's section of the dossier: compact, every row cited."""
    if result.get("error"):
        return f"**Could not scan:** {result['error']}\n\n**Next:** {ready.get('ask', '')}"

    def pred(field: str) -> str:
        p = getattr(need, field) or {}
        if not p.get("value"):
            return "to ask"
        return f"{p['value']}" + ("" if p.get("confident") else " (to confirm)")

    sem = result["semantic"]
    if not sem["on"]:
        semantic = f"off ({_cell(sem['error'], 80)})"
    elif sem["best_similarity"] is None:
        semantic = "on, no close match"
    else:
        semantic = f"on, best similarity {sem['best_similarity']}"
    related, incidental = result["related"], result["incidental"]
    captured = " · ".join(f"{k}: {v}" for k, v in
                          (("location", need.location), ("term", need.term),
                           ("quantity", need.quantity)) if v) or "none stated"
    lines = [
        (f"- **Item:** {result['head']} · **searched:** {', '.join(result['searched'])} "
         f"· **semantic lane:** {semantic}"),
        (f"- **Classification:** category {pred('category')} · procurement type "
         f"{pred('procurement_type')} · tender type {pred('tender_type')}"),
        f"- **Captured:** {captured}",
        (f"- **Graph scanned:** {result.get('tenders_in_graph') or '?'} tenders · "
         f"**{len(related)} related**"
         + (f" (of {followups['total_related']} before the follow-up answers)"
            if followups and followups.get("total_related", len(related)) != len(related) else "")
         + f" · {len(incidental)} incidental"),
        "",
    ]
    lines += _render_followups(followups)
    lines += [f"### Related tenders ({len(related)})", ""]
    if related:
        lines += ["| # | PUR | Tender no | Subject | Why | BOQ | PQC/SCC/Spec | Value | Opened |",
                  "|---:|---|---|---|---|---:|---|---|---|"]
        for i, r in enumerate(related, 1):
            c = r.get("counts")
            docs = f"{c['pqc_clauses']}/{c['scc_clauses']}/{c['spec_clauses']}" if c else "-"
            lines.append(
                f"| {i} | {r['case_key']} | {_cell(r.get('tender_no'), 20)} | "
                f"{_cell(r['subject'])} | {r['tier']}: {r['why']} | "
                f"{c['boq_lines'] if c else '-'} | {docs} | "
                f"{_cell(r.get('estimated_value'), 20)} | {_cell(r.get('bid_opening_date'), 12)} |")
    else:
        lines.append("None.")
    if incidental:
        lines += ["", f"**Incidental mentions, not counted as related ({len(incidental)}):** "
                  + ", ".join(e["case_key"] for e in incidental)]
    lines += ["", "### Readiness", "",
              ("**Follow-up questions open.** " if followups and followups.get("pending")
               else "**Ready for References.** " if ready["ready"] else "**Not ready.** ")
              + ready["reason"] + (f" {ready['ask']}" if ready.get("ask") else "")]
    return "\n".join(lines)


def _refine(need: nam.NeedRecord, text: str) -> None:
    """Add the officer's extra words to a need and reclassify it, keeping
    every field the officer already set themselves."""
    need.raw_text = f"{need.raw_text}; {text.strip()}"
    predicted = nam.classify(need.raw_text)
    for field in ("category", "subtype", "procurement_type", "tender_type"):
        if (getattr(need, field) or {}).get("basis") != "you set this":
            setattr(need, field, predicted[field])
    for field in ("location", "term", "quantity"):
        if not getattr(need, field):
            setattr(need, field, predicted[field])


# Answers that are words about the item itself join the requirement, so later
# steps (similar cases, the semantic lane on a rescan) search with them too.
# The model's questions (ids q1, q2, ...) are about the item too.
_JOINS_TEXT = ("variant", "material", "scope")


def _record_answer(need: nam.NeedRecord, question_id: str, value: str) -> None:
    value = value.strip()
    if value.startswith(nam_followup.NOT):
        return      # "No" narrows the list but says nothing to add to the need
    if value and (question_id in _JOINS_TEXT or re.fullmatch(r"q\d+", question_id)):
        _refine(need, value)


def _next_question(need: nam.NeedRecord, head: str, narrowed: list[dict], answers: dict,
                   asked: list[dict], ask_model) -> tuple[dict | None, str, str]:
    """(question or None, why asking stopped, note on the model's turn).

    The model decides first; the harness has already checked what it said
    (nam_followup.check_model_question). When the model is off, fails, or its
    question does not hold up against the tenders, the question comes from
    the tenders' own facets instead."""
    if len(answers) >= nam_followup.MAX_QUESTIONS:
        return None, f"asked the most questions ({nam_followup.MAX_QUESTIONS})", ""
    if len(narrowed) <= nam_followup.narrow_enough():
        return None, f"{len(narrowed)} related tender(s) left, nothing more to narrow", ""
    note = "model not configured"
    if ask_model is not None:
        out = nam_followup.ask_model_for_question(ask_model, need.raw_text, head, narrowed, asked)
        if "question" in out:
            return {"id": f"q{len(answers) + 1}", **out["question"]}, "", ""
        if "done" in out:
            return None, out["done"], ""
        note = out["rejected"]
        if any(re.fullmatch(r"q\d+", k) for k in answers) and not nam_scope.tender_only():
            # the facet questions would repeat what the model already asked
            # (while the aim is the one final tender, they are asked anyway:
            # a facet the model already used no longer splits the list, so
            # next_question skips it by itself)
            return None, "no further question held up against the tenders", note
    q = nam_followup.next_question(narrowed, head, answers)
    return q, ("" if q else "the related tenders no longer differ in a way worth asking"), note


# -- several items in one request (PR flow 1(c)) -------------------------------------

MAX_ITEMS = 6
_LIST = re.compile(r",|&|\band\b", re.IGNORECASE)
_SPLIT_SYSTEM = (
    "You split a procurement request into the distinct items it asks to buy. Reply with "
    'JSON only: {"items": ["...", "..."]}. Copy each item\'s words from the request. A '
    "request for one thing, or for one thing with its size, range or grade, is one item. "
    f"At most {MAX_ITEMS} items.\n" + guard.RULE)


def _from_first_term(part: str) -> str:
    """A list part from its first substantive word: "I want Supply of X" -> "X"."""
    words = part.split()
    for i, w in enumerate(words):
        if search_terms(w):
            return " ".join(words[i:]).strip(" ,;")
    return ""


def _rule_items(text: str) -> list[str]:
    """The items of a list in the phrase that names the thing: its parts split
    on commas and "and". A part that starts with a number is the previous
    part's size or range, not an item; a part naming nothing means the words
    were not a list of items, so the request is one item."""
    phrase = next((seg for seg in _BOUNDARY.split(text or "") if search_terms(seg)), "")
    parts: list[str] = []
    for raw in _LIST.split(phrase):
        part = raw.strip()
        if not part:
            continue
        if parts and part[0].isdigit():
            parts[-1] = f"{parts[-1]} {part}"
            continue
        if not search_terms(part):
            return []
        parts.append(part)
    items = [_from_first_term(p) for p in parts]
    distinct = {" ".join(search_terms(i)) for i in items}
    return items if len(items) > 1 and len(distinct) == len(items) else []


def split_items(text: str, ask_model=None) -> list[str]:
    """The distinct items a request asks for; one entry or none means one item.
    The model splits when there is one (the PR flow says the LLM does), and
    the harness keeps its split only if every item is made of the request's
    own words and names a different thing. Else the list rule decides."""
    phrase = next((seg for seg in _BOUNDARY.split(text or "") if search_terms(seg)), "")
    if not _LIST.search(phrase):
        return []                   # no list in the words: one item, and no model call
    if ask_model is not None:
        try:
            raw = nam_followup._json_object(
                ask_model(_SPLIT_SYSTEM, guard.wrap(text[:600], "user", "request")))
        except Exception:  # noqa: BLE001 - the rule below is the fallback
            raw = None
        items = [" ".join(str(i).split())[:200] for i in (raw or {}).get("items") or []
                 if str(i).strip()]
        own = {f for w in search_terms(text) for f in _forms(w)}
        if (1 <= len(items) <= MAX_ITEMS
                and all(search_terms(i) and set(search_terms(i)) <= own for i in items)
                and len({" ".join(search_terms(i)) for i in items}) == len(items)):
            return items if len(items) > 1 else []
    return _rule_items(text)[:MAX_ITEMS]


def rank(related: list[dict]) -> list[str]:
    """The tenders in the PR flow's order (1(i)): those with an award (ordered)
    first, then the rest, the latest first within each. The KG records no
    delivery or cancellation, so those two cannot be told apart yet."""
    latest = sorted(related, key=lambda r: str(r.get("bid_opening_date") or ""), reverse=True)
    return [r["case_key"] for r in sorted(latest, key=lambda r: not r.get("awarded_to"))]


# -- the details every need must carry (PR flow 1(e)) ---------------------------------

def _detail_fields(need: nam.NeedRecord) -> list[dict]:
    """The guided form's fields need capture asks for, for the case's form
    (policy/guided_forms.json "capture"), each with `per_item` and
    `options_from`; none until the form is known."""
    from nam_agent.nam.playbooks import need_classify, need_guided_form

    form = need_classify.route(need)["guided_form"]
    if not form:
        return []
    fields = {q["key"]: q for q in need_guided_form.questions(form)}
    out = []
    for entry in need_guided_form.policy().get("capture", {}).get(form, []):
        spec = {"key": entry} if isinstance(entry, str) else entry
        if spec["key"] in fields:
            q = dict(fields[spec["key"]])
            if spec.get("text"):
                q["label"] = spec["text"]
            q["per_item"] = bool(spec.get("per_item"))
            q["options_from"] = spec.get("options_from", "")
            q["choices"] = [str(o) for o in spec.get("options") or []]
            q["in_days"] = [int(n) for n in spec.get("options_in_days") or []]
            out.append(q)
    return out


def _quantities_key(kg_url: str, text: str) -> str:
    return f"qty|{kg_url}|{text}"


def prefetch_quantities(kg_url: str, text: str, fetch) -> None:
    """Start the KG's price records for an item in the background: what NRL
    ordered before, for its quantity and unit questions later (the lookup
    can take half a minute, so it is never waited for)."""
    key = _quantities_key(kg_url, text)
    if kg_url and text and key not in _LATE:
        def call():
            try:
                return fetch(kg_url, "/api/kg/prices", {"requirement": text, "limit": 5})
            except Exception:  # noqa: BLE001 - no suggestions, never a failed step
                return {}
        _LATE[key] = _POOL.submit(call)


CASE_WAIT_S = 4.0       # the most a detail question waits for the tenders' records
CASES_READ = 4          # the matching tenders whose records are read for suggestions
_CASES: dict = {}       # case_key -> its KG record, for this process


def _case_records(keys: list[str], kg_url: str, fetch) -> list[dict]:
    """The KG records (BOQ, department) of the first matching tenders, read
    in parallel, cached; a record not back in CASE_WAIT_S is left out."""
    def one(key):
        try:
            return fetch(kg_url, "/api/kg/case", {"case_key": key})
        except Exception:  # noqa: BLE001 - a suggestion less, never a failed step
            return {}
    wanted = [k for k in keys[:CASES_READ] if k not in _CASES]
    futures = {k: _POOL.submit(one, k) for k in wanted}
    for key, fut in futures.items():
        try:
            _CASES[key] = fut.result(timeout=CASE_WAIT_S)
        except FutureTimeout:
            pass
    return [_CASES[k] for k in keys[:CASES_READ] if _CASES.get(k)]


def _data_options(source: str, item: str, related: list[dict], kg_url: str,
                  fetch=nam._get) -> list[dict]:
    """The values the matching past tenders carry for a detail, most common
    first, as suggestions (policy "options_from"); [] when there are none.
    A related tender's own field first; else its KG record (its BOQ lines
    that name the item's words, its department)."""
    from collections import Counter

    kind, _, field = source.partition(".")
    counts: Counter = Counter()
    if kind == "related":
        for r in related:
            value = " ".join(str(r.get(field) or "").split())[:80]
            if value:
                counts[value] += 1
        if not counts:
            for rec in _case_records([r["case_key"] for r in related], kg_url, fetch):
                value = " ".join(str(rec.get(field) or "").split())[:80]
                if value:
                    counts[value] += 1
    elif kind == "past_quantities":
        # Two numbers per value, never mixed: the distinct tenders that carry
        # it, and the line-item records it appears on. A tender with 20 pipe
        # lines in metres is 20 line items and 1 tender ("m (216 tenders)"
        # was really 216 line items in 15 tenders).
        tenders: dict[str, set] = {}
        lines: Counter = Counter()
        words = {f for w in search_terms(item) for f in _forms(w)}
        keys = [r["case_key"] for r in related]
        _case_records(keys, kg_url, fetch)
        for key in keys[:CASES_READ]:
            for line in (_CASES.get(key) or {}).get("boq") or []:
                text = {f for w in search_terms(str(line.get("item") or "")) for f in _forms(w)}
                if words and not words & text:
                    continue
                value = _plain_number(str(line.get("qty") if field == "qty"
                                          else line.get("uom") or ""))
                if value and value not in ("None", "0"):
                    tenders.setdefault(value, set()).add(key)
                    lines[value] += 1
        fut = _LATE.get(_quantities_key(kg_url, item))
        rows = (ready(fut) or {}).get("rows") or []
        for r in rows:
            if not r.get("matched") or not r.get("typical_qty"):
                continue
            value = _plain_number(str(r["typical_qty"].get("median") or "") if field == "qty"
                                  else str(r.get("uom") or ""))
            if not value or value in ("None", "0"):
                continue
            sources = {str(x) for x in r.get("sources") or []}
            seen = tenders.setdefault(value, set())
            if not sources & seen:
                # past price records of tenders not read above: their lines
                # count once each (n is the records the KG holds for it)
                lines[value] += int(r["typical_qty"].get("n") or 1)
            seen |= sources
        ranked = sorted(tenders, key=lambda v: (-len(tenders[v]), -lines[v]))
        return [{"label": v, "value": v, "count": len(tenders[v]), "lines": lines[v]}
                for v in ranked[:nam_followup.MAX_OPTIONS] if tenders[v] or lines[v]]
    return [{"label": v, "value": v, "count": n}
            for v, n in counts.most_common(nam_followup.MAX_OPTIONS)]


def _detail_question(q: dict, suggested: list[dict] | None = None, item: str = "") -> dict:
    """A detail as a question, always with choices: the form's own choices
    are closed (only they are taken); otherwise the tenders' values first,
    then the policy's own and the dates it names (open: a typed value is
    taken too). The question names the item it is for."""
    from datetime import date, timedelta

    closed = [{"label": o, "value": o} for o in q.get("options") or []]
    options = list(suggested or [])
    seen = {o["value"].lower() for o in options}
    for choice in q.get("choices") or []:
        if choice.lower() not in seen:
            seen.add(choice.lower())
            options.append({"label": choice, "value": choice, "count": 0})
    for days in q.get("in_days") or []:
        when = (date.today() + timedelta(days=days)).isoformat()
        options.append({"label": f"In {days // 30 or 1} month{'s' if days >= 60 else ''} "
                                 f"({when})" if days % 30 == 0 else f"In {days} days ({when})",
                        "value": when, "count": 0})
    text = q["label"].replace("{item}", item or "they")
    return {"id": f"d:{q['key']}", "text": text,
            "why": ("Values the matching past NRL tenders carry first; pick one or type your "
                    "own." if suggested else "Every PR needs it."),
            "options": closed or options[:nam_followup.MAX_OPTIONS], "closed": bool(closed),
            "narrows": False, "source": "harness", "field": q["key"],
            "type": q.get("type", "text"), "per_item": bool(q.get("per_item"))}


_DMY = re.compile(r"^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\s*$")


def _as_date(value: str) -> str | None:
    """An ISO date from what an officer types: 2027-04-12 or 12/04/2027."""
    from datetime import date

    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        pass
    m = _DMY.match(value)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            return None
    return None


def _category_question(need: nam.NeedRecord) -> dict:
    from nam_agent.nam.playbooks import need_classify

    q = need_classify._question(need, "category")
    return {**q, "id": "d:category", "narrows": False, "source": "harness"}


def _with_unknown(question: dict) -> dict:
    """Every question can be answered "I don't know" (PR flow 1(g))."""
    options = [o for o in question.get("options") or []
               if o.get("value") != nam_followup.UNKNOWN]
    return {**question, "options": [*options, {"label": nam_followup.UNKNOWN_LABEL,
                                               "value": nam_followup.UNKNOWN, "count": 0}]}


_SNAPSHOT = ("raw_text", "category", "subtype", "procurement_type", "tender_type",
             "location", "term", "quantity")


def _snapshot(need: nam.NeedRecord) -> dict:
    return json.loads(json.dumps({k: getattr(need, k) for k in _SNAPSHOT}))


def _take(need: nam.NeedRecord, question: dict, value: str, total: int,
          details: dict | None = None) -> tuple[str, dict | None, str]:
    """(answer to store, default or None, why it was not taken). An answer
    that does not fit the question is not taken: the same question is asked
    again with the reason, and nothing restarts (PR flow 1(f))."""
    from nam_agent.nam.playbooks import need_guided_form

    qid = question["id"]
    unknown = value == nam_followup.UNKNOWN or value.lower() == nam_followup.UNKNOWN_LABEL.lower()
    options = [o for o in question.get("options") or [] if o.get("value") != nam_followup.UNKNOWN]
    if qid == "d:category":
        if unknown:
            guess = (need.category or {}).get("value")
            if not guess:
                return "", None, "the category decides the estimate and the documents: pick one"
            value = guess
        error = nam.apply_update(need, {"category": value})
        if error:
            return "", None, error
        return value, ({"value": value, "basis": "not known: NAM's own reading of the need"}
                       if unknown else None), ""
    if qid.startswith("d:"):
        field = question.get("field") or qid[2:]
        if unknown and any(o.get("count") for o in options) and not question.get("closed"):
            best = max(options, key=lambda o: o.get("count") or 0)
            value = best["value"]
            default = {"value": value, "basis": (
                f"not known: NAM took the value most of the matching past NRL tenders carry "
                f"({best.get('count') or 0} of {sum(o.get('count') or 0 for o in options)})")}
        elif unknown:
            value = "not known: use the NRL standard"
            return value, {"value": value, "basis": "not known to the officer; the NRL standard "
                                                    "applies and a reviewer confirms it"}, ""
        else:
            default = None
        if question.get("closed") and options and value not in {o["value"] for o in options}:
            return "", None, f"answer one of: {', '.join(o['label'] for o in options)}"
        kind = question.get("type")
        if kind == "date":
            iso = _as_date(value)
            if iso is None:
                return "", None, "give the date, e.g. 2027-04-12 or 12/04/2027"
            value = iso
        if kind == "number":
            try:
                float(value)
            except ValueError:
                return "", None, "give a number"
        if not question.get("per_item") or not (details or {}).get("__items"):
            # one item: the need's own field; several: each item keeps its own
            need_guided_form._onto_need(need, {"key": field, "need": _need_target(field)}, value,
                                        details or {})
        return value, default, ""
    if unknown:
        if not options:
            return "", None, ""
        best = max(options, key=lambda o: o.get("count") or 0)
        return best["value"], {"value": best["label"],
                               "basis": (f"not known: NAM took the answer most of the related "
                                         f"past NRL tenders carry ({best.get('count') or 0} of "
                                         f"{total})")}, ""
    _record_answer(need, qid, value)
    return value, None, ""


def _label(question: dict | None, value: str) -> str:
    """An answer as the officer saw it: its option's label, else the value."""
    return next((str(o.get("label")) for o in (question or {}).get("options") or []
                 if o.get("value") == value and value), value)


def _need_target(field: str) -> str | None:
    from nam_agent.nam.playbooks import need_guided_form

    return (need_guided_form.policy()["fields"].get(field) or {}).get("need")


def capture(conn: sqlite3.Connection, home: Path, kg_url: str, text: str,
            need_id: str = "", session_id: str = "", fetch=nam._get,
            answer: dict | None = None, skip_followups: bool = False,
            ask_model=None, undo: str = "", category: str = "") -> dict:
    """Run Need capture: create (or refine) the need, scan, ask the follow-up
    questions one at a time (nam_followup.py), write the dossier.

    `text` is the officer's words: the first message creates the need, a later
    one refines it and rescans. `answer` ({question_id, value}; value "" skips,
    followup.UNKNOWN is "I don't know") answers the pending question without
    rescanning. `undo` (a question id) takes that answer back, and every
    answer after it, and asks the same question again. `category` is the
    officer's pick before typing (PR flow 1(a)). `skip_followups` stops the
    questions. `ask_model` ((system, user) -> text, llm.asker(ctx,
    "followup")) splits the items and writes the questions; without it the
    list rule and the tenders' facets do.

    The questions, in the PR flow's order: the category while it is not
    confident (1(a)); per item (1(c)), the questions that tell its past
    tenders apart (1(e), 1(f)); then the details every need carries, from the
    guided form (policy/guided_forms.json "capture"). Asking stops when all
    are answered (1(h))."""
    text = (text or "").strip()
    deadline = time.monotonic() + nam_followup.ASK_BUDGET_S
    if need_id:
        need = nam.load(conn, need_id)
        if need is None:
            return {"error": f"unknown need_id {need_id}"}
        if text:
            _refine(need, text)
    else:
        if not text:
            return {"error": "text is required: state what is to be procured"}
        if vague(text):
            return {"error": f"\"{text[:60]}\" names no item: say what is to be procured "
                             "(for example: steel pipes, gate valves)"}
        need = nam.create(conn, text, session_id)
    if category:
        error = nam.apply_update(need, {"category": category})
        if error:
            return {"error": error, "need_id": need.need_id}

    state = dict(need.followups or {})
    items = [dict(x) for x in state.get("items") or []]
    if not need_id:
        items = [{"text": t, "status": "open"} for t in split_items(text, ask_model)]
        if items:
            items[0]["status"] = "current"
    cur = int(state.get("item") or 0)
    answers: dict = dict(state.get("answers") or {})
    questions: dict = dict(state.get("questions") or {})   # id -> question text
    asked: dict = dict(state.get("asked") or {})           # id -> the question as asked
    before: dict = dict(state.get("before") or {})         # id -> the need before the answer
    defaults: dict = dict(state.get("defaults") or {})     # id -> {value, basis}
    inferred: dict = dict(state.get("inferred") or {})     # id -> taken from the officer's words
    narrowing_done = bool(state.get("narrowing_done"))
    early: dict = dict(state.get("early") or {})          # detail key -> given before asked
    stopped = state.get("stopped", "")
    result = state.get("scan")

    def item_text() -> str | None:
        return items[cur]["text"] if items else None

    if text and need_id and items:
        items[cur]["text"] = f"{items[cur]['text']}; {text}"
    if text or not result:
        result = scan(need, kg_url, fetch, item_text()) if kg_url else {
            "error": "The knowledge graph is not configured (NAM_KG_URL is unset)."}

    ignored, undone = "", ""
    accepted = False
    pending_before = state.get("pending") or {}
    done = bool(state.get("done"))
    if skip_followups:
        # "skip the remaining questions": this item's narrowing questions end
        # and the flow goes on (the next item, the details); pressed while a
        # detail is asked, or with nothing left, the asking ends
        on_detail = str((state.get("pending") or {}).get("id") or "").startswith("d:")
        if narrowing_done or on_detail:
            done = True
        else:
            narrowing_done, done = True, False
            stopped = "the officer skipped the remaining questions"

    def narrowing(answers_: dict) -> dict:
        return {k: v for k, v in answers_.items() if not k.startswith("d:")}

    def pool(entries: list[dict]) -> list[dict]:
        """The tenders of the confirmed category: a Goods need is built from
        supply tenders, not from the installation or maintenance contracts
        that also name the item (fit_category)."""
        cat = need.category or {}
        known = cat.get("confident") or "d:category" in {**all_answers(), **answers}
        return fit_category(entries, cat.get("value") if known else "")

    def all_answers() -> dict:
        out = {}
        for x in items:
            out.update(x.get("answers") or {})
        out.update(answers)
        return out

    if undo:
        if undo not in answers:
            idx = next((i for i, x in enumerate(items) if undo in (x.get("answers") or {})), None)
            if idx is not None:                 # an earlier item's answer: reopen that item
                saved = items[idx]
                answers, questions = dict(saved["answers"]), dict(saved["questions"])
                asked, before = dict(saved["asked"]), dict(saved["before"])
                defaults, result = dict(saved["defaults"]), saved["scan"]
                inferred = dict(saved.get("inferred") or {})
                items[idx] = {"text": saved["text"], "status": "current"}
                for j in range(idx + 1, len(items)):
                    items[j] = {"text": items[j]["text"], "status": "open"}
                cur = idx
        if undo in answers:
            order = list(answers)
            for k in order[order.index(undo):]:
                if k == undo:
                    snap = before.get(k)
                    if snap:
                        for field, value in snap.items():
                            setattr(need, field, value)
                answers.pop(k, None)
                defaults.pop(k, None)
                inferred.pop(k, None)
                before.pop(k, None)
            pending_before = asked.get(undo) or {"id": undo, "text": questions.get(undo, undo),
                                                 "options": []}
            narrowing_done = narrowing_done and undo.startswith("d:") and undo != "d:category"
            done, stopped, undone = False, "", undo
        else:
            ignored = f"undo {undo}: no such answer"

    late = merge_late(result, kg_url, fetch) if kg_url else None
    if late is not None:
        result = late              # the meaning lane's tenders, come in since the last step
    failed = bool(result.get("error"))
    everything = [] if failed else result["related"]

    if answer is not None and not undone:
        qid = str(answer.get("question_id") or "")
        if qid and qid == pending_before.get("id"):
            snap = _snapshot(need)
            in_play = len(nam_followup.apply(pool(everything), narrowing(answers),
                                             result.get("head", ""))[0])
            value, default, why = _take(need, pending_before, str(answer.get("value") or "")
                                        .strip(), in_play,
                                        {**{k[2:]: v for k, v in answers.items()
                                            if k.startswith("d:")},
                                         "__items": len(items) > 1})
            if why:
                ignored = f"answer to {qid} not taken: {why}"
            else:
                answers[qid], before[qid] = value, snap
                if default:
                    defaults[qid] = default
                accepted = True
        else:
            ignored = f"answer to {qid or 'nothing'} ignored: the open question is " \
                      f"{pending_before.get('id') or 'none'}"

    head = result.get("head", "")
    narrowed, trail = nam_followup.apply(pool(everything), narrowing(answers), head)
    model_note = ""

    def tender_question() -> dict | None:
        nonlocal stopped, model_note, narrowing_done, narrowed, trail
        if narrowing_done or failed or len(everything) < nam_followup.FOLLOWUP_MIN_RELATED:
            narrowing_done = True
            return None
        said = {f for w in search_terms(item_text() or _strip_captured(need)) for f in _forms(w)}
        words_said = item_text() or _strip_captured(need)
        for _ in range(nam_followup.MAX_QUESTIONS):
            prior = [{"question": questions.get(k, k), "answer": v}
                     for k, v in narrowing(answers).items()]
            # one step spends one model budget: past it, the tenders' own question
            model = ask_model if time.monotonic() < deadline else None
            q, stopped, model_note = _next_question(need, head, narrowed, narrowing(answers),
                                                    prior, model)
            if q is None:
                narrowing_done = True
                return None
            # an option the officer already named ("gate valves": gate) is taken
            # from their words and said, not asked again (PR flow 1(f))
            named = [o for o in q.get("options") or [] if str(o.get("value") or "")
                     and not str(o["value"]).startswith(nam_followup.NOT)
                     and search_terms(o["value"]) and set(search_terms(o["value"])) <= said
                     and _said_in_full(o, words_said)]
            if len(named) != 1 or q["id"] in answers:
                return q
            before[q["id"]] = _snapshot(need)
            questions[q["id"]], asked[q["id"]] = q["text"], _with_unknown(q)
            answers[q["id"]] = named[0]["value"]
            inferred[q["id"]] = named[0]["label"]
            narrowed, trail = nam_followup.apply(pool(everything), narrowing(answers), head)
        return None

    def detail(per_item: bool) -> dict | None:
        """The next detail not yet given: an item's own (per_item), from this
        item's answers; the need's, from every item's. With its suggestions."""
        if nam_scope.tender_only():
            return None                 # the chat stops at the matching tender (scope.py)
        given = answers if per_item else all_answers()
        several = len(items) > 1
        for q in _detail_fields(need):
            if q["per_item"] != per_item or f"d:{q['key']}" in given:
                continue
            if q["key"] in early:
                # given before it was asked ("5" while the tenders were being
                # narrowed): taken from the officer's words, not asked again
                qid = f"d:{q['key']}"
                answers[qid] = str(early.pop(q["key"]))
                questions[qid] = q["label"].replace("{item}", items[cur]["text"] if items
                                                    else "they")
                inferred[qid] = answers[qid]
                continue
            if q.get("need") and getattr(need, q["need"], "") and not (per_item and several):
                continue
            source = q.get("options_from") or ""
            pool = narrowed if per_item else related_now()
            label = (items[cur]["text"] if items else result.get("head", "")) or ""
            return _detail_question(q, _data_options(
                source, result.get("query", ""), pool, kg_url, fetch) if source else [], label)
        return None

    def related_now() -> list[dict]:
        seen, out = set(), []
        for x in [*[i.get("related") or [] for i in items if i.get("status") == "done"],
                  narrowed]:
            for r in x:
                if r["case_key"] not in seen:
                    seen.add(r["case_key"])
                    out.append(r)
        return out

    pending = None
    if undone:
        pending = pending_before                # asked again, and the flow goes on from it
    elif done or failed:
        pass
    elif pending_before and not (text or accepted or skip_followups):
        pending = pending_before                # nothing new to decide on: same question
        if str(pending["id"]).startswith("d:") and pending["id"] != "d:category":
            # asked again: with the tenders' suggestions that have come in since
            fresh = detail(per_item=bool(pending.get("per_item")))
            if fresh is not None and fresh["id"] == pending["id"]:
                pending = fresh
    else:
        # the category narrows tenders: with none found, it is not asked (the
        # need is asked for in other words first)
        if everything and not (need.category or {}).get("confident") \
                and "d:category" not in all_answers():
            pending = _category_question(need)
        if pending is None:
            pending = tender_question()
        while pending is None and narrowing_done:
            pending = detail(per_item=True)          # this item's own details first
            if pending is not None or not (items and cur + 1 < len(items)):
                break
            items[cur] = {"text": items[cur]["text"], "status": "done", "answers": answers,
                          "questions": questions, "asked": asked, "before": before,
                          "defaults": defaults, "scan": result, "stopped": stopped,
                          "related": narrowed, "inferred": inferred}
            cur += 1
            items[cur]["status"] = "current"
            answers, questions, asked, before, defaults, inferred = {}, {}, {}, {}, {}, {}
            narrowing_done, stopped = False, ""
            result = scan(need, kg_url, fetch, item_text())
            failed = bool(result.get("error"))
            everything = [] if failed else result["related"]
            head = result.get("head", "")
            narrowed, trail = nam_followup.apply(pool(everything), {}, head)
            pending = tender_question()
        if pending is None and narrowing_done:
            pending = detail(per_item=False)         # then the need's, once
        if pending is None:
            done = True
    if pending is not None:
        pending = _with_unknown(pending)
        if pending["id"] not in answers:
            questions[pending["id"]] = pending["text"]
            asked[pending["id"]] = pending

    for step in trail:
        step["question"] = questions.get(step["id"], step["id"])
        if step["id"] in defaults:
            step["note"] = f"{step.get('note') or ''} {defaults[step['id']]['basis']}".strip()

    # the related tenders of every item: the finished items', then this one's
    related, seen = [], set()
    for x in [*[i.get("related") or [] for i in items if i.get("status") == "done"], narrowed]:
        for r in x:
            if r["case_key"] not in seen:
                seen.add(r["case_key"])
                related.append(r)
    every_default = {}
    for x in items:
        every_default.update(x.get("defaults") or {})
    every_default.update(defaults)

    ready = (readiness(related) if not failed else
             {"ready": False, "material_cases": [], "reason": result["error"],
              "ask": "Name the item being procured." if "names an item" in result["error"] else ""})
    followups = {"pending": pending, "asked": len(answers), "trail": trail, "done": done,
                 "max": nam_followup.MAX_QUESTIONS,
                 # why asking ended, in words (the model's reason when it decided)
                 "stopped": stopped if done else "",
                 # set when the model was asked but its question was not used
                 "model_note": model_note,
                 "total_related": len(everything), "narrowed_to": len(narrowed),
                 "ignored": ignored, "undone": undone,
                 # each item's own tenders and why its asking ended: a request
                 # for several items ends with one final tender per item
                 "items": [{"text": x["text"], "status": x["status"],
                            "answered": len(x.get("answers") or {}),
                            "total": len(everything) if i == cur else
                            len((x.get("scan") or {}).get("related") or []),
                            # every tender found for the item, before any answer
                            "found": [r["case_key"] for r in
                                      (everything if i == cur else
                                       (x.get("scan") or {}).get("related") or [])],
                            "related": [r["case_key"] for r in
                                        (narrowed if i == cur else x.get("related") or [])],
                            "stopped": (stopped if done else "") if i == cur
                            else str(x.get("stopped") or "")}
                           for i, x in enumerate(items)],
                 "item": cur if items else None,
                 # every item's answers, the earlier items' first: each can be undone
                 "answered": [
                     {"id": k, "question": qs.get(k, k), "answer": _label(ak.get(k), v),
                      "item": n,
                      **({"default": ds[k]} if k in ds else {}),
                      **({"from_your_words": True} if k in inf else {})}
                     for n, (an, qs, ds, inf, ak) in enumerate(
                         [(x.get("answers") or {}, x.get("questions") or {},
                           x.get("defaults") or {}, x.get("inferred") or {},
                           x.get("asked") or {})
                          for x in items if x.get("status") == "done"]
                         + [(answers, questions, defaults, inferred, asked)])
                     for k, v in an.items()],
                 "defaults": every_default,
                 "ranked": rank(related)}

    per_item = {q["key"] for q in _detail_fields(need) if q["per_item"]}
    need.related_cases = [r["case_key"] for r in related]
    need.readiness = ready
    need.followups = {"scan": result, "answers": answers, "pending": pending, "done": done,
                      "early": early,
                      "questions": questions, "stopped": stopped, "asked": asked,
                      "before": before, "defaults": defaults, "narrowing_done": narrowing_done,
                      "inferred": inferred,
                      "items": items, "item": cur, "ranked": followups["ranked"],
                      # each item's tenders left and why asking ended (drive.py
                      # answers "which tender matches best" from this)
                      "item_results": followups["items"],
                      # the need's details, and each item's own (quantity, unit) when
                      # there are several: per_item fields belong to their item
                      "details": {k[2:]: v for k, v in all_answers().items()
                                  if k.startswith("d:") and k != "d:category"
                                  and not (len(items) > 1 and k[2:] in per_item)},
                      "item_details": [
                          {"text": x["text"], **{k[2:]: v for k, v in (
                              answers if i == cur else x.get("answers") or {}).items()
                              if k.startswith("d:") and k[2:] in per_item}}
                          for i, x in enumerate(items)]}
    moving = ready["ready"] and pending is None
    need.stage = "references" if moving else "capture"
    nam.save(conn, need, session_id, reason=("need capture: " + (
        f"answer to {undone} taken back" if undone else "scan and follow-up questions")))
    path = nam_dossier.write_step(home, need.need_id, "capture",
                                  render(need, {**result, "related": related}, ready, followups),
                                  requirement=need.raw_text, stage=need.stage)
    return {
        "need_id": need.need_id,
        "stage": need.stage,
        # What the UI acts on: offer the move to References when NAM is ready
        # and no follow-up question is open.
        "next_step": "references" if moving else None,
        "readiness": ready,
        # the item as searched: "cpvc pipes" when the KG matched it whole
        "item": " ".join((result or {}).get("terms") or []) if (result or {}).get("exact")
        else head,
        "related": related,
        "incidental": [e["case_key"] for e in result.get("incidental", [])],
        "semantic": result.get("semantic"),
        "tenders_in_graph": result.get("tenders_in_graph"),
        "followups": followups,
        # value, the words that decided it, whether it is confident, and the id
        "category": need.category,
        "ask_about": need.unresolved,
        "error": result.get("error"),
        "dossier_path": str(path),
        "dossier": nam_dossier.read(home, need.need_id),
    }


def make_tool(conn: sqlite3.Connection, home: Path, kg_url: str, ask_model=None) -> Tool:
    def nam_capture_need(requirement: str = "", need_id: str = "", question_id: str = "",
                         answer: str = "", undo: str = "", _notify=None) -> str:
        if _notify:
            try:
                _notify("kg", {"lane": "nam_capture", "requirement": requirement[:120]})
            except Exception:  # noqa: BLE001 - telemetry must never break a step
                pass
        from nam_agent.nam import chatstate

        reply = {"question_id": question_id, "value": answer} if question_id else None
        session = chatstate.current()
        out = capture(conn, home, kg_url, "" if (reply or undo) else requirement, need_id,
                      session or "", answer=reply, ask_model=ask_model,
                      undo=undo if need_id else "")
        if out.get("error") and not out.get("need_id"):
            return f"NAM need capture failed: {out['error']}"
        if session and out.get("need_id"):
            # the case is this chat's: the officer's next answer goes to the
            # harness (drive.py), not to a new case the model opens
            chatstate.put(conn, session, "case", out["need_id"])
        section = nam_dossier.read(home, out["need_id"])
        pending = (out.get("followups") or {}).get("pending")
        if pending:
            labels = [nam_followup.option_label(o) for o in pending["options"]]
            instruction = (
                f"Ask the user this ONE follow-up question and nothing else, by calling "
                f"ask_options with question={json.dumps(pending['text'])}"
                + (f" and options={json.dumps(labels)}" if labels else
                   " and 3-5 likely answers as options")
                + f". Then stop. When they answer, call this tool with need_id={out['need_id']}, "
                f"question_id={pending['id']} and their answer (\"\" to skip).")
        elif out["next_step"]:
            instruction = ("NAM has enough to go on. Show the user the related tenders table "
                           "as it is, then tell them you are moving to the References step.")
        else:
            instruction = (
                "NAM is not ready to move on. Say what was found in one line, then ask ONE "
                "question only, in plain words, with the ask_options tool: the most important "
                f"missing detail ({out['readiness'].get('ask') or 'more detail on the item'}). "
                "Never list several questions. Offer options only when they are values from "
                "the tenders above; never options of your own (a category such as Goods is "
                "not an item). When they answer, call this tool "
                f"again with need_id={out['need_id']} and their answer.")
        return (f"Need {out['need_id']} captured; stage {out['stage']}. "
                f"Dossier: {out['dossier_path']}\n{instruction}\n\n{section}")

    return Tool(
        name="nam_capture_need",
        description=(
            "NAM step 1, Need capture. Call this when the user states something they need to "
            "procure ('i need to procure pipes', 'AMC for housekeeping at Plant-1'). It scans "
            "the whole NRL knowledge graph for every related past tender, then asks the follow-up "
            "questions that tell those tenders apart (as many as the need calls for, maybe "
            "none), writes them into the "
            "need's dossier file, and says whether NAM is ready to move to the References step. "
            "To add detail to a need already captured, pass its need_id with the new words."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "requirement": {"type": "string",
                                "description": "The need in the user's words, or the extra "
                                               "detail when need_id is given"},
                "need_id": {"type": "string",
                            "description": "An existing need (NAM-YYYY-NNNN) to refine"},
                "question_id": {"type": "string",
                                "description": "The follow-up question being answered"},
                "answer": {"type": "string",
                           "description": "The user's answer to that question; \"\" skips it"},
                "undo": {"type": "string",
                         "description": "A question id whose answer the user takes back; "
                                        "with need_id. The same question is asked again"},
            },
        },
        fn=nam_capture_need,
        wants_notify=True,
    )
