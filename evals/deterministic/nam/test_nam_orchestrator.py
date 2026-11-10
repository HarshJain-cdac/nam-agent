"""The stage machine (plan Step 9): no stage is skipped or left without its
gate, and an irreversible action runs only on a valid confirmation from an
allowed role, exactly once.

The flows not built yet are scripted here: each only leaves its artifact,
which is all the orchestrator reads. need.capture is the real one."""

import dataclasses
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest
from nam_fakes import ask, classified, ctx, fake_kg

import nam_agent.nam as harness
from nam_agent.nam import need, orchestrator, playbooks, schema, store
from nam_agent.nam.playbooks import Outcome

ALL = harness.Caller(tenant="nrl", user="boss", roles=tuple(f"U{i}" for i in range(1, 8)))
ROLE = {r: harness.Caller(tenant="nrl", user=f"user_{r.lower()}", roles=(r,))
        for r in ("U1", "U2", "U3", "U4", "U5", "U6", "U7")}
VARIANT = {"question_id": "variant", "value": "ring joint"}

# what each scripted flow leaves behind
SCRIPT = {
    "precedent.select": (None, None),
    "quantity.finalise": ("quantity", {"summary": "20 nos", "complete": True, "lines": [
        {"item": "ring joint gasket", "qty": 20, "uom": "nos", "material_code": "MC-GSK-1",
         "source": "officer"}]}),
    # a line with no rate is a New Item: the guardrails need no evidence for it
    "estimate.goods": ("estimate", {"summary": "Rs 1.2 lakh", "category": "goods", "lines": [
        {"item": "ring joint gasket", "qty": 20, "uom": "nos", "material_code": "MC-GSK-1",
         "rate": 450, "amount": 9000, "basis": "quotation",
         "source": {"officer": ALL.user, "quote": "rate=450"}}]}),
    "docs.generate": ("documents", {"summary": "BOQ, SCC, PQC, spec", "complete": True,
                                    "documents": []}),
    "validate": ("validation", {"status": "pass"}),
    "package.freeze": ("package", {"summary": "5 documents", "documents": [
        {"kind": k} for k in ("estimate", "boq", "specification", "pqc", "scc")]}),
    "pr.build": ("pr", {"summary": "PR for 20 gaskets", "missing": [], "items": [
        {"material_code": "MC-GSK-1", "description": "ring joint gasket", "qty": 20,
         "uom": "nos"}]}),
    "approval": ("approval_file", {"summary": "file PRX", "complete": True}),
    "handoff.nit": ("handoff", {"summary": "package to NIT"}),
    "case.edit": (None, None),
}


def _scripted(flow_id, artifact, value):
    def handle(request, context):
        case_id = request["case_id"]
        if flow_id == "precedent.select":
            record = need.load(context.conn, case_id)
            record.selected_cases = ["PUR-4"]
            need.save(context.conn, record, reason="officer selected precedent")
        made = value
        if flow_id == "handoff.nit":           # the frozen package's version
            frozen = orchestrator.workflow(need.load(context.conn, case_id))["frozen"]
            made = {**value, "package_version": frozen["at_version"]}
        override = (request.get("options") or {}).get("skip_questions")
        revised = None
        if artifact:
            revised = ({**made, "summary": f"{made['summary']} (revised)"} if "summary" in made
                       else {**made, "rev": 2})
        arts = {artifact: revised} if artifact and override else (
            {artifact: made} if artifact else {})
        return Outcome(case_id=case_id, status="ok", artifacts=arts,
                       result={"reason": f"scripted {flow_id}", "candidates": [], "refs": {}})
    return handle


@pytest.fixture()
def flows(monkeypatch):
    table = playbooks.registry()
    for fid, (artifact, value) in SCRIPT.items():
        table[fid] = dataclasses.replace(table[fid], handler=_scripted(fid, artifact, value),
                                         result_schema="flow.nam.route")
    monkeypatch.setattr("nam_agent.nam.playbooks.registry", lambda: table)
    return table


