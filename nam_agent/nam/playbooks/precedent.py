"""precedent.search (FRS F6) and precedent.select (FRS F7): NAM lists the past
tenders like the need, and the officer picks the precedent. Generation reads
only what the officer picked, "rather than by AI selection alone" (R2).

precedent.search
  1. The query is the need's search words (need.search_query), plus any words
     in the last user message: "try another with SS316 windings" searches
     again with them added (the "try another" loop of Diagram B). A PUR case
     id or tender number in the message is looked up too (F6: description or
     identifier).
  2. tenders (lexical) lists every tender with those words; when it finds
     none, tenders (similar) gives the closest few by meaning, marked weak
     when the KG says the match is weak.
  3. Ranked by match strength (need.match_strength): how much of the need
     each tender's subject and line items contain, with the reason. A similar
     case is close, not exact (R4); nothing is selected here.
  4. Every tender shown is added to the `shortlist` artifact. The officer
     picks from it with precedent.select, or says none fit (need.guided_form).

precedent.select
  1. The tenders: the answer to the `precedent` question (one or more case
     keys, comma separated), else the PUR case ids and tender numbers named
     in the last user message or the system prompt. A tender number must
     resolve to one tender; "none" clears the selection.
  2. Each must have been shown to the officer: on the shortlist, or among the
     related tenders need capture found. One that was not is refused with
     the reason, and nothing is selected.
  3. Each one's record is read (tender_record) to report what it carries:
     BOQ lines, PQC, SCC and specification clauses.
  4. The selection replaces the case's selected precedent. On a case past S2
     it is rework: the later stages and their approvals are done again.
"""

from __future__ import annotations

import re

from nam_agent.nam import need as need_store
from nam_agent.nam import orchestrator, router, tools
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome
from nam_agent.nam.playbooks.need_replicate import _IN_TEXT

PRECEDENT = "precedent"
TERMS = "terms"
NONE = "none"
SHOWN_MAX = 200
_FILLER = re.compile(r"\b(try|another|again|search|find|show|look|for|with|more|other|"
                     r"none|of|these|them|those|fit|fits|match|matches|please|the|a|an|"
                     r"and|or|similar|tenders?|cases?|past|previous|precedents?)\b", re.IGNORECASE)
_NONE = re.compile(r"^\s*(none|no(ne)? of (these|them|those))\b", re.IGNORECASE)


def _last_user(request: dict) -> str:
    for msg in reversed(request.get("messages") or []):
        if msg.get("role") == "user":
            return (msg.get("content") or "").strip()
    return ""


def _shortlist(need) -> dict:
    return orchestrator._artifact(orchestrator.workflow(need), "shortlist") or {}


def shown(need) -> list[str]:
    """Every tender put in front of the officer for this case."""
    keys = list(need.related_cases or [])
    for key in _shortlist(need).get("shown") or []:
        if key not in keys:
            keys.append(key)
    return keys


def _refs(text: str) -> list[str]:
    found = []
    for m in _IN_TEXT.finditer(text or ""):
        ref = m.group(1).upper() if m.group(1).upper().startswith("PUR-") else m.group(1)
        if ref not in found:
            found.append(ref)
    return found


def _resolve(ctx, ref: str) -> tuple[str | None, str]:
    """(the one case key a reference names, why not)."""
    found = tools.call(ctx, "resolve_reference", {"ref": ref})
    keys = [m["cite"]["case_key"] for m in found["matches"]]
    if not keys:
        return None, "no past tender matches it"
    if found["ambiguous"]:
        return None, f"it matches {', '.join(keys)}; name the one meant"
    return keys[0], ""


def _card(query: str, row: dict, rank: int) -> dict:
    cite = row["cite"]
    strength, basis = need_store.match_strength(
        query, {"job_name": cite.get("job_name"),
                "matching_item_count": row["matching_item_count"]}, {})
    return {"case_key": cite["case_key"], "tender_no": cite.get("tender_no"),
            "subject": cite.get("job_name"), "rank": rank,
            "match_strength": strength, "match_basis": basis,
            "matching_items": row["matching_items"][:6],
            "matching_item_count": row["matching_item_count"]}


