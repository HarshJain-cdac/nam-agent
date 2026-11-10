"""estimate.goods, estimate.wage and estimate.works (FRS F13-F16, F47; plan
Step 15.5): every rate has a basis and a source the guardrails can check, a
line without one is a New Item, and the officer's inputs are kept.

The KG holds past NRL tenders only, so a rate is a KG median (cited), a
number the officer typed, or a policy percentage of other components."""

from decimal import Decimal

import pytest
from nam_fakes import ctx
from nam_walk import call, confirm, goods_at_s4, services_at_s4, works_at_s4

from nam_agent.nam import audit, guardrails, need, orchestrator, store
from nam_agent.nam.playbooks import estimate

G, W, M = "Goods estimate", "Works estimate from the SOR", "Estimate the manpower cost"


def _stored(c, case_id):
    return orchestrator._artifact(orchestrator.workflow(need.load(c.conn, case_id)), "estimate")


# -- goods ----------------------------------------------------------------------------

def test_a_goods_rate_is_the_last_po_price_brought_up_to_today(tmp_path):
    # PR flow 2(a)-(b): the last price paid (the PO, before an NRL estimate),
    # plus the policy's escalation a year to the evaluation date, cited
    from datetime import date
    from decimal import Decimal

    from nam_agent.nam import guardrails

    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    body = call(c, case_id, G)
    line = body["result"]["lines"][0]
    src = line["source"]
    assert line["basis"] == "po" and src["rule"] == "escalation"
    assert src["base"]["tool"] == "price_history" and "/unit_price/median" in src["base"]["path"]
    pct = guardrails.policy()["goods_price"]["escalation_pct"]
    assert src["pct"] == pct and src["years"] == date.today().year - src["from_year"]
    want = guardrails.escalate(Decimal(str(src["base_value"])), pct, src["years"])
    assert line["rate"] == float(want) and line["amount"] == float(want * 20)
    assert line["last_price"]["rate"] == src["base_value"]
    assert line["comparison"]["po_rate"] == src["base_value"]
    assert any("medians of past NRL tenders" in w and "R7" in w for w in body["warnings"])
    assert body["status"] == "needs_approval" and body["pending_action"]["kind"] == "gate:estimate"
    stored = _stored(c, case_id)
    assert stored["provisional"] is True and stored["total"] == line["amount"]
    assert stored["precedents"] == ["PUR-4"]
    calls = [e["name"] for e in audit.events(c.conn, run_id=body["run_id"], kind="tool")]
    assert calls == ["price_history"]


