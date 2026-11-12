"""What the officer sees beside the reply: cards built from the case, never
from the model's words.

Three groups (the event's "group"; its "kind" is the event's own,
nam_cards), each card a dict the page draws the same way:

  title, badge   what it is, and a badge on the right (a match, a PR's state)
  status, year   PR + PO, PR ongoing, or PR ongoing with its PO cancelled
                 (narrow.status_of), and the year of its latest line
  subtitle       one line under the title (the long text)
  fields         [label, value, state]: the facts as a label/value list; a
                 state of "missing" marks a field still to give
  lines          a short list under the fields (the materials a PUR holds)
  note           one line at the foot (what a PR still needs)
  action         a button that sends the officer's choice

The groups: records (an item's records still in), purs (PUR cases), pr (one
cart item's PR). Cards come the latest year first, then by status
(narrow.by_year_then_status). The tools send them with
chatstate.emit("nam_cards", ...), and the turn keeps them, so a reopened chat
shows them again.
"""

from __future__ import annotations

from nam_agent.nam import guard, narrow


def _last_order(c: dict) -> dict:
    return next((ln for ln in c.get("lines") or [] if ln.get("kind") in ("PO", "SERVICE")
                 and ln.get("rate_inr")), None) or ((c.get("lines") or [{}])[0])


def _rs(v) -> str:
    try:
        return f"INR {float(v):,.2f}"
    except (TypeError, ValueError):
        return ""


def _fields(*rows) -> list[list[str]]:
    """[label, value, state] rows, the empty ones left out."""
    return [[label, str(value), state] for label, value, state in rows if value not in (None, "")]


def _held(c: dict, answers: list[dict]) -> tuple[int, int]:
    """(how many of the officer's answers the record carries, how many)."""
    said = [a["answer"] for a in answers or []]
    got = sum(1 for a in said if narrow.answer(None, [c], a) is not None)
    return got, len(said)


def records(item: str, cands: list[dict], answers: list[dict] | None = None) -> dict:
    """The records still in for an item, as the PRs that hold them: one card
    per PR (its tender, PO, status and year), the records inside it with
    that PR's own line (quantity, rate, date). An item is finished on one PR:
    the officer chooses a record of a PR by what it says, never by a code.

    The group carries "choices": each card choice's words, sent as the
    officer's message, and the record and PR they stand for, which the
    harness keeps with the case to read the click exactly."""
    by: dict[str, list[tuple[dict, dict]]] = {}
    for c in cands:
        prs = narrow.prs_of(c)
        if not prs:
            st = narrow.status_of(c.get("lines") or [])
            prs = [{"pr_no": "", "lines": c.get("lines") or [], **st, "po": [], "pur": "",
                    "job": "", "last": _last_order(c)}]
        for p in prs:
            by.setdefault(p["pr_no"], []).append((c, p))
    rows, choices = [], {}
    for pr_no, pairs in by.items():
        p0 = pairs[0][1]
        items = []
        for c, p in pairs:
            last = p["last"]
            got, of = _held(c, answers or [])
            rate = _rs(last.get("rate_inr"))
            qty = f"{last.get('quantity')} {last.get('unit') or ''}".strip() \
                if last.get("quantity") not in (None, "") else ""
            detail = " · ".join(x for x in (
                f"Last rate {rate} per {last.get('unit') or 'unit'}" if rate else "",
                f"quantity {qty}" if qty else "",
                f"{last.get('kind') or ''} {narrow.doc_no(last.get('po_no') or last.get('doc_no'))}"
                .strip(), last.get("date") or "", p["label"]) if x)
            send = (f"Use \"{guard.sanitise(c.get('description') or '', 80)}\""
                    + (f" ({c['code']})" if c.get("code") else "")
                    + (f" from PR {narrow.doc_no(pr_no)}" if pr_no else ""))
            choices[send] = {"key": c["key"], "pr": pr_no}
            items.append({
                "full": bool(of) and got == of, "_rank": (-(got / of if of else 1),
                                                         str(last.get("date") or "")),
                "key": c["key"], "code": c.get("code") or narrow.unit(c),
                "title": guard.sanitise(c.get("description") or "", 120),
                "match": (f"{round(100 * got / of)}% · {got} of {of} details" if of else ""),
                "detail": detail, "send": send})
        items.sort(key=lambda x: x["_rank"][1], reverse=True)
        items.sort(key=lambda x: x["_rank"][0])
        for x in items:
            x.pop("_rank")
        rows.append({
            "title": f"PR {narrow.doc_no(pr_no)}" if pr_no else "No PR in SAP", "badge": "",
            "subtitle": guard.sanitise(p0["job"], 160),
            "status": p0["label"], "rank": p0["rank"], "year": p0["year"], "latest": p0["latest"],
            "chips": [{"text": f"{len(items)} matching item{'s' if len(items) > 1 else ''}",
                       "strong": True}]
                     + [{"text": f"PO {narrow.doc_no(x)}"} for x in p0["po"][:2]]
                     + ([{"text": p0["pur"]}] if p0["pur"] else []),
            "items": items,
            "foot": f"PR date {p0['latest']}" if p0["latest"] else "",
            "action": {"label": "Continue with this", "pick": True,
                       "other": f"Show me something else for {item}"}})
    return {"group": "records", "title": f"Past PRs that match '{item}'", "by_year": True,
            "cards": narrow.by_year_then_status(rows), "choices": choices}


