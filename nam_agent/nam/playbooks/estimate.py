"""estimate.goods (F13, F16, F48), estimate.wage (F14, F16) and
estimate.works (F15, F16, F47): a rate for every line of the finalised
quantity, each with its basis and its source, for the officer to approve at
the estimate gate (S4).

No number here is made up. A rate is one of:

  a figure from a tool result of this run   cited by evidence id and path
                                            (guardrails.cite), checked again
                                            by guardrails._estimate
  a number the officer typed                {"officer": user}, in this
                                            request, or held by the stored
                                            estimate from an earlier one
  a policy percentage of other components   policy/guardrails.json

and a line with none of these has no rate: it is a New Item, and the
officer gives it. The KG holds past NRL tenders only: no NRL SOR or DSR
schedule, no SAP rate contract or PO, no wage notification. Those come from
the officer here until their connectors exist.

The officer's input is one answer, in the format of lines.py:
answer {question_id: "rates", value: "2: rate=450; all: insurance=esi"}.
It is kept in the estimate's `inputs`, so later runs need not repeat it.

Goods (R5-R7, R10, R38)
  The officer's budgetary quotation for a line is its rate (basis
  quotation, the last resort). Else the KG's rates for the item, the NRL
  estimate first (internal), then the awarded rate (po), then a bidder's
  quote (quotation); never the historical tender's own embedded rate as if
  it were current (R7: it is said to be historical). A quotation is compared
  with the awarded rate: VARN, CAGR, the R10 flag.
Works (R5, R8, R9, R35, R36, F47)
  The officer's schedule rate (NRL SOR, DSR, DAR, APWD SOR, SOR RR) with its
  item reference, a Similar rate with its match score, or a Derived rate
  from its components. Else the KG's NRL estimate (internal). A case within
  an empanelment must use NRL SOR on every line (F47).
Wage (R12-R14, R39)
  Per manday line: the basic wage the officer gives, + statutory 13.13%, +
  non-statutory 35-40.6% (the officer picks, by worker category), Sunday and
  holiday lines x2 then + 15% CP, + any allowance. Then the reimbursements
  as LS lines: PF/EDLI 13% of basic, ESI 4.75% or WCI 2.79%, PPE 5%, Tools
  and Tackles 5% of the wages, each rounded up to the nearest Rs 50,000.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from nam_agent.nam import guardrails, orchestrator, tools
from nam_agent.nam import need as need_store
from nam_agent.nam.errors import NamError
from nam_agent.nam.futures import ready
from nam_agent.nam.playbooks import Outcome, lines, need_classify

RATES = "rates"
LOOKUPS = 100                     # price_history calls in one run, at most
CENT = Decimal("0.01")
_LEVELS = (("estimate", "internal"), ("award", "po"), ("invoice", "po"), ("quote", "quotation"))
_SCHEDULES = ("nrl_sor", "dsr", "dsr_em", "dar", "apwd_sor", "sor_rr")
_MANDAY = {"mdy", "manday", "mandays", "man-day", "man-days", "md", "hr", "hour", "hours"}
_WAGE = {"rate", "basic", "non_statutory", "sunday", "allowance", "insurance"}
_FIELDS = {
    # decision=accept|revise: the officer's decision on a flagged line (PR flow 2(e))
    "goods": {"rate", "kg", "decision"},
    # a manday line within Works is priced as wages (R14), with the wage inputs
    "works": {"rate", "schedule", "ref", "basis", "score", "c", "kg"} | _WAGE,
    "wage": _WAGE,
}
# F13's estimate header, given with all: (the location defaults to the case's)
# eval_date: the evaluation date the last prices are brought up to (PR flow 2(b))
_HEADER = {"start", "end", "cost_centre", "location", "eval_date"}


def _d(value) -> Decimal:
    return Decimal(str(value))


def _num(d: Decimal) -> float:
    return float(d.quantize(CENT))


def _pct(base: Decimal, pct) -> Decimal:
    return (base * _d(pct) / 100).quantize(CENT)


# -- the officer's input -------------------------------------------------------------

def _inputs(stored: dict, rows: list[dict], text: str, kind: str, user: str = ""
            ) -> tuple[dict, dict]:
    """(per-item inputs, settings) after the officer's answer, each item's
    inputs with who typed them ("by"). Raises LineError."""
    per = {k: dict(v) for k, v in ((stored or {}).get("inputs") or {}).items()}
    settings = dict((stored or {}).get("settings") or {})
    if not text:
        return per, settings
    for e in lines.parse(text, _FIELDS[kind] | _HEADER):
        if e.target == "add" or e.remove:
            raise lines.LineError("lines are added or removed in quantity.finalise")
        if e.target == "all":
            settings.update({k: v.strip()[:120] for k, v in e.fields.items()})
            continue
        if e.index >= len(rows):
            raise lines.LineError(f"there is no line {e.target}: the case has {len(rows)}")
        if set(e.fields) & _HEADER:
            raise lines.LineError(f"{', '.join(sorted(_HEADER))} are given for the whole "
                                  "estimate: all: start=2026-11-01, end=2027-10-31")
        item = rows[e.index]["item"]
        decision = (e.fields.get("decision") or "").strip().lower()
        if decision and decision not in ("accept", "revise"):
            raise lines.LineError(f"line {e.target}: decision is accept or revise")
        # an item's numbers are cited as the officer's who typed them last
        mine = per.setdefault(item, {})
        if "rate" in e.fields and "decision" not in e.fields:
            mine.pop("decision", None)       # a new rate is a new decision to take
        mine.update({k: v.strip() for k, v in e.fields.items()}, by=user)
    return per, settings


def _officer(user: str, key: str, value) -> dict:
    return {"officer": user, "quote": f"{key}={value}"[:300]}


# -- the KG ----------------------------------------------------------------------------

PREFETCH_S = 90.0        # the most a run waits for the price records fetched in parallel
PREFETCH_WORKERS = 8


class _Cached:
    """The KG's GET with the price records already fetched: the audited,
    cited price_history calls below read them instead of waiting ~40s each
    on the KG one after another."""

    def __init__(self, base, found: dict):
        self.base, self.found = base, found

    def __call__(self, kg_url: str, path: str, params: dict):
        key = (path, str(params.get("requirement")), int(params.get("limit") or 0))
        if key in self.found:
            return self.found[key]
        return self.base(kg_url, path, params)


class _Prices:
    """price_history per item, at most LOOKUPS calls in one run."""

    def __init__(self, ctx):
        self.ctx, self.seen, self.capped = ctx, {}, False

    def prefetch(self, items: list[str]) -> None:
        """Fetch every item's price records in parallel, PREFETCH_S at most;
        one not back in time is fetched when its line asks, as before."""
        import dataclasses
        from concurrent.futures import ThreadPoolExecutor, wait

        base = getattr(self.ctx.fetch, "inner", self.ctx.fetch)
        keys = list(dict.fromkeys(i[:200] for i in items if i))[:LOOKUPS]
        if len(keys) < 2 or not self.ctx.kg_url:
            return

        def one(item: str):
            try:
                return base(self.ctx.kg_url, "/api/kg/prices", {"requirement": item, "limit": 5})
            except Exception:  # noqa: BLE001 - the line's own call reports it
                return None
        pool = ThreadPoolExecutor(max_workers=min(PREFETCH_WORKERS, len(keys)))
        futures = {k: pool.submit(one, k) for k in keys}
        wait(futures.values(), timeout=PREFETCH_S)
        pool.shutdown(wait=False, cancel_futures=True)
        found = {("/api/kg/prices", k, 5): ready(f) for k, f in futures.items()
                 if ready(f) is not None}
        if found:
            self.ctx = dataclasses.replace(self.ctx, fetch=_Cached(base, found))

    def rows(self, item: str) -> tuple[dict | None, list]:
        key = item[:200]
        if key not in self.seen:
            if len(self.seen) >= LOOKUPS:
                self.capped = True
                return None, []
            self.seen[key] = tools.call(self.ctx, "price_history",
                                        {"requirement": key, "limit": 5})
        result = self.seen[key]
        return result, result["rows"]


def _same_unit(a, b) -> bool:
    return (a or "").strip().lower().rstrip("s") == (b or "").strip().lower().rstrip("s")


def _kg_rate(prices: _Prices, row: dict, levels=_LEVELS) -> dict | None:
    """The KG rate for a line, in the hierarchy's order, cited."""
    result, found = prices.rows(row["item"])
    for level, basis in levels:
        for i, hist in enumerate(found):
            if not hist["matched"] or hist["price_level"] != level:
                continue
            if row.get("uom") and hist["uom"] and not _same_unit(row["uom"], hist["uom"]):
                continue
            stat = "unit_price" if hist["unit_price"] else "estimated_rate"
            if not hist[stat]:
                continue
            path = f"rows/{i}/{stat}/median"
            years = hist["years"]
            return {"rate": float(_d(hist[stat]["median"])), "basis": basis, "level": level,
                    "source": guardrails.cite(result, path), "years": years,
                    "n": hist[stat]["n"]}
    return None


