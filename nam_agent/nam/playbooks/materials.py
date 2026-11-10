"""material.suggest (FRS F11): materials or services, with their
specification, suggested from similar past tenders, for the officer to pick
(S3). The "satisfied?" loop of the FRS is the refine answer.

  1. The past tenders matching the need (tenders, lexical, plus any words the
     officer added) give their line items; the closest few give their
     specification clauses verbatim (tender_record).
  2. The officer picks: answer {question_id: "pick", value: "2: use=yes;
     4: use=no"}, or adds search words: answer {question_id: "refine",
     value: "SS316 high pressure"}.
  3. A picked item becomes a line in quantity.finalise, with no quantity
     yet: the officer gives it there. Nothing is picked for the officer.
"""

from __future__ import annotations

from nam_agent.nam import need as need_store
from nam_agent.nam import orchestrator, tools
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome, lines

ANSWERS = ("pick", "refine")
TENDERS = 20
SPEC_FROM = 3            # tender_record reads, at most, for specifications
MAX = 50


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def handle(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    answer = request.get("answer") or {}
    asked = answer.get("question_id") or ""
    if asked and asked not in ANSWERS:
        raise NamError("bad_request", f"material.suggest asks only {', '.join(ANSWERS)}")
    wf = orchestrator.workflow(need)
    stored = orchestrator._artifact(wf, "materials") or {}
    warnings: list[str] = []
    words = list(stored.get("refine") or [])
    given = str(answer.get("value") or "").strip()
    if asked == "refine" and given:
        words = [*words, given[:120]][-5:]
    query = " ".join([need_store.search_query(need), *words]).strip()[:200]
    if len(query) < 2:
        raise NamError("stage_gate", "the need says too little to search on (need.capture)",
                       case_id=case_id)

    picked = {_norm(s["item"]): s["picked"] for s in stored.get("suggestions") or []}
    if stored.get("query") == query:
        suggestions = [dict(s) for s in stored["suggestions"]]
    else:
        found = tools.call(ctx, "tenders", {"query": query, "mode": "lexical",
                                            "limit": TENDERS})
        suggestions, seen = [], set()
        for t in found["tenders"]:
            for item in t["matching_items"]:
                key = _norm(item)
                if key and key not in seen and len(suggestions) < MAX:
                    seen.add(key)
                    suggestions.append({"item": item[:300], "source": t["cite"]["case_key"],
                                        "spec": [], "picked": picked.get(key, False)})
        specs = {}
        for key in list(dict.fromkeys(s["source"] for s in suggestions))[:SPEC_FROM]:
            record = tools.call(ctx, "tender_record", {"case_key": key})
            if record["found"]:
                specs[key] = [c["text"][:1000] for c in record["clauses"]["tech_spec"]][:3]
        for s in suggestions:
            s["spec"] = specs.get(s["source"], [])
        if not suggestions:
            warnings.append(f"No past tender's line items match '{query[:60]}': refine the "
                            "words, or add the lines in quantity.finalise.")
    if asked == "pick" and given:
        try:
            draft = [dict(s) for s in suggestions]
            for e in lines.parse(given, {"use"}):
                if e.target in ("add", "all") or e.remove:
                    raise lines.LineError("pick a suggestion by its number: '2: use=yes'")
                if e.index >= len(draft):
                    raise lines.LineError(f"there is no suggestion {e.target}")
                if e.fields.get("use") not in ("yes", "no"):
                    raise lines.LineError("use is yes or no")
                draft[e.index]["picked"] = e.fields["use"] == "yes"
            suggestions = draft
        except lines.LineError as exc:
            warnings.append(str(exc))

    chosen = [s for s in suggestions if s["picked"]]
    value = {"summary": f"{len(suggestions)} suggestion(s), {len(chosen)} picked"[:300],
             "query": query, "refine": words, "suggestions": suggestions}
    return Outcome(
        case_id=case_id, status="ok",
        result={"query": query, "suggestions": [{"n": i, **s}
                                                for i, s in enumerate(suggestions, 1)],
                "picked": len(chosen)},
        questions=() if chosen else ({"id": "pick", "source": "harness", "options": [],
                                      "text": "Pick the items you need ('2: use=yes'), or "
                                              "refine the search.",
                                      "why": "Only the officer picks an item (F11)."},),
        warnings=tuple(warnings),
        citations=tuple({"source": "kg", "ref": f"tender:{k}", "authority": "precedent"}
                        for k in dict.fromkeys(s["source"] for s in chosen)),
        next=("quantity.finalise",) if chosen else (),
        artifacts={"materials": value} if value != stored else {})


def picked_lines(wf: dict, rows: list[dict]) -> list[dict]:
    """quantity.finalise's new lines: the picked items not on a line yet."""
    have = {_norm(r["item"]) for r in rows}
    out = []
    for s in (orchestrator._artifact(wf, "materials") or {}).get("suggestions") or []:
        if s["picked"] and _norm(s["item"]) not in have:
            have.add(_norm(s["item"]))
            out.append({"item": s["item"], "qty": None, "uom": None, "material_code": None,
                        "source": f"suggested from {s['source']}"})
    return out
