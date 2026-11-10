"""The guardrails (plan Step 14): each test tries to break one, the way a
model or a careless flow would, and checks the harness refuses or corrects
it in code.

The flows of Steps 15 are not built yet, so each is scripted here: it leaves
the artifact a test gives it, and may call a tool first. Only
nam_agent/nam/guardrails.py stands between that artifact and the record."""

import dataclasses
import sqlite3
import threading

import pytest
from nam_fakes import ask, classified, ctx

import nam_agent.nam as harness
from nam_agent.nam import audit, guardrails, need, orchestrator, playbooks, schema, store, tools
from nam_agent.nam.contracts.base import CONTRACTS
from nam_agent.nam.playbooks import Outcome

ALL = harness.Caller(tenant="nrl", user="boss", roles=tuple(f"U{i}" for i in range(1, 8)))
AUX = harness.Caller(tenant="aux", user="boss", roles=ALL.roles)
VARIANT = {"question_id": "variant", "value": "ring joint"}
ITEM = {"material_code": "MC-GSK-1", "description": "ring joint gasket", "qty": 20,
        "uom": "nos"}
PACKAGE = {"summary": "5 documents", "documents": [
    {"kind": k} for k in ("estimate", "boq", "specification", "pqc", "scc")]}

# what a scripted flow leaves when a test does not say
DEFAULT = {
    "quantity.finalise": ("quantity", {"summary": "20 nos", "complete": True, "lines": [
        {"item": "ring joint gasket", "qty": 20, "uom": "nos", "material_code": "MC-GSK-1",
         "source": "officer"}]}),
    "estimate.goods": ("estimate", {"summary": "new item", "category": "goods", "lines": [
        {"item": "ring joint gasket", "qty": 20, "uom": "nos", "material_code": "MC-GSK-1",
         "rate": 450, "amount": 9000, "basis": "quotation",
         "source": {"officer": ALL.user, "quote": "rate=450"}}]}),
    "docs.generate": ("documents", {"summary": "BOQ, SCC, PQC, spec", "complete": True,
                                    "documents": []}),
    "validate": ("validation", {"status": "pass"}),
    "package.freeze": ("package", PACKAGE),
    "pr.build": ("pr", {"summary": "PR for 20 gaskets", "missing": [], "items": [ITEM]}),
    "approval": ("approval_file", {"summary": "file PRX", "complete": True}),
    "handoff.nit": ("handoff", lambda request, c: {
        "summary": "package to NIT",
        "package_version": _wf(c, request["case_id"])["frozen"]["at_version"]}),
}


def _wf(c, case_id):
    return orchestrator.workflow(need.load(c.conn, case_id))


def _leaves(artifact, value, before=None):
    """A scripted flow: runs `before(request, context)` (a tool call, a sneaky
    save), then leaves `value` (or value(request, context)) as its artifact."""
    def handle(request, context):
        if before:
            before(request, context)
        made = value(request, context) if callable(value) else value
        return Outcome(case_id=request["case_id"], status="ok",
                       artifacts={artifact: made} if artifact else {},
                       result={"reason": "scripted", "candidates": [], "refs": {}})
    return handle


def _select(request, context):
    record = need.load(context.conn, request["case_id"])
    record.selected_cases = ["PUR-4"]
    need.save(context.conn, record, reason="officer selected precedent")


@pytest.fixture()
def flows(monkeypatch):
    return scripted_registry(monkeypatch)


def scripted_registry(monkeypatch) -> dict:
    """The flow registry with every flow scripted (DEFAULT), patched in."""
    table = playbooks.registry()
    for fid, (artifact, value) in DEFAULT.items():
        table[fid] = dataclasses.replace(table[fid], handler=_leaves(artifact, value),
                                         result_schema="flow.nam.route")
    table["precedent.select"] = dataclasses.replace(
        table["precedent.select"], handler=_leaves(None, None, _select),
        result_schema="flow.nam.route")
    for fid in ("estimate.wage", "estimate.works"):
        table[fid] = dataclasses.replace(table[fid], handler=_leaves(*DEFAULT["estimate.goods"]),
                                         result_schema="flow.nam.route")
    monkeypatch.setattr("nam_agent.nam.playbooks.registry", lambda: table)
    return table


