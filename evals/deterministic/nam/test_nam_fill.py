"""DETERMINISTIC EVAL — the chat beside a case fills what the harness needs.

A Goods case is walked through the real harness (nam_fakes, no model) and
the officer's chat messages go through the driver with the case open
(drive.turn case_id=...): "what next" asks the next missing item with its
options, a click or a typed value is applied by the harness's own flow, and
the case shows the change. Nothing is applied from a model's words.
"""

from __future__ import annotations

import sqlite3

import pytest
from nam_fakes import ask, fake_kg

import nam_agent.nam as harness
from nam_agent.nam import drive, fill, orchestrator
from nam_agent.nam import need as nam

ROLES = ("U1", "U2", "U3", "U4", "U5", "U6", "U7")


@pytest.fixture()
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(nam, "_get", fake_kg())
    conn = sqlite3.connect(":memory:")
    ctx = harness.Context(conn=conn, home=tmp_path, kg_url="http://kg", fetch=fake_kg())
    # the chat acts in the "local" tenant (fill.CALLER), as the screens do
    who = {r: harness.Caller(tenant="local", user=f"user-{r.lower()}", roles=(r,))
           for r in ROLES}

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
    call("Assess need for request #123", case_id,
         answer={"question_id": first["questions"][0]["id"], "value": "ring joint"})
    awaiting: dict = {}

    def say(message):
        return drive.turn(conn, tmp_path, "http://kg", "screen-chat", message,
                          fetch=fake_kg(), awaiting=awaiting, case_id=case_id)

    return {"conn": conn, "id": case_id, "call": call, "say": say,
            "load": lambda: nam.load(conn, case_id)}


def test_what_next_asks_the_classification_with_options_and_a_click_fills_it(case):
    out = case["say"]("what next")
    assert out["ask"]["field"] == "procurement_type"
    assert set(nam.PROCUREMENT_TYPES) <= {o.split(" (")[0] for o in out["ask"]["options"]}
    out = case["say"](nam.PROCUREMENT_TYPES[1])
    assert case["load"]().procurement_type["value"] == nam.PROCUREMENT_TYPES[1]
    assert out["ask"]["field"] == "tender_type"                 # the next, asked at once
    out = case["say"]("not one of them")                         # not an answer: the model's
    assert out is None
    out = case["say"]("what's next")
    out = case["say"](nam.TENDER_TYPES[0])
    # classified: the stage machine moved on, and the next thing is asked at once
    assert not case["load"]().unresolved and out["ask"]["field"] == "precedent"


def _at_estimate(case, rate=None):
    c = case
    for field, value in (("procurement_type", "OPEX"), ("tender_type", "Open")):
        c["say"]("what next")
        c["say"](value)
    c["call"]("Select the precedent", c["id"], text="use PUR-4")
    c["call"]("Finalise the quantity", c["id"])
    c["call"]("Prepare the estimate", c["id"], approve=False)


def test_a_rate_typed_in_the_chat_reaches_the_estimate(case):
    _at_estimate(case)
    out = case["say"]("line 1 rate 9999")
    line = orchestrator._artifact(orchestrator.workflow(case["load"]()), "estimate")["lines"][0]
    assert line["rate"] == 9999.0 and "rate 9999" in out["reply"]
    # now flagged: the chat asks for the decision, with Accept and Revise
    assert out["ask"]["field"] == "decision:0"
    assert out["ask"]["options"] == ["Accept line 1", "Revise line 1"]
    out = case["say"]("Accept line 1")
    est = orchestrator._artifact(orchestrator.workflow(case["load"]()), "estimate")
    assert est["lines"][0]["decision"]["value"] == "accept"
    assert orchestrator.undecided(est) == [] and "accepted" in out["reply"]


def test_a_decision_on_a_line_with_no_flag_is_refused_not_faked(case):
    _at_estimate(case)
    out = case["say"]("accept line 1")
    assert out["reply"].startswith("Not taken") and "no flag" in out["reply"]
    out = case["say"]("line 7 rate 10")
    assert "no line 7" in out["reply"]


def test_other_words_are_the_models_and_the_precedent_comes_after_classifying(case):
    assert case["say"]("tell me about spiral wound gaskets") is None
    for value in ("OPEX", "Open"):
        case["say"]("what next")
        case["say"](value)
    assert [x["kind"] for x in fill.needs(case["load"]())] == ["precedent"]