def _award(prices: _Prices, row: dict) -> dict | None:
    return _kg_rate(prices, row, levels=(("award", "po"), ("invoice", "po")))


# -- one line per category ---------------------------------------------------------

def _goods_line(row, given, user, prices, warnings, i, eval_year: int | None = None) -> dict:
    line = _priced_goods(row, given, user, prices, warnings, i, eval_year or date.today().year)
    flag = (line.get("comparison") or {}).get("flag")
    decision = (given.get("decision") or "").lower()
    if decision and flag in guardrails.policy()["rate_comparison"]["decide"]:
        line["decision"] = {"value": decision, "by": given.get("by") or user,
                            "rate": line["rate"]}
    return line


def _escalated(line: dict, kg: dict, eval_year: int, warnings: list, i: int) -> dict:
    """The last price brought up to the evaluation date (PR flow 2(b)): the
    policy's percentage a year, compounded and rounded each year, from the
    latest year the price was paid. The rate check then compares the two."""
    pct = guardrails.policy()["goods_price"]["escalation_pct"]
    last = max(kg["years"]) if kg["years"] else None
    years = max(0, eval_year - last) if last else 0
    rate = guardrails.escalate(_d(kg["rate"]), pct, years)
    line.update(rate=_num(rate), basis=kg["basis"],
                source={"rule": "escalation", "pct": float(pct), "years": years,
                        "base": kg["source"], "base_value": kg["rate"],
                        "from_year": last, "to_year": eval_year},
                last_price={"rate": kg["rate"], "year": last, "level": kg["level"],
                            "n": kg["n"]})
    comp = {"quoted": _num(rate), "po_rate": kg["rate"],
            "years": float(years) if last else None, "flag": "new_item"}
    comp["flag"] = guardrails._r10(comp)[0]
    line["comparison"] = comp
    warnings.append(f"line {i + 1}: the median of {kg['n']} past NRL {kg['level']} rate(s) "
                    f"of {last or 'an unknown year'}, + {pct}% a year for {years} year(s)")
    return line


