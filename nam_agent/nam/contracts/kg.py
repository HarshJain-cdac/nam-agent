"""The contracts answered from the NRL tender knowledge graph (the read-only
precedent service, NAM_KG_URL, /api/kg/*).

Each method turns the KG's reply into the contract's shape (base.py) and
nothing more: no ranking of its own, no value the KG did not return. Lists
are put in a fixed order, so the same KG reply always gives the same result.

How a KG reply maps to the three outcomes:

    reply with rows                  found: True
    reply with no rows               found: False ("asked, holds nothing")
    404 for a named tender           found: False
    unreachable, timeout, 5xx, a
    garbled body, or no rows with a
    retrieval_error                  Unavailable (retried, then kg_unavailable)
    any other 4xx                    Rejected (not retried)

Everything here is precedent: authority "precedent" on every result.
"""

from __future__ import annotations

import re
import urllib.error
from collections.abc import Callable

from nam_agent.nam.contracts.base import (
    RECORD_CLAUSES,
    RECORD_FIELDS,
    RECORD_ITEMS,
    Rejected,
    Unavailable,
)

Fetch = Callable[[str, str, dict], dict]


def _cite(row: dict, key: str = "case_key") -> dict:
    def text(value):
        return None if value in (None, "") else str(value)[:300]

    return {"case_key": str(row.get(key) or row.get("pur_case_id") or "unknown")[:60],
            "tender_no": text(row.get("tender_no")), "job_name": text(row.get("job_name")),
            "department": text(row.get("department"))}


def _strings(values, cap: int = 50) -> list[str]:
    return [str(v)[:300] for v in (values or [])[:cap] if v not in (None, "")]


def _years(values) -> list[int]:
    out = []
    for v in values or []:
        try:
            year = int(v)
        except (TypeError, ValueError):
            continue
        if 1950 <= year <= 2100:
            out.append(year)
    return sorted(set(out))[:60]


_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")


def _decimal(value) -> tuple[str | None, bool]:
    """(the number as an exact decimal string, or None; whether a recorded
    value could not be read as one). Only thousands separators are dropped:
    anything else is left out rather than guessed."""
    if value is None or value == "" or isinstance(value, bool):
        return None, False
    text = str(value).replace(",", "").strip()
    return (text, False) if _DECIMAL.match(text) else (None, True)