def purs(title: str, ranked: list[dict], coverage: bool = False) -> dict:
    """PUR cases as cards: the coverage ranking (with its match) or one
    item's past tenders, each with what it holds."""
    rows = []
    for t in ranked:
        held = ([{"code": ", ".join(codes[:2]), "title": n} for n, codes in t["codes"].items()]
                if coverage else [{"code": r, "title": ""} for r in list(t.get("records") or [])[:6]])
        rows.append({
            "title": t["pur"], "badge": f"{t['match']}% match" if coverage else "",
            "subtitle": t.get("job_name") or "",
            "status": t["label"], "rank": t["rank"], "year": t["year"], "latest": t["latest"],
            "chips": ([{"text": t["covers"], "strong": True}] if coverage else
                      [{"text": f"{len(t.get('records') or [])} matching item(s)", "strong": True}])
                     + [{"text": f"PR {narrow.doc_no(p)}"} for p in t["pr"][:2]]
                     + [{"text": f"PO {narrow.doc_no(p)}"} for p in t["po"][:2]],
            "items": held,
            "foot": f"Last line {t['latest']}" if t["latest"] else ""})
    return {"group": "purs", "title": title, "by_year": True, "cards": rows}


def pr(item: str, p: dict) -> dict:
    """One cart item's PR as a card: its fields, the missing ones marked."""
    from nam_agent.nam import pr as prmod

    h = p.get("header") or {}
    ln = (p.get("lines") or [{}])[0]
    price = p.get("price") or {}
    done = p.get("status") in prmod.DONE
    officer = [(label.replace(" (YYYY-MM-DD)", ""), h.get(k) or "missing",
                "" if h.get(k) else "missing") for k, label in prmod.OFFICER.items()]
    missing = len(p.get("missing") or [])
    if p.get("items"):
        # one PR for several items: its fields, and one line per item
        return {"group": "pr", "title": "Common PR", "cards": [{
            "title": f"One PR for {len(p['lines'])} items",
            "badge": "ready for SAP" if done else "draft", "status": "", "year": "",
            "subtitle": ", ".join(p["items"]),
            "fields": _fields(*officer,
                              ("Reference", ", ".join(p.get("reference_tenders") or []), "")),
            "items": [{"code": x.get("material_code") or "",
                       "title": f"{x.get('item', '')}: {x.get('short_text') or ''}",
                       "detail": " · ".join(y for y in (
                           f"quantity {x.get('quantity') or '?'} {x.get('unit') or ''}".strip(),
                           _rs(x.get("valuation_price")) or "no price") if y),
                       "full": True} for x in p["lines"]],
            "note": ("Ready for SAP." if done else
                     f"{missing} thing(s) still to give before it can be confirmed." if missing
                     else "Every mandatory field is filled: it can be confirmed.")}]}
    return {"group": "pr", "title": f"PR for '{item}'", "cards": [{
        "title": ln.get("short_text") or item,
        "badge": "ready for SAP" if done else "draft",
        "status": price.get("result") or "", "year": "",
        "fields": _fields(("Code", ln.get("material_code"), "code"),
                          ("Based on PR", narrow.doc_no((p.get("based_on") or {}).get("pr")), ""),
                          ("Its PO", ", ".join(narrow.doc_no(x) for x in
                                               (p.get("based_on") or {}).get("po") or []), ""),
                          ("Quantity", f"{ln.get('quantity') or '?'} {ln.get('unit') or ''}".strip(),
                           "" if ln.get("quantity") else "missing"),
                          ("Valuation price", _rs(ln.get("valuation_price")) or "missing",
                           "" if ln.get("valuation_price") else "missing"),
                          *officer,
                          ("Reference", ", ".join(p.get("reference_tenders") or []), "")),
        "note": ("Ready for SAP." if done else
                 f"{missing} thing(s) still to give before it can be confirmed." if missing
                 else "Every mandatory field is filled: it can be confirmed.")}]}