def _script(flows, flow_id, artifact, value, before=None):
    flows[flow_id] = dataclasses.replace(flows[flow_id], handler=_leaves(artifact, value, before))


def _valid(body):
    name = "error" if "error" in body else "response"
    assert schema.validate(body, schema.load(name)) == [], body
    return body


def _flow(c, case_id, flow_id, text="", caller=ALL, **extra):
    if flow_id.startswith("estimate.") and not text:
        text = "the rate is 450"          # the officer's number the scripted line cites
    return _valid(harness.run(ask(text, system=f"run {flow_id}", case_id=case_id, **extra), c,
                              caller))


def _confirm(c, case_id, caller=ALL):
    pending = _flow(c, case_id, "case.status", caller=caller)["pending_action"]
    assert pending, _flow(c, case_id, "case.status", caller=caller)["result"]
    return _valid(harness.run(ask("", system="confirm", case_id=case_id,
                                  confirm={"action_id": pending["id"]}), c, caller))


def _refused(body, rule):
    assert body.get("error", {}).get("code") == "guardrail", body
    assert any(d.startswith(rule) for d in body["error"]["details"]), body["error"]
    return body["error"]


def _edit(c, case_id, change):
    """A change made outside the harness (the dashboard, or a tampered row)."""
    with store.acting(store.Actor(tenant="nrl", user="dashboard", flow="test")):
        record = need.load(c.conn, case_id)
        change(record)
        need.save(c.conn, record, reason="edited outside the harness")


def _opened(c, caller=ALL, text="i need to procure gaskets"):
    first = _valid(harness.run(ask(text), c, caller))
    _valid(harness.run(ask("", case_id=first["case_id"], answer=VARIANT), c, caller))
    second = _valid(classified(c, first["case_id"], caller))
    assert second["stage"] == "S2", second
    return first["case_id"]


def _at_estimate(c, caller=ALL):
    case_id = _opened(c, caller)
    _flow(c, case_id, "precedent.select", "use PUR-4", caller)
    _flow(c, case_id, "quantity.finalise", caller=caller)
    assert _confirm(c, case_id, caller)["stage"] == "S4"
    return case_id


def _at_pr(c, caller=ALL):
    case_id = _at_estimate(c, caller)
    _flow(c, case_id, "estimate.goods", caller=caller)
    _confirm(c, case_id, caller)
    _flow(c, case_id, "docs.generate", caller=caller)
    _confirm(c, case_id, caller)
    _confirm(c, case_id, caller)
    _flow(c, case_id, "validate", caller=caller)
    _flow(c, case_id, "package.freeze", caller=caller)
    assert _confirm(c, case_id, caller)["stage"] == "S7"
    return case_id


def _at_handoff(c):
    case_id = _at_pr(c)
    _flow(c, case_id, "pr.build")
    _confirm(c, case_id)
    _flow(c, case_id, "approval")
    assert _confirm(c, case_id)["stage"] == "S10"
    return case_id


def _guard_events(c, case_id, status):
    return [e["name"] for e in audit.events(c.conn, case_id=case_id, kind="guardrail")
            if e["status"] == status]


# -- the price evidence a scripted estimate may cite --------------------------

def _price_tool(median="450.00"):
    contract = CONTRACTS["price_history"]
    row = {"item": "ring joint gasket", "uom": "nos", "price_level": "award",
           "unit_price": {"n": 3, "min": "410.00", "median": median, "max": "480.00"},
           "estimated_rate": None, "totals": None, "observations": 3, "matched": True,
           "years": [2025], "spans_years": False, "sources": ["PUR-4"]}
    result = {"found": True, "source": {"system": "kg", "endpoint": "/price"},
              "authority": "precedent", "warnings": [], "weak": None, "rows": [row]}
    return {"price_history": tools.NamTool(
        "price_history", contract.description, contract.effect, contract.input_schema,
        contract.output_schema, lambda **_: result)}


