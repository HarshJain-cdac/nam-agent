"""The knowledge graph's tools and SAP's, two sources the agent uses apart.

  SAP (sap_search)  NRL's SAP purchasing records, on the KG MCP server
                    (NAM_KG_URL, e.g. http://<mcp-host>:8091/mcp): PR, PO
                    and service lines by item, material code, PR, PO or PUR
                    case. Numbers come from here: how many, which materials,
                    rates, PR and PO numbers.
  KG (kg_*)         NRL's tender knowledge graph, its
                    read-only precedent API (NAM_KG_API_URL, default
                    http://127.0.0.1:8077): past tenders, their line items,
                    bidders, clauses and files. A PUR case's own data that
                    SAP's lines do not show comes from here.

Over HTTP, never by importing the KG (its pinned dependencies stay its own),
so the KG restarting never takes NAM harness down: a tool that cannot reach it says
UNAVAILABLE, never "nothing found". With NAM_KG_API_URL set to "mcp" the KG
tools read the MCP server instead, through kg_mcp.legacy_get.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from nam_agent.nam import guard, kg_mcp
from nam_agent.tools.registry import Tool

# Generous: a cold vector search against pgvector's HNSW index measured ~26s on
# this corpus, and the first call after the KG service starts pays import cost
# on top. A timeout that fires mid-answer is worse than a slow answer.
_TIMEOUT = float(os.getenv("NAM_KG_TIMEOUT", "120"))


def _get(base_url: str, path: str, params: dict) -> dict:
    """One read from the KG: its REST API, or the MCP server's tools when
    base_url is an MCP endpoint."""
    # a model writes "PUR‑INSTMNT‑1002364" with U+2011: the KG knows it with "-"
    params = {k: guard.plain(v).strip() if isinstance(v, str) else v for k, v in params.items()}
    if is_mcp(base_url):
        return kg_mcp.legacy_get(base_url, path, params)
    url = base_url.rstrip("/") + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read())


def is_mcp(base_url: str) -> bool:
    return (base_url or "").rstrip("/").endswith("/mcp")


def kg_api(setting: str, kg_url: str) -> str:
    """Where the KG tools read: NAM_KG_API_URL, or the MCP server when it
    is "mcp"."""
    value = (setting or "").strip()
    return kg_url if value.lower() == "mcp" or not value else value


def _unavailable(exc: Exception, base_url: str) -> str:
    """One shape for every failure, and it never pretends there was no data.

    A tool that returns "no precedent found" when the service is actually down
    would let the agent draft a NIT as though the corpus held nothing relevant.
    The distinction between "asked and there is none" and "could not ask" has
    to survive into what the model reads.
    """
    if isinstance(exc, urllib.error.HTTPError):
        detail = f"HTTP {exc.code}"
    elif isinstance(exc, urllib.error.URLError):
        detail = f"cannot reach {base_url} ({exc.reason})"
    else:
        detail = str(exc)[:200]
    return (
        f"KG PRECEDENT UNAVAILABLE: {detail}. This is NOT the same as 'no precedent exists' — "
        f"the knowledge graph could not be consulted at all. Tell the user precedent lookup is "
        f"offline, and continue without citing any past tender. Do not invent precedent."
    )


def _missing(what: str, value) -> str:
    """The reply to a call that names nothing to look up: the agent's to fix,
    never sent to the KG (its schema refuses an empty query, and that error
    used to read as an outage)."""
    if str(value or "").strip():
        return ""
    return (f"NOTHING TO LOOK UP: {what} is empty. Call again with the words to search for; "
            "this is not a KG outage.")


def _emit(notify, lane: str, requirement: str, **extra) -> None:
    """Tell the harness the graph is being consulted.

    The dashboard diagram has a Knowledge Graph block that lights on this, so
    "when does the agent actually hit the KG" is something you watch rather
    than something you infer from a log. Fired twice per call — once on the way
    out, once with the result — so a slow lookup shows as sustained activity
    instead of a blink.
    """
    if not notify:
        return
    try:
        notify("kg", {"lane": lane, "requirement": requirement[:120], **extra})
    except Exception:  # noqa: BLE001 - telemetry must never break a lookup
        pass


def _cite(source: dict) -> str:
    """Case id, SUBJECT, department.

    The subject is the load-bearing part. Handed clauses labelled only
    "[PUR-CORPAFF-1004973 · Corporate Affairs]", the model read NRL's actual
    Housekeeping Chemicals PQC as generic MSE boilerplate from an unrelated
    contract and told the user no precedent existed — while quoting it. With
    the tender's own title on the citation, "Annual Rate Contract for Supply of
    Housekeeping Chemicals" is self-evidently the right source.
    """
    bits = [source.get("tender_no") or source.get("pur_case_id") or "unknown tender"]
    job = (source.get("job_name") or "").strip()
    if job:
        bits.append(job[:90])
    if source.get("department"):
        bits.append(source["department"])
    return " · ".join(b for b in bits if b)


def make_precedent_tool(base_url: str) -> Tool:
    def kg_precedent_search(requirement: str, limit: int = 3, _notify=None) -> str:
        if _missing("requirement", requirement):
            return _missing("requirement", requirement)
        _emit(_notify, "precedent", requirement)
        try:
            data = _get(base_url, "/api/kg/precedent",
                        {"requirement": requirement, "limit": max(1, min(int(limit), 10))})
        except Exception as exc:  # noqa: BLE001 - the model observes this as text
            return _unavailable(exc, base_url)

        tenders = data.get("tenders") or []
        _emit(_notify, "precedent", requirement, hits=len(tenders),
              weak=bool(data.get("precedent_gap")))
        # Weak or empty is not "never bought": NRL item names are abbreviated
        # ("TG Bimetallic U320mm") and the ranked search misses them. Read the
        # line items before anything is said about whether NRL bought it.
        check = "" if tenders and not data.get("precedent_gap") else _item_check(base_url, requirement)
        if not tenders:
            return check or (f"No past NRL tender in the knowledge graph is close enough to "
                             f"'{requirement}' to serve as precedent. Say so plainly — do not "
                             f"substitute a general-knowledge example for a real past tender.")

        lines = []
        if data.get("precedent_gap"):
            lines.append(
                f"WEAK PRECEDENT — {data['precedent_gap']} Treat everything below as context "
                f"only, and do not cost or draft this requirement from it."
            )
        elif data.get("best_similarity") is not None:
            lines.append(f"Closest item match: {data['best_similarity']} similarity.")

        for t in tenders:
            lines.append(f"\n— {_cite(t)}")
            if t.get("job_name"):
                lines.append(f"  Subject: {t['job_name']}")
            if t.get("matched_items"):
                lines.append(f"  Matched what was asked for: {', '.join(t['matched_items'][:6])}")
            for label, key in (("PQC", "pqc"), ("SCC", "scc"), ("Tech spec", "technical_specs")):
                if t.get(key):
                    lines.append(f"  {label}: {len(t[key])} clause(s) on file "
                                 f"(call kg_clause_lookup to read them verbatim)")
            if t.get("amended_fields"):
                lines.append(f"  NOTE: amended by corrigendum — {', '.join(t['amended_fields'])}")
        if check:
            lines.append("\n" + check)
        return "\n".join(lines)

    return Tool(
        name="kg_precedent_search",
        description=(
            "Look up the past NRL tenders closest to a stated requirement in the tender knowledge "
            "graph — what was procured, which department ran it, and how many PQC/SCC/technical-spec "
            "clauses are on file. Use it to ground a NIT in real precedent and before drafting any "
            "clause text; for 'have we bought this item' use kg_item_history. A WEAK result comes "
            "with an ITEM CHECK: that check, not the weak label, says whether NRL bought it. "
            "DO NOT use it to answer the NIT intake wizard's questions for the user: when a flow is "
            "active, the user's own answer is the only valid input to advance_nit_flow. You may quote "
            "precedent to HELP them answer, but never submit a precedent value as their answer. "
            "Every result names the tender it came from — always cite that tender when you use it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "requirement": {"type": "string",
                                "description": "What is being procured, in plain words, e.g. "
                                               "'housekeeping chemicals' or '6 inch carbon steel pipe'"},
                "limit": {"type": "integer", "description": "How many past tenders to return (1-10, default 3)"},
            },
            "required": ["requirement"],
        },
        fn=kg_precedent_search,
        wants_notify=True,
    )


def make_price_tool(base_url: str) -> Tool:
    def kg_price_reference(requirement: str, limit: int = 3, _notify=None) -> str:
        if _missing("requirement", requirement):
            return _missing("requirement", requirement)
        _emit(_notify, "prices", requirement)
        try:
            data = _get(base_url, "/api/kg/prices",
                        {"requirement": requirement, "limit": max(1, min(int(limit), 10))})
        except Exception as exc:  # noqa: BLE001
            return _unavailable(exc, base_url)

        _emit(_notify, "prices", requirement, hits=len(data.get("rows") or []),
              weak=bool(data.get("precedent_gap")))
        if not data.get("rows"):
            return (f"No historical rate for '{requirement}' in the knowledge graph. "
                    f"Do not estimate one — say there is no precedent rate.")
        header = ""
        if data.get("precedent_gap"):
            header = (f"WEAK PRECEDENT — {data['precedent_gap']} Do not present these as "
                      f"comparable rates for this requirement.\n\n")
        # The service already formats these the way the KG's own drafter reads
        # them: one row per item x unit x price level, with the years each
        # observation came from. Reformatting here would only lose that.
        return header + (data.get("formatted") or "")[:6000]

    return Tool(
        name="kg_price_reference",
        description=(
            "Historical NRL rates for a requirement, from the tender knowledge graph's 45,791 price "
            "records. One row per item x unit x price level — an issuer's estimate, a bidder's quote, "
            "an awarded rate and an invoiced amount are four different numbers and are never merged. "
            "Use it for 'what did we last pay for X', for sanity-checking an estimate, and for the "
            "price-schedule basis of a NIT. Rates carry the tender and the years they were observed; "
            "cite both. Never quote a rate this tool did not return."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "requirement": {"type": "string", "description": "The item or work, in plain words"},
                "limit": {"type": "integer", "description": "How many past tenders to draw rates from (1-10, default 3)"},
            },
            "required": ["requirement"],
        },
        fn=kg_price_reference,
        wants_notify=True,
    )


def make_clause_tool(base_url: str) -> Tool:
    def kg_clause_lookup(requirement: str, kind: str = "pqc", limit: int = 3, _notify=None) -> str:
        if _missing("requirement", requirement):
            return _missing("requirement", requirement)
        _emit(_notify, f"clauses/{kind}", requirement)
        if kind not in ("pqc", "scc", "tech_spec"):
            return f"kind must be one of pqc, scc, tech_spec — got '{kind}'."
        try:
            data = _get(base_url, "/api/kg/clauses",
                        {"requirement": requirement, "kind": kind,
                         "limit": max(1, min(int(limit), 10))})
        except Exception as exc:  # noqa: BLE001
            return _unavailable(exc, base_url)

        clauses = data.get("clauses") or []
        _emit(_notify, f"clauses/{kind}", requirement, hits=len(clauses))
        if not clauses:
            return (f"No {kind.upper()} clause on file for anything resembling '{requirement}'. "
                    f"Do not write one from general knowledge — NIT clauses are quoted from "
                    f"precedent or left for a human to supply.")
        out = [
            (f"{len(clauses)} {kind.upper()} clause(s), verbatim as issued. The bracket on each "
             f"clause is the tender it came from and that tender's subject — read it: a clause "
             f"from a tender whose subject matches the request IS the precedent for this "
             f"request, whatever the clause's own wording happens to be about."),
        ]
        for c in clauses[:12]:
            out.append(f"\n[{_cite(c['source'])}]\n{c['text'][:1500]}")
        return "\n".join(out)

    return Tool(
        name="kg_clause_lookup",
        description=(
            "Read the VERBATIM text of Pre-Qualification Criteria (pqc), Special Contract Conditions "
            "(scc) or technical specification (tech_spec) clauses from past NRL tenders, with the "
            "tender each came from. The graph stores these word-for-word precisely so a new NIT can "
            "reuse the wording that was actually issued. Use this when a NIT needs clause text — "
            "quote and adapt what comes back, and cite the source tender. If nothing comes back, say "
            "so and leave the section for a human; never compose an NRL clause from general knowledge."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "requirement": {"type": "string", "description": "What is being procured, in plain words"},
                "kind": {"type": "string", "enum": ["pqc", "scc", "tech_spec"],
                         "description": "Which kind of clause to read"},
                "limit": {"type": "integer", "description": "How many past tenders to draw clauses from (1-10, default 3)"},
            },
            "required": ["requirement"],
        },
        fn=kg_clause_lookup,
        wants_notify=True,
    )


def make_find_tenders_tool(base_url: str) -> Tool:
    def kg_find_tenders(query: str, limit: int = 30, _notify=None) -> str:
        if _missing("query", query):
            return _missing("query", query)
        _emit(_notify, "tenders", query)
        try:
            data = _get(base_url, "/api/kg/tenders",
                        {"query": query, "limit": max(1, min(int(limit), 100))})
        except Exception as exc:  # noqa: BLE001
            return _unavailable(exc, base_url)
        rows = data.get("tenders") or []
        _emit(_notify, "tenders", query, hits=len(rows))
        if not rows:
            return (f"No past NRL tender matches '{query}' by job name or line item. "
                    f"Searched for: {', '.join(data.get('terms') or [])}.")
        shown = len(rows)
        head = (f"{data['count']} past tender(s) match '{query}' "
                f"(searched: {', '.join(data.get('terms') or [])})"
                + (f", showing {shown}" if shown < data["count"] else "") + ".")
        out = [
            head,
            ("REPRODUCE THE TABLE BELOW VERBATIM in your reply. Do not summarise it, "
             "do not retype its values, and do not add tenders, item counts, tender "
             "modes or bidder counts that are not in it."),
            "", "| Tender | Items | Subject |", "|---|---:|---|",
        ]
        for r in rows:
            subject = (r.get("job_name") or "").replace("|", "/")[:70]
            out.append(f"| {r['case_key']} | {r['matching_item_count']} | {subject} |")
        exact = [r for r in rows if r.get("exact_items")]
        if exact:
            out.append("\nExact item on file (every word and size asked for):")
            out += [f"- {r['case_key']}: " + "; ".join(r["exact_items"][:4]) for r in exact[:5]]
        return "\n".join(out)

    return Tool(
        name="kg_find_tenders",
        description=(
            "List EVERY past NRL tender that involved something, by job name or line item. "
            "Use this for 'find me all the ...', 'how many times have we bought ...', 'which "
            "tenders involved ...' — any question whose honest answer is a complete list. "
            "kg_precedent_search is different: it returns only the closest few, for drafting. "
            "Answering a 'find all' question from that short list under-reports, and reporting "
            "'no precedent' when the list is merely short is the failure this tool exists to "
            "prevent. Singular and plural are searched together ('pipe' also finds 'pipes'), "
            "because the index does not stem. Present the result as the table it returns."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The thing procured, e.g. 'pipe', 'gaskets'"},
                "limit": {"type": "integer", "description": "Max tenders to list (1-100, default 30)"},
            },
            "required": ["query"],
        },
        fn=kg_find_tenders,
        wants_notify=True,
    )


def _money(value) -> str:
    try:
        return f"{float(str(value).replace(',', '')):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _number(value) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _qty(value) -> str:
    n = _number(value)
    return (str(int(n)) if n is not None and n == int(n) else str(value or "")) if value else ""


def _per_unit(r: dict) -> float | None:
    """A comparative statement records a line total; total / qty is the rate it implies."""
    total, qty = _number(r.get("total_price")), _number(r.get("qty"))
    return round(total / qty, 2) if total and qty else None


def _rate_order(r: dict) -> tuple:
    """L1 first, then the comparative statement, then BOQ rows; cheapest first within each."""
    unit = _number(r.get("unit_price")) or _number(r.get("estimated_rate")) or _per_unit(r) or 0.0
    return (0 if r.get("bid_rank") == "L1" else 1,
            0 if r.get("source") == "ComparativeStatement" else 1, unit, str(sorted(r.items())))


def _rate(r: dict) -> str:
    """One price observation as recorded: level, figure, basis, source."""
    uom = r.get("uom") or "unit"
    if r.get("unit_price"):
        figure = f"Rs {_money(r['unit_price'])}/{uom}"
    elif r.get("estimated_rate"):
        figure = f"Rs {_money(r['estimated_rate'])}/{uom}"
    else:
        figure = f"Rs {_money(r['total_price'])} for {_qty(r.get('qty')) or '?'} {uom}"
        per = _per_unit(r)
        if per is not None:
            figure += f" (= Rs {per:,.2f}/{uom})"
    bits = [r.get("price_level") or "price", figure]
    if r.get("bid_rank"):
        bits.append(r["bid_rank"])
    source = "comparative statement" if r.get("source") == "ComparativeStatement" else r.get("source")
    if source:
        bits.append(source + (f" {r['observed_on']}" if r.get("observed_on") else ""))
    return " ".join(bits)


def _rs(value) -> str:
    n = _number(value)
    return f"Rs {n:,.0f}" if n is not None and n == int(n) else (f"Rs {n:,.2f}" if n is not None else "-")


def _values(values: list[str], cap: int = 8) -> str:
    if not values:
        return "not stated in the item names"
    return ", ".join(values[:cap]) + (f" (+{len(values) - cap} more)" if len(values) > cap else "")


COMPARE_RULES = (
    "TO ANSWER A COMPARISON ('compare', 'which is more relevant', 'which should I use'): "
    "(1) reproduce the comparison table; (2) one bullet per row where the tenders differ, "
    "with both figures and both PUR ids; (3) 'Pick:' one tender for each purpose the user "
    "stated (or for the request as asked), each with the figures that decide it - spec match "
    "to the request first, then recency, then price and competition. Use only figures in this "
    "output; never pick without them."
)


def _comparison_table(comparison: list[dict], sizes: str) -> list[str]:
    """One column per tender, one row per comparison point."""
    cols = comparison[:5]
    def row(label, cells):
        return f"| {label} | " + " | ".join(str(c).replace("|", "/") for c in cells) + " |"

    out = ["", "Comparison (one column per tender; every figure read from the graph):",
           row("", [c["case_key"] for c in cols]), "|---|" + "---|" * len(cols)]
    out.append(row("Tender no.", [c.get("tender_no") or "-" for c in cols]))
    out.append(row("Subject", [(c.get("job_name") or "-")[:70] for c in cols]))
    out.append(row("Contract type (from the subject)", [c.get("contract_type") or "-" for c in cols]))
    out.append(row("Bid opening", [c.get("bid_opening_date") or "-" for c in cols]))
    out.append(row("Department", [c.get("department") or "-" for c in cols]))
    out.append(row("Matching lines (exact" + (f" {sizes}" if sizes else "") + " / other size)",
                   [f"{c['lines']['exact']} / {c['lines']['similar']}" for c in cols]))
    attrs = list(dict.fromkeys(a for c in cols for a in (c.get("specs") or {})))
    for attr in attrs:
        out.append(row(f"Spec: {attr}", [_values((c.get("specs") or {}).get(attr) or []) for c in cols]))
    def qty(c):
        q = c.get("quantity")
        if not q:
            return "not on file"
        lo, hi = _qty(q["per_line_min"]), _qty(q["per_line_max"])
        span = lo if lo == hi else f"{lo}-{hi}"
        return f"{span} {q.get('uom') or ''} a line, total {_qty(q['total'])}".replace("  ", " ")
    out.append(row("Quantity (planned or bid)", [qty(c) for c in cols]))
    def low(c):
        st = c.get("lowest_bid_per_unit")
        return (f"{_rs(st['min'])} / {_rs(st['median'])} / {_rs(st['max'])} ({st['n']} lines)"
                if st else "no bid on file")
    out.append(row("Lowest bid per unit (min / median / max)", [low(c) for c in cols]))
    if sizes:
        def low_exact(c):
            st = c.get("lowest_bid_per_unit_exact")
            return (f"{_rs(st['min'])} / {_rs(st['median'])} / {_rs(st['max'])} ({st['n']} lines)"
                    if st else "no exact-size line priced")
        out.append(row(f"Lowest bid per unit, {sizes} lines only", [low_exact(c) for c in cols]))
    def spread(c):
        st = c.get("highest_over_lowest_pct")
        return (f"+{st['median']}% median (+{st['min']}% to +{st['max']}%)" if st
                else "one bid per line, or none")
    out.append(row("Highest bid above the lowest", [spread(c) for c in cols]))
    out.append(row("Bidders", [f"{len(c.get('bidders') or [])}: " + ", ".join(c.get("bidders") or [])
                               if c.get("bidders") else "not on file" for c in cols]))
    out.append(row("Award", [", ".join(c["awarded_to"]) if c.get("awarded_to") else "not on file"
                             for c in cols]))
    if len(comparison) > len(cols):
        out.append(f"(+{len(comparison) - len(cols)} more tender(s): "
                   + ", ".join(c["case_key"] for c in comparison[len(cols):]) + ")")
    return out


def _format_items(data: dict, query: str) -> str:
    """The /api/kg/items reply as the agent should read it: the answer, a
    side-by-side comparison of the tenders, then each tender's own rows
    exactly as written. Rows are grouped per tender so no tender can be
    crowded out of view: a global cap once showed 12 rows of one tender and
    hid the other's 24, and the model explained the gap with a reason it
    invented ("only an aggregated range")."""
    forms = "; ".join(f"{c['concept']} ({', '.join(sorted(c['forms'], key=len)[:3])})"
                      for c in data.get("concepts") or [])
    sizes = ", ".join(f"{s['value']} {s.get('unit') or ''}".strip() for s in data.get("sizes") or [])
    searched = data.get("items_searched")
    counts = data.get("counts") or {}
    answer = data.get("answer")
    scope = (f"Searched all {searched:,} line items" if isinstance(searched, int) else "Searched the line items") \
        + f" for: {forms}" + (f"; size {sizes}" if sizes else "") + "."
    if answer == "no":
        return (f"ITEM CHECK — NO: no past NRL line item names every part of '{query}'. {scope} "
                f"This is a real negative from the full item list, not an outage: say NRL's "
                f"records show no purchase of it, and do not offer a different item as if it were it.")
    rows = (data.get("exact") or []) + (data.get("similar") or [])
    comparison = data.get("comparison") or []
    cases = [c["case_key"] for c in comparison] or list(dict.fromkeys(r["case_key"] for r in rows))
    n = len(cases)
    lines = []
    if answer == "yes":
        lines.append(f"ITEM CHECK — YES: {counts.get('exact', 0)} line item(s) in {n} tender(s) match "
                     f"every word" + (f" and the size {sizes}" if sizes else "") + f". {scope}")
    else:
        lines.append(f"ITEM CHECK — NOT AT THIS SIZE: the item was bought ({counts.get('similar', 0)} "
                     f"line item(s) in {n} tender(s)), but no line item has {sizes}. {scope}")
        lines.append(f"No row below is {sizes}: never call one the size asked for, and never "
                     f"call a size 'closest' or 'near' (a size in NB or inches is not near one "
                     f"in mm). Say NRL has not bought it at {sizes} and list the sizes it did buy.")
    lines.append("Quote items exactly as written, with their tender; quote rates with the price "
                 "level and source shown; never add one. Rows not shown below EXIST with their "
                 "rates: never say a tender lacks item rates; call kg_item_history again with "
                 "tender=<PUR id> to read all of its rows.")
    if comparison:
        lines += _comparison_table(comparison, sizes)
        if n > 1:
            lines += ["", COMPARE_RULES]
    by_case = {c: [r for r in rows if r["case_key"] == c] for c in cases}
    total = {c["case_key"]: c["lines"]["exact"] + c["lines"]["similar"] for c in comparison}
    for case, mine in by_case.items():
        if not mine:
            continue
        lines += ["", f"{case} items:", "| Item (as written) | Size match | Code | Rates on file |",
                  "|---|---|---|---|"]
        for r in mine:
            rates = "; ".join(_rate(x) for x in sorted(r.get("rates") or [], key=_rate_order)[:3]) \
                or "none on file"
            item = str(r.get("item") or "").replace("|", "/")
            match = "exact" if r.get("tier", "exact") == "exact" else "other size"
            lines.append(f"| {item} | {match} | {r.get('material_code') or '-'} | {rates} |")
        more = total.get(case, len(mine)) - len(mine)
        if more > 0:
            lines.append(f"(+{more} more row(s) of {case}, with their rates, not shown here: "
                         f"call kg_item_history with tender={case} to read them)")
    if not comparison:   # an older KG without the comparison block
        for key, t in (data.get("tenders") or {}).items():
            bits = [key, t.get("tender_no"), (t.get("job_name") or "")[:90], t.get("department"),
                    f"bid opening {t['bid_opening_date']}" if t.get("bid_opening_date") else None,
                    f"bidders: {', '.join(t['bidders'])}" if t.get("bidders") else None,
                    f"awarded to: {', '.join(t['awarded_to'])}" if t.get("awarded_to")
                    else "award not on file"]
            lines.append("- " + " · ".join(b for b in bits if b))
    return "\n".join(lines)


def _item_check(base_url: str, query: str) -> str:
    """The item-level answer, or "" when it cannot be had (old KG, outage):
    the caller's own result then stands, never a guessed one."""
    try:
        return _format_items(_get(base_url, "/api/kg/items", {"query": query, "limit": 40}), query)
    except Exception:  # noqa: BLE001 - an extra check must not break the main one
        return ""


