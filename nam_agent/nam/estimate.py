"""Stage 2, Estimate creation (manual steps 2a to 2f), in code.

  2a last price     per line, the latest PO (or service order) of the picked
                    record from SAP; else the past tender's price from the
                    knowledge graph; else none (a new item, priced from its
                    PR valuation price when SAP has one). Each price keeps its
                    source: document, date, PUR case.
  2b up to today    7% for every year from the last price's year to the
                    evaluation date's: 100.00 in 2024 is 114.49 in 2026.
  2c the sheet      code, description, unit, quantity, rate, amount, source.
                    The officer may change any quantity or rate.
  2d each rate      YRS (years since the last price) and CAGR against it:
                      Cleared        rose by less than 10%, or 10% a year or less
                      AHR            rose by more than 10% a year (flagged)
                      Manual review  rose by 10% or more within a year (flagged)
                      New item       no past price
                    A flag is a warning: NAM never changes a price by itself.
  2e decisions      every flagged line needs accept or revise
  2f lock           refused until every flagged line is decided and every
                    quantity given; the locked rate is the PR's valuation price.
"""

from __future__ import annotations

from datetime import date

ESCALATION = 0.07          # a year (2b)
THRESHOLD = 0.10           # 10%: the rise, and the CAGR, a rate may show (2d)
CLEARED, AHR, MANUAL, NEW = "Cleared", "AHR", "Manual review", "New item"
FLAGS = (AHR, MANUAL)


def _day(text) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def _num(value) -> float | None:
    try:
        v = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def last_price(record: dict) -> dict | None:
    """2a: {rate, date, source} of the record's last price, or None."""
    lines = record.get("lines") or []
    for kinds, what in ((("PO", "SERVICE"), "SAP PO"), (("TENDER",), "past tender")):
        for ln in lines:                         # newest first
            rate = _num(ln.get("rate_inr"))
            if ln.get("kind") in kinds and rate:
                doc = ln.get("po_no") or ln.get("doc_no") or "-"
                return {"rate": rate, "date": ln.get("date"),
                        "source": f"{what} {doc}" + (f" dated {ln['date']}" if ln.get("date")
                                                       else "")
                        + (f", {ln['pur_case_id']}" if ln.get("pur_case_id") else "")}
    return None


def pr_valuation(record: dict) -> dict | None:
    """The latest PR line's valuation price, for a new item."""
    for ln in record.get("lines") or []:
        rate = _num(ln.get("rate_inr"))
        if ln.get("kind") == "PR" and rate:
            return {"rate": rate, "date": ln.get("date"),
                    "source": f"PR valuation price, PR {ln.get('pr_no') or ln.get('doc_no')}"}
    return None


def escalate(rate: float, since, until: date) -> float:
    """2b: 7% for every year from `since`'s year to `until`'s."""
    start = _day(since)
    years = max(0, until.year - start.year) if start else 0
    return round(rate * (1 + ESCALATION) ** years, 2)


def judge(line: dict, until: date) -> dict:
    """2d: YRS, CAGR and the result for a line's rate."""
    last, rate = line.get("last_rate"), line.get("rate") or 0
    if not last:
        return {"yrs": None, "cagr": None, "result": NEW, "flag": False}
    start = _day(line.get("last_date"))
    yrs = round((until - start).days / 365.25, 2) if start else None
    rise = rate / last - 1
    cagr = (rate / last) ** (1 / yrs) - 1 if yrs and yrs >= 1 else None
    if rise < THRESHOLD:
        result = CLEARED
    elif yrs is None or yrs < 1:
        result = MANUAL
    elif cagr is not None and cagr <= THRESHOLD:
        result = CLEARED
    else:
        result = AHR
    return {"yrs": yrs, "cagr": round(cagr * 100, 1) if cagr is not None else None,
            "result": result, "flag": result in FLAGS}


def _refresh(line: dict, until: date) -> dict:
    line["amount"] = round((line.get("quantity") or 0) * (line.get("rate") or 0), 2)
    line.update(judge(line, until))
    return line


