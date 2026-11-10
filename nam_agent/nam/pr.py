"""Stage 3, PR Creation (manual steps 3a and 3b), in code: one PR for each
item in the cart.

  3a fill     the item's PR from the earlier steps, each field with where it
              came from: the material code, short text and specification,
              the quantity and unit (stage 1), the valuation price (the
              cart's locked estimate when there is one, else the last price
              brought up to today at 7% a year, with its result), the
              reference tender (the picked record's PUR case). The officer
              gives the delivery date, plant, storage location and
              justification; what they gave for an earlier item's PR is
              offered again, and said so.
  common      when the officer wants one PR for several cart items, their PRs
              are merged (combine): one line per item, one set of the
              officer's fields (what any item's PR already had), every
              reference tender. Each item's own PR is then "merged" and the
              common PR is the one confirmed.
  3b confirm  refused until every mandatory field is filled and a flagged
              price is accepted or revised. SAP's PR interface is not
              connected yet: a confirmed PR is "ready for SAP", written out as
              the bulk-PR-upload sheet, and has no PR number until SAP returns
              one. NAM never makes one up.
"""

from __future__ import annotations

import re
from datetime import date

from nam_agent.nam import estimate

OFFICER = {
    "delivery_date": "Date needed (YYYY-MM-DD)",
    "plant": "Plant",
    "storage_location": "Storage location",
    "justification": "Justification",
}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DONE = ("ready_for_sap", "sent")


def _price(case: dict, it: dict, until: date) -> dict:
    """The item's valuation price: the locked estimate's line, else its last
    price brought up to today (2a, 2b) with the result (2d)."""
    est = case.get("estimate") or {}
    if est.get("locked"):
        for ln in est.get("lines") or []:
            if ln.get("item") == it["name"]:
                return {"rate": ln.get("valuation_price", ln["rate"]), "result": ln["result"],
                        "flag": False, "source": f"the locked estimate, line {ln['line']}"}
    ln = estimate.build([{"name": it["name"], "picked": it["picked"]}], until)["lines"][0]
    return {"rate": ln["rate"], "result": ln["result"], "flag": ln["flag"],
            "source": ln["source"], "last_rate": ln.get("last_rate"),
            "last_date": ln.get("last_date")}


def fill(case: dict, it: dict, until: date | None = None) -> dict:
    """3a: the item's PR, keeping what the officer already gave for it, and
    offering what they gave for another item's PR."""
    old = it.get("pr") or {}
    header = {k: (old.get("header") or {}).get(k, "") for k in OFFICER}
    offered = {}
    for other in case.get("items") or []:
        if other is it or not other.get("pr"):
            continue
        for k in OFFICER:
            v = (other["pr"].get("header") or {}).get(k)
            if v and not header[k]:
                header[k], offered[k] = v, other["name"]
    p = it["picked"]
    price = old.get("price") if old.get("price", {}).get("decided") else _price(
        case, it, until or date.today())
    spec = [p.get("description") or ""] + [
        f"{k}: {v['value']}" + (" (NRL default)" if v.get("default") else "")
        for k, v in (it.get("details") or {}).items()]
    line = {"line": 1, "material_code": p.get("code") or "",
            "short_text": (p.get("description") or "")[:40], "specification": "; ".join(spec),
            "quantity": p.get("quantity"), "unit": p.get("unit") or "",
            "valuation_price": price["rate"],
            "from": {"material_code": "the material picked in stage 1",
                     "specification": "stage 1", "quantity": "stage 1",
                     "valuation_price": price["source"]}}
    pr = {"item": it["name"], "header": header, "offered": offered,
          "reference_tenders": [p["pur"]] if p.get("pur") else [], "lines": [line],
          "price": price, "status": old.get("status") if old.get("status") in DONE else "draft",
          "pr_no": old.get("pr_no")}
    pr["missing"] = missing(pr)
    return pr


def combine(case: dict, names: list[str]) -> dict:
    """One PR for several cart items: their lines, one set of the officer's
    fields, every reference tender. ValueError when an item is not in the
    cart, has no drafted PR, or still has a flagged price to decide."""
    items = []
    for n in names:
        it = next((i for i in case.get("items") or [] if i["name"].lower() == n.lower()), None)
        if it is None or not it.get("picked"):
            raise ValueError(f"'{n}' is not in the cart")
        if not it.get("pr"):
            raise ValueError(f"'{n}' has no PR drafted yet")
        if (it["pr"].get("price") or {}).get("flag"):
            raise ValueError(f"the price of '{n}' is flagged {it['pr']['price']['result']}: the "
                             "officer accepts it or gives the rate first")
        items.append(it)
    if len(items) < 2:
        raise ValueError("a common PR needs at least two items")
    old = case.get("common_pr") or {}
    header = {k: (old.get("header") or {}).get(k, "") for k in OFFICER}
    offered = {}
    for it in items:
        for k in OFFICER:
            v = (it["pr"].get("header") or {}).get(k)
            if v and not header[k]:
                header[k], offered[k] = v, it["name"]
    lines = []
    for n, it in enumerate(items, 1):
        ln = dict(it["pr"]["lines"][0], line=n, item=it["name"])
        lines.append(ln)
    refs = sorted({r for it in items for r in it["pr"].get("reference_tenders") or []})
    pr = {"item": "common PR of " + ", ".join(i["name"] for i in items),
          "items": [i["name"] for i in items], "header": header, "offered": offered,
          "reference_tenders": refs, "lines": lines, "price": {},
          "status": old.get("status") if old.get("status") in DONE else "draft",
          "pr_no": old.get("pr_no")}
    pr["missing"] = missing(pr)
    return pr