def _priced_goods(row, given, user, prices, warnings, i, eval_year: int) -> dict:
    line = _base(row)
    if given.get("rate"):
        rate = lines.number(given["rate"], f"line {i + 1} rate")
        line.update(rate=lines.plain(rate), basis="quotation",
                    source=_officer(user, "rate", given["rate"]))
        award = _award(prices, row) if given.get("kg") != "no" else None
        if award is not None:
            years = (eval_year - max(award["years"])) if award["years"] else None
            comp = {"quoted": float(rate), "po_rate": award["rate"],
                    "years": float(years) if years is not None else None, "flag": "new_item"}
            comp["flag"] = guardrails._r10(comp)[0]
            line["comparison"] = comp
            if years is not None:
                warnings.append(f"line {i + 1}: the PO date is known to the year only "
                                f"({max(award['years'])}): the years are approximate (R38)")
        else:
            line["comparison"] = {"quoted": float(rate), "po_rate": None, "years": None,
                                  "flag": "new_item"}
        return line
    levels = tuple(tuple(x) for x in guardrails.policy()["goods_price"]["levels"])
    kg = _kg_rate(prices, row, levels=levels) if given.get("kg") != "no" else None
    if kg is None:
        return _from_kg(line, kg, warnings, i)
    return _escalated(line, kg, eval_year, warnings, i)