def _tender_items(base_url: str, tender: str) -> str:
    """Every line item of one tender (its BOQ), for kg_item_history with a
    tender and no item."""
    try:
        case = _get(base_url, "/api/kg/case", {"case_key": guard.plain_ids(tender).strip()})
    except Exception as exc:  # noqa: BLE001
        return _unavailable(exc, base_url)
    boq = case.get("boq") or []
    if case.get("error") or not boq:
        return (f"TENDER ITEMS — {tender}: the knowledge graph holds no line item for this "
                "tender." if not case.get("error") else f"TENDER ITEMS — {case['error']}.")
    rows = [f"TENDER ITEMS — {case.get('case_key') or tender} · {case.get('job_name') or ''}: "
            f"{len(boq)} line item(s).", "", "| # | Item | Material code |", "|---|---|---|"]
    rows += [f"| {n} | {' '.join(str(b.get('item') or '').split())[:90].replace('|', '/')} | "
             f"{b.get('material_code') or '-'} |" for n, b in enumerate(boq[:60], 1)]
    if len(boq) > 60:
        rows.append(f"(+{len(boq) - 60} more)")
    return "\n".join(rows)


def make_item_history_tool(base_url: str) -> Tool:
    def kg_item_history(item: str, tender: str = "", _notify=None) -> str:
        if not str(item or "").strip():
            if not str(tender or "").strip():
                return _missing("item", item)
            return _tender_items(base_url, tender)
        _emit(_notify, "items", item)
        params = {"query": item, "limit": 40}
        if tender:
            params.update(tender=tender.strip(), per_tender=100)
        try:
            data = _get(base_url, "/api/kg/items", params)
        except Exception as exc:  # noqa: BLE001
            return _unavailable(exc, base_url)
        _emit(_notify, "items", item, hits=sum((data.get("counts") or {}).values()))
        return _format_items(data, item)

    return Tool(
        name="kg_item_history",
        description=(
            "Have we bought this item before, at this size, and at what rate? Reads EVERY line "
            "item in the tender knowledge graph and keeps the ones naming every word asked for, "
            "NRL abbreviations included (TG temperature gauge, PG pressure gauge, BiMet "
            "bimetallic, VLV valve, U320 = 320 mm stem). Returns YES with the exact-size rows, "
            "NOT AT THIS SIZE with the sizes NRL did buy, or NO; each row with its tender, "
            "material code and the recorded rates (price level, L1, comparative statement), "
            "plus a side-by-side comparison of the tenders (specs, quantity, lowest bid per "
            "unit, bid spread, bidders). Use it FIRST for any 'did we buy / have we purchased / "
            "what did we pay for / compare' question about a specific item, and again with "
            "tender=<PUR id> to read every row of one tender; kg_precedent_search ranks whole "
            "tenders for drafting and misses abbreviated item names."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "item": {"type": "string",
                         "description": "The item with its key attributes and size, e.g. "
                                        "'bimetallic temperature gauge 320 mm stem'"},
                "tender": {"type": "string",
                           "description": "Optional PUR case id: list every matching row of "
                                          "that one tender, e.g. 'PUR-INSTMNT-1003188'; with "
                                          "item='' every line item of that tender"},
            },
            "required": ["item"],
        },
        fn=kg_item_history,
        wants_notify=True,
    )