def _valid(body):
    name = "error" if "error" in body else "response"
    assert schema.validate(body, schema.load(name)) == [], body
    return body


def _flow(c, case_id, flow_id, caller=ALL, **extra):
    # the officer names the precedent: a selection nobody named is refused
    text = "use PUR-4 as the precedent" if flow_id == "precedent.select" else ""
    if flow_id.startswith("estimate."):
        text = "the rate is 450"          # the officer's number the scripted line cites
    return _valid(harness.run(ask(text, system=f"run {flow_id}", case_id=case_id, **extra), c,
                              caller))


def _confirm(c, case_id, action_id, caller, **extra):
    return _valid(harness.run(ask("", system="confirm", case_id=case_id,
                                  confirm={"action_id": action_id, **extra}), c, caller))


def _opened(c):
    first = _valid(harness.run(ask("i need to procure gaskets"), c, ALL))
    _valid(harness.run(ask("", case_id=first["case_id"], answer=VARIANT), c, ALL))
    second = _valid(classified(c, first["case_id"], ALL))
    assert second["stage"] == "S2", second
    return first["case_id"]


def _later_flows(flows, stage):
    idx = [s.id for s in orchestrator.STAGES].index(stage)
    later = {s.id for s in orchestrator.STAGES[idx + 1:]}
    return [f for f in flows.values() if "*" not in f.stages and set(f.stages) <= later]


# -- the whole case, and every skip refused on the way -------------------------

WALK = [  # stage, the flow that does its work, then (kind, confirming role) to leave it
    ("S2", "precedent.select", []),
    ("S3", "quantity.finalise", [("gate:quantity", "U1")]),
    ("S4", "estimate.goods", [("gate:estimate", "U1")]),
    # S5 and S6 are optional (the PR follows the estimate): the case is at S7,
    # and running their flow takes it back to them
    ("S7", "docs.generate", [("gate:clauses", "U4"), ("gate:documents", "U4")]),
    ("S7", "validate", []),
    ("S6", "package.freeze", [("freeze_package", "U5")]),
    ("S7", "pr.build", [("submit_pr", "U3")]),
    ("S8", "approval", [("approve", "U7")]),
    ("S10", "handoff.nit", [("handoff_nit", "U6")]),
]


