"""The KG over MCP (Streamable HTTP), shaped like the old precedent REST API.

The knowledge graph is served as an MCP server (NAM_KG_URL, e.g.
http://<mcp-host>:8091/mcp) rather than the FastAPI service at
/api/kg/*. The tools and NAM code were written against the REST replies, so
`legacy_get` keeps their `_get(base_url, path, params)` signature and answers
each old path from the MCP tools, in the old shape. Callers do not change.

What each old read is answered from:

  tenders       search_tenders (subject and meaning) and search_items (line
                items): one row per tender with the lines that name the item,
                whether its subject names it, and the lines that ARE the item
                (the whole phrase, in order) as exact_items
  items         search_items: the lines that are the item at the size asked
                (exact) or at another size (similar), with their rates
  case,         case_details: the tender's fields, its line items, PQC, SCC,
  case_summary  specs, bidders, files and contract work orders (PO numbers)
  documents     case_details' files (SUPPORTED_BY), by document type
  resolve       search_tenders, matched on the PUR id or the tender number
  precedent,    search_tenders / similar_items
  prices,
  clauses
  /health       tools/list: the server answers

Every limit the server puts on an argument (top_k <= 50) is read from its
own tools/list, never written here: top_k=100 was refused outright. Where the
server holds nothing, the reply says so (an empty list) rather than
inventing a value.

Plain JSON-RPC over urllib: the server answers a POST with one JSON body, so
no MCP SDK is needed and the core stays stdlib-only.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request

_TIMEOUT = float(os.getenv("NAM_KG_TIMEOUT", "120"))
_SIMILAR = 0.75   # an item at or above this similarity counts as the item asked for
_WORD = re.compile(r"[a-z][a-z0-9]*")
_NUMBER = re.compile(r"(\d+(?:\.\d+)?)\s*(mm|cm|nb|inch|in|bar|kg|deg\w*|°c)?", re.IGNORECASE)

_schemas: dict[str, dict] = {}
_lock = threading.Lock()


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/mcp") else base + "/mcp"


def _rpc(base_url: str, method: str, params: dict, timeout: float) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(_endpoint(base_url), data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    for attempt in (1, 2):   # one dropped connection is retried; an HTTP error is an answer
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError:
            raise
        except urllib.error.URLError:
            if attempt == 2:
                raise
    if raw.lstrip().startswith("event:") or raw.lstrip().startswith("data:"):
        raw = next(line[5:].strip() for line in raw.splitlines() if line.startswith("data:"))
    msg = json.loads(raw)
    if "error" in msg:
        raise RuntimeError(str(msg["error"].get("message", msg["error"]))[:200])
    return msg["result"]


def reachable(base_url: str) -> bool:
    _rpc(base_url, "tools/list", {}, 3)
    return True


def tools(base_url: str) -> dict[str, dict]:
    """{name: tool} from the server's tools/list, read once per server."""
    key = _endpoint(base_url)
    with _lock:
        if key not in _schemas:
            listed = _rpc(base_url, "tools/list", {}, 30).get("tools") or []
            _schemas[key] = {t["name"]: t for t in listed if isinstance(t, dict) and t.get("name")}
        return _schemas[key]


def _fit(base_url: str, tool: str, args: dict) -> dict:
    """The arguments within the server's own limits (its inputSchema's
    maximum and minimum)."""
    try:
        props = ((tools(base_url).get(tool) or {}).get("inputSchema") or {}).get("properties") or {}
    except Exception:  # noqa: BLE001 - no schema: the arguments as given
        return args
    out = {}
    for k, v in args.items():
        spec = props.get(k) or {}
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if isinstance(spec.get("maximum"), (int, float)):
                v = min(v, spec["maximum"])
            if isinstance(spec.get("minimum"), (int, float)):
                v = max(v, spec["minimum"])
        out[k] = v
    return out


def envelope(base_url: str, tool: str, **args) -> dict:
    """The tool's whole envelope (status, data, sap, provenance)."""
    result = _rpc(base_url, "tools/call", {"name": tool, "arguments": _fit(base_url, tool, args)},
                  _TIMEOUT)
    text = "".join(c.get("text", "") for c in result.get("content") or [])
    if result.get("isError"):
        raise RuntimeError(text[:200] or f"{tool} failed")
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    return json.loads(text)


def call(base_url: str, tool: str, **args):
    """The tool's `data` payload; {} when the store holds nothing for it."""
    return envelope(base_url, tool, **args).get("data") or {}


# -- words ---------------------------------------------------------------------------

