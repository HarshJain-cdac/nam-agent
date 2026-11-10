"""pr.build (FRS F27, R20) and pr.submit (FRS F28, F29), plan Step 15.7, and
one goods case end to end through every built flow from S1 to its PR number.

S6 (validate, package.freeze) is Step 15.8: it is scripted here with what
those flows will leave, the validation and the package."""

import dataclasses

import pytest
from nam_fakes import OFFICER, ctx
from nam_walk import BUYER, call, confirm, goods_at_s6

from nam_agent.nam import identity, need, orchestrator, playbooks, store
from nam_agent.nam.playbooks import Outcome

CHECKER = identity.Caller(tenant="nrl", user="v5", roles=("U3", "U5"))
OFFICE = "all: cost_centre=4100, plant=NR01, purchasing_group=P12"


@pytest.fixture()
def s6(monkeypatch):
    table = playbooks.registry()

    def leaves(name, value):
        def handle(request, context):
            return Outcome(case_id=request["case_id"], status="ok",
                           result={"reason": "scripted", "candidates": [], "refs": {}},
                           artifacts={name: value})
        return handle

    kinds = ("estimate", "boq", "specification", "pqc", "scc")
    for fid, name, value in (("validate", "validation", {"status": "pass"}),
                             ("package.freeze", "package", {"summary": "5 documents",
                                                             "documents": [{"kind": k}
                                                                           for k in kinds]})):
        table[fid] = dataclasses.replace(table[fid], handler=leaves(name, value),
                                         result_schema="flow.nam.route")
    monkeypatch.setattr("nam_agent.nam.playbooks.registry", lambda: table)
    return table


def _at_s7(c):
    case_id = goods_at_s6(c)
    call(c, case_id, "Validate the PR and documents", caller=CHECKER)
    body = call(c, case_id, "Freeze the package", caller=CHECKER)
    assert confirm(c, case_id, body, CHECKER)["stage"] == "S7"
    return case_id


def _submitted(c, item: str = "") -> str:
    case_id = _at_s7(c) if not item else _other_at_s7(c, item)
    call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields", value=OFFICE)
    body = call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields",
                value="all: justification=worn gaskets, delivery_date=2026-12-01")
    confirm(c, case_id, body, BUYER)
    return case_id


def _other_at_s7(c, item: str) -> str:
    """A second case, of other items, so the duplicate-PR check lets it through."""
    case_id = _at_s7(c)
    wf = orchestrator.workflow(need.load(c.conn, case_id))
    for name in ("estimate", "quantity"):
        for line in wf["artifacts"][name]["value"]["lines"]:
            line["item"], line["material_code"] = item, None
    rec = need.load(c.conn, case_id)
    rec.workflow = wf
    need.save(c.conn, rec, reason="test: another item")
    return case_id


def _estimate_line(c, case_id):
    wf = orchestrator.workflow(need.load(c.conn, case_id))
    return orchestrator._artifact(wf, "estimate")["lines"][0]


def _pr(c, case_id):
    return orchestrator._artifact(orchestrator.workflow(need.load(c.conn, case_id)), "pr")


def test_the_pr_is_built_from_the_case_and_asks_for_the_rest(tmp_path, s6):
    c = ctx(tmp_path)
    case_id = _at_s7(c)
    body = call(c, case_id, "Prepare the PR", caller=BUYER)
    header = body["result"]["header"]
    assert header["category"] == need.GOODS and header["tender_type"] == "Open"
    # the valuation price is the approved estimate's rate (PR flow 2(f), 3(a))
    est = _estimate_line(c, case_id)
    assert header["procurement_type"] == "OPEX"
    assert header["estimated_value"] == f"{est['amount']:.2f}"
    assert header["date_opened"] == need.load(c.conn, case_id).created_at[:10]
    assert body["result"]["items"] == [{"material_code": "MC-PUR-4-1",
                                        "description": "ring joint gasket R24", "qty": 20,
                                        "uom": "Nos", "rate": est["rate"],
                                        "amount": est["amount"]}]
    assert {"Cost centre", "Plant", "Purchasing group", "Justification for procurement",
            "Delivery date (YYYY-MM-DD)"} <= set(body["result"]["missing"])
    assert body["status"] == "needs_input" and body["pending_action"] is None
    assert body["questions"][0]["id"] == "fields"


def test_a_complete_pr_is_submitted_once_with_its_number(tmp_path, s6):
    c = ctx(tmp_path)
    case_id = _at_s7(c)
    call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields", value=OFFICE)
    body = call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields",
                value="all: justification=worn gaskets, delivery_date=2026-12-01")
    assert body["result"]["missing"] == [] and body["pending_action"]["kind"] == "submit_pr"
    status = call(c, case_id, "Submit the PR", caller=BUYER)
    assert status["result"]["submitted"] is False and "no SAP connector" in status["result"]["sap"]
    done = confirm(c, case_id, body, BUYER)
    number = done["result"]["result"]["pr_number"]
    assert number.startswith("PRX-") and done["stage"] == "S8"
    shown = call(c, case_id, "Submit the PR", caller=BUYER)
    assert shown["result"] == {"submitted": True, "pr_number": number, "sap_pr": None,
                               "sap_status": None, "items": 1, "sap": "not sent: no SAP connector"}
    assert shown["questions"][0]["id"] == "sap_pr"


