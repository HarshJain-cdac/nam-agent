"""The cart node: the need's requirements as the agent reads them first,
every turn.

One short block, rebuilt from the case (case.py) whenever it is read, so it
is always the case's own state and never the model's account of it:

  required   every item the need names, in order, with where it stands:
             waiting for the category, not searched, waiting for the choice,
             now, waiting, in the cart, PR ready for SAP, not taken forward
  types      the types SAP holds for each item (narrow.types), as found when
             it was searched
  details    each answer and detail the officer gave for the item
  in cart    the one material code picked (nam_pick), with its short text,
             quantity, unit and PUR case
  now, next  the item being worked and the one after it

The model writes nothing here. An item reaches the cart only through
nam_pick (one code from the records still in), a detail only through
nam_answer, and an undo takes both back, because the node is read from the
case's current version.
"""

from __future__ import annotations

from nam_agent.nam import case as nam_case
from nam_agent.nam import guard

DONE = ("ready_for_sap", "sent")


def _status(data: dict, it: dict, now: dict | None, decided: bool) -> str:
    if (it.get("pr") or {}).get("status") in DONE:
        return "PR ready for SAP"
    if it.get("picked"):
        return "in the cart"
    if decided and not it.get("selected"):
        return "not taken forward"
    if data.get("asking_category") or not data.get("category"):
        return "waiting for the category"
    if not it.get("searched"):
        return "not searched"
    offer = [i for i in data.get("items") or [] if i.get("candidates")]
    if not decided and len(offer) > 1:
        return "waiting for the choice of items"
    if now is not None and it is now:
        return "now"
    return "waiting"


def _types(it: dict) -> list[tuple[str, int]]:
    if it.get("types"):
        return [tuple(t) for t in it["types"]]
    from nam_agent.nam import narrow

    return narrow.types(it.get("candidates") or [], it.get("query") or it["name"])[:12]


def _details(it: dict) -> list[str]:
    out = [f"{a['question']}: {a['answer']}" for a in it.get("answers") or []]
    out += [f"{k} = {v['value']}" + (" (NRL default)" if v.get("default") else "")
            for k, v in (it.get("details") or {}).items()]
    return out


def node_data(data: dict) -> dict:
    """The node as data, for the case JSON and the tests."""
    items = data.get("items") or []
    decided = any("selected" in it for it in items)
    now = nam_case.current_item(data) if data.get("category") and items else None
    if now is not None and not now.get("searched"):
        now = None
    rows = []
    for it in items:
        p = it.get("picked") or {}
        rows.append({
            "item": it["name"],
            "status": _status(data, it, now, decided),
            "types": [{"type": t, "count": n} for t, n in _types(it)],
            "found": len(it.get("candidates") or []),
            "still_in": len(nam_case.remaining(it)) if it.get("searched") else None,
            "details": _details(it),
            "cart": ({"code": p.get("code") or p.get("key"), "short_text": p.get("description"),
                      "quantity": p.get("quantity"), "unit": p.get("unit"),
                      "pur": p.get("pur")} if p else None),
        })
    waiting = [r["item"] for r in rows if r["status"] == "waiting"]
    guess = (data.get("asking_category") or {}).get("guess") or ""
    return {"case_id": data["case_id"], "need": data.get("need") or "",
            "category": data.get("category") or "", "category_guess": guess,
            "review": nam_case.review(data), "items": rows,
            "now": now["name"] if now is not None else None,
            "next": waiting[0] if waiting else None}


def node(data: dict) -> str:
    """The node as the agent reads it, at the top of its instructions."""
    d = node_data(data)
    out = [f"CART for {d['case_id']} (kept by the harness from the case; read it, never "
           f"restate it from memory). Need: '{guard.sanitise(d['need'], 200)}'"
           + (f"; {d['category']}" if d["category"] else
              f"; looks like {d['category_guess']}, waiting for the officer to confirm"
              if d["category_guess"] else "; category not given yet") + "."]
    out.append("Required: " + "; ".join(f"{n}. {r['item']} [{r['status']}]"
                                         for n, r in enumerate(d["items"], 1)) + ".")
    for r in d["items"]:
        line = f"- {r['item']}:"
        if r["cart"]:
            c = r["cart"]
            line += (f" IN CART {c['code']} {guard.sanitise(c['short_text'] or '', 80)}, "
                     f"quantity {c['quantity'] or '?'} {c['unit'] or ''}, PUR {c['pur'] or '-'}.")
        elif r["status"] not in ("not searched", "waiting for the category"):
            line += f" SAP holds {r['found']} record(s)"
            if r["still_in"] is not None and r["still_in"] != r["found"]:
                line += f", {r['still_in']} still in"
            line += "."
        if r["types"]:
            line += " Types in SAP: " + ", ".join(f"{t['type']} {t['count']}"
                                                  for t in r["types"][:8]) + "."
        if r["details"]:
            line += " Details: " + "; ".join(guard.sanitise(x, 120) for x in r["details"]) + "."
        out.append(line)
    out.append(f"Now: {d['now'] or '-'}. Next: {d['next'] or '-'}.")
    if data.get("suggested"):
        out.append("Suggested (bought with these before, waiting for the officer): "
                   + "; ".join(f"{s['name']} ({guard.sanitise(s.get('suggested') or '', 100)})"
                               for s in data["suggested"]) + ".")
    review = {"due": "every item is in: ask whether that is everything",
              "asked": "asked whether that is everything, waiting for the officer",
              "adding": "the officer is adding more items to this cart",
              "done": "the officer said that is everything"}.get(d["review"])
    if review:
        out.append(f"Cart review: {review}.")
    return "\n".join(out)
