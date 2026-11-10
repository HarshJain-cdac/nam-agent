"""case.plan (playbooks/plan.py): the user's query as a to-do list, completed
step by step. Each step runs as its own call would and is verified on the
case; the plan stops at a question, an approval, a step for another role or
a refusal, and "continue" resumes it. One Goods case goes from a sentence to
the NIT handoff with nothing but "continue", answers and approvals."""

from nam_fakes import OFFICER, ctx
from nam_walk import (
    APPROVER,
    BUYER,
    CHECKER,
    COMMERCIAL,
    DEPT,
    DRAFTER,
    approve_documents,
    call,
    goods_at_s4,
    goods_drafted,
)

import nam_agent.nam as harness
from nam_agent.nam import need, orchestrator
from nam_agent.nam.playbooks import plan

BY_ROLE = {"U1": OFFICER, "U2": DEPT, "U3": BUYER, "U4": DRAFTER, "U5": CHECKER,
           "U6": COMMERCIAL, "U7": APPROVER}
ANSWERS = {"variant": "ring joint", "category": "goods", "procurement_type": "OPEX",
           "tender_type": "Open", "precedent": "PUR-4",
           "fields": ["all: delivery_period=8 weeks, delivery_location=NRL stores",
                      "all: cost_centre=4100, plant=NR01, purchasing_group=P12",
                      "all: justification=worn gaskets, delivery_date=2026-12-01"],
           "requirements": "all: apply=yes", "authority": "GM (Materials)",
           "aarohan_ref": "NRL/MAT/2026/77"}


def _items(body):
    return [(i["flow"], i["status"]) for i in body["result"]["todo"]]


def _answer(answers: dict, qid: str):
    value = answers[qid]
    if isinstance(value, list):           # asked more than once: the next one each time
        return value.pop(0) if len(value) > 1 else value[0]
    return value


def drive(c, body, limit=60):
    """Only 'continue': the open question answered, the pending approval
    confirmed by a user of its role, a step for another role continued by
    that role's user."""
    answers = {k: (list(v) if isinstance(v, list) else v) for k, v in ANSWERS.items()}
    case_id, who, steps = body["case_id"], OFFICER, 0
    while not body["result"].get("complete") and steps < limit:
        steps += 1
        if body.get("pending_action"):
            act = body["pending_action"]
            approver = BY_ROLE[act["allowed_roles"][0]]
            done = harness.run({"system": "confirm", "messages": [{"role": "user", "content": ""}],
                                "case_id": case_id, "confirm": {"action_id": act["id"]}},
                               c, approver)
            assert "error" not in done, done
            body = call(c, case_id, "continue", caller=who)
            continue
        nxt = next(i for i in body["result"]["todo"] if i["status"] != "done")
        if nxt["status"] == "blocked" and "forbidden_role" in nxt["why"]:
            who = BY_ROLE[nxt["roles"][0]]
            body = call(c, case_id, "continue", caller=who)
            continue
        assert nxt["status"] == "waiting_input", (nxt, body["warnings"])
        q = body["questions"][0]
        text = "use PUR-4" if q["id"] == "precedent" else ""
        body = call(c, case_id, "continue", text, answer=q["id"], value=_answer(answers, q["id"]),
                    caller=who)
        assert "error" not in body, body
    return body, steps


def test_a_query_becomes_a_todo_list_to_its_goal():
    assert plan.goal_of("Take this need up to the PR") == "S7"
    # NAM ends with the PR (PR Creation Flow): "the whole procurement" is that
    assert plan.goal_of("Complete the whole procurement") == "S7"
    assert plan.goal_of("generate the NIT") is None                # not NAM's
    assert plan.goal_of("Prepare the estimate step by step") == "S4"
    assert plan.goal_of("make a to-do list") is None
    items = plan.make("S7")
    assert [i["flow"] for i in items][:4] == ["need.capture", "need.classify",
                                              "precedent.search", "precedent.select"]
    # the documents and validation are optional: a PR goal leaves them out
    assert [i["flow"] for i in items][4:] == ["quantity.finalise", "estimate", "pr.build",
                                              "pr.submit"]
    assert "docs.generate" in [i["flow"] for i in plan.make("S6")]
    assert items[0]["roles"] and all(i["status"] == "pending" for i in items)