def _stem(word: str) -> str:
    """One form for singular and plural: "gauges" and "gauge" are "gauge"
    (cutting "es" made "gaug", and a gauge line stopped counting)."""
    if word.endswith("ies") and len(word) > 5:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if len(w) > 1]


def _phrase(query: str) -> re.Pattern | None:
    """The query's words in order, each in any plural form ("temperature
    gauges" for "temperature gauge"), with anything but a letter between."""
    words = [_stem(w) for w in _words(query) if not w.isdigit()]
    if not words:
        return None
    parts = [re.escape(w) + r"[a-z]{0,3}" for w in words]
    return re.compile(r"\b" + r"[^a-z]+".join(parts) + r"\b", re.IGNORECASE)


def _all_words(query: str, text: str) -> bool:
    have = {_stem(w) for w in _words(text)}
    want = [_stem(w) for w in _words(query) if not w.isdigit()]
    return bool(want) and all(w in have for w in want)


def _sizes(query: str) -> list[dict]:
    return [{"value": n, "unit": (u or "").lower()} for n, u in _NUMBER.findall(query or "")]


def _has_sizes(sizes: list[dict], text: str) -> bool:
    low = (text or "").lower()
    return all(re.search(rf"(?<![\d.]){re.escape(s['value'])}(?!\d)", low) for s in sizes)


# -- the old reads ---------------------------------------------------------------------

def _row_rates(row: dict) -> list[dict]:
    """A search_items row's figures as the old rate records."""
    out = []
    for key, level in (("unit_price", "bid"), ("estimated_rate", "estimate")):
        value = row.get(key)
        try:
            if value in (None, "") or float(str(value).replace(",", "")) == 0:
                continue
        except ValueError:
            continue
        out.append({"unit_price": value, "uom": row.get("uom"), "qty": row.get("order_qty"),
                    "price_level": level, "source": row.get("stated_by")})
    return out


def _search_tenders(base: str, query: str, limit: int) -> list[dict]:
    data = call(base, "search_tenders", query=query, top_k=limit)
    rows = data if isinstance(data, list) else data.get("rows") or []
    for r in rows:
        r["case_key"] = r.get("case_key") or r.get("pur_case_id")
    return [r for r in rows if r.get("case_key")]


def _search_items(base: str, query: str, limit: int = 50) -> list[dict]:
    data = call(base, "search_items", query=query, top_k=limit)
    rows = data if isinstance(data, list) else data.get("rows") or []
    return [r for r in rows if isinstance(r, dict) and r.get("pur_case_id")]


def _similar(base: str, query: str, limit: int) -> list[dict]:
    data = call(base, "similar_items", query=query, top_k=limit)
    return data if isinstance(data, list) else data.get("items") or []


def _tenders(base: str, query: str, limit: int) -> dict:
    """search_tenders and search_items as the old /api/kg/tenders rows."""
    phrase = _phrase(query)
    rows = _search_tenders(base, query, limit)
    try:
        items = _search_items(base, query)
    except Exception:  # noqa: BLE001 - the line items failed on the server: subjects stand
        items = []
    lines: dict[str, list[str]] = {}
    for r in items:
        text = " ".join(str(r.get("item_description") or "").split())
        if text and _all_words(query, text) and text not in lines.setdefault(r["pur_case_id"], []):
            lines[r["pur_case_id"]].append(text)
    info = {r["pur_case_id"]: r for r in items}
    by_key = {r["case_key"]: r for r in rows}
    keys = list(by_key) + [k for k in lines if k not in by_key]
    out = []
    for key in keys:
        t = by_key.get(key) or info.get(key) or {}
        found = lines.get(key, [])
        name = str(t.get("job_name") or "")
        by_name = bool(phrase and phrase.search(name))
        if not (found or by_name or _all_words(query, name)):
            continue                      # ranked by meaning only: not this item
        out.append({"case_key": key, "tender_no": t.get("tender_no"), "job_name": name,
                    "department": t.get("department"), "matched_by_name": by_name,
                    "matching_item_count": len(found), "matching_items": found[:20],
                    "exact_items": [x for x in found if phrase and phrase.search(x)][:20]})
    exact = sum(1 for r in out if r["exact_items"]) if len(_words(query)) > 1 else 0
    return {"tenders": out, "count": len(out), "terms": _words(query),
            "item_match": {"exact": exact}}