def build(items: list[dict], until: date) -> dict:
    """2a-2c: one line per picked item."""
    lines = []
    for n, it in enumerate(items, 1):
        rec = it["picked"]
        last = last_price(rec)
        line = {"line": n, "item": it["name"], "code": rec.get("code") or "",
                "description": rec.get("description") or "", "unit": rec.get("unit") or "",
                "quantity": rec.get("quantity") or 0}
        if last:
            line.update(last_rate=last["rate"], last_date=last["date"], source=last["source"],
                        rate=escalate(last["rate"], last["date"], until))
        else:
            pv = pr_valuation(rec)
            line.update(last_rate=None, last_date=None,
                        source=pv["source"] if pv else "no past price: give the rate",
                        rate=pv["rate"] if pv else 0)
        lines.append(_refresh(line, until))
    return {"evaluation_date": until.isoformat(), "lines": lines, "locked": False,
            "total": round(sum(ln["amount"] for ln in lines), 2)}


def set_line(est: dict, n: int, quantity=None, rate=None) -> dict:
    """2c: the officer's quantity or rate; a changed rate is judged again and
    its decision cleared."""
    line = _line(est, n)
    if quantity is not None:
        line["quantity"] = float(quantity)
    if rate is not None:
        line["rate"] = float(rate)
        line.pop("decision", None)
    _refresh(line, date.fromisoformat(est["evaluation_date"]))
    est["total"] = round(sum(ln["amount"] for ln in est["lines"]), 2)
    return line


def decide(est: dict, n: int, decision: str, note: str = "") -> dict:
    """2e: accept or revise a flagged line (revise: set_line with the new rate)."""
    line = _line(est, n)
    if not line.get("flag"):
        raise ValueError(f"line {n} is {line.get('result')}: only a flagged line needs a decision")
    if decision not in ("accept", "revise"):
        raise ValueError("decision is accept or revise")
    line["decision"] = {"decision": decision, "note": note[:200], "rate": line["rate"]}
    return line


def blockers(est: dict) -> list[str]:
    """What stops the lock (2e, 2f)."""
    out = []
    for ln in est.get("lines") or []:
        if not ln.get("quantity"):
            out.append(f"line {ln['line']} has no quantity")
        if not ln.get("rate"):
            out.append(f"line {ln['line']} has no rate")
        if ln.get("flag") and (ln.get("decision") or {}).get("decision") != "accept":
            out.append(f"line {ln['line']} is {ln['result']}: accept it, or revise its rate")
    return out


def lock(est: dict) -> dict:
    """2f: the basis of each line, the valuation price, locked."""
    for ln in est["lines"]:
        cagr = f", CAGR {ln['cagr']}%" if ln.get("cagr") is not None else ""
        ln["basis"] = (f"{ln['source']}; last rate {ln['last_rate']} escalated at 7% a year to "
                       f"{est['evaluation_date']}{cagr}; {ln['result']}" if ln.get("last_rate")
                       else f"{ln['source']}; {ln['result']}")
        ln["valuation_price"] = ln["rate"]
    est["locked"] = True
    return est


def _line(est: dict, n: int) -> dict:
    for ln in est.get("lines") or []:
        if ln["line"] == int(n):
            return ln
    raise ValueError(f"no line {n}; the lines are 1 to {len(est.get('lines') or [])}")


def table(est: dict) -> str:
    rows = ["| # | Code | Description | Unit | Qty | Last rate (date) | Rate | Amount | YRS "
            "| CAGR | Result | Source |", "|---|---|---|---|---:|---|---:|---:|---:|---:|---|---|"]
    for ln in est["lines"]:
        last = (f"{ln['last_rate']:,.2f} ({ln.get('last_date') or '-'})" if ln.get("last_rate")
                else "-")
        result = ln["result"] + (f" ({ln['decision']['decision']})" if ln.get("decision") else "")
        rows.append(f"| {ln['line']} | {ln['code'] or '-'} | {ln['description'][:50]} | "
                    f"{ln['unit'] or '-'} | {ln['quantity']} | {last} | {ln['rate']:,.2f} | "
                    f"{ln['amount']:,.2f} | {ln['yrs'] if ln['yrs'] is not None else '-'} | "
                    f"{str(ln['cagr']) + '%' if ln['cagr'] is not None else '-'} | {result} | "
                    f"{ln['source']} |")
    rows.append(f"\nTotal excl. GST: INR {est['total']:,.2f}. Evaluation date "
                f"{est['evaluation_date']}.")
    return "\n".join(rows)
