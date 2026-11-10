"""case.ask: an answer without a step, for the words that need none.

"What did we pay for gaskets last time?", "Show me past tenders for
gaskets", "What is a PQC?", "Why was PUR-4 chosen?", "What is left to do?",
"approve it", "hello". The router sends a message here when it asks or
greets rather than asks for a change (intent.py); a change runs its step.

Every answer is put together in code from what is on record, each fact with
its source and authority (knowledge.py):

  a NAM term          the glossary (policy/glossary.json)       policy
  past tenders        the KG's tender search (tenders)           precedent
  past prices         the KG's price history (price_history)     precedent
  the case            its stage, what the stage still needs, the pending
                      approval and who may give it, the to-do list, the
                      estimate, the quantity, the documents, the precedent
                      and who selected it (the Need Record and its history)

It writes nothing: no record, no artifact, no approval. An approval is
given only by confirming the pending action as a user of an allowed role
(plan D6), so "approve it" is answered with the pending action and who may
confirm it. A KG that cannot be reached is said, not hidden.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

from nam_agent.nam import knowledge, orchestrator, skillset, store, tools
from nam_agent.nam import need as need_store
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome

GLOSSARY = Path(__file__).resolve().parents[1] / "policy" / "glossary.json"
_STOP = {"the", "a", "an", "of", "for", "on", "we", "our", "last", "time", "past", "previous",
         "tenders", "tender", "price", "prices", "rate", "rates", "pay", "paid", "cost", "did",
         "what", "show", "me", "list", "find", "similar", "nrl", "please", "this", "case",
         "is", "was", "how", "much", "do", "any", "earlier", "before", "buy", "bought"}


@cache
def glossary() -> dict[str, str]:
    return json.loads(GLOSSARY.read_text(encoding="utf-8"))["terms"]


def _fact(says: str, source: str, authority: str) -> dict:
    return {"says": says[:400], "source": source[:120], "authority": authority}


def _subject(text: str, need) -> str:
    """The item a question is about: the words after "for", else the case's."""
    m = re.search(r"\b(?:for|of|on)\s+([a-z0-9][a-z0-9 \-]{1,80})", text.lower())
    words = [w for w in re.findall(r"[a-z0-9\-]+", m.group(1) if m else text.lower())
             if w not in _STOP]
    if words:
        return " ".join(words[:4])
    return (need.raw_text.split(";")[0][:80] if need is not None else "")


def _topic(text: str) -> str:
    low = text.lower()
    if skillset.for_text(low) and re.search(
            r"\b(what|explain|how|which|define|meaning|mean|tell me about|extract\w*|check\w*)\b",
            low + (" what" if low.rstrip().endswith("?") else "")) \
            and not re.search(r"\b(this case|our case|the case)\b", low):
        return "skill"
    for name, pattern in (
        ("approve", r"^\s*(i\s+)?(approve|approved|sign off|confirm|accept)"),
        ("greeting", r"^\s*(hi|hello|hey|good (morning|afternoon|evening)|namaste)\b"),
        ("term", r"\bwhat (is|are|does)\b.*\b(" + "|".join(re.escape(t) for t in glossary())
         + r")\b|\b(" + "|".join(re.escape(t) for t in glossary()) + r")\b.*\bmean"),
        ("prices", r"\b(pay|paid|price|prices|rate|rates|cost)\b.*\b(for|of)\b"),
        ("tenders", r"\b(tenders?|precedents?|similar|bought|purchased)\b"),
        ("precedent", r"\b(why|who)\b.*\b(pur-|precedent|chosen|selected)"),
        ("estimate", r"\b(estimate|total|value|amount|cost)\b"),
        ("quantity", r"\b(quantity|qty|how many|lines?)\b"),
        ("documents", r"\b(documents?|boq|scc|pqc|specification|drafts?)\b"),
        ("todo", r"\b(left|remaining|next|to do|to-do|pending|status|stage|where)\b"),
    ):
        if re.search(pattern, low):
            return name
    return "status"


def _skill(text: str) -> tuple[str, list[dict]]:
    """From the NAM skill whose subject the question names: what it is, what
    to extract and how, or its checks (nam_agent/nam/skills/)."""
    low = text.lower()
    skill = skillset.for_text(low)[0]
    if re.search(r"\bextract\w*|\bread\b|\bparse\b|\bfields?\b", low):
        heads = ("What to extract", "How to extract")
    elif re.search(r"\bcheck\w*|\bvalidat\w*|\bverif\w*", low):
        heads = ("Checks",)
    else:
        heads = ("What",)
    parts = [(head, skill.section(head)) for head in heads]
    parts = [(h, " ".join(t.split())) for h, t in parts if t]
    if not parts:                     # a skill laid out its own way: all of it
        parts = [(skill.name, " ".join(skill.body.split()))]
    facts = [_fact(f"{h}: {t}", f"skill:{skill.name}", "policy") for h, t in parts]
    answer = " ".join(f"{h}: {t}" for h, t in parts)[:1990]
    return (answer or skill.description), facts


