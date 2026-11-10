"""DETERMINISTIC EVAL — the agent knows where a case is on the PR journey.

The PR Creation Flow (Need Identification -> Estimate creation -> PR
Creation) is laid over the harness's stages in policy/journey.json, and
nam_agent/nam/journey.py reads every step's state from the case itself. A Goods
case is driven through the real harness (nam_fakes, no model) and at each
point the journey must say what was done, the step now, what the user still
has to give and what comes next. The same facts reach case.status, the
workflow view, the chat's system prompt and the driver's reply.
"""

from __future__ import annotations

import sqlite3

import pytest
from nam_fakes import ask, classified, fake_kg

import nam_agent.nam as harness
from evals.helpers import ScriptedClient, make_agent, response, text_block
from nam_agent.nam import drive, edits, journey
from nam_agent.nam import need as nam

ROLES = ("U1", "U2", "U3", "U4", "U5", "U6", "U7")


@pytest.fixture()
def case(tmp_path):
    ctx = harness.Context(conn=sqlite3.connect(":memory:"), home=tmp_path, kg_url="http://kg",
                          fetch=fake_kg())
    who = {r: harness.Caller(tenant="nrl", user=f"user-{r.lower()}", roles=(r,)) for r in ROLES}

    def call(system, case_id=None, role="U1", approve=True, **extra):
        reply = harness.run(ask(extra.pop("text", ""), system=system,
                                **({"case_id": case_id} if case_id else {}), **extra),
                            ctx, who[role])
        while approve and reply.get("status") == "needs_approval":
            act = reply["pending_action"]
            reply = harness.run(ask("", system="Approve the pending step", case_id=case_id,
                                    confirm={"action_id": act["id"], "decision": "approve"}),
                                ctx, who[act["allowed_roles"][0]])
        return reply

    first = call("Assess need for request #123", text="i need to procure gaskets")
    case_id = first["case_id"]

    def now():
        return journey.build(nam.load(ctx.conn, case_id))

    return {"ctx": ctx, "id": case_id, "call": call, "who": who, "first": first, "now": now}


def _step(j, sid):
    return next(s for s in j["steps"] if s["id"] == sid)


def test_the_policy_names_only_what_the_harness_has():
    assert journey.problems() == []
    bad = {"phases": [{"id": "x", "stages": ["S1", "S99"], "steps": [
        {"id": "z", "stage": "S1", "done_when": ["check:nope", "gate:quantity", "record:x"]},
        {"id": "z", "stage": "S4", "done_when": [["flagged:estimate", "action:submit_pr"]]}]}]}
    found = journey.problems(bad)
    assert any("S99" in p for p in found) and any("check:nope" in p for p in found)
    assert any("record:x" in p for p in found) and any("twice" in p for p in found)
    assert any("not in its phase" in p for p in found)
    assert not any("gate:quantity" in p or "flagged:estimate" in p for p in found)


def test_at_capture_the_open_question_is_the_step_and_what_to_give(case):
    j = case["now"]()
    assert j["stage"]["id"] == "S1" and j["phase"] == "Need Identification"
    assert j["step"] == "1f" and j["previous_step"] == "1e" and j["next_step"] == "1h"
    question = case["first"]["questions"][0]["text"]
    assert any(question in t for t in j["to_give"])
    assert j["next_stage"]["id"] == "S2"
    assert _step(j, "1g")["state"] == "with_stage"           # a behaviour, not a milestone


def test_each_stage_moves_the_steps_and_says_what_comes_next(case):
    c = case
    c["call"]("Assess need for request #123", c["id"],
              answer={"question_id": c["first"]["questions"][0]["id"], "value": "ring joint"})
    j = c["now"]()
    assert j["step"] == "1h" and not any("open question" in t for t in j["to_give"])

    classified(c["ctx"], c["id"], c["who"]["U1"])
    j = c["now"]()
    assert j["stage"]["id"] == "S2" and j["step"] == "1j" and j["next_step"] == "1k"
    s1 = next(s for s in j["stages"] if s["id"] == "S1")
    assert s1["state"] == "done"
    s2 = next(s for s in j["stages"] if s["id"] == "S2")
    assert any("precedent.select" in n["flow_ids"] for n in s2["needs"])

    c["call"]("Select the precedent", c["id"], text="use PUR-4")
    c["call"]("Finalise the quantity", c["id"], approve=False)
    j = c["now"]()
    assert j["stage"]["id"] == "S3" and j["step"] == "1k" and j["previous_step"] == "1j"
    assert j["pending"] and j["pending"]["kind"] == "gate:quantity"
    s3 = next(s for s in j["stages"] if s["id"] == "S3")
    assert any(n["kind"] == "gate" and n["by"] for n in s3["needs"])   # who approves it
    assert j["next_step"] == "2a"

    c["call"]("Finalise the quantity", c["id"])
    c["call"]("Prepare the estimate", c["id"], approve=False)
    j = c["now"]()
    assert j["stage"]["id"] == "S4" and j["phase"] == "Estimate creation"
    assert all(_step(j, s)["state"] == "done" for s in ("1a", "1j", "1k", "2a", "2c", "2d"))
    assert j["step"] == "2f"                    # no flagged line: nothing to decide in 2e
    assert j["next_stage"]["id"] == "S5"
    s3 = next(s for s in j["stages"] if s["id"] == "S3")
    assert s3["produced"] and s3["approved"][0]["name"] == "quantity"

    text = journey.text(j)
    for words in ("Stage now: S4", "Done before:", "Step now: 2f", "Next stage: S5",
                  "Waiting for approval"):
        assert words in text, words