def _works_line(row, given, user, prices, warnings, i) -> dict:
    line = _base(row)
    schedule = given.get("schedule") or None
    if schedule is not None and schedule not in _SCHEDULES:
        raise lines.LineError(f"line {i + 1}: schedule is one of {', '.join(_SCHEDULES)}")
    basis = given.get("basis") or ("derived" if given.get("c") else "same")
    if basis not in ("same", "similar", "derived", "first_principle"):
        raise lines.LineError(f"line {i + 1}: basis is same, similar, derived or "
                              "first_principle")
    if given.get("c"):
        if basis not in ("derived", "first_principle"):
            raise lines.LineError(f"line {i + 1}: components make a derived or "
                                  "first_principle rate")
        comps = []
        for part in given["c"].split("|"):
            name, _, amount = part.partition(":")
            if not name.strip() or not amount.strip():
                raise lines.LineError(f"line {i + 1}: components are c=name:amount|name:amount")
            value = lines.number(amount, f"line {i + 1} component {name.strip()}")
            comps.append({"name": name.strip()[:120], "amount": lines.plain(value),
                          "source": _officer(user, name.strip(), amount.strip())})
        total = sum((_d(c["amount"]) for c in comps), Decimal(0))
        line.update(rate=lines.plain(total), basis=basis, schedule=schedule, components=comps)
        return line
    if given.get("rate"):
        rate = lines.number(given["rate"], f"line {i + 1} rate")
        line.update(rate=lines.plain(rate), basis=basis, schedule=schedule or "nrl_sor",
                    source=_officer(user, "rate", given["rate"]))
        if basis == "similar":
            if not given.get("score"):
                raise lines.LineError(f"line {i + 1}: a Similar rate needs score=0.96 and "
                                      "ref=... (the item it is like)")
            score = lines.number(given["score"], f"line {i + 1} score")
            if score > 1:
                raise lines.LineError(f"line {i + 1}: score is between 0 and 1")
            floor = guardrails.policy()["similarity"]["similar_rate"]
            if score < floor:
                raise lines.LineError(f"line {i + 1}: a match of {lines.plain(score)} is below "
                                      f"the Similar threshold {floor} (R34): give the "
                                      "components instead (c=name:amount|...), a Derived rate")
            line["match"] = {"score": float(score), "threshold": floor,
                             "basis": (f"like {given.get('ref') or 'an item'} in "
                                       f"{schedule or 'nrl_sor'}")[:300]}
        if given.get("ref"):
            line["source"]["quote"] = f"rate={given['rate']} for {given['ref']}"[:300]
        return line
    kg = _kg_rate(prices, row, levels=(("estimate", "internal"),)) \
        if given.get("kg") != "no" else None
    return _from_kg(line, kg, warnings, i)


def _from_kg(line: dict, kg: dict | None, warnings: list, i: int) -> dict:
    if kg is None:
        line.update(rate=None, basis="new_item")
        return line
    line.update(rate=kg["rate"], basis=kg["basis"], source=kg["source"])
    years = sorted(set(kg["years"]))
    span = (f"{years[0]}" if len(years) == 1 else f"{years[0]}-{years[-1]}") if years \
        else "years unknown"
    warnings.append(f"line {i + 1}: the median of {kg['n']} past NRL {kg['level']} rate(s), "
                    f"{span}")
    return line


def _base(row: dict) -> dict:
    return {"item": row["item"], "qty": row["qty"], "uom": row.get("uom"),
            "material_code": row.get("material_code")}