def test_the_sap_pr_number_is_recorded_after_submission(tmp_path, s6):
    c = ctx(tmp_path)
    case_id = _at_s7(c)
    call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields", value=OFFICE)
    early = call(c, case_id, "Record the SAP PR number", caller=BUYER, answer="sap_pr",
                 value="1000012345")
    assert any("not submitted yet" in w for w in early["warnings"])
    body = call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields",
                value="all: justification=worn gaskets, delivery_date=2026-12-01")
    confirm(c, case_id, body, BUYER)
    bad = call(c, case_id, "Record the SAP PR number", caller=BUYER, answer="sap_pr",
               value="12AB")
    assert any("not a SAP PR number" in w for w in bad["warnings"])
    good = call(c, case_id, "Record the SAP PR number", caller=BUYER, answer="sap_pr",
                value="1000012345")
    assert good["result"]["sap_pr"] == "1000012345"
    assert good["questions"][0]["id"] == "sap_status"
    done = call(c, case_id, "Record the SAP PR number", caller=BUYER, answer="sap_status",
                value="Released")
    assert done["result"]["sap_status"] == "Released" and done["questions"] == []
    ref = orchestrator._artifact(orchestrator.workflow(need.load(c.conn, case_id)),
                                 "pr_reference")
    assert (ref["sap_pr"], ref["sap_status"], ref["by"]) == ("1000012345", "Released", "b1")


def test_one_sap_pr_number_belongs_to_one_case(tmp_path, s6):
    c = ctx(tmp_path)
    first, second = _submitted(c), _submitted(c, "valves")
    call(c, first, "Record the SAP PR number", caller=BUYER, answer="sap_pr", value="1000012345")
    body = call(c, second, "Record the SAP PR number", caller=BUYER, answer="sap_pr",
                value="1000012345")
    assert any("already recorded on " + first in w for w in body["warnings"])
    assert body["result"]["sap_pr"] is None


def test_the_pr_items_carry_the_estimate_rate_and_its_cost_centre(tmp_path, s6):
    c = ctx(tmp_path)
    case_id = goods_at_s6(c)
    wf = orchestrator.workflow(need.load(c.conn, case_id))
    wf["artifacts"]["estimate"]["value"]["header"] = {"cost_centre": "4100"}
    rec = need.load(c.conn, case_id)
    rec.workflow = wf
    need.save(c.conn, rec, reason="test: the estimate's cost centre")
    call(c, case_id, "Validate the PR and documents", caller=CHECKER)
    body = call(c, case_id, "Freeze the package", caller=CHECKER)
    confirm(c, case_id, body, CHECKER)
    body = call(c, case_id, "Prepare the PR", caller=BUYER)
    assert body["result"]["header"]["cost_centre"] == "4100"
    item = body["result"]["items"][0]
    est = _estimate_line(c, case_id)
    assert (item["rate"], item["amount"]) == (est["rate"], est["amount"])
    assert "Cost centre" not in body["result"]["missing"]


@pytest.mark.parametrize(("value", "says"), [
    ("all: estimated_value=1", "comes from the case"), ("all: delivery_date=soon", "YYYY-MM-DD"),
    ("1: plant=NR01", "all: name=value"), ("all: colour=red", "not a field"),
])
def test_a_bad_pr_field_changes_nothing(tmp_path, s6, value, says):
    c = ctx(tmp_path)
    case_id = _at_s7(c)
    call(c, case_id, "Prepare the PR", caller=BUYER)
    version = store.version_of(c.conn, case_id)
    body = call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields", value=value)
    assert any(says in w for w in body["warnings"]), body["warnings"]
    assert store.version_of(c.conn, case_id) == version


def test_only_the_buyer_builds_and_submits(tmp_path, s6):
    c = ctx(tmp_path)
    case_id = _at_s7(c)
    for system in ("Prepare the PR", "Submit the PR"):
        assert call(c, case_id, system)["error"]["code"] == "forbidden_role"


def test_a_goods_case_reaches_its_pr_number_through_the_real_flows(tmp_path, s6):
    """S1 to S8: capture, classify, search, select, quantity, estimate,
    documents, (scripted S6), PR, every gate confirmed by its role."""
    c = ctx(tmp_path)
    case_id = _at_s7(c)
    call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields", value=OFFICE)
    body = call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields",
                value="all: justification=worn gaskets, delivery_date=2026-12-01")
    done = confirm(c, case_id, body, BUYER)
    status = call(c, case_id, "case.status")["result"]
    assert done["stage"] == "S8"
    assert status["gates"] == ["clauses", "documents", "estimate", "quantity"]
    assert status["actions"] == ["freeze_package", "submit_pr"]
    # every flow's write is in the history, under its name or as the
    # artifact it left ("quantity from quantity.finalise")
    history = " ".join(f"{e['flow']} {e['reason']}" for e in store.versions(c.conn, case_id))
    for flow in ("need.capture", "need.classify", "precedent.select", "quantity.finalise",
                 "estimate.goods", "docs.generate", "pr.build"):
        assert flow in history, flow
    assert OFFICER.user in {e["by"]["user"] for e in store.versions(c.conn, case_id)}