class KgBackend:
    def __init__(self, fetch: Fetch, kg_url: str):
        self._fetch = fetch
        self._url = kg_url

    # -- the one way to the KG ------------------------------------------------

    def _get(self, path: str, params: dict, *, not_found_ok: bool = False) -> dict | None:
        if not self._url:
            raise Unavailable("the knowledge graph is not configured (NAM_KG_URL is unset)")
        try:
            data = self._fetch(self._url, path, params)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not_found_ok:
                return None
            if 400 <= exc.code < 500:
                raise Rejected(f"the KG refused {path}: HTTP {exc.code}") from None
            raise Unavailable(f"the KG answered {path} with HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise Unavailable(f"cannot reach the KG for {path} ({str(reason)[:120]})") from None
        except ValueError as exc:       # a body that is not JSON
            raise Unavailable(f"the KG sent a body that is not JSON for {path} "
                              f"({str(exc)[:80]})") from None
        if not isinstance(data, dict):
            raise Unavailable(f"the KG sent a {type(data).__name__} for {path}, not an object")
        return data

    @staticmethod
    def _head(endpoint: str, found: bool, warnings: list[str] | None = None) -> dict:
        return {"found": found, "source": {"system": "kg", "endpoint": endpoint},
                "authority": "precedent", "warnings": [w[:300] for w in (warnings or [])][:10]}

    @staticmethod
    def _retrieval(data: dict, empty: bool, path: str) -> list[str]:
        """A 200 that says the retrieval itself failed: with nothing in it that
        is 'could not ask', not 'no data'; with rows it is a warning."""
        err = data.get("retrieval_error") or data.get("error")
        if not err:
            return []
        if empty:
            raise Unavailable(f"the KG could not search for {path}: {str(err)[:160]}")
        return [f"the KG's search was partial: {str(err)[:200]}"]

    # -- contracts ------------------------------------------------------------

    def tenders(self, query: str, mode: str = "lexical", limit: int = 30) -> dict:
        if mode == "similar":
            path = "/api/kg/precedent"
            data = self._get(path, {"requirement": query, "limit": min(limit, 10)})
            rows = data.get("tenders") or []
            warnings = self._retrieval(data, not rows, path)
            tenders = [{"cite": _cite(t, "pur_case_id"),
                        "matching_items": _strings(t.get("matched_items")),
                        "matching_item_count": len(t.get("matched_items") or [])}
                       for t in rows[:10]]
            best = data.get("best_similarity")
            return {**self._head(path, bool(tenders), warnings), "total": len(tenders),
                    "best_similarity": float(best) if isinstance(best, (int, float)) else None,
                    "weak": (str(data["precedent_gap"])[:300]
                             if data.get("precedent_gap") else None),
                    "tenders": tenders}
        path = "/api/kg/tenders"
        data = self._get(path, {"query": query, "limit": limit})
        rows = data.get("tenders") or []
        warnings = self._retrieval(data, not rows, path)
        tenders = [{"cite": _cite(t), "matching_items": _strings(t.get("matching_items")),
                    "matching_item_count": int(t.get("matching_item_count") or 0)}
                   for t in rows[:limit]]
        # the KG ranks them; ties keep a fixed order by case key
        return {**self._head(path, bool(tenders), warnings),
                "total": int(data.get("count") or len(tenders)), "best_similarity": None,
                "weak": None, "tenders": tenders}

    def resolve_reference(self, ref: str) -> dict:
        path = "/api/kg/resolve"
        data = self._get(path, {"ref": ref})
        matches = data.get("matches") or []
        self._retrieval(data, not matches, path)
        out = sorted(({"cite": _cite(m), "match": m.get("match") or data.get("tier")}
                      for m in matches[:10] if m.get("match") or data.get("tier")),
                     key=lambda m: m["cite"]["case_key"])
        return {**self._head(path, bool(out)), "tier": data.get("tier") if out else None,
                "ambiguous": len(out) > 1, "matches": out}

    def _price_rows(self, requirement: str, limit: int) -> tuple[dict, list[dict], str]:
        path = "/api/kg/prices"
        data = self._get(path, {"requirement": requirement, "limit": limit})
        rows = data.get("rows") or []
        return data, rows, path

    def price_history(self, requirement: str, limit: int = 5) -> dict:
        data, rows, path = self._price_rows(requirement, limit)
        warnings = self._retrieval(data, not rows, path)
        out = [{"item": str(r.get("item") or "")[:300], "uom": r.get("uom") or None,
                "price_level": str(r.get("price_level") or "estimate")[:40],
                "unit_price": r.get("unit_price"), "estimated_rate": r.get("estimated_rate"),
                "totals": r.get("totals"), "observations": int(r.get("observations") or 0),
                "matched": bool(r.get("matched")), "years": _years(r.get("years")),
                "spans_years": bool(r.get("spans_years")),
                "sources": _strings(r.get("sources"))}
               for r in rows[:200]]
        if any(r.get("unparsed") for r in rows):
            warnings.append("some recorded amounts could not be read as numbers and are "
                            "left out of the figures")
        return {**self._head(path, bool(out), warnings),
                "weak": str(data["precedent_gap"])[:300] if data.get("precedent_gap") else None,
                "rows": out}

    def quantity_history(self, requirement: str, limit: int = 5) -> dict:
        data, rows, path = self._price_rows(requirement, limit)
        warnings = self._retrieval(data, not rows, path)
        # one row per item and unit: the price levels of the same order
        # share its quantity, so they are not counted twice
        by_key: dict[tuple, dict] = {}
        for r in rows:
            if not r.get("typical_qty"):
                continue
            key = (str(r.get("item") or ""), r.get("uom") or None)
            best = by_key.get(key)
            if best is None or r["typical_qty"]["n"] > best["typical_qty"]["n"]:
                by_key[key] = {"item": key[0][:300], "uom": key[1],
                               "typical_qty": r["typical_qty"], "matched": bool(r.get("matched")),
                               "years": _years(r.get("years")),
                               "sources": _strings(r.get("sources"))}
        out = sorted(by_key.values(), key=lambda r: (not r["matched"], r["item"].lower(),
                                                     r["uom"] or ""))[:200]
        return {**self._head(path, bool(out), warnings), "rows": out}

    def clauses(self, requirement: str, kind: str, limit: int = 5) -> dict:
        path = "/api/kg/clauses"
        data = self._get(path, {"requirement": requirement, "kind": kind, "limit": limit})
        rows = data.get("clauses") or []
        warnings = self._retrieval(data, not rows, path)
        out = [{"kind": kind, "text": str(c.get("text") or "")[:20000],
                "cite": _cite(c.get("source") or {}, "pur_case_id")}
               for c in rows[:60] if c.get("text")]
        return {**self._head(path, bool(out), warnings), "clauses": out}

    def boilerplate(self, section: str, limit: int = 3) -> dict:
        path = "/api/kg/boilerplate"
        data = self._get(path, {"section": section, "limit": limit})
        rows = data.get("clauses") or []
        warnings = self._retrieval(data, not rows, path)
        seen, out = set(), []
        for c in rows:                        # the same text from many tenders: once
            text = str(c.get("text") or "")[:20000]
            if text and text not in seen and len(out) < 60:
                seen.add(text)
                out.append({"text": text, "cite": _cite(c.get("source") or {}, "pur_case_id")})
        return {**self._head(path, bool(out), warnings), "section": section, "clauses": out}

    def documents(self, case_key: str, doc_type: str = "") -> dict:
        path = "/api/kg/documents"
        data = self._get(path, {"case_key": case_key, "doc_type": doc_type},
                         not_found_ok=True) or {}
        rows = data.get("documents") or []
        warnings = self._retrieval(data, not rows, path) if data else []
        # the path on the KG host is not passed on: the bytes are served by
        # the documents flow (Step 11), never by a path a caller could follow
        out = [{"document_type": d.get("document_type") or None,
                "filename": (str(d["filename"])[:300] if d.get("filename") else None),
                "extension": str(d.get("extension") or "")[:12],
                "byte_size": d.get("byte_size") if isinstance(d.get("byte_size"), int) else None,
                "readable": bool(d.get("readable"))}
               for d in rows[:60]]
        best = data.get("best_nit")
        if isinstance(best, dict):
            best = best.get("filename")
        return {**self._head(path, bool(out), warnings),
                "best_nit": str(best)[:300] if best else None, "documents": out}

    def enrichment(self, case_key: str) -> dict:
        path = "/api/kg/case"
        data = self._get(path, {"case_key": case_key, "clause_limit": 1, "item_limit": 1},
                         not_found_ok=True)
        if not data or data.get("error"):
            return {**self._head(path, False), "cite": None, "fields": {}, "awarded_to": [],
                    "counts": {}}
        fields = {k: str(data[k])[:300] for k in (
            "work_category", "estimated_value", "emd_amount", "bid_type", "bid_opening_date",
            "bid_submission_end_date", "pre_bid_meeting_date", "pr_no")
            if data.get(k) not in (None, "")}
        # what the tender holds, counted in full (the call above asked for one
        # clause and one item, so its own counts are not)
        warnings, counts = [], {}
        try:
            summary = self._get("/api/kg/case_summary", {"case_keys": case_key})
            row = next((c for c in summary.get("cases") or [] if c.get("case_key") == case_key),
                       {})
            counts = {k: row[k] for k in ("boq_lines", "pqc_clauses", "scc_clauses",
                                          "spec_clauses")
                      if isinstance(row.get(k), int) and row[k] >= 0}
            if row.get("error") or not counts:
                warnings.append("the KG could not count what this tender holds")
        except (Unavailable, Rejected) as exc:
            warnings.append(f"the KG could not count what this tender holds ({exc})")
        return {**self._head(path, True, warnings), "cite": _cite(data), "fields": fields,
                "awarded_to": sorted(set(_strings(data.get("awarded_to")))), "counts": counts}

    def tender_record(self, case_key: str) -> dict:
        path = "/api/kg/case"
        data = self._get(path, {"case_key": case_key, "clause_limit": RECORD_CLAUSES,
                                "item_limit": RECORD_ITEMS}, not_found_ok=True)
        empty = {"cite": None, "fields": {}, "boq": [],
                 "clauses": {"pqc": [], "scc": [], "tech_spec": []}, "sor_references": [],
                 "awarded_to": [], "complete": False}
        if not data or data.get("error"):
            return {**self._head(path, False), **empty}
        fields = {k: str(data[k])[:300] for k in RECORD_FIELDS if data.get(k) not in (None, "")}
        unread = 0
        boq = []
        for row in (data.get("boq") or [])[:RECORD_ITEMS]:
            if not row.get("item"):
                continue
            line = {"item": str(row["item"])[:300]}
            for key in ("material_code", "uom", "price_level"):
                line[key] = str(row[key])[:300] if row.get(key) not in (None, "") else None
            for key in ("qty", "unit_price", "estimated_rate", "total_price"):
                line[key], bad = _decimal(row.get(key))
                unread += bad
            boq.append(line)
        clauses = {k: [{"text": str(c.get("text") or "")[:20000],
                        "summary": str(c.get("summary") or "")[:300]}
                       for c in (data.get(k) or [])[:RECORD_CLAUSES] if c.get("text")]
                   for k in ("pqc", "scc", "tech_spec")}
        # a list as long as the limit asked for may have been cut there
        complete = len(data.get("boq") or []) < RECORD_ITEMS and all(
            len(data.get(k) or []) < RECORD_CLAUSES for k in clauses)
        warnings = []
        if not complete:
            warnings.append("the tender holds more than the KG returns in one record: the copy "
                            "is not the whole tender")
        if unread:
            warnings.append(f"{unread} recorded amount(s) could not be read as numbers and "
                            "are left empty")
        return {**self._head(path, True, warnings), "cite": _cite(data), "fields": fields,
                "boq": boq, "clauses": clauses,
                "sor_references": _strings(data.get("sor_references")),
                "awarded_to": sorted(set(_strings(data.get("awarded_to")))),
                "complete": complete}

    def suppliers(self, requirement: str, limit: int = 20) -> dict:
        listed = self._get("/api/kg/tenders", {"query": requirement, "limit": limit})
        keys = [t["case_key"] for t in (listed.get("tenders") or [])[:limit] if t.get("case_key")]
        self._retrieval(listed, not keys, "/api/kg/tenders")
        path = "/api/kg/case_summary"
        if not keys:
            return {**self._head(path, False), "suppliers": [], "tenders_checked": 0}
        data = self._get(path, {"case_keys": ",".join(keys)})
        cases = data.get("cases") or []
        failed = [c.get("case_key") for c in cases if c.get("error")]
        warnings = ([f"awardees could not be read for {len(failed)} tender(s)"] if failed else [])
        by_name: dict[str, set[str]] = {}
        for c in cases:
            for name in _strings(c.get("awarded_to")):
                by_name.setdefault(name.strip(), set()).add(str(c.get("case_key"))[:120])
        out = sorted(({"name": n[:300], "awards": len(t), "tenders": sorted(t)[:50]}
                      for n, t in by_name.items() if n),
                     key=lambda s: (-s["awards"], s["name"].lower()))[:100]
        if cases and len(failed) == len(cases):
            raise Unavailable("the KG could not read the awardees of any matching tender")
        return {**self._head(path, bool(out), warnings), "suppliers": out,
                "tenders_checked": len(cases)}