def set_fields(pr: dict, **values) -> dict:
    for k, v in values.items():
        if k not in OFFICER:
            raise ValueError(f"'{k}' is not a field the officer gives; they are "
                             + ", ".join(OFFICER))
        v = " ".join(str(v or "").split())
        if k == "delivery_date" and v and not _DATE.match(v):
            raise ValueError("delivery_date is YYYY-MM-DD")
        pr["header"][k] = v
        pr.get("offered", {}).pop(k, None)
    pr["missing"] = missing(pr)
    return pr


def set_price(pr: dict, rate=None, accept: bool = False) -> dict:
    """2e for one PR: the officer revises the valuation price, or accepts a
    flagged one."""
    price = pr["price"]
    if rate is not None:
        price.update(rate=float(rate), decided="revised", flag=False,
                     source=f"the officer's rate (was {price['source']})")
        pr["lines"][0]["valuation_price"] = float(rate)
        pr["lines"][0]["from"]["valuation_price"] = price["source"]
    elif accept:
        if not price.get("flag"):
            raise ValueError(f"the price is {price.get('result')}: there is no flag to accept")
        price.update(decided="accepted", flag=False)
    pr["missing"] = missing(pr)
    return pr


def missing(pr: dict) -> list[str]:
    out = [label for k, label in OFFICER.items() if not pr["header"].get(k)]
    for ln in pr.get("lines") or []:
        of = f" of '{ln['item']}'" if ln.get("item") else ""
        if not ln["short_text"] or not ln["quantity"] or not ln["unit"]:
            out.append(("the quantity and unit" if ln["short_text"] else "the short text") + of)
        if not ln["valuation_price"]:
            out.append(f"the valuation price{of} (no past price: the officer gives the rate)")
    if (pr.get("price") or {}).get("flag"):
        out.append(f"a decision on the price, flagged {pr['price']['result']}: accept it or "
                   "give the rate")
    return out


def table(pr: dict) -> str:
    h = pr["header"]
    rows = [f"PR for '{pr['item']}' ({pr['status']})"
            + (f", SAP PR {pr['pr_no']}" if pr.get("pr_no") else ""),
            "| Field | Value |", "|---|---|"]
    rows += [f"| {label} | {h.get(k) or '(missing)'}"
             + (f" (as for '{pr['offered'][k]}')" if k in pr.get("offered", {}) else "") + " |"
             for k, label in OFFICER.items()]
    rows.append(f"| Reference tender | {', '.join(pr['reference_tenders']) or '-'} |")
    p = pr.get("price") or {}
    rows.append(f"| Price basis | {p.get('source', '-')}; {p.get('result', '-')}"
                + (f", {p['decided']}" if p.get("decided") else "") + " |")
    rows += ["", "| # | Material code | Short text | Qty | Unit | Valuation price (INR) |",
             "|---|---|---|---:|---|---:|"]
    rows += [f"| {ln['line']} | {ln['material_code'] or '-'} | {ln['short_text']} | "
             f"{ln['quantity'] or '-'} | {ln['unit'] or '-'} | {ln['valuation_price'] or 0:,.2f} |"
             for ln in pr["lines"]]
    if pr.get("missing"):
        rows.append("\nMissing: " + "; ".join(pr["missing"]) + ".")
    return "\n".join(rows)


def sheet_rows(case: dict) -> list[list]:
    """Every confirmed PR of the case as bulk-upload rows: one per line, the
    PR's fields on each."""
    head = ["NAM case", "PR", "Item", "Line", "Material code", "Short text", "Specification",
            "Quantity", "Unit", "Valuation price", "Delivery date", "Plant",
            "Storage location", "Justification", "Reference tender"]
    rows, n = [head], 0
    common = case.get("common_pr") or {}
    if common.get("status") in DONE:
        n += 1
        h = common["header"]
        rows += [[case["case_id"], n, ln.get("item", ""), ln["line"], ln["material_code"],
                  ln["short_text"], ln["specification"], ln["quantity"], ln["unit"],
                  ln["valuation_price"], h["delivery_date"], h["plant"], h["storage_location"],
                  h["justification"], ", ".join(common["reference_tenders"])]
                 for ln in common["lines"]]
    for it in case.get("items") or []:
        pr = it.get("pr") or {}
        if pr.get("status") not in DONE:
            continue
        n += 1
        h = pr["header"]
        rows += [[case["case_id"], n, it["name"], ln["line"], ln["material_code"],
                  ln["short_text"], ln["specification"], ln["quantity"], ln["unit"],
                  ln["valuation_price"], h["delivery_date"], h["plant"], h["storage_location"],
                  h["justification"], ", ".join(pr["reference_tenders"])]
                 for ln in pr["lines"]]
    return rows
