"""The feedback loop (verify.py, llm.checked): every step is checked against
the case as stored before its reply goes out, a step that is not done is
never reported done, a rejected model answer goes back with its reason, and
the whole case is verified again before the freeze and the handoff. Each
test tries to make a step end falsely."""

import dataclasses
import sqlite3

import pytest
from nam_fakes import ctx
from nam_walk import CHECKER, call, confirm, goods_at_s4, goods_validated
from test_nam_guardrails import DEFAULT, _at_estimate, _flow, _script, scripted_registry

from nam_agent.nam import audit, llm, need, orchestrator, verify
from nam_agent.nam.docs import store as docs
from nam_agent.nam.playbooks import Outcome

SCRIPTED = {"reason": "scripted", "candidates": [], "refs": {}}


@pytest.fixture()
def flows(monkeypatch):
    """Every flow scripted, as in test_nam_guardrails.py."""
    return scripted_registry(monkeypatch)


def _checks(body):
    return {c["name"]: c["ok"] for c in body["verification"]["checks"]}


def test_every_reply_carries_its_verification_and_what_the_stage_needs(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    body = call(c, case_id, "Goods estimate")
    v = body["verification"]
    assert v["passed"] and body["status"] == "needs_approval"
    assert {"recorded", "schema:estimate", "cited:estimate", "stored:estimate",
            "estimate_priced", "estimate_current"} <= set(_checks(body))
    assert v["stage_needs"] == ["the estimate approval is not given"]
    done = confirm(c, case_id, body)
    assert done["verification"]["passed"] and _checks(done) == {"recorded": True}


def test_an_estimate_that_says_ok_with_a_new_item_is_not_done(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    unpriced = {**DEFAULT["estimate.goods"][1], "lines": [
        {"item": "ring joint gasket", "qty": 20, "uom": "nos", "material_code": "MC-GSK-1",
         "rate": None, "basis": "new_item"}]}
    _script(flows, "estimate.goods", "estimate", unpriced)
    body = _flow(c, case_id, "estimate.goods")
    assert body["status"] == "needs_input" and body["pending_action"] is None
    assert _checks(body)["estimate_priced"] is False and not body["verification"]["passed"]
    assert "estimate.goods" in body["next"]
    assert any(w.startswith("Not verified (estimate_priced)") for w in body["warnings"])


def test_a_pr_step_that_says_ok_without_its_pr_is_not_done(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    _script(flows, "estimate.goods", *DEFAULT["estimate.goods"])
    # a PR flow that claims ok and builds nothing, let in at S4 to try it
    flows["pr.build"] = dataclasses.replace(
        flows["pr.build"], stages=(*flows["pr.build"].stages, "S4"),
        handler=lambda request, context: Outcome(case_id=request["case_id"], status="ok",
                                                 result=SCRIPTED))
    body = _flow(c, case_id, "pr.build")
    assert body["status"] == "needs_input" and _checks(body)["pr_complete"] is False


def test_a_step_that_needs_input_but_says_nothing_fails_verification(tmp_path, flows):
    c = ctx(tmp_path)
    case_id = _at_estimate(c)
    flows["estimate.goods"] = dataclasses.replace(
        flows["estimate.goods"],
        handler=lambda request, context: Outcome(case_id=request["case_id"],
                                                 status="needs_input", result=SCRIPTED))
    body = _flow(c, case_id, "estimate.goods")
    assert _checks(body)["answerable"] is False and not body["verification"]["passed"]


def test_an_artifact_naming_a_document_that_is_not_stored_fails(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_validated(c)
    wf = orchestrator.workflow(need.load(c.conn, case_id))
    from nam_agent.nam import store

    with store.acting(store.Actor(tenant="nrl", user="v5", run_id="run_x")):
        checks = verify._artifact_checks(c.conn, c.home, "nrl", wf, "validation")
        assert all(x["ok"] for x in checks)
        wf["artifacts"]["validation"]["value"]["report"]["version"] = 99
        bad = {x["name"]: x for x in verify._artifact_checks(c.conn, c.home, "nrl", wf,
                                                              "validation")}
    assert not bad["stored"]["ok"] and "not in the document store" in bad["stored"]["says"]


def test_a_changed_document_file_stops_the_freeze(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_validated(c)
    body = call(c, case_id, "Freeze the package", caller=CHECKER)
    assert body["pending_action"]["kind"] == "freeze_package"
    wf = orchestrator.workflow(need.load(c.conn, case_id))
    doc = orchestrator._artifact(wf, "documents")["documents"][1]
    row = docs._version_row(c.conn, doc["doc_id"], doc["version"])
    docs._blob(c.home, "nrl", row["sha256"]).write_bytes(b"changed on disk")
    refused = confirm_raw(c, case_id, body, CHECKER)
    assert refused["error"]["code"] == "guardrail"
    said = " ".join(refused["error"]["details"])
    assert "verification" in said and "does not match its hash" in said
    assert "run docs.generate" in said


def confirm_raw(c, case_id, body, caller):
    import nam_agent.nam as harness

    return harness.run({"system": "confirm", "messages": [{"role": "user", "content": ""}],
                        "case_id": case_id, "confirm": {"action_id": body["pending_action"]["id"]}},
                       c, caller)


def test_the_case_check_finds_an_artifact_older_than_its_input(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_validated(c)
    rec = need.load(c.conn, case_id)
    wf = orchestrator.workflow(rec)
    wf["artifacts"]["estimate"]["at_version"] = 1          # older than the quantity
    rec.workflow = wf
    from nam_agent.nam import store

    with store.acting(store.Actor(tenant="nrl", user="v5", run_id="run_x")):
        problems = verify.case(c.conn, c.home, rec)
    assert any("estimate is older than the quantity" in p and "run estimate.goods" in p
               for p in problems)


# -- the model's answers: checked, sent back with the reason, then given up --------

class _Ctx:
    def __init__(self, conn):
        self.conn = conn


def _asker(conn, answers):
    sent = []

    def ask(system, user):
        sent.append(user)
        return answers[len(sent) - 1]

    ask.ctx, ask.task, ask.last = _Ctx(conn), "route", "m"
    return ask, sent


def _pick(text):
    if text not in ("a", "b"):
        raise llm.Rejected(f"'{text}' is not a or b")
    return text


def test_a_rejected_answer_goes_back_with_its_reason_and_the_next_is_used(tmp_path):
    conn = sqlite3.connect(":memory:")
    ask, sent = _asker(conn, ["zz", "b"])
    assert llm.checked(ask, "sys", "pick", _pick) == "b"
    assert len(sent) == 2 and "'zz' is not a or b" in sent[1]
    rows = audit.events(conn, kind="model")
    assert [(r["error_code"], r["detail"]["outcome"]) for r in rows] == [("rejected",
                                                                          "rejected")]


def test_every_answer_rejected_ends_in_rejected_after_the_retries(tmp_path):
    conn = sqlite3.connect(":memory:")
    ask, sent = _asker(conn, ["x", "y", "z", "a"])
    with pytest.raises(llm.Rejected, match="'z' is not a or b"):
        llm.checked(ask, "sys", "pick", _pick)
    assert len(sent) == 1 + llm.RETRIES
    assert len(audit.events(conn, kind="model")) == 1 + llm.RETRIES


def test_an_outage_is_not_retried_by_the_check():
    def down(system, user):
        raise llm.ModelUnavailable("route: m: timeout")

    with pytest.raises(llm.ModelUnavailable):
        llm.checked(down, "sys", "pick", _pick)


def test_every_flow_with_an_artifact_has_its_done_check():
    from nam_agent.nam import playbooks

    writers = {f for flows_ in orchestrator.ARTIFACTS.values() for f in flows_}
    exempt = {"need.replicate", "need.enrich", "material.suggest", "precedent.search",
              "document.update"}     # their artifact is a record of what was shown or done
    built = {f.id for f in playbooks.registry().values() if f.handler is not None}
    assert (writers & built) - exempt <= set(verify.DONE), (writers - exempt) - set(verify.DONE)