# -- precedent.search ----------------------------------------------------------------

def search(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    answer = request.get("answer") or {}
    said = str(answer.get("value") or "") if answer.get("question_id") == TERMS \
        else _last_user(request)

    refs = _refs(said)
    words = " ".join(_FILLER.sub(" ", _IN_TEXT.sub(" ", said)).split())[:200]
    base = need_store.search_query(need)
    query = " ".join(dict.fromkeys(f"{base} {words}".split()))[:300].strip()
    if not query and not refs:
        raise NamError("bad_request", "nothing to search on: the need has no description")

    warnings: list[str] = []
    rows, best, weak, total, mode = [], None, None, 0, "lexical"
    if query:
        found = tools.call(ctx, "tenders", {"query": query, "mode": "lexical", "limit": 30})
        rows, total = found["tenders"], found["total"]
        warnings += found["warnings"]
        if not rows:
            mode = "similar"
            found = tools.call(ctx, "tenders", {"query": query, "mode": "similar", "limit": 10})
            rows, total, best, weak = (found["tenders"], found["total"],
                                       found["best_similarity"], found["weak"])
            warnings += found["warnings"]
    cards = [_card(query, r, 0) for r in rows]
    cards.sort(key=lambda c: (-c["match_strength"], c["case_key"]))
    for ref in refs:                                   # an identifier the officer gave
        key, why = _resolve(ctx, ref)
        if key is None:
            warnings.append(f"{ref}: {why}")
        elif key not in {c["case_key"] for c in cards}:
            cards.insert(0, {"case_key": key, "tender_no": None, "subject": None, "rank": 0,
                             "match_strength": 100, "match_basis": f"named by you as {ref}",
                             "matching_items": [], "matching_item_count": 0})
    cards = cards[:20]
    for rank, card in enumerate(cards, 1):
        card["rank"] = rank
    if weak:
        warnings.append(f"The closest tenders are a weak match: {weak}")

    before = _shortlist(need)
    keys = [c["case_key"] for c in cards]
    shown_now = shown(need)
    shown_now += [k for k in keys if k not in shown_now]
    searches = int(before.get("searches") or 0) + 1
    shortlist = {"query": query, "mode": mode, "last": keys, "shown": shown_now[-SHOWN_MAX:],
                 "searches": searches}

    selected = list(need.selected_cases)
    result = {"query": query, "added_terms": words or None, "mode": mode, "total": total,
              "best_similarity": best, "candidates": cards, "selected": selected}
    if not cards:
        question = {"id": TERMS, "source": "harness", "options": [],
                    "text": "No past tender matches. Add or change the search words, or use "
                            "the guided form.",
                    "why": f"Nothing in the knowledge graph matched '{query}'."}
        return Outcome(case_id=case_id, status="needs_input", result=result,
                       questions=(question,), warnings=tuple(warnings),
                       next=("need.guided_form",), artifacts={"shortlist": shortlist})
    options = [{"label": (f"{c['tender_no'] or c['case_key']}: {c['subject'] or ''} "
                          f"({c['match_strength']}%)")[:200], "value": c["case_key"],
                "count": c["matching_item_count"]} for c in cards[:9]]
    options.append({"label": "None of these fit", "value": NONE, "count": 0})
    question = {"id": PRECEDENT, "source": "harness", "options": options,
                "text": "Which past tender should this case start from? Pick one or more, "
                        "or none.",
                "why": "NAM ranks the tenders; the precedent is your choice (F7, R2). Answer "
                       "with precedent.select, several as PUR-1,PUR-2."}
    return Outcome(case_id=case_id, status="needs_input", result=result,
                   questions=(question,), warnings=tuple(warnings),
                   citations=tuple({"source": "kg", "ref": f"tender:{c['case_key']}",
                                    "authority": "precedent"} for c in cards),
                   next=("precedent.select", "need.guided_form"),
                   artifacts={"shortlist": shortlist})


# -- precedent.select ----------------------------------------------------------------

def select(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    past_s2 = orchestrator.workflow(need)["stage"] not in ("S1", "S2")
    answer = request.get("answer") or {}
    if answer.get("question_id") == PRECEDENT:
        said = str(answer.get("value") or "")
        refs = [p.strip() for p in said.split(",") if p.strip()]
    else:
        said = _last_user(request)
        refs = _refs(said) or _refs(request["system"])
        if not refs and _NONE.match(said):
            refs = [NONE]
        found = router.refs(request["system"]).get("tender")
        if found and found not in refs:
            refs.append(found)

    options = shown(need)
    ask = {"id": PRECEDENT, "source": "harness",
           "text": "Which of the tenders shown should be the precedent?",
           "why": "Only a tender you were shown and named can be the precedent (F7).",
           "options": [{"label": k, "value": k, "count": 0} for k in options[:9]]
           + [{"label": "None of these fit", "value": NONE, "count": 0}]}
    previous = list(need.selected_cases)

    if not refs:
        return _held(case_id, previous, [], ("Name the tender(s) to select.",), ask)
    if [r.lower() for r in refs] == [NONE]:
        if previous:
            need.selected_cases = []
            need.stage = "references"
            need_store.save(ctx.conn, need, reason="officer cleared the precedent: none fit")
        return Outcome(case_id=case_id, status="ok",
                       result={"selected": [], "previous": previous, "refused": []},
                       warnings=("No precedent: use the guided form, or search again.",),
                       next=("need.guided_form", "precedent.search"))

    keys, refused = [], []
    for ref in refs[:10]:
        key = ref.upper() if ref.upper() in options else None
        if key is None:
            key, why = _resolve(ctx, ref)
            if key is None:
                refused.append({"ref": ref, "why": why})
                continue
        if key not in options:
            refused.append({"ref": ref, "why": f"{key} was not among the tenders shown for "
                            "this case; search for it first (precedent.search)"})
        elif key not in keys:
            keys.append(key)
    if refused:
        return _held(case_id, previous, refused,
                     tuple(f"{r['ref']}: {r['why']}" for r in refused), ask)

    cards = []
    for key in keys:
        record = tools.call(ctx, "tender_record", {"case_key": key})
        if not record["found"]:
            refused.append({"ref": key, "why": "the knowledge graph holds no record for it"})
            continue
        clauses = record["clauses"]
        cards.append({"case_key": key, "tender_no": record["fields"].get("tender_no"),
                      "subject": record["fields"].get("job_name"),
                      "counts": {"boq": len(record["boq"]), "pqc": len(clauses["pqc"]),
                                 "scc": len(clauses["scc"]),
                                 "tech_spec": len(clauses["tech_spec"]),
                                 "sor_references": len(record["sor_references"])},
                      "complete": record["complete"]})
    if refused:
        return _held(case_id, previous, refused,
                     tuple(f"{r['ref']}: {r['why']}" for r in refused), ask)

    warnings = []
    if keys != previous:
        need.selected_cases = keys
        need.stage = "selected"
        need_store.save(ctx.conn, need, reason=f"officer selected precedent {', '.join(keys)}")
        if past_s2 and previous:
            warnings.append("The precedent changed after S2: the quantity, estimate and "
                            "documents built on the old one are rework.")
    for card in cards:
        if not any(card["counts"][k] for k in ("boq", "pqc", "scc", "tech_spec")):
            warnings.append(f"{card['case_key']} carries no BOQ lines or clauses on record.")
        elif not card["complete"]:
            warnings.append(f"{card['case_key']} is larger than one KG record: not all of it "
                            "was counted.")
    return Outcome(case_id=case_id, status="ok",
                   result={"selected": cards, "previous": previous, "refused": []},
                   citations=tuple({"source": "kg", "ref": f"tender:{k}",
                                    "authority": "precedent"} for k in keys),
                   warnings=tuple(warnings), next=("quantity.finalise",))


def _held(case_id, previous, refused, warnings, ask) -> Outcome:
    """Nothing selected: the officer is asked again."""
    return Outcome(case_id=case_id, status="needs_input",
                   result={"selected": [], "previous": previous, "refused": refused},
                   questions=(ask,), warnings=tuple(warnings))