def _wage_line(row, given, user, i) -> dict:
    """One manday or hour line: its rate built from the basic wage (R12, R39)."""
    line = _base(row)
    if not given.get("basic"):
        raise _Ask(f"line {i + 1} ({row['item'][:40]}): basic=... (the basic wage per "
                   f"{row['uom']})")
    if not given.get("non_statutory"):
        raise _Ask(f"line {i + 1}: non_statutory=35 to 40.6 (the percentage for this worker "
                   "category)")
    rules = guardrails.policy()["percentages"]
    lo, hi = rules["non_statutory"]
    ns = lines.number(given["non_statutory"], f"line {i + 1} non_statutory")
    if not lo <= ns <= hi:
        raise lines.LineError(f"line {i + 1}: non_statutory is between {lo} and {hi}")
    basic = lines.number(given["basic"], f"line {i + 1} basic")
    comps = [{"name": "basic wage", "amount": _num(basic),
              "source": _officer(user, "basic", given["basic"])}]

    def rule(name: str, key: str, pct, of: list[int]) -> None:
        base = sum((_d(comps[k]["amount"]) for k in of), Decimal(0))
        comps.append({"name": name, "amount": _num(_pct(base, pct)),
                      "source": {"rule": key, "pct": float(pct), "of": of}})

    rule("statutory 13.13%", "statutory", rules["statutory"], [0])
    rule(f"non-statutory {lines.plain(ns)}%", "non_statutory", ns, [0])
    sunday = (given.get("sunday") or "no").lower()
    if sunday not in ("yes", "no"):
        raise lines.LineError(f"line {i + 1}: sunday is yes or no")
    if sunday == "yes":
        rule("Sunday or holiday: double", "sunday_double", rules["sunday_double"], [0, 1, 2])
        rule("Sunday or holiday CP 15%", "sunday_cp", rules["sunday_cp"], [0, 1, 2, 3])
    if given.get("allowance"):
        amount = lines.number(given["allowance"], f"line {i + 1} allowance", zero=True)
        comps.append({"name": "shift or height allowance", "amount": _num(amount),
                      "source": _officer(user, "allowance", given["allowance"])})
    rate = sum((_d(c["amount"]) for c in comps), Decimal(0))
    line.update(rate=_num(rate), basis="wage", components=comps)
    return line


class _Ask(Exception):
    """An input the estimate cannot go on without."""


def _is_manday(row: dict) -> bool:
    return (row.get("uom") or "").strip().lower() in _MANDAY


def _insurance(rows: list[dict], mdy: list[int], per: dict, settings: dict) -> dict[int, str]:
    """ESI or WCI for each manday line: its own choice, else the case-wide one
    (R13: by worker category, so one estimate may carry both)."""
    case_wide = (settings.get("insurance") or "").lower()
    out = {}
    for k in mdy:
        choice = ((per.get(rows[k]["item"]) or {}).get("insurance") or case_wide).lower()
        if choice not in ("esi", "wci"):
            raise _Ask(f"line {k + 1}: insurance=esi or insurance=wci (ESI 4.75% or WCI "
                       "2.79%, by worker category), or all: insurance=... for every line")
        out[k] = choice
    return out


def _reimbursements(rows: list[dict], settings: dict, per: dict | None = None) -> list[dict]:
    """R13: the LS lines over the manday lines, each rounded up. ESI and WCI
    are each over the lines that carry it."""
    mdy = [k for k, line in enumerate(rows) if line["basis"] == "wage" and _is_manday(line)]
    if not mdy:
        return []
    insured = _insurance(rows, mdy, per or {}, settings)
    rules = guardrails.policy()["percentages"]
    step = guardrails.policy()["rounding"]["reimbursement_step"]
    out = []
    groups = [("pf_edli", "PF/EDLI", True, mdy)]
    groups += [(key, key.upper(), False, [k for k in mdy if insured[k] == key])
               for key in ("esi", "wci")]
    groups += [("ppe", "PPE", False, mdy), ("tools_tackles", "Tools and Tackles", False, mdy)]
    for key, name, on_basic, over in groups:
        if not over:
            continue
        pct = rules[key]
        comps = []
        for k in over:
            ref = rows[k]
            per = _d(ref["components"][0]["amount"]) if on_basic else _d(ref["rate"])
            comps.append({"name": (f"line {k + 1} {'basic wage' if on_basic else 'wages'} x "
                                   f"{ref['qty']} {ref['uom']}")[:120],
                          "amount": _num(per * _d(ref["qty"])), "base": True,
                          "source": {"line": k, "component": 0} if on_basic else {"line": k}})
        bases = list(range(len(comps)))
        base = sum((_d(c["amount"]) for c in comps), Decimal(0))
        value = _pct(base, pct)
        comps.append({"name": f"{name} {lines.plain(_d(pct))}%", "amount": _num(value),
                      "source": {"rule": key, "pct": float(pct), "of": bases}})
        rounded = guardrails.round_up(value, step)
        comps.append({"name": f"rounded up to the nearest Rs {step:,}",
                      "amount": _num(rounded - value),
                      "source": {"rule": "round_up", "of": [len(comps) - 1], "step": step}})
        out.append({"item": f"{name} reimbursement (LS)", "qty": 1, "uom": "LS",
                    "material_code": None, "rate": _num(rounded), "basis": "wage",
                    "components": comps})
    return out