def _items(base: str, query: str, tender: str = "") -> dict:
    """search_items as the old item check: the lines that are the item at
    the size asked (exact), at another size (similar), or none."""
    phrase, sizes = _phrase(query), _sizes(query)
    rows = _search_items(base, query)
    if tender:
        rows = [r for r in rows if r["pur_case_id"] == tender]
    exact, similar, seen = [], [], set()
    for r in rows:
        text = " ".join(str(r.get("item_description") or "").split())
        key = (r["pur_case_id"], text, r.get("material_code"))
        if not text or key in seen or not ((phrase and phrase.search(text))
                                           or _all_words(query, text)):
            continue
        seen.add(key)
        row = {"case_key": r["pur_case_id"], "item": text,
               "material_code": r.get("material_code") or None, "rates": _row_rates(r)}
        (exact if _has_sizes(sizes, text) else similar).append(
            {**row, "tier": "exact" if _has_sizes(sizes, text) else "similar"})
    answer = "yes" if exact else ("similar" if similar else "no")
    kept = {x["case_key"] for x in exact + similar}
    tenders = {r["pur_case_id"]: {"tender_no": r.get("tender_no"), "job_name": r.get("job_name"),
                                  "department": r.get("department"), "awarded_to": [],
                                  "bidders": []}
               for r in rows if r["pur_case_id"] in kept}
    return {"query": query, "answer": answer, "exact": exact, "similar": similar,
            "counts": {"exact": len(exact), "similar": len(similar)}, "tenders": tenders,
            "concepts": [{"concept": " ".join(_words(query)), "forms": _words(query)}],
            "sizes": sizes, "items_searched": None}


def _names(related: dict, rel: str) -> list[str]:
    return [str(n.get("name")) for n in related.get(rel) or [] if n.get("name")]


def _case(base: str, key: str) -> dict:
    """case_details as the old /api/kg/case and case_summary record."""
    env = envelope(base, "case_details", case_key=key)
    d = env.get("data") or {}
    t, rel = d.get("tender") or {}, d.get("related") or {}
    if not t:
        return {"error": f"no tender {key}"}
    # SAP's own purchase orders for this tender, and apart from them the
    # contract and work-order numbers its documents state (not all are POs)
    pos = []
    for m in ((env.get("sap") or {}).get("materials") or {}).values():
        last = (m or {}).get("last_po") or {}
        if last.get("po_no") and last.get("pur_case_id") in (None, key):
            pos.append(str(last["po_no"]))
    orders = [str((n.get("properties") or {}).get("work_order_number"))
              for n in rel.get("HAS_CONTRACT") or []
              if (n.get("properties") or {}).get("work_order_number")]
    files = []
    for n in rel.get("SUPPORTED_BY") or []:
        p = n.get("properties") or {}
        files.append({"document_type": p.get("document_type"),
                      "filename": p.get("original_filename") or p.get("source_document"),
                      "byte_size": p.get("byte_size"),
                      "readable": p.get("terminal_state") != "MISSING"})
    boq = [{"item": n.get("name"), "material_code": (n.get("properties") or {}).get(
        "material_code") or ""} for n in rel.get("PROCURES_ITEM") or []]
    clauses = {k: [{"text": x, "summary": x[:300]} for x in _names(rel, r)]
               for k, r in (("pqc", "HAS_PQC"), ("scc", "HAS_SCC"), ("tech_spec", "HAS_SPEC"))}
    return {
        "case_key": key, "tender_no": t.get("tender_no"), "pr_no": t.get("pr_no"),
        "job_name": t.get("job_name"), "estimated_value": t.get("estimated_value"),
        "emd_amount": t.get("emd_amount"), "bid_type": t.get("bid_type"),
        "bid_opening_date": t.get("bid_opening_date"),
        "bid_opening_iso": t.get("bid_opening_date_iso"),
        "bid_submission_end_date": t.get("bid_submission_end_date"),
        "pre_bid_meeting_date": t.get("pre_bid_meeting_date"),
        "department": next(iter(_names(rel, "IN_DEPARTMENT")), None),
        "work_category": next(iter(_names(rel, "IN_CATEGORY")), None),
        "procurement_category": next(iter(_names(rel, "IN_PROCUREMENT_CATEGORY")), None),
        "boq_lines": len(boq), "pqc_clauses": len(clauses["pqc"]),
        "scc_clauses": len(clauses["scc"]), "spec_clauses": len(clauses["tech_spec"]),
        **clauses, "technical_specs": _names(rel, "HAS_SPEC"), "boq": boq,
        "bidders": list(dict.fromkeys(_names(rel, "BID"))), "awarded_to": [],
        "purchase_orders": list(dict.fromkeys(pos)),
        "work_orders": [o for o in dict.fromkeys(orders) if o not in pos], "documents": files,
        "sor_references": [],
        "counts": {"pqc": len(clauses["pqc"]), "scc": len(clauses["scc"]),
                   "tech_spec": len(clauses["tech_spec"]), "boq": len(boq),
                   "sor_references": 0},
    }