def _term(text: str) -> tuple[str, list[dict]]:
    low = text.lower()
    found = [t for t in glossary() if re.search(rf"\b{re.escape(t)}\b", low)]
    if not found:
        return "That term is not in the NAM glossary.", []
    facts = [_fact(f"{t.upper()}: {glossary()[t]}", "policy/glossary.json", "policy")
             for t in found[:3]]
    return " ".join(f["says"] for f in facts), facts


def _tenders(ctx, item: str, warnings: list[str]) -> tuple[str, list[dict]]:
    if not item:
        return "Name the item, e.g. 'past tenders for gate valves'.", []
    try:
        res = tools.call(ctx, "tenders", {"query": item[:200], "limit": 5})
    except NamError as exc:
        if exc.code != "kg_unavailable":
            raise
        warnings.append("The knowledge graph could not be reached.")
        return f"I could not search past tenders for {item}: the knowledge graph is down.", []
    rows = res["tenders"][:5]
    if not rows:
        return f"No past NRL tender in the knowledge graph names {item}.", []
    facts = [_fact(f"{r['cite']['case_key']}: {r['cite'].get('job_name') or ''}"
                   + (f" ({r['matching_item_count']} matching line(s))"
                      if r["matching_item_count"] else ""),
                   f"kg tender {r['cite']['case_key']}", "precedent") for r in rows]
    return (f"{res['total']} past NRL tender(s) name {item}; the first {len(rows)}: "
            + "; ".join(r["cite"]["case_key"] for r in rows)
            + ". Selecting one as the precedent is the officer's call (precedent.select)."), facts


def _prices(ctx, item: str, warnings: list[str]) -> tuple[str, list[dict]]:
    if not item:
        return "Name the item, e.g. 'what did we pay for gate valves'.", []
    try:
        res = tools.call(ctx, "price_history", {"requirement": item[:200], "limit": 5})
    except NamError as exc:
        if exc.code != "kg_unavailable":
            raise
        warnings.append("The knowledge graph could not be reached.")
        return f"I could not look up past prices for {item}: the knowledge graph is down.", []
    facts = []
    for row in res["rows"]:
        stat = row["unit_price"] or row["estimated_rate"]
        if not row["matched"] or not stat:
            continue
        years = row["years"]
        span = (f"{years[0]}-{years[-1]}" if len(years) > 1 else str(years[0])) if years else \
            "years unknown"
        facts.append(_fact(f"{row['item'][:80]} ({row['uom'] or 'unit not recorded'}): "
                           f"{row['price_level']} median Rs {stat['median']} over {stat['n']} "
                           f"record(s), {span}", "kg price_history "
                           + ", ".join(row["sources"][:3]), "precedent"))
    if not facts:
        return f"The knowledge graph holds no recorded price for {item}.", []
    return (f"Past NRL prices for {item} (medians of past tenders, not a current rate, R7): "
            + "; ".join(f["says"] for f in facts[:3])), facts[:10]