# The files of a tender that answer "the PR / PO / award of this tender",
# listed first and in this order; the KG's other files follow with counts.
_RECORD_TYPES = ("PO", "AwardNote", "AdminApprovalNote", "Estimate", "ComparativeStatement")
_PUR_ID = re.compile(r"\bPUR-[A-Z]+-\d+\b", re.IGNORECASE)


def _resolve(base_url: str, ref: str) -> tuple[str, str]:
    """(PUR case id, "") for a reference, or ("", why not)."""
    ref = guard.plain(ref)
    m = _PUR_ID.search(ref)
    if m:
        return m.group(0).upper(), ""
    found = _get(base_url, "/api/kg/resolve", {"ref": ref.strip()})
    keys = list(dict.fromkeys(x["case_key"] for x in found.get("matches") or []))
    if len(keys) == 1:
        return keys[0], ""
    if keys:
        return "", f"'{ref}' names {len(keys)} tenders: {', '.join(keys[:8])}. Ask which one."
    return "", f"No past NRL tender is recorded as '{ref}'."


def _tender_record(case: dict, docs: list[dict]) -> str:
    key = case["case_key"]
    head = " · ".join(str(b) for b in (key, case.get("tender_no"), case.get("job_name"),
                                        case.get("department")) if b)
    award = ", ".join(case.get("awarded_to") or []) or "not on file"
    rows = [("PR no.", case.get("pr_no") or "not on file"),
            ("PO no. (SAP)", ", ".join(case.get("purchase_orders") or [])
             or "not recorded in the knowledge graph"),
            ("Contract / work order no. (from its documents)",
             ", ".join((case.get("work_orders") or [])[:12]) or "none on file"),
            ("Awarded to", award),
            ("Estimated value", case.get("estimated_value") or "not on file"),
            ("Bid opening", case.get("bid_opening_date") or "not on file")]
    out = [f"TENDER RECORD — {head}",
           ("Reproduce both tables. Quote file names exactly: the files were not opened, so "
            "never describe what a file says, and give only the PO numbers in the table."),
           "", "| Field | On file |", "|---|---|"]
    out += [f"| {k} | {str(v).replace('|', '/')} |" for k, v in rows]
    by_type: dict[str, list[str]] = {}
    for d in docs:
        names = by_type.setdefault(str(d.get("document_type") or "Other"), [])
        name = str(d.get("filename") or "").replace("|", "/")
        if name and name not in names:
            names.append(name)
    if not by_type:
        out.append("\nThe knowledge graph lists no files for this tender.")
        return "\n".join(out)
    order = [t for t in _RECORD_TYPES if t in by_type] + sorted(set(by_type) - set(_RECORD_TYPES))
    note = (f"Files the knowledge graph holds for {key}, as it classified them "
            "(on the KG share; a file's class may be wrong, its name is what it is):")
    out += ["", note, "", "| Type | Files |", "|---|---|"]
    for t in order:
        names = by_type[t]
        shown = ", ".join(names[:4]) + (f" (+{len(names) - 4} more)" if len(names) > 4 else "")
        out.append(f"| {t} | {shown} |")
    return "\n".join(out)