def test_a_flagged_line_is_a_decision_the_user_owes(case):
    c = case
    c["call"]("Assess need for request #123", c["id"],
              answer={"question_id": c["first"]["questions"][0]["id"], "value": "ring joint"})
    classified(c["ctx"], c["id"], c["who"]["U1"])
    c["call"]("Select the precedent", c["id"], text="use PUR-4")
    c["call"]("Finalise the quantity", c["id"])
    c["call"]("Prepare the estimate", c["id"], approve=False)
    need = nam.load(c["ctx"].conn, c["id"])
    edits.apply(need, "rates", {"edits": [{"line": 0, "rate": 9999}]},
                lambda req: c["call"](req["system"], req["case_id"], approve=False,
                                      answer=req["answer"]))
    j = c["now"]()
    assert j["step"] == "2e"
    assert any("flagged AHR" in t for t in j["to_give"])
    # no approval is proposed while the flagged line has no decision
    assert c["now"]()["pending"] is None and c["now"]()["undecided"] == [0]
    need = nam.load(c["ctx"].conn, c["id"])
    edits.apply(need, "rates", {"edits": [{"line": 0, "decision": "accept"}]},
                lambda req: c["call"](req["system"], req["case_id"], approve=False,
                                      answer=req["answer"]))
    assert c["now"]()["undecided"] == []
    c["call"]("Prepare the estimate", c["id"])                # the estimate approval
    j = c["now"]()
    # the PR follows the approved estimate (S5, S6 optional): step 3a is now
    assert _step(j, "2e")["state"] == "done" and j["stage"]["id"] == "S7"
    assert j["phase"] == "PR Creation" and j["step"] == "3a"
    assert "NIT" not in journey.text(j).split("Guide the officer")[0]


def test_case_status_and_the_focus_carry_the_journey(case):
    reply = case["call"]("What is the status of the case", case["id"], approve=False)
    assert reply["flow"] == "case.status"
    got = reply["result"]["journey"]
    assert got["step"] == "1f" and got["next_step"] == "1h" and "Step now: 1f" in got["text"]
    focus = journey.focus(case["ctx"].conn, case["id"], "Need capture")
    assert focus["screen"] == "Need capture" and "Step now: 1f" in focus["journey"]


class _Recording(ScriptedClient):
    def __init__(self, script):
        super().__init__(script)
        self.systems: list[str] = []

    def _create(self, **kwargs):
        self.systems.append(str(kwargs.get("system")))
        return super()._create(**kwargs)


def test_the_chat_knows_the_case_it_opened_and_the_driver_says_whats_next(tmp_path,
                                                                          monkeypatch):
    monkeypatch.setattr(nam, "_get", fake_kg())
    # enough replies for the follow-up writer, the intent reading (which
    # refuses a reply that is not its JSON and asks again) and the answer
    model = _Recording([response([text_block("You are at step 1f.")]) for _ in range(12)])
    nam_agent = make_agent(tmp_path, client=model, kg_url="http://kg", nam_chat="driver")
    nam_agent.respond("i need gaskets")                            # the driver opens the case
    nam_agent.respond("where are we on this need?")                # the model answers
    assert model.systems, "the model was called"
    prompt = model.systems[-1]
    assert "open in this conversation" in prompt and "Step now: 1f" in prompt
    assert "Next step: 1h" in prompt
    assert nam_agent.session.focus is None                         # set for one turn only

    # the driver's own reply names the journey's next step, not the NIT
    out = drive.turn(nam_agent.conn, tmp_path, "http://kg", "chat-x", "i need gaskets",
                     fetch=fake_kg())
    out = drive.turn(nam_agent.conn, tmp_path, "http://kg", "chat-x", "stop asking",
                     fetch=fake_kg())
    assert "NIT" not in out["reply"] and "Next:" in out["reply"]