def _line(**over):
    base = {"item": "ring joint gasket", "qty": 20, "uom": "nos", "material_code": "MC-GSK-1",
            "basis": "same", "schedule": "nrl_sor"}
    return {**base, **over}


def _estimate_from(lines, *, cited=True, total=None, **extra):
    """An estimate whose lines are filled in after a price_history call:
    a line given as a function of the citation gets it."""
    seen = {}

    def call(request, context):
        result = tools.call(context, "price_history", {"requirement": "ring joint gasket"},
                            tools=_price_tool())
        seen["cite"] = guardrails.cite(result, "rows/0/unit_price/median") if cited else None

    def value(request, context):
        made = [ln(seen["cite"]) if callable(ln) else ln for ln in lines]
        est = {"summary": "estimate", "category": "goods", "lines": made, **extra}
        if total is not None:
            est["total"] = total
        return est
    return value, call


def _run_estimate(c, flows, case_id, lines, text="", flow_id="estimate.goods", **kw):
    value, call = _estimate_from(lines, **kw)
    _script(flows, flow_id, "estimate", value, call)
    return _flow(c, case_id, flow_id, text)


# -- no invented rate (R5-R8, R35) ----------------------------------------------

def test_a_rate_traced_to_this_runs_tool_result_is_accepted(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    body = _run_estimate(c, flows, case_id,
                         [lambda cite: _line(rate=450, amount=9000, source=cite)], total=9000)
    assert body["status"] == "needs_approval", body
    est = _wf(c, case_id)["artifacts"]["estimate"]["value"]
    assert est["lines"][0]["source"]["evidence"] == "ev_1" and est["provisional"] is True


@pytest.mark.parametrize("bad", [
    pytest.param(lambda cite: _line(rate=450), id="no-source"),
    pytest.param(lambda cite: _line(rate=520, source=cite), id="number-changed"),
    pytest.param(lambda cite: _line(rate=450, source={**cite, "evidence": "ev_7"}),
                 id="evidence-not-in-run"),
    pytest.param(lambda cite: _line(rate=450, source={**cite, "tool": "suppliers"}),
                 id="wrong-tool"),
    pytest.param(lambda cite: _line(rate=410, source={**cite, "path": "rows/0/years/0"}),
                 id="other-path"),
    pytest.param(lambda cite: _line(rate=450, source={"tool": "price_history"}),
                 id="unsourced-claim"),
    pytest.param(lambda cite: _line(rate=None, basis="same"), id="no-rate-but-same"),
])
def test_an_invented_rate_is_refused(tmp_path, flows, bad):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    _refused(_run_estimate(c, flows, case_id, [bad]), "rate_source")
    assert "estimate" not in _wf(c, case_id)["artifacts"]
    assert "rate_source" in _guard_events(c, case_id, "blocked")


def test_evidence_from_an_earlier_run_cannot_be_cited_again(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    kept = {}
    first = _run_estimate(c, flows, case_id, [lambda cite: kept.setdefault(
        "line", _line(rate=450, source=cite))])
    assert first["status"] == "needs_approval"
    # a second run that calls no tool but reuses the first run's citation
    _script(flows, "estimate.goods", "estimate", {"summary": "again", "category": "goods",
                                                   "lines": [kept["line"]]})
    _refused(_flow(c, case_id, "estimate.goods"), "rate_source")


def test_changing_the_tool_result_does_not_change_the_evidence(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)

    def call(request, context):
        result = tools.call(context, "price_history", {"requirement": "ring joint gasket"},
                            tools=_price_tool())
        result["rows"][0]["unit_price"]["median"] = "999.00"     # edited after the call
        call.cite = {"tool": "price_history", "evidence": "ev_1",
                     "path": "rows/0/unit_price/median", "value": "999.00"}

    _script(flows, "estimate.goods", "estimate", lambda r, c_: {
        "summary": "x", "category": "goods", "lines": [_line(rate=999, source=call.cite)]},
        call)
    _refused(_flow(c, case_id, "estimate.goods"), "rate_source")


def test_an_officer_rate_must_be_one_the_officer_typed(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    mine = _line(rate=475.5, basis="quotation", source={"officer": "boss", "quote": "budgetary"})
    _refused(_run_estimate(c, flows, case_id, [mine], text="use the quotation"), "rate_source")
    other = {**mine, "source": {"officer": "someone_else"}}
    _refused(_run_estimate(c, flows, case_id, [other], text="the rate is 475.50"),
             "rate_source")
    body = _run_estimate(c, flows, case_id, [mine], text="the budgetary quote is Rs 475.50")
    assert body["status"] == "needs_approval", body


def _derived(*, rate, statutory, pct=13.13):
    return _line(rate=rate, basis="wage", schedule=None, components=[
        {"name": "basic wage", "amount": 600, "source": {"officer": "boss"}},
        {"name": "statutory", "amount": statutory,
         "source": {"rule": "statutory", "of": [0], "pct": pct}}])


def test_a_computed_rate_is_checked_component_by_component(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    _edit(c, case_id, lambda r: setattr(r, "category", {**r.category, "value": "Works"}))
    text = "basic wage 600 per day"
    flow = "estimate.works"           # the rules on components are the same in every flow

    def works(lines):
        value, call = _estimate_from(lines)
        _script(flows, flow, "estimate",
                lambda r, c_: {**value(r, c_), "category": "works"}, call)
        return _flow(c, case_id, flow, text)

    _refused(works([_derived(rate=700, statutory=78.78)]), "arithmetic")   # 600 + 78.78
    _refused(works([_derived(rate=690, statutory=90)]), "rate_source")     # not 13.13%
    _refused(works([_derived(rate=678.78, statutory=78.78, pct=20)]), "rate_source")
    assert works([_derived(rate=678.78, statutory=78.78)])["status"] == "needs_approval"


def test_amounts_and_totals_are_recomputed(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    _refused(_run_estimate(c, flows, case_id,
                           [lambda cite: _line(rate=450, amount=8000, source=cite)]),
             "arithmetic")
    _refused(_run_estimate(c, flows, case_id, [lambda cite: _line(rate=450, source=cite)],
                           total=10000), "arithmetic")


# -- the DSR alert and the last resort (R6, R8, R9, R36, R37) --------------------------

def test_a_dsr_rate_without_its_alert_gets_one(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    body = _run_estimate(c, flows, case_id, [lambda cite: _line(
        rate=450, schedule="dsr", basis="similar", nrl_approved=True, source=cite,
        match={"score": 0.97, "threshold": 0.95, "basis": "same class, other rating"})])
    assert body["status"] == "needs_approval", body
    line = _wf(c, case_id)["artifacts"]["estimate"]["value"]["lines"][0]
    alerts = _wf(c, case_id)["artifacts"]["estimate"]["value"]["alerts"]
    assert line["nrl_approved"] is False
    assert [a["code"] for a in alerts] == ["dsr_fallback"] and "DSR 2023" in alerts[0]["says"]
    assert any("DSR 2023" in w for w in body["warnings"])
    assert {"dsr_alert"} <= set(_guard_events(c, case_id, "corrected"))


def test_a_quoted_rate_carries_the_last_resort_alert_and_a_goods_po_does_not(tmp_path, flows):
    # for goods the last PO price is the PR flow's own basis (2(a)); a vendor's
    # quotation is still the last resort
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    _run_estimate(c, flows, case_id, [lambda cite: _line(rate=450, basis="quotation",
                                                         schedule=None, source=cite)])
    alerts = _wf(c, case_id)["artifacts"]["estimate"]["value"]["alerts"]
    assert [a["code"] for a in alerts] == ["last_resort"]
    other = ctx(tmp_path / "po")
    case_id = _at_estimate(other)
    _run_estimate(other, flows, case_id, [lambda cite: _line(rate=450, basis="po",
                                                             schedule=None, source=cite)])
    alerts = _wf(other, case_id)["artifacts"]["estimate"]["value"]["alerts"]
    assert [a["code"] for a in alerts] == []


# -- thresholds are never final (R4, R10, R34, R38) -------------------------------

@pytest.mark.parametrize("match", [
    pytest.param(None, id="similar-without-match"),
    pytest.param({"score": 0.90, "threshold": 0.95, "basis": "close"}, id="below-threshold"),
    pytest.param({"score": 0.90, "threshold": 0.85, "basis": "close"}, id="threshold-lowered"),
])
def test_a_similar_rate_below_its_threshold_is_refused(tmp_path, flows, match):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    extra = {"match": match} if match else {}
    _refused(_run_estimate(c, flows, case_id, [lambda cite: _line(
        rate=450, basis="similar", source=cite, **extra)]), "threshold")


@pytest.mark.parametrize(("comparison", "flag"), [
    ({"quoted": 450, "po_rate": 360, "years": 3}, "Regular"),     # VARN 25%, CAGR 7.7%
    ({"quoted": 450, "po_rate": 300, "years": 2}, "AHR"),         # CAGR 22.5%
    ({"quoted": 450, "po_rate": 430, "years": 4}, "ALR"),         # VARN 4.65%
    ({"quoted": 450, "po_rate": 300, "years": 0.6}, "manual_review"),
    ({"quoted": 450, "po_rate": None, "years": None}, "new_item"),
])
def test_the_rate_comparison_flag_is_the_rules_not_the_models(tmp_path, flows, comparison,
                                                              flag):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    wrong = "ALR" if flag != "ALR" else "AHR"
    _refused(_run_estimate(c, flows, case_id, [lambda cite: _line(
        rate=450, source=cite, comparison={**comparison, "flag": wrong})]), "threshold")
    body = _run_estimate(c, flows, case_id, [lambda cite: _line(
        rate=450, source=cite, comparison={**comparison, "flag": flag, "final": True})],
        provisional=False)
    decide = flag in guardrails.policy()["rate_comparison"]["decide"]
    # a flagged line waits for the officer's Accept or Revise (PR flow 2(e)):
    # no approval is proposed until it has one
    assert body["status"] == ("ok" if decide else "needs_approval"), body
    rec = need.load(c.conn, case_id)
    assert ("estimate_decided" in {m["name"] for m in orchestrator.missing(
        rec, orchestrator.workflow(rec))}) == decide
    est = _wf(c, case_id)["artifacts"]["estimate"]["value"]
    assert est["lines"][0]["comparison"]["final"] is False and est["provisional"] is True
    if flag in ("manual_review", "AHR"):
        assert [a["code"] for a in est["alerts"]] == [
            "manual_review" if flag == "manual_review" else "high_rate"]
    # the estimate gate is still the officer's, once nothing is left to decide
    pending = _flow(c, case_id, "case.status")["pending_action"]
    assert (pending is None) if decide else (pending["kind"] == "gate:estimate")


def test_a_misstated_varn_is_refused(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    _refused(_run_estimate(c, flows, case_id, [lambda cite: _line(
        rate=450, source=cite, comparison={"quoted": 450, "po_rate": 360, "years": 3,
                                           "varn": 5, "flag": "Regular"})]), "threshold")


# -- precedent only if the officer selected it (R2) ----------------------------------

def test_a_precedent_the_officer_did_not_name_is_not_selected(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _opened(c)
    error = _refused(_flow(c, case_id, "precedent.select", "pick the best one for me"),
                     "precedent")
    assert "PUR-4" in error["message"]
    assert need.load(c.conn, case_id).selected_cases == []
    assert any("guardrail: reverted" in e["reason"]
               for e in harness.history(c, ALL, case_id)["entries"])
    assert _flow(c, case_id, "case.status")["stage"] == "S2"


def test_only_a_selection_flow_may_change_the_precedent(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    sneak = _leaves("quantity", DEFAULT["quantity.finalise"][1], lambda r, ctx_: _edit_in_flow(
        ctx_, r["case_id"], ["PUR-4", "PUR-9"]))
    flows["quantity.finalise"] = dataclasses.replace(flows["quantity.finalise"], handler=sneak)
    _refused(_flow(c, case_id, "quantity.finalise", "also use PUR-9"), "precedent")
    assert need.load(c.conn, case_id).selected_cases == ["PUR-4"]


def _edit_in_flow(context, case_id, selected):
    record = need.load(context.conn, case_id)
    record.selected_cases = selected
    need.save(context.conn, record, reason="flow wrote the selection")


def test_an_artifact_built_on_an_unselected_tender_is_refused(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    _flow(c, case_id, "estimate.goods")
    _confirm(c, case_id)
    _script(flows, "docs.generate", "documents", {"summary": "SCC", "complete": True,
                                                  "documents": [], "precedents": ["PUR-9"]})
    _refused(_flow(c, case_id, "docs.generate"), "precedent")
    _script(flows, "docs.generate", "documents", {"summary": "SCC", "complete": True,
                                                  "documents": [], "precedents": ["PUR-4"]})
    assert _flow(c, case_id, "docs.generate")["status"] == "needs_approval"


# -- category rules (R1, R14, R28) ---------------------------------------------------

def test_an_estimate_flow_of_another_category_does_not_run(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    ran = []
    flows["estimate.works"] = dataclasses.replace(
        flows["estimate.works"], handler=lambda r, ctx_: ran.append(1))
    _refused(_flow(c, case_id, "estimate.works"), "category")
    assert ran == []                                   # refused before it ran
    _refused(_flow(c, case_id, "estimate.wage"), "category")


def test_manpower_within_works_uses_the_wage_estimator(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    _edit(c, case_id, lambda r: setattr(r, "category", {**r.category, "value": "Works"}))
    _script(flows, "estimate.wage", "estimate", {**DEFAULT["estimate.goods"][1],
                                                 "category": "works"})
    assert _flow(c, case_id, "estimate.wage")["status"] == "needs_approval"
    _refused(_flow(c, case_id, "estimate.goods"), "category")


def test_an_estimate_for_another_category_is_refused(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    _script(flows, "estimate.goods", "estimate", {**DEFAULT["estimate.goods"][1],
                                                  "category": "services"})
    _refused(_flow(c, case_id, "estimate.goods"), "category")


def test_a_codified_line_needs_its_material_code(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    _edit(c, case_id, lambda r: setattr(r, "subtype", {**r.subtype, "value": "Codified"}))
    line = {**DEFAULT["estimate.goods"][1]["lines"][0], "material_code": None}
    _script(flows, "estimate.goods", "estimate", {**DEFAULT["estimate.goods"][1],
                                                  "lines": [line]})
    _refused(_flow(c, case_id, "estimate.goods"), "category")


def test_a_subtype_outside_the_category_is_refused_and_reverted(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    before = need.load(c.conn, case_id).subtype

    def sneaky(request, context):
        record = need.load(context.conn, request["case_id"])
        record.subtype = {**record.subtype, "value": "Consultancy"}       # a Services subtype
        need.save(context.conn, record, reason="flow set the subtype")

    _script(flows, "estimate.goods", "estimate", DEFAULT["estimate.goods"][1], sneaky)
    _refused(_flow(c, case_id, "estimate.goods"), "category")
    assert need.load(c.conn, case_id).subtype == before


# -- duplicates ----------------------------------------------------------------------

def test_a_second_open_case_for_the_same_need_is_flagged(tmp_path, flows):
    c = ctx(tmp_path)
    first = _opened(c)
    again = _valid(harness.run(ask("i need to procure gaskets"), c, ALL))
    assert any(first in w and "separate need" in w for w in again["warnings"]), again
    other = _valid(harness.run(ask("i need to procure gaskets"), c, AUX))   # another tenant
    assert not any("separate need" in w for w in other["warnings"])


def test_a_duplicate_pr_is_refused(tmp_path, flows):
    c = ctx(tmp_path)
    first = _at_pr(c)
    _flow(c, first, "pr.build")
    done = _confirm(c, first)
    number = done["result"]["result"]["pr_number"]
    second = _at_pr(c)
    error = _refused(_flow(c, second, "pr.build"), "duplicate_pr")
    assert number in " ".join(error["details"]) and first in " ".join(error["details"])
    # another material code is another item
    _script(flows, "pr.build", "pr", {"summary": "PR", "missing": [], "items": [
        {**ITEM, "material_code": "MC-GSK-2"}]})
    assert _flow(c, second, "pr.build")["status"] == "needs_approval"


def test_a_pr_built_before_the_other_was_submitted_is_still_refused(tmp_path, flows):
    c = ctx(tmp_path)
    first, second = _at_pr(c), _at_pr(c)
    _flow(c, first, "pr.build")
    _flow(c, second, "pr.build")                  # both built while neither is submitted
    _confirm(c, first)
    _refused(_confirm(c, second), "duplicate_pr")
    status = _flow(c, second, "case.status")
    assert status["stage"] == "S7" and status["pending_action"]["kind"] == "submit_pr"
    assert "submit_pr" not in _wf(c, second)["actions"]


def test_two_submissions_at_once_register_one_pr(tmp_path, flows, monkeypatch):
    # past the pre-check, as two confirmations at the same moment would be:
    # the check under the write lock still refuses the second
    db = tmp_path / "state.db"
    c = ctx(tmp_path, conn=sqlite3.connect(db, timeout=30, check_same_thread=False))
    first, second = _at_pr(c), _at_pr(c)
    _flow(c, first, "pr.build")
    _flow(c, second, "pr.build")
    monkeypatch.setattr(guardrails, "before_action", lambda *a, **kw: None)
    bodies = {}

    def go(case_id):
        own = ctx(tmp_path, conn=sqlite3.connect(db, timeout=30))
        bodies[case_id] = _confirm(own, case_id)

    threads = [threading.Thread(target=go, args=(k,)) for k in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    refused = [k for k, b in bodies.items() if "error" in b]
    assert len(refused) == 1, bodies
    _refused(bodies[refused[0]], "duplicate_pr")
    rows = c.conn.execute("SELECT case_id, pr_number FROM nam_pr_register").fetchall()
    assert [r[0] for r in rows] != refused and len(rows) == 1 and rows[0][1].startswith("PRX-")
    assert _wf(c, refused[0])["pending"] is None or \
        "claim" not in _wf(c, refused[0])["pending"]


def test_another_tenants_pr_is_not_a_duplicate(tmp_path, flows):
    c = ctx(tmp_path)
    mine = _at_pr(c)
    _flow(c, mine, "pr.build")
    _confirm(c, mine)
    theirs = _at_pr(c, AUX)
    _flow(c, theirs, "pr.build", caller=AUX)
    assert _confirm(c, theirs, AUX)["result"]["result"]["pr_number"].startswith("PRX-")


def test_a_pr_without_items_cannot_be_built(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_pr(c)
    _script(flows, "pr.build", "pr", {"summary": "PR", "missing": []})
    _refused(_flow(c, case_id, "pr.build"), "artifact")


# -- the package and the hand-off (R24, R32) ------------------------------------------

def test_a_package_without_a_mandatory_document_is_refused(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    _flow(c, case_id, "estimate.goods")
    _confirm(c, case_id)
    _flow(c, case_id, "docs.generate")
    _confirm(c, case_id)
    _confirm(c, case_id)
    _flow(c, case_id, "validate")
    _script(flows, "package.freeze", "package", {"summary": "4 documents", "documents": [
        {"kind": k} for k in ("estimate", "boq", "specification", "scc")]})
    error = _refused(_flow(c, case_id, "package.freeze"), "mandatory_docs")
    assert "pqc" in error["message"]


def test_freezing_after_the_documents_changed_needs_a_new_validation(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    _flow(c, case_id, "estimate.goods")
    _confirm(c, case_id)
    _flow(c, case_id, "docs.generate")
    _confirm(c, case_id)
    _confirm(c, case_id)
    _flow(c, case_id, "validate")
    _flow(c, case_id, "package.freeze")

    def newer_documents(record):
        record.workflow["artifacts"]["documents"]["at_version"] = record.version + 5
    _edit(c, case_id, newer_documents)
    _refused(_confirm(c, case_id), "handoff")
    assert not _wf(c, case_id).get("frozen")


def test_the_handoff_needs_every_approval(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_handoff(c)
    _edit(c, case_id, lambda r: r.workflow["actions"].pop("approve"))     # a tampered record
    error = _refused(_flow(c, case_id, "handoff.nit"), "handoff")
    assert "approve is not confirmed" in " ".join(error["details"])


def test_handoff_before_approval_is_refused_at_the_action(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_handoff(c)
    assert _flow(c, case_id, "handoff.nit")["pending_action"]["kind"] == "handoff_nit"
    _edit(c, case_id, lambda r: r.workflow["gates"].pop("documents"))
    error = _refused(_confirm(c, case_id), "handoff")
    assert "documents approval is not given" in " ".join(error["details"])
    assert not _wf(c, case_id).get("closed")


def test_the_handoff_carries_the_frozen_package(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_handoff(c)
    _script(flows, "handoff.nit", "handoff", {"summary": "package", "package_version": 1})
    _refused(_flow(c, case_id, "handoff.nit"), "handoff")
    assert "handoff" not in _wf(c, case_id)["artifacts"]


def test_the_whole_case_still_reaches_nit_through_the_guardrails(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_handoff(c)
    _flow(c, case_id, "handoff.nit")
    assert _confirm(c, case_id)["result"]["kind"] == "handoff_nit"
    assert _wf(c, case_id)["closed"] is True


def test_every_refusal_is_audited(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _opened(c)
    _flow(c, case_id, "precedent.select", "choose for me")
    rows = [e for e in audit.events(c.conn, case_id=case_id, kind="guardrail")]
    assert [(e["name"], e["status"], e["flow"]) for e in rows] == [
        ("precedent", "blocked", "precedent.select")]


def test_the_policy_holds_the_reviewed_frs_values():
    p = guardrails.policy()
    assert p["status"].startswith("REVIEWED")
    assert p["duplicates"]["pr_open_days"] == 180
    assert p["similarity"]["similar_rate"] == 0.95
    assert {k: p["rate_comparison"][k] for k in ("varn_pct", "cagr_pct", "min_years")} == {
        "varn_pct": 10, "cagr_pct": 10, "min_years": 1}
    assert set(p["mandatory_documents"]) == {"goods", "services", "works"}
    assert set(p["estimate_flows"]) == {f for f in playbooks.registry() if
                                        f.startswith("estimate.")}


def _resolved(ref, keys):
    matches = [{"cite": {"case_key": k, "tender_no": None, "job_name": None,
                         "department": None}, "match": "core"} for k in keys]
    return {"found": bool(keys), "source": {"system": "kg", "endpoint": "/api/kg/resolve"},
            "authority": "precedent", "warnings": [], "tier": "core" if keys else None,
            "ambiguous": len(keys) > 1, "matches": matches}


@pytest.mark.parametrize(("ref", "keys", "said", "named"), [
    ("OC01000435", ["PUR-4"], "replicate oc01000435", True),
    ("OC01000435", ["PUR-4", "PUR-5"], "replicate oc01000435", False),   # ambiguous
    ("OC01000435", ["PUR-4"], "replicate the gasket one", False),         # not what was said
    ("OC01000435", ["PUR-5"], "replicate oc01000435", False),             # another tender
])
def test_a_reference_names_a_tender_only_when_it_resolved_to_it_alone(ref, keys, said, named):
    with guardrails.evidence():
        guardrails.note("resolve_reference", _resolved(ref, keys), {"ref": ref})
        assert guardrails._named("PUR-4", said) is named
    assert guardrails._named("PUR-4", said) is False          # no evidence outside a run