def make_tender_record_tool(base_url: str) -> Tool:
    def kg_tender_record(tender: str, _notify=None) -> str:
        if _missing("tender", tender):
            return _missing("tender", tender)
        _emit(_notify, "case", tender)
        try:
            key, why = _resolve(base_url, tender)
            if not key:
                return why
            case = _get(base_url, "/api/kg/case", {"case_key": key, "clause_limit": 1,
                                                   "item_limit": 1})
            if case.get("error"):
                return f"No past NRL tender is recorded as {key}."
            docs = _get(base_url, "/api/kg/documents", {"case_key": key}).get("documents") or []
        except Exception as exc:  # noqa: BLE001
            return _unavailable(exc, base_url)
        _emit(_notify, "case", tender, hits=len(docs))
        return _tender_record({**case, "case_key": key}, docs)

    return Tool(
        name="kg_tender_record",
        description=(
            "The record of ONE past tender: its PR number, award, estimated value and bid "
            "opening, and the files the knowledge graph holds for it (PO, award note, admin "
            "approval note, estimate, comparative statement, ...), by file name. Use it for "
            "'get me the PR / PO / award / documents of PUR-... (or tender no. ...)'. Call it "
            "directly: do not ask which documents first, it returns them all."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tender": {"type": "string",
                           "description": ("PUR case id or tender number, e.g. "
                                           "'PUR-MECHMNT-1008357' or 'OC01000435'")},
            },
            "required": ["tender"],
        },
        fn=kg_tender_record,
        wants_notify=True,
    )