# -- the flows -------------------------------------------------------------------------

def _core(est: dict) -> dict:
    """An estimate without what the guardrails add to it, to tell whether a run
    changed anything (an unchanged one keeps the pending approval)."""
    def line(x: dict) -> dict:
        x = {k: v for k, v in x.items() if k != "nrl_approved"}
        for key in ("match", "comparison"):
            if key in x:
                x[key] = {k: v for k, v in x[key].items() if k not in ("final", "varn", "cagr")}
        return x
    return {**{k: v for k, v in est.items() if k not in ("alerts", "provisional")},
            "lines": [line(x) for x in est.get("lines") or []]}


def _run(request: dict, ctx, kind: str, category: str) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    wf = orchestrator.workflow(need)
    quantity = orchestrator._artifact(wf, "quantity") or {}
    rows = quantity.get("lines") or []
    if not quantity.get("complete") or not rows:
        raise NamError("stage_gate", "the quantity is not finalised (quantity.finalise)",
                       case_id=case_id)
    stored = orchestrator._artifact(wf, "estimate") or {}
    if stored.get("category") != category or stored.get("kind", "") not in ("", kind):
        stored = {}
    answer = request.get("answer") or {}
    if answer and answer.get("question_id") != RATES:
        raise NamError("bad_request", f"the estimate asks only '{RATES}'")
    user = caller = ctx_user()
    warnings: list[str] = []
    ask = ""
    try:
        per, settings = _inputs(stored, rows, str(answer.get("value") or ""), kind, user)
    except lines.LineError as exc:
        warnings.append(str(exc))
        per, settings = _inputs(stored, rows, "", kind)

    try:
        header = _header(settings, need)
    except lines.LineError as exc:
        warnings.append(str(exc))
        header = _header({}, need)
    prices = _Prices(ctx)
    prices.prefetch([r["item"] for r in rows
                     if (per.get(r["item"]) or {}).get("kg") != "no" and not _is_manday(r)])
    built: list[dict] = []
    eval_year = date.today().year
    if settings.get("eval_date"):
        try:
            eval_year = date.fromisoformat(settings["eval_date"]).year
        except ValueError:
            warnings.append("eval_date is a date, YYYY-MM-DD: today is used")
    try:
        for i, row in enumerate(rows):
            given = per.get(row["item"], {})
            user = given.get("by") or caller
            if kind == "goods":
                built.append(_goods_line(row, given, user, prices, warnings, i, eval_year))
            elif kind == "works" and _is_manday(row):
                built.append(_wage_line(row, given, user, i))
            elif kind == "works":
                built.append(_works_line(row, given, user, prices, warnings, i))
            elif _is_manday(row):
                built.append(_wage_line(row, given, user, i))
            elif given.get("rate"):
                rate = lines.number(given["rate"], f"line {i + 1} rate")
                built.append({**_base(row), "rate": lines.plain(rate), "basis": "wage",
                              "components": [{"name": "reimbursable at the officer's rate",
                                              "amount": lines.plain(rate),
                                              "source": _officer(user, "rate", given["rate"])}]})
            else:
                built.append({**_base(row), "rate": None, "basis": "new_item"})
        if kind in ("wage", "works"):
            built += _reimbursements(built, settings, per)
    except _Ask as exc:
        ask = str(exc)
    except lines.LineError as exc:
        warnings.append(str(exc))
        ask = str(exc)

    medians = [w for w in warnings if " past NRL " in w]
    if medians:
        warnings = [w for w in warnings if " past NRL " not in w]
        warnings.insert(0, ("Rates from the KG are medians of past NRL tenders; it holds no "
                            "single latest rate (R7), so check them before approving: "
                            + "; ".join(medians[:20]))[:300])
    if prices.capped:
        warnings.append(f"Only the first {LOOKUPS} distinct items were looked up in the KG; "
                        "run the estimate again for the rest.")
    if kind == "works" and _within_empanelment(need, wf):
        warnings += [f"line {i + 1}: within an empanelment every rate must be NRL SOR; this "
                     "one is not (F47: an unauthorised substitution)"
                     for i, line in enumerate(built) if line.get("schedule") != "nrl_sor"][:50]

    question = {"id": RATES, "source": "harness", "options": [],
                "text": ("Give the rates the knowledge graph could not, e.g. '2: rate=450'"
                         if kind != "wage" else
                         "Give the wage inputs, e.g. '1: basic=650, non_statutory=35'"),
                "why": ""}
    if ask:
        question["why"] = ask[:500]
        return Outcome(case_id=case_id, status="needs_input",
                       result=_result(category, kind, [], None, per, settings,
                                      complete=False, header=header),
                       questions=(question,), warnings=tuple(warnings))

    total = sum(((_d(x["rate"]) * _d(x["qty"])).quantize(CENT) for x in built
                 if x["rate"] is not None), Decimal(0))
    for x in built:
        if x["rate"] is not None:
            x["amount"] = _num(_d(x["rate"]) * _d(x["qty"]))
    new_items = [i + 1 for i, x in enumerate(built) if x["rate"] is None]
    value = {"summary": (f"{len(built)} line(s), Rs {total:,.2f}"
                         + (f"; {len(new_items)} New Item(s) without a rate" if new_items
                            else ""))[:300],
             "category": category, "kind": kind, "lines": built, "total": _num(total),
             "provisional": True, "precedents": list(need.selected_cases), "inputs": per,
             "settings": settings, "header": header}
    if new_items:
        question["why"] = (f"lines {', '.join(map(str, new_items[:20]))} have no rate: "
                           "they are New Items")[:500]
    citations = tuple({"source": "kg", "ref": f"tender:{k}", "authority": "precedent"}
                      for k in need.selected_cases)
    # an estimate older than the quantity is stored again even when its lines
    # read the same, so the S4 check sees it is of the quantity now finalised
    stale = guardrails._at_version(wf, "estimate") < guardrails._at_version(wf, "quantity")
    changed = _core(value) != _core(stored) or stale
    return Outcome(case_id=case_id, status="ok",
                   result=_result(category, kind, built, _num(total), per, settings,
                                  complete=True, header=header),
                   questions=(question,) if new_items else (),
                   warnings=tuple(warnings), citations=citations,
                   next=("docs.generate",), artifacts={"estimate": value} if changed else {})