def test_the_needs_follow_the_pr_flows_order(case):
    kinds = [x["kind"] for x in fill.needs(case["load"]())]
    assert kinds[:2] == ["classify", "classify"]


def test_what_next_with_a_capture_question_open_asks_it_again(tmp_path, monkeypatch):
    monkeypatch.setattr(nam, "_get", fake_kg())
    conn, awaiting = sqlite3.connect(":memory:"), {}
    first = drive.turn(conn, tmp_path, "http://kg", "chat-9", "i need gaskets", fetch=fake_kg(),
                       awaiting=awaiting)
    case_id = first["tool_calls"][0]["output"].split()[1]
    again = drive.turn(conn, tmp_path, "http://kg", "screen", "what next", fetch=fake_kg(),
                       awaiting=awaiting, case_id=case_id)
    assert again["ask"]["question"] == first["ask"]["question"]
    assert again["ask"]["options"] == first["ask"]["options"]


def test_a_turn_the_harness_takes_lights_the_nam_box(tmp_path, monkeypatch):
    # the architecture diagram's "nam" stage (diagram.js STAGE) is fed by this event
    from evals.helpers import ScriptedClient, make_agent

    monkeypatch.setattr(nam, "_get", fake_kg())
    nam_agent = make_agent(tmp_path, client=ScriptedClient([]), kg_url="http://kg",
                     nam_chat="driver")
    seen = []
    nam_agent.respond("i need gaskets", observer=lambda kind, ev: seen.append((kind, ev)))
    assert ("nam", {"by": "driver", "step": "nam_capture_need"}) in seen


def test_the_chat_alone_takes_a_case_from_capture_to_the_submitted_pr(case):
    # the officer's own words only: "next", a click on an option, a value typed;
    # every step is the harness's (precedent, quantity, estimate, PR, approvals)
    out = case["say"]("what next")
    said, steps = [], 0
    while out and steps < 30:
        steps += 1
        ask = out.get("ask")
        said.append(out["reply"])
        if not ask:
            break
        options = ask["options"]
        if ask["field"] == "precedent":
            assert options[-1] == "None of these fit" and options[0].startswith("PUR-4")
        if options:
            value = options[0]
        elif "Cost centre" in ask["question"]:
            value = "4100"
        elif "Plant" in ask["question"]:
            value = "NR01"
        elif "Purchasing group" in ask["question"]:
            value = "P12"
        else:
            value = "worn gaskets need replacing"
        out = case["say"](value)
    wf = orchestrator.workflow(case["load"]())
    assert "submit_pr" in wf["actions"] and wf["stage"] == "S8"
    assert {"quantity", "estimate"} <= set(wf["gates"])
    assert "NAM is done" in said[-1] and "NIT" not in "".join(said)
    assert any("Approve it?" in s for s in said)


def test_ok_runs_the_next_harness_step_and_asks_its_approval(case):
    for value in ("OPEX", "Open"):
        case["say"]("what next")
        case["say"](value)
    case["say"]("what next")
    case["say"]("PUR-4: Supply of Ring Joint Gaskets")
    wf = orchestrator.workflow(case["load"]())
    assert wf["stage"] == "S3" and wf["pending"]["kind"] == "gate:quantity"
    out = case["say"]("yes")                                  # "yes" approves what is asked
    assert out["reply"].startswith("Approved.")
    assert "quantity" in orchestrator.workflow(case["load"]())["gates"]


def test_next_on_a_rate_only_the_officer_can_give_asks_it_again(case):
    # live, 2026-11-03: "next" on "What is the rate of line 1?" replied only
    # "Not taken: give the rate as a number.", turn after turn
    _at_estimate(case)
    est = orchestrator._artifact(orchestrator.workflow(case["load"]()), "estimate")
    est["lines"][0]["rate"] = None
    n = case["load"]()
    wf = dict(n.workflow)
    wf["artifacts"] = {**wf["artifacts"], "estimate": {**wf["artifacts"]["estimate"],
                                                       "value": est}}
    n.workflow = wf
    nam.save(case["conn"], n)
    case["say"]("what next")
    out = case["say"]("next")
    assert out["reply"].startswith("This needs your answer") and "rate of line 1" in out["reply"]
    assert out["ask"]["field"] == "rate:0"