_SAP_ROWS = 30         # lines shown, the newest first; the rest are counted


def _sap_table(data: dict, query: str) -> str:
    """sap_search's reply as the agent should read it: one row per SAP line."""
    lines = []
    for res in data.get("results") or []:
        for ln in res.get("lines") or []:
            if isinstance(ln, dict):
                lines.append({**ln, "description": ln.get("material_name")
                              or res.get("description") or ""})
    if not lines:
        return (f"SAP — NO LINE: SAP's PR, PO and service exports hold no line for '{query}'. "
                "This is a real negative from SAP, not an outage: say SAP has no record of it.")
    lines.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    pos = sorted({str(x["po_no"]) for x in lines if x.get("po_no")})
    prs = sorted({str(x["pr_no"]) for x in lines if x.get("pr_no")})
    out = [f"SAP — {len(lines)} line(s) for '{query}' (PR: {', '.join(prs[:10]) or 'none'}; "
           f"PO: {', '.join(pos[:10]) or 'none'}).",
           "Reproduce the table; quote numbers exactly as written; never add a line, a rate or "
           "a PR/PO number it does not show.", "",
           "| Kind | Doc no. | Date | Material | Description | Qty | Unit | Rate (INR) | PR | PO "
           "| PUR case |", "|---|---|---|---|---|---:|---|---:|---|---|---|"]
    for x in lines[:_SAP_ROWS]:
        cells = [x.get("kind"), x.get("doc_no"), x.get("date"), x.get("material"),
                 " ".join(str(x.get("description") or "").split())[:70], x.get("quantity"),
                 x.get("unit"), x.get("rate_inr"), x.get("pr_no"), x.get("po_no"),
                 x.get("pur_case_id")]
        out.append("| " + " | ".join(str(c if c not in (None, "") else "-").replace("|", "/")
                                     for c in cells) + " |")
    if len(lines) > _SAP_ROWS:
        out.append(f"(+{len(lines) - _SAP_ROWS} older line(s) not shown)")
    return "\n".join(out)