def _orders(p: dict) -> str:
    """The purchase orders of the PR an item follows, from its SAP lines."""
    pos = sorted({str(ln.get("po_no")) for ln in p.get("lines") or [] if ln.get("po_no")})
    return ", ".join(narrow.doc_no(x) for x in pos[:3])


def added(item: str, p: dict, n: int, of: int) -> dict:
    """What went into the cart, so the officer sees exactly what was chosen."""
    last = next((ln for ln in p.get("lines") or [] if ln.get("rate_inr")), {})
    return {"group": "cart", "title": "Added to the cart", "cards": [{
        "title": p.get("description") or item, "badge": f"{n} of {of} in the cart",
        "status": "", "year": "",
        "fields": _fields(("Item", item, ""), ("Code", p.get("code"), "code"),
                          ("PR", narrow.doc_no(p.get("pr_no")), ""),
                          ("PO", _orders(p), ""),
                          ("PUR", p.get("pur"), ""),
                          ("Quantity", (f"{p.get('quantity'):g} {p.get('unit') or ''}".strip()
                                        if p.get("quantity") else ""), ""),
                          ("Last rate", _rs(last.get("rate_inr")), "")),
        "note": "Wrong one? Use Undo last step."}]}


def cart(data: dict) -> dict:
    """Every item in the cart, for the officer to check before the PURs and the
    PRs: one card per item, from the case (never the model's account)."""
    from nam_agent.nam import case

    chosen = case.chosen(data)
    rows = []
    for n, it in enumerate([i for i in chosen if i.get("picked")], 1):
        p = it["picked"]
        last = next((ln for ln in p.get("lines") or [] if ln.get("rate_inr")), {})
        rows.append({
            "title": p.get("description") or it["name"], "badge": f"{n} of {len(chosen)}",
            "status": "", "year": "",
            "fields": _fields(("Item", it["name"], ""), ("Code", p.get("code"), "code"),
                              ("PR", narrow.doc_no(p.get("pr_no")), ""),
                              ("PO", _orders(p), ""),
                              ("PUR", p.get("pur"), ""),
                              ("Quantity", (f"{p.get('quantity'):g} {p.get('unit') or ''}".strip()
                                            if p.get("quantity") else ""), ""),
                              ("Last rate", _rs(last.get("rate_inr")), ""))})
    return {"group": "cart", "title": "Your cart", "cards": rows}