def test_a_quotation_is_compared_with_the_awarded_rate(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    body = call(c, case_id, G, answer="rates", value="1: rate=520")
    line = body["result"]["lines"][0]
    assert line["basis"] == "quotation" and line["source"] == {"officer": "u1",
                                                               "quote": "rate=520"}
    comp = line["comparison"]
    assert comp["quoted"] == 520 and comp["po_rate"] == 400.0 and comp["flag"] == "Regular"
    stored = _stored(c, case_id)["lines"][0]
    assert stored["comparison"]["varn"] == 30.0 and stored["comparison"]["final"] is False
    assert {a["code"] for a in _stored(c, case_id)["alerts"]} == {"last_resort"}
    assert any("known to the year only" in w for w in body["warnings"])


def test_a_typed_rate_is_kept_for_later_runs_and_other_callers(tmp_path):
    from nam_agent.nam import identity

    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    call(c, case_id, G, answer="rates", value="1: rate=520")
    version = store.version_of(c.conn, case_id)
    again = call(c, case_id, G)                                 # nothing typed this time
    assert again["result"]["lines"][0]["rate"] == 520
    assert store.version_of(c.conn, case_id) == version         # unchanged: no new version
    commercial = identity.Caller(tenant="nrl", user="c6", roles=("U6",))
    other = call(c, case_id, G, caller=commercial)
    assert "error" not in other, other
    assert other["result"]["lines"][0]["source"]["officer"] == "u1"


def test_kg_no_makes_a_line_a_new_item(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    body = call(c, case_id, G, answer="rates", value="1: kg=no")
    assert body["result"]["lines"][0]["rate"] is None and body["result"]["new_items"] == [1]
    assert {a["code"] for a in _stored(c, case_id)["alerts"]} == {"new_item"}
    assert body["questions"][0]["id"] == "rates"


def test_an_unreadable_rate_changes_nothing(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    call(c, case_id, G)
    version = store.version_of(c.conn, case_id)
    body = call(c, case_id, G, answer="rates", value="1: rate=cheap")
    assert any("not a positive number" in w for w in body["warnings"])
    assert store.version_of(c.conn, case_id) == version


def test_the_estimate_needs_the_approved_quantity(tmp_path):
    from nam_walk import goods_at_s3

    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    assert call(c, case_id, G)["error"]["code"] == "stage_gate"


# -- works ----------------------------------------------------------------------------

LINES = ("1: remove; add: item=cement plaster 12mm, qty=120, uom=Sqm; add: item=excavation "
         "in soil, qty=40, uom=Cum; add: item=RCC M25, qty=30, uom=Cum; add: item=brick work, "
         "qty=50, uom=Cum; add: item=special door, qty=4, uom=Nos")


def test_works_rates_from_schedules_matches_and_components(tmp_path):
    c = ctx(tmp_path)
    case_id = works_at_s4(c, LINES)
    body = call(c, case_id, W, answer="rates",
                value="2: rate=245, schedule=nrl_sor, ref=2.1.4; 3: rate=7800, schedule=dsr; "
                      "4: rate=6100, basis=similar, score=0.97, ref=4.1.2; "
                      "5: c=frame:9000|shutter:14000|fixing:1500")
    got = [(x["rate"], x["basis"], x.get("schedule")) for x in body["result"]["lines"]]
    assert got == [(310.5, "internal", None), (245, "same", "nrl_sor"), (7800, "same", "dsr"),
                   (6100, "similar", "nrl_sor"), (24500, "derived", None)]
    stored = _stored(c, case_id)
    assert stored["lines"][2]["nrl_approved"] is False                    # DSR (R9)
    assert {(a["code"], a["line"]) for a in stored["alerts"]} == {("dsr_fallback", 2)}
    assert stored["lines"][3]["match"]["final"] is False
    assert stored["total"] == 120 * 310.5 + 40 * 245 + 30 * 7800 + 50 * 6100 + 4 * 24500


def test_a_match_below_the_threshold_is_asked_about_not_refused(tmp_path):
    c = ctx(tmp_path)
    case_id = works_at_s4(c, LINES)
    body = call(c, case_id, W, answer="rates", value="4: rate=6100, basis=similar, score=0.80")
    assert body["status"] == "needs_input" and "below the Similar threshold" in \
        body["questions"][0]["why"]
    assert _stored(c, case_id) is None


def test_within_an_empanelment_every_rate_must_be_nrl_sor(tmp_path):
    c = ctx(tmp_path)
    case_id = works_at_s4(c, LINES, impanelment="Within impanelment")
    body = call(c, case_id, W, answer="rates", value="2: rate=245, schedule=nrl_sor")
    flagged = [w for w in body["warnings"] if "F47" in w]
    assert [w.split(":")[0] for w in flagged] == ["line 1", "line 3", "line 4", "line 5"]


def test_manpower_in_works_is_priced_as_wages_in_the_works_estimate(tmp_path):
    c = ctx(tmp_path)
    case_id = works_at_s4(c, LINES + "; add: item=mason, qty=100, uom=MDY")
    body = call(c, case_id, W)
    assert body["status"] == "needs_input" and "line 6" in body["questions"][0]["why"]
    body = call(c, case_id, W, answer="rates",
                value="6: basic=700, non_statutory=35; all: insurance=wci")
    got = body["result"]["lines"]
    assert body["result"]["kind"] == "works" and got[5]["basis"] == "wage"
    assert got[5]["components"][0]["amount"] == 700.0
    assert [x["item"] for x in got[6:]] == [
        "PF/EDLI reimbursement (LS)", "WCI reimbursement (LS)", "PPE reimbursement (LS)",
        "Tools and Tackles reimbursement (LS)"]
    assert _stored(c, case_id)["lines"][5]["basis"] == "wage"     # the guardrails took it


def test_esi_and_wci_are_chosen_per_line(tmp_path):
    c = ctx(tmp_path)
    case_id = services_at_s4(c, MANPOWER)
    body = call(c, case_id, M, answer="rates",
                value="1: basic=650, non_statutory=35, insurance=esi; "
                      "2: basic=650, non_statutory=35, sunday=yes, insurance=wci; 3: rate=25000")
    ls = {x["item"]: x for x in body["result"]["lines"][3:]}
    assert list(ls) == ["PF/EDLI reimbursement (LS)", "ESI reimbursement (LS)",
                        "WCI reimbursement (LS)", "PPE reimbursement (LS)",
                        "Tools and Tackles reimbursement (LS)"]
    assert [c["source"] for c in ls["ESI reimbursement (LS)"]["components"] if c.get("base")] \
        == [{"line": 0}]
    assert [c["source"] for c in ls["WCI reimbursement (LS)"]["components"] if c.get("base")] \
        == [{"line": 1}]
    assert _stored(c, case_id) is not None


def test_the_estimate_header_has_the_period_cost_centre_and_location(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    body = call(c, case_id, G, answer="rates",
                value="all: start=2026-11-01, end=2027-10-31, cost_centre=4100, "
                      "location=Plant 2")
    assert body["result"]["header"] == {"start": "2026-11-01", "end": "2027-10-31",
                                        "cost_centre": "4100", "location": "Plant 2"}
    assert _stored(c, case_id)["header"]["cost_centre"] == "4100"
    bad = call(c, case_id, G, answer="rates", value="all: start=2027-01-01, end=2026-01-01")
    assert any("end is before start" in w for w in bad["warnings"])
    assert call(c, case_id, G, answer="rates", value="1: start=2026-11-01")["warnings"]


def test_a_new_item_holds_the_estimate_gate_until_priced(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    body = call(c, case_id, G, answer="rates", value="1: kg=no")
    assert body["pending_action"] is None and body["stage"] == "S4"
    status = call(c, case_id, "What is the status")
    assert any("New Item" in m for m in status["result"]["missing"])
    body = call(c, case_id, G, answer="rates", value="1: rate=480")
    assert body["pending_action"]["kind"] == "gate:estimate"


def test_an_estimate_of_an_earlier_quantity_is_not_approved_again(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    assert confirm(c, case_id, call(c, case_id, G))["stage"] == "S7"
    body = call(c, case_id, "Finalise the quantity", answer="lines", value="1: qty=35")
    assert body["stage"] == "S3"
    body = confirm(c, case_id, body)
    assert body["stage"] == "S4" and body["pending_action"] is None
    status = call(c, case_id, "What is the status")
    assert any("earlier quantity" in m for m in status["result"]["missing"])
    again = call(c, case_id, G)
    assert again["result"]["lines"][0]["qty"] == 35
    assert again["pending_action"]["kind"] == "gate:estimate"


# -- wage -----------------------------------------------------------------------------

MANPOWER = ("1: qty=3000, uom=MDY; add: item=Sunday duty unskilled, qty=520, uom=MDY; "
            "add: item=cleaning material, qty=1, uom=LS")
INPUTS = ("1: basic=650, non_statutory=35; 2: basic=650, non_statutory=35, sunday=yes; "
          "3: rate=25000; all: insurance=esi")


def test_a_wage_rate_is_built_from_the_basic_wage(tmp_path):
    c = ctx(tmp_path)
    case_id = services_at_s4(c, MANPOWER)
    body = call(c, case_id, M, answer="rates", value=INPUTS)
    lines = body["result"]["lines"]
    weekday, sunday = lines[0], lines[1]
    assert [x["amount"] for x in weekday["components"]] == [650.0, 85.34, 227.5]
    assert weekday["rate"] == 962.84
    gross = Decimal("962.84")
    double = gross + gross
    cp = (double * Decimal("0.15")).quantize(Decimal("0.01"))
    assert sunday["rate"] == float(double + cp)
    assert [x["source"].get("rule") for x in sunday["components"][1:]] == [
        "statutory", "non_statutory", "sunday_double", "sunday_cp"]
    assert lines[2]["rate"] == 25000 and lines[2]["basis"] == "wage"
    assert body["status"] == "needs_approval"


def test_the_reimbursements_are_ls_lines_rounded_up(tmp_path):
    c = ctx(tmp_path)
    case_id = services_at_s4(c, MANPOWER)
    body = call(c, case_id, M, answer="rates", value=INPUTS)
    ls = {x["item"]: x for x in body["result"]["lines"][3:]}
    assert list(ls) == ["PF/EDLI reimbursement (LS)", "ESI reimbursement (LS)",
                        "PPE reimbursement (LS)", "Tools and Tackles reimbursement (LS)"]
    pf = ls["PF/EDLI reimbursement (LS)"]
    basic = 650 * 3000 + 650 * 520
    assert pf["components"][2]["amount"] == round(basic * 0.13, 2)
    assert pf["rate"] == 300000.0 and pf["rate"] % 50000 == 0
    assert all(x["rate"] % 50000 == 0 and x["qty"] == 1 and x["uom"] == "LS"
               for x in ls.values())
    assert [x["source"] for x in pf["components"][:2]] == [{"line": 0, "component": 0},
                                                           {"line": 1, "component": 0}]


@pytest.mark.parametrize(("value", "asks"), [
    ("1: non_statutory=35; 2: basic=650, non_statutory=35; all: insurance=esi", "basic="),
    ("1: basic=650; 2: basic=650, non_statutory=35; all: insurance=esi", "non_statutory="),
    ("1: basic=650, non_statutory=35; 2: basic=650, non_statutory=35", "insurance="),
])
def test_a_missing_wage_input_is_asked_never_assumed(tmp_path, value, asks):
    c = ctx(tmp_path)
    case_id = services_at_s4(c, "1: qty=3000, uom=MDY; add: item=Sunday duty, qty=520, uom=MDY")
    body = call(c, case_id, M, answer="rates", value=value)
    assert body["status"] == "needs_input" and asks in body["questions"][0]["why"]
    assert _stored(c, case_id) is None


def test_a_non_statutory_percentage_outside_the_range_is_refused(tmp_path):
    c = ctx(tmp_path)
    case_id = services_at_s4(c, "1: qty=3000, uom=MDY")
    body = call(c, case_id, M, answer="rates",
                value="1: basic=650, non_statutory=50; all: insurance=esi")
    assert body["status"] == "needs_input" and "between 35 and 40.6" in body["warnings"][-1]


def test_a_goods_flow_does_not_run_on_a_services_case(tmp_path):
    c = ctx(tmp_path)
    case_id = services_at_s4(c, "1: qty=3000, uom=MDY")
    assert call(c, case_id, "run estimate.goods")["error"]["code"] == "guardrail"


# -- the guardrail rules the wage estimate relies on ---------------------------------

def _wage_estimate(**change):
    est = {"summary": "s", "category": "services", "lines": [
        {"item": "a", "qty": 10, "uom": "MDY", "rate": 100.0, "basis": "wage",
         "components": [{"name": "basic", "amount": 100.0,
                         "source": {"officer": "u1", "quote": "basic=100"}}]},
        {"item": "PF", "qty": 1, "uom": "LS", "rate": 50000.0, "basis": "wage", "components": [
            {"name": "base", "amount": 1000.0, "base": True,
             "source": {"line": 0, "component": 0}},
            {"name": "pf", "amount": 130.0, "source": {"rule": "pf_edli", "pct": 13, "of": [0]}},
            {"name": "round", "amount": 49870.0,
             "source": {"rule": "round_up", "of": [1], "step": 50000}}]}]}
    for path, value in change.items():
        line, comp, key = path.split("__")
        est["lines"][int(line)]["components"][int(comp)][key] = value
    return est


def _check(est):
    from nam_agent.nam import playbooks

    flow = playbooks.registry()["estimate.wage"]
    record = need.NeedRecord(need_id="NAM-1", created_at="", raw_text="x",
                             category={"value": need.SERVICES, "confident": True})
    with store.acting(store.Actor("nrl", "u1", roles=("U1",))):
        return guardrails._estimate(flow, {"messages": [{"role": "user", "content": "basic=100"}]},
                                    record, est)[0]


def test_the_base_line_and_round_up_sources_are_checked():
    assert _check(_wage_estimate()) == []
    rules = {b.rule for b in _check(_wage_estimate(**{"1__0__amount": 999.0}))}
    assert rules == {"rate_source"}                          # not line 0's basic x qty
    assert _check(_wage_estimate(**{"1__2__amount": 49000.0}))    # not the round-up
    assert _check(_wage_estimate(**{"1__2__source": {"rule": "round_up", "of": [1],
                                                       "step": 1000}}))
    assert _check(_wage_estimate(**{"1__0__source": {"line": 1, "component": 0}}))   # not earlier


def test_round_up_is_a_ceiling():
    assert guardrails.round_up(Decimal(1), 50000) == 50000
    assert guardrails.round_up(Decimal(50000), 50000) == 50000
    assert guardrails.round_up(Decimal("50000.01"), 50000) == 100000


def test_an_officer_number_from_an_earlier_estimate_is_accepted_only_as_it_was():
    prior = {"lines": [{"item": "a", "rate": 100.0, "source": {"officer": "u1"},
                        "components": [{"amount": 100.0, "source": {"officer": "u1"}}]}]}
    accepted = guardrails._accepted(prior)
    assert ("a", "u1", Decimal("100.00")) in accepted
    assert ("a", "u1", Decimal("101.00")) not in accepted


def test_the_estimate_kinds_are_the_three_flows():
    assert estimate._FIELDS.keys() == {"goods", "works", "wage"}