def make_sap_tool(base_url: str) -> Tool:
    def sap_search(query: str, top_k: int = 10, _notify=None) -> str:
        if _missing("query", query):
            return _missing("query", query)
        _emit(_notify, "sap", query)
        try:
            data = kg_mcp.call(base_url, "sap_search", query=guard.plain(query).strip(),
                               top_k=max(1, int(top_k)))
        except Exception as exc:  # noqa: BLE001
            return _unavailable(exc, base_url)
        out = _sap_table(data if isinstance(data, dict) else {}, query)
        _emit(_notify, "sap", query, hits=out.count("\n| ") - 1)
        return out

    return Tool(
        name="sap_search",
        description=(
            "NRL's SAP purchasing records: PR, PO and service lines for a need, found by "
            "keyword and meaning, or by an exact material code, PR number, PO number or PUR "
            "case id. Each line has its date, quantity, unit, rate and PUR case. Use it for "
            "'what did we pay for ...', 'the PR / PO of ...', 'when did we last buy ...', "
            "'how many ... did we order'. An exact number or id answers fastest."),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": (
                    "The item in words ('thermowell 320 mm'), or an exact material code, PR "
                    "no., PO no. or PUR id ('PUR-INSTMNT-1002364', '4500024959')")},
                "top_k": {"type": "integer", "description": "Matches to return (default 10)"},
            },
            "required": ["query"],
        },
        fn=sap_search,
        wants_notify=True,
    )


def allowed(setting: str) -> set[str] | None:
    """The KG tools the agent is given (NAM_KG_TOOLS, comma separated), or
    None for every one ("" or "all"): "sap_search" tests SAP alone."""
    names = {n.strip() for n in (setting or "").split(",") if n.strip()}
    return None if not names or names == {"all"} else names


def kg_on(only: set[str] | None) -> bool:
    """Whether the tender knowledge graph's kg_* tools are given."""
    return only is None or any(n.startswith("kg_") for n in only)


def make_tools(base_url: str, only: set[str] | None = None,
               kg_api_url: str = "") -> list[Tool]:
    """sap_search on the MCP server (base_url); the KG tools on kg_api_url
    (the MCP server too when it is empty)."""
    every = make_all_tools(base_url, kg_api_url)
    return every if only is None else [t for t in every if t.name in only]


def make_all_tools(base_url: str, kg_api_url: str = "") -> list[Tool]:
    kg = kg_api_url or base_url
    return [
        make_item_history_tool(kg),
        make_tender_record_tool(kg),
        make_find_tenders_tool(kg),
        make_precedent_tool(kg),
        make_price_tool(kg),
        make_clause_tool(kg),
        make_sap_tool(base_url),
    ]