def _case_answer(topic: str, need, wf: dict, conn) -> tuple[str, list[dict], list[str]]:
    stage = orchestrator.BY_ID[wf["stage"]]
    pending = wf.get("pending")
    missing = [m["says"] for m in orchestrator.missing(need, wf)] if not wf.get("closed") else []
    case_src = f"case {need.need_id} v{need.version}"
    facts = [_fact(f"{need.need_id} is at {stage.id} ({stage.name})"
                   + (", handed off to NIT" if wf.get("closed") else ""), case_src, "record")]
    suggested: list[str] = []
    if topic == "approve":
        if pending:
            text = (f"Approvals are not given in words. The pending one is {pending['kind']} "
                    f"({pending['summary']}); a user with role "
                    f"{' or '.join(pending['allowed_roles'])} confirms it with its action id.")
        else:
            text = "Nothing is waiting for approval on this case."
        return text, facts, suggested
    if topic == "estimate":
        est = orchestrator._artifact(wf, "estimate")
        if not est:
            return "There is no estimate yet.", facts, ["estimate.goods", "estimate.wage",
                                                        "estimate.works"]
        facts += [_fact(f"{f['fact']}: {f['value']}", json.dumps(f["source"])[:120]
                        if isinstance(f["source"], dict) else str(f["source"]), f["authority"])
                  for f in knowledge.facts({"estimate": est})[:20]]
        new = [i + 1 for i, x in enumerate(est["lines"]) if x.get("rate") is None]
        return (f"The estimate is Rs {est.get('total'):,.2f} over {len(est['lines'])} line(s)"
                + (f"; line(s) {', '.join(map(str, new))} have no rate yet" if new else "")
                + ". It is provisional until the estimate approval."), facts, suggested
    if topic == "quantity":
        qty = orchestrator._artifact(wf, "quantity")
        if not qty:
            return "The quantity is not finalised yet.", facts, ["quantity.finalise"]
        lines = [f"{i}. {x['item'][:60]}: {x['qty']} {x.get('uom') or ''}".strip()
                 for i, x in enumerate(qty["lines"], 1)]
        return "The quantity: " + "; ".join(lines[:20]), facts, suggested
    if topic == "documents":
        docs = orchestrator._artifact(wf, "documents")
        if not docs:
            return "The documents are not generated yet.", facts, ["docs.generate"]
        listed = [f"{d['kind']} v{d.get('version')}" for d in docs["documents"]]
        return ("The documents: " + ", ".join(listed)
                + ("" if docs.get("complete") else "; some still miss a field or a clause")
                ), facts, suggested
    if topic == "precedent":
        chosen = list(need.selected_cases)
        if not chosen:
            return "No precedent is selected.", facts, ["precedent.search"]
        by = [v for v in store.versions(conn, need.need_id)
              if v["flow"] in ("precedent.select", "need.replicate")]
        who = f" by {by[-1]['by']['user']} ({by[-1]['reason'][:120]})" if by else ""
        facts.append(_fact(f"selected: {', '.join(chosen)}{who}", case_src, "record"))
        return (f"{', '.join(chosen)} was selected{who}. Only a tender the officer was shown "
                "and named can be the precedent (R2)."), facts, suggested
    # the stage, what it needs, what is pending, the to-do list
    parts = [facts[0]["says"] + "."]
    if missing:
        parts.append("It still needs: " + "; ".join(missing[:5]) + ".")
    if pending:
        parts.append(f"Waiting for {pending['kind']} by "
                     f"{' or '.join(pending['allowed_roles'])}.")
    todo = orchestrator._artifact(wf, "todo")
    if todo:
        done = sum(i["status"] == "done" for i in todo["items"])
        nxt = next((i for i in todo["items"] if i["status"] != "done"), None)
        parts.append(f"To-do list: {done} of {len(todo['items'])} done"
                     + (f"; next: {nxt['title']}" if nxt else "") + ".")
        suggested.append("case.plan")
    return " ".join(parts), facts, suggested


def handle(request: dict, ctx) -> Outcome:
    case_id = request.get("case_id")
    need = need_store.load(ctx.conn, case_id) if case_id else None
    if case_id and need is None:
        raise NamError("not_found", f"no case {case_id}")
    text = next((m.get("content", "") for m in reversed(request["messages"])
                 if m.get("role") == "user"), "") or request["system"]
    topic = _topic(text)
    warnings: list[str] = []
    suggested: list[str] = []
    if topic == "skill":
        answer, facts = _skill(text)
    elif topic == "term":
        answer, facts = _term(text)
    elif topic == "tenders" and (need is None or "for" in text.lower()):
        answer, facts = _tenders(ctx, _subject(text, need), warnings)
    elif topic == "prices":
        answer, facts = _prices(ctx, _subject(text, need), warnings)
    elif need is None:
        answer = ("I am NAM, the Need Assessment Module. Tell me what you need to procure, "
                  "e.g. 'We need 20 ring joint gaskets', and I will open the case and take "
                  "it step by step.") if topic in ("greeting", "status", "todo") else \
            "There is no case yet: say what you need to procure, or name the case."
        facts, suggested = [], ["need.capture"]
    else:
        wf = orchestrator.workflow(need)
        if topic == "tenders":
            topic = "precedent"
        answer, facts, suggested = _case_answer(
            "status" if topic in ("greeting", "todo", "term", "tenders") else topic,
            need, wf, ctx.conn)
    return Outcome(case_id=case_id, status="ok",
                   result={"answer": answer[:2000], "topic": topic, "facts": facts[:30],
                           "suggested": suggested[:5]},
                   warnings=tuple(warnings),
                   citations=tuple({"source": "kg", "ref": f["source"][:120],
                                    "authority": "precedent"}
                                   for f in facts if f["authority"] == "precedent")[:20],
                   next=tuple(suggested[:5]))