def test_a_case_goes_from_s1_to_s10_only_through_its_gates(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _opened(c)
    for stage, flow_id, confirms in WALK:
        body = _flow(c, case_id, "case.status")
        assert body["stage"] == stage, (flow_id, body["result"])
        for ahead in _later_flows(flows, stage):          # nothing can be skipped
            refused = _flow(c, case_id, ahead.id)
            assert refused["error"]["code"] == "stage_gate", (stage, ahead.id, refused)
            assert any(d.startswith(("missing: ", "pending: "))
                       for d in refused["error"]["details"])
        body = _flow(c, case_id, flow_id)
        for kind, role in confirms:
            assert body["status"] == "needs_approval", (flow_id, body)
            pending = body["pending_action"]
            assert pending["kind"] == kind and role in pending["allowed_roles"]
            body = _confirm(c, case_id, pending["id"], ROLE[role])
            assert body["result"]["decision"] == "approve" and not body["result"]["replay"]
            if kind == "submit_pr":
                assert body["result"]["result"]["pr_number"].startswith("PRX-")
    status = _flow(c, case_id, "case.status")["result"]
    assert status["actions"] == ["approve", "freeze_package", "handoff_nit", "submit_pr"]
    assert status["gates"] == ["clauses", "documents", "estimate", "quantity"]
    assert status["pending_action"] is None and status["missing"] == []
    closed = _flow(c, case_id, "docs.generate")
    assert closed["error"]["code"] == "stage_gate" and "closed" in closed["error"]["message"]


def test_the_pr_follows_the_approved_estimate_with_no_documents(tmp_path, flows):
    # PR Creation Flow: Stage 2's locked estimate, then Stage 3's PR. S5 and S6
    # are passed through while their work is not started
    c = ctx(tmp_path)
    case_id = _opened(c)
    for flow_id, confirms in (("precedent.select", []),
                              ("quantity.finalise", ["gate:quantity"]),
                              ("estimate.goods", ["gate:estimate"])):
        body = _flow(c, case_id, flow_id)
        for _kind in confirms:
            body = _confirm(c, case_id, body["pending_action"]["id"], ROLE["U1"])
    assert _flow(c, case_id, "case.status")["stage"] == "S7"
    body = _flow(c, case_id, "pr.build")
    assert body["pending_action"]["kind"] == "submit_pr"
    rec = need.load(c.conn, case_id)
    assert orchestrator.skipped(orchestrator.workflow(rec), orchestrator.BY_ID["S5"])
    assert not orchestrator._artifact(orchestrator.workflow(rec), "documents")


def test_a_new_case_cannot_start_past_s1(tmp_path, flows):
    body = _valid(harness.run(ask("", system="run estimate.goods"), ctx(tmp_path), ALL))
    assert body["error"]["code"] in ("stage_gate", "bad_request")


# -- confirmations ------------------------------------------------------------------

def _at_quantity_gate(c):
    case_id = _opened(c)
    _flow(c, case_id, "precedent.select")
    body = _flow(c, case_id, "quantity.finalise")
    assert body["pending_action"]["kind"] == "gate:quantity"
    return case_id, body


@pytest.mark.parametrize("role", ["U2", "U3", "U4", "U5", "U6", "U7"])
def test_only_an_allowed_role_passes_a_gate(tmp_path, flows, role):
    c = ctx(tmp_path)
    case_id, body = _at_quantity_gate(c)
    version = body["case_version"]
    refused = _confirm(c, case_id, body["pending_action"]["id"], ROLE[role])
    assert refused["error"]["code"] == "forbidden_role"
    assert store.version_of(c.conn, case_id) == version          # nothing changed


def test_an_irreversible_action_needs_its_role(tmp_path, flows):
    c = ctx(tmp_path)
    case_id, body = _at_quantity_gate(c)
    body = _confirm(c, case_id, body["pending_action"]["id"], ROLE["U1"])
    body = _flow(c, case_id, "estimate.goods")
    body = _confirm(c, case_id, body["pending_action"]["id"], ROLE["U1"])
    body = _flow(c, case_id, "docs.generate")
    for _ in range(2):
        body = _confirm(c, case_id, body["pending_action"]["id"], ROLE["U4"])
    _flow(c, case_id, "validate")
    body = _flow(c, case_id, "package.freeze")
    assert body["pending_action"]["kind"] == "freeze_package"
    for role in ("U1", "U3", "U4", "U6", "U7"):
        refused = _confirm(c, case_id, body["pending_action"]["id"], ROLE[role])
        assert refused["error"]["code"] == "forbidden_action", role
    assert "freeze_package" not in orchestrator.workflow(need.load(c.conn, case_id))["actions"]


def test_a_confirmation_is_run_once_and_replayed_after(tmp_path, flows):
    c = ctx(tmp_path)
    case_id, body = _at_quantity_gate(c)
    action_id = body["pending_action"]["id"]
    first = _confirm(c, case_id, action_id, ROLE["U1"])
    version = first["case_version"]
    again = _confirm(c, case_id, action_id, ROLE["U1"])
    assert again["result"]["replay"] and again["warnings"]
    assert again["result"]["at"] == first["result"]["at"]
    assert store.version_of(c.conn, case_id) == version


def test_a_proposal_goes_stale_when_the_case_changes(tmp_path, flows):
    c = ctx(tmp_path)
    case_id, body = _at_quantity_gate(c)
    old = body["pending_action"]["id"]
    changed = _flow(c, case_id, "quantity.finalise", options={"skip_questions": True})
    new = changed["pending_action"]["id"]
    assert new != old and changed["pending_action"]["kind"] == "gate:quantity"
    refused = _confirm(c, case_id, old, ROLE["U1"])
    assert refused["error"]["code"] == "stale_version" and refused["error"]["retryable"]
    assert _confirm(c, case_id, new, ROLE["U1"])["stage"] == "S4"


def test_a_change_made_outside_the_harness_also_stales_the_proposal(tmp_path, flows):
    c = ctx(tmp_path)
    case_id, body = _at_quantity_gate(c)
    record = need.load(c.conn, case_id)
    record.location = "Plant 2"                       # e.g. the dashboard's update route
    need.save(c.conn, record, reason="dashboard: officer set fields")
    refused = _confirm(c, case_id, body["pending_action"]["id"], ROLE["U1"])
    assert refused["error"]["code"] == "stale_version"


def test_a_rejection_needs_a_reason_and_holds_the_stage(tmp_path, flows):
    c = ctx(tmp_path)
    case_id, body = _at_quantity_gate(c)
    action_id = body["pending_action"]["id"]
    no_reason = _confirm(c, case_id, action_id, ROLE["U1"], decision="reject")
    assert no_reason["error"]["code"] == "bad_request"
    rejected = _confirm(c, case_id, action_id, ROLE["U1"], decision="reject",
                        reason="quantity too high for one year")
    assert rejected["stage"] == "S3" and rejected["pending_action"] is None
    assert rejected["result"]["decision"] == "reject"
    # not proposed again until the case changes
    assert _flow(c, case_id, "case.status")["pending_action"] is None
    redone = _flow(c, case_id, "quantity.finalise", options={"skip_questions": True})
    assert redone["pending_action"]["kind"] == "gate:quantity"


def test_confirming_needs_a_case_and_a_pending_action(tmp_path, flows):
    c = ctx(tmp_path)
    no_case = _valid(harness.run(ask("", system="confirm",
                                     confirm={"action_id": "act_0000abcd"}), c, ALL))
    assert no_case["error"]["code"] == "bad_request"
    case_id = _opened(c)
    nothing = _confirm(c, case_id, "act_0000abcd", ALL)
    assert nothing["error"]["code"] == "not_found"


def test_a_flow_cannot_propose_an_action_or_write_anothers_artifact(tmp_path, flows,
                                                                    monkeypatch):
    c = ctx(tmp_path)
    case_id, _ = _at_quantity_gate(c)
    _confirm(c, case_id, _flow(c, case_id, "case.status")["pending_action"]["id"], ROLE["U1"])

    def sneaky(request, context):
        return Outcome(case_id=request["case_id"], status="ok",
                       result={"reason": "x", "candidates": [], "refs": {}},
                       artifacts={"package": {"summary": "frozen already"}})

    flows["estimate.goods"] = dataclasses.replace(flows["estimate.goods"], handler=sneaky)
    body = _flow(c, case_id, "estimate.goods")
    assert body["error"]["code"] == "internal"
    assert "package" not in orchestrator.workflow(need.load(c.conn, case_id))["artifacts"]


# -- going back, the freeze, restart, concurrency ------------------------------------

def test_rework_goes_back_and_the_approvals_are_given_again(tmp_path, flows):
    c = ctx(tmp_path)
    case_id, body = _at_quantity_gate(c)
    _confirm(c, case_id, body["pending_action"]["id"], ROLE["U1"])
    assert _flow(c, case_id, "case.status")["stage"] == "S4"
    back = _flow(c, case_id, "quantity.finalise", options={"skip_questions": True})
    assert back["stage"] == "S3" and back["pending_action"]["kind"] == "gate:quantity"
    history = harness.history(c, ALL, case_id)["entries"]
    assert any("back to S3" in e["reason"] and "quantity" in e["reason"] for e in history)


def _frozen(c):
    case_id, body = _at_quantity_gate(c)
    _confirm(c, case_id, body["pending_action"]["id"], ROLE["U1"])
    body = _flow(c, case_id, "estimate.goods")
    _confirm(c, case_id, body["pending_action"]["id"], ROLE["U1"])
    body = _flow(c, case_id, "docs.generate")
    body = _confirm(c, case_id, body["pending_action"]["id"], ROLE["U4"])
    _confirm(c, case_id, body["pending_action"]["id"], ROLE["U4"])
    _flow(c, case_id, "validate")
    body = _flow(c, case_id, "package.freeze")
    body = _confirm(c, case_id, body["pending_action"]["id"], ROLE["U5"])
    assert body["stage"] == "S7"
    return case_id


def test_after_the_freeze_nothing_goes_back(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _frozen(c)
    for flow_id in ("quantity.finalise", "docs.generate", "case.edit", "need.capture"):
        refused = _flow(c, case_id, flow_id)
        assert refused["error"]["code"] == "stage_gate" and "frozen" in \
            refused["error"]["message"], flow_id
    assert _flow(c, case_id, "case.status")["stage"] == "S7"


def test_a_case_resumes_after_a_restart(tmp_path, flows):
    db = tmp_path / "state.db"

    def fresh():
        return harness.Context(conn=sqlite3.connect(db), home=tmp_path, kg_url="http://kg",
                               fetch=fake_kg())

    c = fresh()
    case_id, body = _at_quantity_gate(c)
    action_id = body["pending_action"]["id"]
    c.conn.close()
    after = fresh()                                   # a new process, the same database
    assert _flow(after, case_id, "case.status")["pending_action"]["id"] == action_id
    assert _confirm(after, case_id, action_id, ROLE["U1"])["stage"] == "S4"


def test_two_confirmations_at_once_run_the_action_once(tmp_path, flows):
    db = tmp_path / "state.db"

    def fresh():
        return harness.Context(conn=sqlite3.connect(db, timeout=30, check_same_thread=False),
                               home=tmp_path, kg_url="http://kg", fetch=fake_kg())

    c = fresh()
    case_id = _frozen(c)
    body = _flow(c, case_id, "pr.build")
    action_id = body["pending_action"]["id"]
    replies, barrier = [], threading.Barrier(4)

    def confirm_it():
        mine = fresh()
        barrier.wait()
        replies.append(_confirm(mine, case_id, action_id, ROLE["U3"]))

    threads = [threading.Thread(target=confirm_it) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ran = [r for r in replies if "result" in r and not r["result"]["replay"]]
    assert len(ran) == 1, replies
    assert all("result" in r or r["error"]["code"] == "stale_version" for r in replies)
    drawn = c.conn.execute("SELECT value FROM nam_counters WHERE name LIKE 'pr:%'").fetchone()[0]
    assert drawn == 1
    wf = orchestrator.workflow(need.load(c.conn, case_id))
    assert wf["actions"]["submit_pr"]["result"] == ran[0]["result"]["result"]


def test_a_confirmation_that_died_can_be_retried_after_its_claim_expires(tmp_path, flows):
    c = ctx(tmp_path)
    case_id, body = _at_quantity_gate(c)
    action_id = body["pending_action"]["id"]
    record = need.load(c.conn, case_id)
    wf = orchestrator.workflow(record)
    old = (datetime.now(UTC) - timedelta(seconds=orchestrator.CLAIM_TIMEOUT_S + 5)).isoformat()
    wf["pending"] = {**wf["pending"], "claim": {"at": old, "version": record.version + 1}}
    record.workflow = wf
    need.save(c.conn, record, reason="confirming gate:quantity")      # then the process died
    assert _confirm(c, case_id, action_id, ROLE["U1"])["stage"] == "S4"


def test_a_live_claim_blocks_a_second_confirmation(tmp_path, flows):
    c = ctx(tmp_path)
    case_id, body = _at_quantity_gate(c)
    record = need.load(c.conn, case_id)
    wf = orchestrator.workflow(record)
    wf["pending"] = {**wf["pending"], "claim": {"at": datetime.now(UTC).isoformat(),
                                                "version": record.version + 1}}
    record.workflow = wf
    need.save(c.conn, record, reason="confirming gate:quantity")
    refused = _confirm(c, case_id, body["pending_action"]["id"], ROLE["U1"])
    assert refused["error"]["code"] == "stale_version"
    # and a run in between leaves the claimed proposal alone
    assert _flow(c, case_id, "case.status")["pending_action"]["id"] == \
        body["pending_action"]["id"]


# -- what the record shows ---------------------------------------------------------

def test_the_record_shows_the_stage_machine(tmp_path, flows):
    c = ctx(tmp_path)
    case_id, body = _at_quantity_gate(c)
    record = harness.get_case(c, ALL, case_id)
    assert schema.validate(record, schema.load("record")) == []
    assert record["stage"] == "S3" and record["pending_action"]["id"] == \
        body["pending_action"]["id"]
    assert record["missing"] == ["the quantity approval is not given"]


def test_an_effect_that_ran_is_not_run_again_on_retry(tmp_path, flows, monkeypatch):
    # the PR was drawn, then saving the record failed: the retry reuses it
    c = ctx(tmp_path)
    case_id = _frozen(c)
    action_id = _flow(c, case_id, "pr.build")["pending_action"]["id"]
    real_save, calls = need.save, {"n": 0}

    def flaky(conn, record, session_id="", reason=""):
        if reason.startswith("approve submit_pr") and not calls["n"]:
            calls["n"] += 1
            raise OSError("disk full")
        return real_save(conn, record, session_id, reason)

    monkeypatch.setattr("nam_agent.nam.need.save", flaky)
    assert _confirm(c, case_id, action_id, ROLE["U3"])["error"]["code"] == "internal"
    monkeypatch.setattr(orchestrator, "CLAIM_TIMEOUT_S", -1)     # the claim is dead
    body = _confirm(c, case_id, action_id, ROLE["U3"])
    assert body["result"]["result"]["pr_number"].endswith("-0001")
    drawn = c.conn.execute("SELECT value FROM nam_counters WHERE name LIKE 'pr:%'").fetchone()[0]
    assert drawn == 1


def test_a_replay_still_needs_the_role(tmp_path, flows):
    c = ctx(tmp_path)
    case_id, body = _at_quantity_gate(c)
    action_id = body["pending_action"]["id"]
    _confirm(c, case_id, action_id, ROLE["U1"])
    assert _confirm(c, case_id, action_id, ROLE["U7"])["error"]["code"] == "forbidden_role"


def test_a_gate_is_confirmed_over_http(tmp_path, flows):
    from nam_agent.nam import api
    from nam_agent.client import NamCallError, NamClient

    db = tmp_path / "state.db"

    def make_ctx():
        conn = sqlite3.connect(db, timeout=30)
        return harness.Context(conn=conn, home=tmp_path, kg_url="http://kg",
                               fetch=fake_kg()), conn.close

    srv = api.serve("127.0.0.1", 0, make_ctx, max_body=1 << 20, model_configured=False,
                    kg_url="")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        officer = NamClient(base, tenant="nrl", user="u1", roles=["U1"])
        first = officer.run("Assess need", [{"role": "user", "content": "i need to procure gaskets"}])
        case_id = first["case_id"]
        officer.run("Assess need", [], case_id=case_id, answer=VARIANT)
        for q, value in (("procurement_type", "OPEX"), ("tender_type", "Open")):
            officer.run("Confirm the classification", [], case_id=case_id,
                        answer={"question_id": q, "value": value})
        officer.run("run precedent.select", [{"role": "user", "content": "PUR-4"}],
                    case_id=case_id)
        reply = officer.run("run quantity.finalise", [], case_id=case_id)
        assert reply["status"] == "needs_approval"
        drafter = NamClient(base, tenant="nrl", user="d1", roles=["U4"])
        with pytest.raises(NamCallError) as exc:
            drafter.confirm("confirm", case_id, reply["pending_action"]["id"])
        assert exc.value.status == 403
        done = officer.confirm("confirm", case_id, reply["pending_action"]["id"])
        assert done["stage"] == "S4" and done["flow"] == "nam.confirm"
    finally:
        srv.shutdown()
        srv.server_close()