def _norm_no(text) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _resolve(base: str, ref: str) -> dict:
    """The tenders a typed reference names: the PUR id itself, the tender
    number as recorded, or its core (the number without its suffix)."""
    ref = ref.strip()
    if re.fullmatch(r"PUR-[A-Z]+-\d+", ref, re.IGNORECASE):
        case = _case(base, ref.upper())
        return {"ref": ref, "tier": "case_key", "ambiguous": False, "matches": [] if
                case.get("error") else [{"case_key": ref.upper(), "tender_no": case.get("tender_no"),
                                         "job_name": case.get("job_name"), "match": "case_key"}]}
    want = _norm_no(ref)
    rows = _search_tenders(base, ref, 50)
    exact = [r for r in rows if _norm_no(r.get("tender_no")) == want]
    core = [r for r in rows if want and _norm_no(r.get("tender_no")).startswith(want)]
    pick, tier = (exact, "tender_no") if exact else (core, "core")
    return {"ref": ref, "tier": tier, "ambiguous": len(pick) > 1, "matches": [
        {"case_key": r["case_key"], "tender_no": r.get("tender_no"),
         "job_name": r.get("job_name"), "match": tier} for r in pick]}


def legacy_get(base_url: str, path: str, params: dict) -> dict:
    """Answer an old /api/kg/<name> request from the MCP tools."""
    name = path.rsplit("/", 1)[-1]
    limit = int(params.get("limit") or 10)

    if name == "health":
        return {"tenders": None, "tools": len(tools(base_url))}

    if name == "tenders":
        return _tenders(base_url, params["query"], limit)

    if name == "items":
        return _items(base_url, params["query"], str(params.get("tender") or ""))

    if name == "precedent":
        query = params["requirement"]
        rows = _search_tenders(base_url, query, limit)
        items = _similar(base_url, query, 3)
        best = max((i.get("similarity") or 0 for i in items), default=None)
        for r in rows:
            r["pur_case_id"] = r["case_key"]
            r["matched_items"] = [i["name"] for i in items
                                  if r["case_key"] in (i.get("tenders") or [])]
        gap = None
        if rows and (best is None or best < _SIMILAR):
            gap = "No line item closely matches the requirement; tenders were ranked on their subject only."
        return {"tenders": rows, "best_similarity": best, "precedent_gap": gap,
                "precedent_found": bool(rows) and gap is None}

    if name == "prices":
        query = params["requirement"]
        items = _similar(base_url, query, limit)
        rows, lines = [], []
        for it in items:
            for p in it.get("prices") or []:
                rows.append({"item": it.get("name"), **p})
                lines.append(f"- {it.get('name')} · {p.get('level')} {p.get('price')} per "
                             f"{p.get('uom') or 'unit'} · {p.get('stated_by') or '-'} · {p.get('tender')}")
        best = max((i.get("similarity") or 0 for i in items), default=0)
        return {"rows": rows, "formatted": "\n".join(lines),
                "precedent_gap": None if best >= _SIMILAR else
                "The closest priced item is not a close match to the requirement."}

    if name == "clauses":
        kind = params.get("kind") or "pqc"
        clauses: list[dict] = []
        for t in _search_tenders(base_url, params["requirement"], limit):
            for text in (_case(base_url, t["case_key"]).get(kind) or []):
                clauses.append({"text": text["text"], "source": {
                    "tender_no": t.get("tender_no"), "pur_case_id": t["case_key"],
                    "job_name": t.get("job_name"), "department": t.get("department")}})
        return {"clauses": clauses}

    if name == "resolve":
        return _resolve(base_url, params["ref"])

    if name == "case":
        return _case(base_url, params["case_key"])

    if name == "case_summary":
        keys = [k.strip() for k in str(params["case_keys"]).split(",") if k.strip()]
        return {"cases": [c for c in (_case(base_url, k) for k in keys) if not c.get("error")]}

    if name == "documents":
        case = _case(base_url, params["case_key"])
        docs = case.get("documents") or []
        if params.get("doc_type"):
            docs = [d for d in docs if d.get("document_type") == params["doc_type"]]
        return {"case_key": params["case_key"], "count": len(docs), "documents": docs}

    raise RuntimeError(f"{path} has no MCP equivalent")
