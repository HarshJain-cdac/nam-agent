"""DETERMINISTIC EVAL — the quantity and estimate steps do not wait on the
KG's price records one line at a time.

On the live KG a price lookup takes ~40s. "Finalise the quantity" asked one
per line with no quantity, one after another (10 lines: minutes, and the
screen showed nothing); the estimate did the same for every line. Now the
quantity's suggestions run in parallel within a budget (they are shown,
never applied), and the estimate fetches every line's records in parallel
before its cited calls read them.
"""

from __future__ import annotations

import time

from nam_fakes import ctx, fake_kg
from nam_walk import call, confirm, goods_at_s3

from nam_agent.nam import need, orchestrator
from nam_agent.nam.playbooks import estimate, quantity

DELAY = 0.6


def _slow_prices(calls: list):
    base = fake_kg()

    def fetch(kg_url, path, params):
        if path == "/api/kg/prices":
            calls.append(params["requirement"])
            time.sleep(DELAY)
        return base(kg_url, path, params)
    return fetch


def test_quantity_suggestions_never_hold_the_sheet_up(tmp_path, monkeypatch):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    calls: list = []
    slow = ctx(tmp_path, fetch=_slow_prices(calls), conn=c.conn)
    monkeypatch.setattr(quantity, "SUGGEST_BUDGET_S", DELAY / 3)
    call(slow, case_id, "Finalise the quantity")
    started = time.monotonic()
    body = call(slow, case_id, "Finalise the quantity", answer="lines",
                value="add: item=one gasket, uom=Nos; add: item=two gasket, uom=Nos; "
                      "add: item=three gasket, uom=Nos")
    took = time.monotonic() - started
    assert took < DELAY * 2, took                       # not 3 lookups one after another
    assert body["result"]["missing"]                     # the sheet itself is there at once


def test_the_estimate_fetches_its_lines_prices_in_parallel(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    call(c, case_id, "Finalise the quantity", answer="lines",
         value="add: item=spiral wound gasket 4in, qty=5, uom=Nos; "
               "add: item=rubber gasket sheet, qty=2, uom=Nos")
    body = call(c, case_id, "case.status")
    if body["pending_action"]:
        confirm(c, case_id, body)
    lines = len(orchestrator._artifact(orchestrator.workflow(need.load(c.conn, case_id)),
                                       "quantity")["lines"])
    assert lines >= 3
    calls: list = []
    slow = ctx(tmp_path, fetch=_slow_prices(calls), conn=c.conn)
    started = time.monotonic()
    body = call(slow, case_id, "Goods estimate")
    took = time.monotonic() - started
    assert "error" not in body, body
    assert took < DELAY * (lines - 1), (took, lines)     # about one lookup, not one per line
    # every rate is still cited to its price_history call (the guardrails passed)
    assert all(x.get("rate") is None or x.get("source") for x in body["result"]["lines"])
    assert estimate.PREFETCH_S > DELAY


def test_lookups_cut_off_by_the_budget_never_fail_the_step(tmp_path, monkeypatch):
    # more items than workers on a slow KG: the queued lookups are cancelled
    # when the budget runs out, and a cancelled lookup counts as done().
    # Reading one crashed "Finalise the quantity" on the live KG.
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    calls: list = []
    slow = ctx(tmp_path, fetch=_slow_prices(calls), conn=c.conn)
    monkeypatch.setattr(quantity, "SUGGEST_BUDGET_S", DELAY / 2)
    monkeypatch.setattr(quantity, "FILL_HISTORY_S", DELAY / 2)
    call(slow, case_id, "Finalise the quantity")
    for k in range(0, 24, 8):
        body = call(slow, case_id, "Finalise the quantity", answer="lines",
                    value="; ".join(f"add: item=gasket kind {i}" for i in range(k, k + 8)))
        assert "error" not in body, body
    body = call(slow, case_id, "Finalise the quantity", answer="fill", value="missing")
    assert "error" not in body, body
    assert body["result"]["missing"]                 # cut off, still reported, never a crash
