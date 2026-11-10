"""Real cases walked to a stage through the real flows, for the evals of
Steps 15.5-15.7. Every response is checked against the response schema and
its flow's result schema on the way."""

from __future__ import annotations

from nam_fakes import OFFICER, ask, classified, run

import nam_agent.nam as harness
from nam_agent.nam import playbooks, schema

VALIDATED: list[str] = []      # every response checked, for the metrics (nam_metrics.py)
VERIFIED: list[tuple] = []     # (flow, status, verification passed) of each reply
DRAFTER = harness.Caller(tenant="nrl", user="d1", roles=("U4",))
BUYER = harness.Caller(tenant="nrl", user="b1", roles=("U3",))


def valid(body: dict) -> dict:
    name = "error" if "error" in body else "response"
    assert schema.validate(body, schema.load(name)) == [], body
    flow = playbooks.registry().get(body.get("flow"))
    if "error" not in body and flow is not None and flow.result_schema:
        assert schema.validate(body["result"], schema.load(flow.result_schema)) == [], body
    VALIDATED.append(body.get("flow") or "error")
    if "verification" in body:
        VERIFIED.append((body["flow"], body["status"], body["verification"]["passed"]))
    return body


def call(c, case_id, system, text="", answer=None, caller=OFFICER, value=None) -> dict:
    req = {"system": system, "messages": [{"role": "user", "content": text}]}
    if case_id:
        req["case_id"] = case_id
    if answer:
        req["answer"] = {"question_id": answer, "value": value}
    return valid(run(req, c, caller))


def confirm(c, case_id, body, caller=OFFICER) -> dict:
    pending = body["pending_action"]
    assert pending, body
    return valid(harness.run({"system": "confirm", "messages": [{"role": "user", "content": ""}],
                              "case_id": case_id, "confirm": {"action_id": pending["id"]}},
                             c, caller))


def goods_at_s3(c) -> str:
    case_id = run(ask("i need to procure gaskets"), c)["case_id"]
    run(ask("", case_id=case_id, answer={"question_id": "variant", "value": "ring joint"}), c)
    classified(c, case_id)
    assert call(c, case_id, "Select the precedent", "use PUR-4")["stage"] == "S3"
    return case_id


def goods_at_s4(c) -> str:
    case_id = goods_at_s3(c)
    body = call(c, case_id, "Finalise the quantity")
    assert confirm(c, case_id, body)["stage"] == "S4"
    return case_id


def goods_at_s5(c) -> str:
    """The estimate approved. The PR follows it (PR Creation Flow): S5 and S6
    are optional, so the case is at S7 until the documents are started, which
    takes it back to S5 (orchestrator optional_unless)."""
    case_id = goods_at_s4(c)
    body = call(c, case_id, "Goods estimate")
    assert confirm(c, case_id, body)["stage"] == "S7"
    return case_id


def goods_at_s6(c) -> str:
    case_id = goods_at_s5(c)
    body = call(c, case_id, "Generate the BOQ and SCC", answer="fields",
                value="all: delivery_period=8 weeks, delivery_location=NRL stores")
    body = confirm(c, case_id, body, DRAFTER)
    # the documents approved; validation is optional too, so the case is at S7
    assert confirm(c, case_id, body, DRAFTER)["stage"] == "S7"
    return case_id


def _form(c, text: str, answers: dict, kind: str = "") -> str:
    case_id = call(c, None, "Fill the guided form", text)["case_id"]
    if kind:
        call(c, case_id, "Fill the guided form", answer="works_kind", value=kind)
    body = call(c, case_id, "Fill the guided form")
    while body["questions"] and body["questions"][0]["id"] in answers:
        q = body["questions"][0]["id"]
        body = call(c, case_id, "Fill the guided form", answer=q, value=answers[q])
    assert body["result"]["complete"], body
    for q, v in (("procurement_type", "OPEX"), ("tender_type", "Open")):
        call(c, case_id, "Confirm the classification", answer=q, value=v)
    return case_id


SERVICES = {"service_type": "Repetitive (AMC, manpower)", "service_code": "",
            "description": "housekeeping manpower", "scope_of_work": "daily cleaning of the "
            "township", "worker_categories": "unskilled", "head_count": "10",
            "term_months": "12", "start_date": "", "location": "Plant 1",
            "justification": "cleaning", "purpose": ""}
WORKS = {"budget": "Capital", "impanelment": "Outside impanelment",
         "description": "admin building civil work", "scope_of_work": "plaster and excavation",
         "line_items": "", "required_by": "2027-03-01", "location": "Township",
         "justification": "new office", "purpose": ""}


def services_at_s4(c, lines: str) -> str:
    case_id = _form(c, "manpower for housekeeping at plant 1", SERVICES)
    body = call(c, case_id, "Finalise the quantity", answer="lines", value=lines)
    assert confirm(c, case_id, body)["stage"] == "S4"
    return case_id