def _header(settings: dict, need) -> dict:
    """F13's estimate header: the period, the cost centre and the location
    (the case's own unless the officer gives one). Raises LineError."""
    out = {"start": settings.get("start") or None, "end": settings.get("end") or None,
           "cost_centre": settings.get("cost_centre") or None,
           "location": settings.get("location") or need.location or None}
    days = {}
    for key in ("start", "end"):
        if out[key]:
            try:
                days[key] = date.fromisoformat(out[key])
            except ValueError:
                raise lines.LineError(f"{key} is a date, YYYY-MM-DD") from None
    if len(days) == 2 and days["end"] < days["start"]:
        raise lines.LineError("end is before start")
    return out


def _result(category, kind, built, total, per, settings, *, complete: bool,
            header: dict) -> dict:
    return {"category": category, "kind": kind, "complete": complete, "lines": built, "total": total,
            "header": header,
            "new_items": [i + 1 for i, x in enumerate(built) if x["rate"] is None],
            "inputs": per, "settings": settings}


def _within_empanelment(need, wf) -> bool:
    if (need.subtype or {}).get("value") == "Within impanelment":
        return True
    form = orchestrator._artifact(wf, "guided_form") or {}
    return (form.get("answers") or {}).get("impanelment") == "Within impanelment"


def ctx_user() -> str:
    from nam_agent.nam import store

    return store.current().user


def goods(request: dict, ctx) -> Outcome:
    return _run(request, ctx, "goods", "goods")


def works(request: dict, ctx) -> Outcome:
    return _run(request, ctx, "works", "works")


def wage(request: dict, ctx) -> Outcome:
    """Manpower: for Services, and for manpower within Works (R14)."""
    return _run(request, ctx, "wage", _wage_category(request, ctx))


def _wage_category(request: dict, ctx) -> str:
    need = need_store.load(ctx.conn, request["case_id"])
    cat = need_classify.route(need)["category"] if need else None
    return "works" if cat == "works" else "services"