def test_the_plan_runs_until_a_question_and_resumes_with_the_answer(tmp_path):
    c = ctx(tmp_path)
    body = call(c, None, "Complete the whole procurement step by step",
                "i need to procure gaskets")
    assert body["flow"] == "case.plan" and body["status"] == "needs_input"
    assert _items(body)[:2] == [("need.capture", "waiting_input"), ("need.classify", "pending")]
    assert body["questions"][0]["id"] == "variant" and body["verification"]["passed"]
    assert body["result"]["ran"] == [{"flow": "need.capture", "status": "needs_input",
                                      "verified": True}]
    stored = orchestrator._artifact(orchestrator.workflow(need.load(c.conn, body["case_id"])),
                                    "todo")
    assert stored["goal"] == "S7" and len(stored["items"]) == 8
    nxt = call(c, body["case_id"], "continue", answer="variant", value="ring joint")
    assert nxt["flow"] == "case.plan" and nxt["route"]["by"] == "resume"
    assert _items(nxt)[0] == ("need.capture", "done")
    assert nxt["result"]["ran"][0] == {"flow": "need.capture", "status": "ok",
                                       "verified": True}


def test_a_step_for_another_role_stops_the_plan_and_names_the_roles(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_drafted(c)
    approve_documents(c, case_id)
    body = call(c, case_id, "Take this case up to the validation step by step")
    assert body["result"]["goal"] == "S6" and body["status"] == "needs_input"
    blocked = next(i for i in body["result"]["todo"] if i["status"] != "done")
    assert blocked["flow"] == "validate" and blocked["status"] == "blocked"
    assert "forbidden_role" in blocked["why"] and blocked["roles"] == ["U3", "U5"]
    assert body["result"]["ran"] == [{"flow": "validate", "status": "forbidden_role",
                                      "verified": False}]
    by_checker = call(c, case_id, "continue", caller=CHECKER)
    assert ("validate", "done") in _items(by_checker)
    assert by_checker["pending_action"]["kind"] == "freeze_package"


def test_a_rework_makes_the_steps_from_there_pending_again(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    body = call(c, case_id, "Take this case up to the estimate step by step")
    assert ("quantity.finalise", "done") in _items(body)
    assert ("estimate", "waiting_approval") in _items(body)
    call(c, case_id, "Finalise the quantity", answer="lines", value="1: qty=35")
    again = dict(_items(call(c, case_id, "continue")))
    assert again["need.capture"] == "done" and again["precedent.select"] == "done"
    assert again["quantity.finalise"] == "waiting_approval"          # back to S3
    assert again["estimate"] == "pending"


def test_a_whole_goods_case_is_completed_from_one_query_step_by_step(tmp_path):
    c = ctx(tmp_path)
    body = call(c, None, "Complete the whole procurement step by step",
                "i need to procure gaskets")
    body, steps = drive(c, body)
    assert body["result"]["complete"], (_items(body), body["result"]["stopped"])
    assert all(s == "done" for _f, s in _items(body))
    wf = orchestrator.workflow(need.load(c.conn, body["case_id"]))
    # done: the PR submitted, with no documents (NAM ends with the PR)
    assert "submit_pr" in wf["actions"] and wf["stage"] == "S8" and not wf.get("closed")
    assert not orchestrator._artifact(wf, "documents")
    assert body["verification"]["passed"] and steps < 60
    # the list is on the case, done, for anyone who reads it
    todo = orchestrator._artifact(wf, "todo")
    assert all(i["status"] == "done" for i in todo["items"])


def test_when_no_precedent_fits_the_guided_form_is_added(tmp_path):
    c = ctx(tmp_path)
    body = call(c, None, "Take this need up to the quantity step by step",
                "i need to procure gaskets")
    for qid in ("variant", "category", "procurement_type", "tender_type"):
        if body["questions"] and body["questions"][0]["id"] == qid:
            body = call(c, body["case_id"], "continue", answer=qid, value=ANSWERS[qid])
    assert body["questions"][0]["id"] == "precedent", _items(body)
    body = call(c, body["case_id"], "continue", "none fit", answer="precedent", value="none")
    flows = [f for f, _s in _items(body)]
    assert flows.index("need.guided_form") == flows.index("precedent.select") + 1
    added = next(i for i in body["result"]["todo"] if i["flow"] == "need.guided_form")
    assert "added" in added["why"] or added["status"] == "waiting_input"