def works_at_s4(c, lines: str, impanelment: str = "Outside impanelment") -> str:
    case_id = _form(c, "civil work for the new admin building",
                    {**WORKS, "impanelment": impanelment}, kind="New")
    body = call(c, case_id, "Finalise the quantity", answer="lines", value=lines)
    assert confirm(c, case_id, body)["stage"] == "S4"
    return case_id


# -- S5 to S10 (the checking and closing flows) -----------------------------------------

DEPT = harness.Caller(tenant="nrl", user="dept2", roles=("U2",))
CHECKER = harness.Caller(tenant="nrl", user="v5", roles=("U3", "U5"))
APPROVER = harness.Caller(tenant="nrl", user="a7", roles=("U7",))
COMMERCIAL = harness.Caller(tenant="nrl", user="c6", roles=("U6",))
FIELDS = "all: delivery_period=8 weeks, delivery_location=NRL stores"
PR_FIELDS = ("all: cost_centre=4100, plant=NR01, purchasing_group=P12",
             "all: justification=worn gaskets, delivery_date=2026-12-01")


def goods_drafted(c, fields: str = FIELDS) -> str:
    """S5: the documents generated, reviewed, the compliance built."""
    case_id = goods_at_s5(c)
    call(c, case_id, "Generate the BOQ and SCC", answer="fields", value=fields)
    call(c, case_id, "Review the drafts", caller=DRAFTER)
    call(c, case_id, "Build the compliance requirements", caller=DEPT,
         answer="requirements", value="all: apply=yes")
    return case_id


def approve_documents(c, case_id) -> dict:
    body = call(c, case_id, "case.status")
    while body["pending_action"] and body["pending_action"]["kind"].startswith("gate:"):
        body = confirm(c, case_id, body, DRAFTER)
    return body


def goods_validated(c) -> str:
    """S6, validated."""
    case_id = goods_drafted(c)
    assert approve_documents(c, case_id)["stage"] == "S7"
    assert call(c, case_id, "Validate the PR and documents",
                caller=CHECKER)["result"]["status"] == "pass"
    return case_id


def goods_frozen(c) -> str:
    """S7: the package frozen."""
    case_id = goods_validated(c)
    body = call(c, case_id, "Freeze the package", caller=CHECKER)
    assert confirm(c, case_id, body, CHECKER)["stage"] == "S7"
    return case_id


def goods_submitted(c) -> str:
    """S8: the PR submitted."""
    case_id = goods_frozen(c)
    call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields", value=PR_FIELDS[0])
    body = call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields", value=PR_FIELDS[1])
    assert confirm(c, case_id, body, BUYER)["stage"] == "S8"
    return case_id


def goods_approved(c) -> str:
    """S10: approved, the package ready for NIT."""
    case_id = goods_submitted(c)
    body = call(c, case_id, "Route for approval", caller=APPROVER, answer="authority",
                value="GM (Materials)")
    assert confirm(c, case_id, body, APPROVER)["stage"] == "S10"
    return case_id


def to_nit(c, case_id: str, fields: str, clause_docs: tuple[str, ...]) -> dict:
    """From an approved estimate (S5) to the handoff confirmed: the documents
    with one suggested clause kept per clause document, reviewed, the
    compliance built, approved, validated, frozen, the PR submitted, approved
    and handed off. Returns the last response."""
    call(c, case_id, "Generate the documents", answer="fields", value=fields)
    for kind in clause_docs:
        call(c, case_id, "Generate the documents", answer=kind,
             value="1: keep=yes, why=the standard clause for this work")
    call(c, case_id, "Review the drafts", caller=DRAFTER)
    call(c, case_id, "Build the compliance requirements", caller=DEPT,
         answer="requirements", value="all: apply=yes")
    assert approve_documents(c, case_id)["stage"] == "S7"
    body = call(c, case_id, "Validate the PR and documents", caller=CHECKER)
    assert body["result"]["status"] == "pass", body["result"]["checks"]
    body = call(c, case_id, "Freeze the package", caller=CHECKER)
    assert confirm(c, case_id, body, CHECKER)["stage"] == "S7"
    call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields", value=PR_FIELDS[0])
    body = call(c, case_id, "Prepare the PR", caller=BUYER, answer="fields", value=PR_FIELDS[1])
    assert confirm(c, case_id, body, BUYER)["stage"] == "S8"
    body = call(c, case_id, "Route for approval", caller=APPROVER, answer="authority",
                value="GM (Services)")
    assert confirm(c, case_id, body, APPROVER)["stage"] == "S10"
    body = call(c, case_id, "Hand off to the NIT module", caller=COMMERCIAL)
    return confirm(c, case_id, body, COMMERCIAL)
