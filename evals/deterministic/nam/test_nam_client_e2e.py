"""The integration guide's smoke test (plan Step 19): the one-file client
(nam_agent/client.py) drives a Goods case over HTTP from need to the
NIT handoff, with only short prompts, the answers the questions ask for, and
each approval confirmed by a user of the role the reply names. As the
backend would, per docs/nam/integration.md."""

import json
import sqlite3
import threading

import pytest
from nam_fakes import fake_kg

import nam_agent.nam as harness
from nam_agent.nam import api
from nam_agent.client import NamCallError, NamClient


@pytest.fixture()
def base(tmp_path):
    db = tmp_path / "state.db"

    def make_ctx():
        conn = sqlite3.connect(db, check_same_thread=False)
        return harness.Context(conn=conn, home=tmp_path, kg_url="http://kg",
                               fetch=fake_kg()), conn.close

    srv = api.serve("127.0.0.1", 0, make_ctx, max_body=1 << 20, model_configured=False,
                    kg_url="", rate_per_minute=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _as(base, user, *roles):
    return NamClient(base, tenant="nrl", user=user, roles=list(roles), timeout=30)


def drive(client, prompt, case_id=None, text="", answers=None):
    """One call site: the prompt, then each question answered from `answers`
    while the reply asks for one of them."""
    reply = client.run(prompt, [{"role": "user", "content": text}], case_id=case_id)
    for _ in range(12):
        q = (reply["questions"] or [None])[0]
        if reply["status"] != "needs_input" or not q or q["id"] not in (answers or {}):
            break
        reply = client.run(prompt, [], case_id=reply["case_id"],
                           case_version=reply["case_version"],
                           answer={"question_id": q["id"], "value": answers.pop(q["id"])})
    return reply


def approve(clients, reply):
    """Confirm the pending action as a user of the first role it allows."""
    while reply["status"] == "needs_approval":
        act = reply["pending_action"]
        who = next(clients[r] for r in act["allowed_roles"] if r in clients)
        reply = who.confirm("Approve the pending step", reply["case_id"], act["id"])
    return reply


def test_a_backend_client_runs_a_goods_case_to_nit(base):
    c = {"U1": _as(base, "u1", "U1"), "U2": _as(base, "dept2", "U2"),
         "U3": _as(base, "b1", "U3"), "U4": _as(base, "d1", "U4"),
         "U5": _as(base, "v5", "U5"), "U6": _as(base, "c6", "U6"),
         "U7": _as(base, "a7", "U7")}
    officer = c["U1"]
    reply = drive(officer, "Assess need for request #123", text="i need to procure gaskets",
                  answers={"variant": "ring joint"})
    case_id = reply["case_id"]
    reply = drive(officer, "Confirm the classification", case_id,
                  answers={"category": "goods", "procurement_type": "OPEX",
                           "tender_type": "Open"})
    assert reply["stage"] == "S2"
    drive(officer, "Select the precedent", case_id, text="use PUR-4")
    assert approve(c, drive(officer, "Finalise the quantity", case_id))["stage"] == "S4"
    # the PR follows the approved estimate: S5 and S6 are optional
    assert approve(c, drive(officer, "Prepare the estimate", case_id))["stage"] == "S7"
    drive(c["U4"], "Generate the BOQ and SCC", case_id, answers={
        "fields": "all: delivery_period=8 weeks, delivery_location=NRL stores"})
    drive(c["U4"], "Review the drafts", case_id)
    drive(c["U2"], "Build the compliance requirements", case_id,
          answers={"requirements": "all: apply=yes"})
    reply = approve(c, drive(officer, f"What is the status of {case_id}"))
    assert reply["stage"] == "S7"           # documents approved; validation is optional
    checked = drive(c["U5"], "Validate the documents", case_id)
    assert checked["result"]["status"] == "pass"
    assert approve(c, drive(c["U5"], "Freeze the package", case_id))["stage"] == "S7"
    reply = drive(c["U3"], "Prepare the PR", case_id, answers={
        "fields": "all: cost_centre=4100, plant=NR01, purchasing_group=P12"})
    reply = drive(c["U3"], "Prepare the PR", case_id, answers={
        "fields": "all: justification=worn gaskets, delivery_date=2026-12-01"})
    assert approve(c, reply)["stage"] == "S8"
    reply = drive(c["U7"], "Route for approval", case_id,
                  answers={"authority": "GM (Materials)"})
    assert approve(c, reply)["stage"] == "S10"
    drive(c["U6"], "Push the documents to Aarohan", case_id,
          answers={"aarohan_ref": "NRL/MAT/2026/77"})
    done = approve(c, drive(c["U6"], "Hand off to the NIT module", case_id))
    assert done["result"]["kind"] == "handoff_nit"

    trace = officer.trace(case_id)
    assert trace["closed"] and trace["integrity"]["ok"]
    assert {a["user"] for a in trace["approvals"]} == {"u1", "d1", "v5", "b1", "a7", "c6"}
    lines = officer.trace_export(case_id).decode().splitlines()
    assert json.loads(lines[-1])["type"] == "integrity"
    assert officer.call_trace(done["run_id"])["events"]
    with pytest.raises(NamCallError) as exc:
        drive(c["U3"], "Prepare the PR", case_id)
    assert exc.value.status == 409 and exc.value.code == "stage_gate"
