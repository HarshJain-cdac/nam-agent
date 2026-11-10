"""case.plan: a to-do list for the user's query, worked through step by step.

    "Take this need up to the PR"         a list from the case's stage to S7
    "Complete the whole procurement"      ... to the PR submitted (S7): NAM's end
    "Continue"                            the next items of the list

1. The list. From the query the plan reads its goal (the estimate, the
   documents, the validation, the PR by default, the approval, the upload or
   the handoff) and lists the steps from the case's stage to it, in
   the order of the stage machine (S1-S10), the ones the case has already
   done marked done: need capture, classification,
   precedent search and selection, quantity, the estimate for the case's
   category, the documents, their review and the compliance list,
   validation, the freeze, the PR and its submission, the approval, the
   upload, the handoff. It is stored on the case (artifact `todo`), so any
   later call, by any user, continues it. A query naming another goal makes
   a new list.

2. Step by step. Each call runs the list's items in order, each through the
   same path as a call of its own (nam_agent/nam/__init__._execute: the role, the
   stage, the guardrails, the stage machine) and each verified after it
   (verify.py). An item is done only when the case shows it done: its
   verification passed and its own done-check holds on the stored case, or
   the case's stage is past it. The run stops at the first item that cannot
   finish now, and says why:
     needs input      the item's question is the reply's question; answer it
                      with "continue" and the answer goes to that item
     needs approval   the pending gate or action is the reply's; once a user
                      of an allowed role confirms it, "continue" goes on
     not allowed      the item needs another role (the roles are listed);
                      that user sends "continue"
     refused          a guardrail or a gate refused it, with its reasons
   At most MAX_STEPS items run in one call.

3. The list follows the case. Before each run every item is re-read
   against the case: a rework that took the case back makes the items from
   there pending again; when a stage still needs something no item gives
   (no precedent fits: the guided form), the item that gives it is added.

The plan proposes no approval and decides nothing itself: every item is a
flow the officer could have called, and the stage machine still holds every
gate.
"""

from __future__ import annotations

import re

from nam_agent.nam import need as need_store
from nam_agent.nam import orchestrator, permissions, verify
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome, need_classify

MAX_STEPS = 12
ESTIMATE = "estimate"            # resolved to the case's estimate flow when it runs

# the main path, in stage order (flow, stage)
PATH = (("need.capture", "S1"), ("need.classify", "S1"),
        ("precedent.search", "S2"), ("precedent.select", "S2"),
        ("quantity.finalise", "S3"), (ESTIMATE, "S4"),
        ("docs.generate", "S5"), ("docs.review", "S5"), ("compliance.build", "S5"),
        ("validate", "S6"), ("package.freeze", "S6"),
        ("pr.build", "S7"), ("pr.submit", "S7"),
        ("approval", "S8"), ("package.publish", "S9"), ("handoff.nit", "S10"))

# what a stage may need that the main path does not give
FALLBACK = {"S2": "need.guided_form"}

# the goal a query names: the last stage of the list
GOALS = (
    (r"\b(hand ?off|handover)\b", "S10"),
    (r"\b(upload|publish|aarohan)\b", "S9"),
    (r"\b(approv\w*)\b", "S8"),
    # NAM ends with the PR (PR Creation Flow): "the whole procurement" is that
    ((r"\b(pr|purchase requisition|requisition|purchase request|whole|entire|"
      r"complete procurement|end to end|everything|all (the )?steps)\b"), "S7"),
    (r"\b(validat\w*|freeze|package)\b", "S6"),
    (r"\b(documents?|boq|scc|pqc|specification|compliance)\b", "S5"),
    (r"\b(estimate|rates?|cost)\b", "S4"),
    (r"\b(quantity|quantities)\b", "S3"),
    (r"\b(precedent|similar)\b", "S2"),
)
_CONTINUE = re.compile(r"^\s*(continue|resume|go on|carry on|proceed|next( step)?)\b",
                       re.IGNORECASE)
TITLES = {"need.capture": "Capture the need", ESTIMATE: "Estimate the cost"}


def _end() -> str:
    """NAM's end, the PR (journey.end_stage, from policy/journey.json)."""
    from nam_agent.nam import journey

    return journey.end_stage()


def _idx(stage: str) -> int:
    return int(stage.removeprefix("S")) - 1


def goal_of(text: str) -> str | None:
    """The last stage the query asks for, or None when it names none."""
    low = text.lower()
    for pattern, stage in GOALS:
        if re.search(pattern, low):
            return stage
    return None


def _title(flow_id: str) -> str:
    from nam_agent.nam import playbooks

    if flow_id in TITLES:
        return TITLES[flow_id]
    return playbooks.registry()[flow_id].title


def _roles(flow_id: str) -> list[str]:
    if flow_id == ESTIMATE:
        flow_id = "estimate.goods"
    return list(permissions.policy()["flows"].get(flow_id, []))


def make(goal: str) -> list[dict]:
    """Every step from S1 to the goal; refresh() marks the ones the case has
    done, so a rework into an earlier stage finds its steps on the list. A
    goal of NAM's end (the PR) leaves out the optional stages (documents,
    validation): the PR follows the approved estimate."""
    return [{"n": 0, "flow": f, "title": _title(f), "stage": st, "status": "pending",
             "roles": _roles(f), "why": ""}
            for f, st in PATH if _idx(st) <= _idx(goal)
            and not (goal == _end() and orchestrator.BY_ID[st].optional_unless)]


def _number(items: list[dict]) -> list[dict]:
    for n, item in enumerate(items, 1):
        item["n"] = n
    return items


def _flow_of(item: dict, need) -> str | None:
    if item["flow"] != ESTIMATE:
        return item["flow"]
    return need_classify.route(need)["estimate_flow"] if need is not None else None


def _closes_stage(item: dict) -> bool:
    """The last step of a stage that has an approval (a gate or an action):
    it is done only once the approval is given and the case moved on."""
    stage = orchestrator.BY_ID[item["stage"]]
    last = [f for f, st in PATH if st == item["stage"]][-1]
    return (stage.gates or stage.action) and item["flow"] == last


def _done_on_case(item: dict, need, wf: dict) -> bool:
    """Whether the case, as stored, shows the item done."""
    if need is None:
        return False
    passed = _idx(item["stage"]) < _idx(wf["stage"]) and not orchestrator.skipped(
        wf, orchestrator.BY_ID[item["stage"]])      # passed through unstarted is not done
    if wf.get("closed") or passed:
        return True
    flow = _flow_of(item, need)
    if flow is None or _idx(item["stage"]) > _idx(wf["stage"]) or _closes_stage(item):
        return False
    if flow == "need.capture":
        return not any(m["name"] == "need" for m in orchestrator.missing(need, wf))
    if flow == "precedent.search":
        return bool(orchestrator._artifact(wf, "shortlist"))
    owned = [name for name, flows in orchestrator.ARTIFACTS.items() if flow in flows]
    if owned and not all(orchestrator._artifact(wf, n) for n in owned):
        return False
    checks = verify.DONE.get(flow, ())
    if flow == "precedent.select":
        return bool(need.selected_cases)
    return bool(checks) and all(c.test(need, wf, {}) for c in checks)


def refresh(items: list[dict], need, *, reset: bool = False) -> list[dict]:
    """The items re-read against the case: done when the case shows it,
    pending again when a rework undid it; the fallback item added when the
    stage needs what no item gives. `reset` (once, as a call starts) clears
    what the last call found waiting or blocked: this call finds out again."""
    wf = orchestrator.workflow(need) if need is not None else {"stage": "S1"}
    form = any(i["flow"] == "need.guided_form" for i in items)
    for item in items:
        done = _done_on_case(item, need, wf)
        if item["flow"] == "precedent.select" and form:
            done = True           # none fit: the guided form takes the precedent's place
        if done:
            item.update(status="done", why="")
        elif item["status"] == "done":
            item.update(status="pending", why="the case went back: to do again")
        elif reset and item["status"] != "pending":
            item.update(status="pending", why="")
    stage = wf.get("stage", "S1")
    extra = FALLBACK.get(stage)
    here = [i for i in items if i["stage"] == stage]
    if (extra and need is not None and here and all(i["status"] == "done" for i in here)
            and orchestrator.missing(need, wf)
            and not any(m["kind"] != "check" for m in orchestrator.missing(need, wf))):
        _add_fallback(items, stage)
    return _number(items)


def _add_fallback(items: list[dict], stage: str) -> None:
    extra = FALLBACK[stage]
    if any(i["flow"] == extra for i in items):
        return
    at = max(k for k, i in enumerate(items) if i["stage"] == stage) + 1
    items.insert(at, {"n": 0, "flow": extra, "title": _title(extra), "stage": stage,
                      "status": "pending", "roles": _roles(extra),
                      "why": "added: no precedent fits, so the guided form captures the need"})
    _number(items)


def _summary(items: list[dict], goal: str) -> str:
    done = sum(i["status"] == "done" for i in items)
    return f"{done} of {len(items)} step(s) done, to {goal}"


def handle(request: dict, ctx) -> Outcome:
    from nam_agent import nam as harness

    case_id = request.get("case_id")
    need = need_store.load(ctx.conn, case_id) if case_id else None
    if case_id and need is None:
        raise NamError("not_found", f"no case {case_id}")
    wf = orchestrator.workflow(need) if need is not None else {"stage": "S1"}
    query = " ".join([request["system"], *(m.get("content", "") for m in request["messages"]
                                           if m.get("role") == "user")])
    stored = orchestrator._artifact(wf, "todo") if need is not None else None
    asked = goal_of(request["system"])
    warnings: list[str] = []
    if stored and (asked is None or asked == stored["goal"]):
        goal, items = stored["goal"], [dict(i) for i in stored["items"]]
    else:
        goal = asked or _end()
        items = make(goal)
        if stored:
            warnings.append(f"A new to-do list, to {goal}: the query names another goal.")
    if not _CONTINUE.match(request["system"]) and need is None and not query.strip():
        raise NamError("bad_request", "say what is needed, e.g. 'We need 20 gate valves'")

    answer = request.get("answer")
    message = next((m.get("content", "") for m in reversed(request["messages"])
                    if m.get("role") == "user"), "")
    ran: list[dict] = []
    stop = {"status": "ok", "questions": (), "why": ""}
    items = refresh(items, need, reset=True)
    for _ in range(MAX_STEPS):
        need = need_store.load(ctx.conn, case_id) if case_id else None
        items = refresh(items, need)
        todo = next((i for i in items if i["status"] != "done"), None)
        if todo is None:
            break
        flow = _flow_of(todo, need)
        if flow is None:
            todo.update(status="blocked", why="the category is not confirmed yet")
            stop = {"status": "needs_input", "questions": (), "why": todo["why"]}
            break
        wf = orchestrator.workflow(need) if need is not None else {"stage": "S1"}
        if need is not None and wf.get("pending"):
            pend = wf["pending"]
            todo.update(status="waiting_approval",
                        why=f"{pend['kind']} awaits a user of {', '.join(pend['allowed_roles'])}")
            stop = {"status": "ok", "questions": (), "why": todo["why"]}
            break
        sub = {"system": todo["title"], "case_id": case_id,
               "messages": [{"role": "user", "content": message if not ran else ""}]}
        if answer and not ran:
            sub["answer"] = answer
        if not case_id:
            sub.pop("case_id")
        try:
            outcome, _after, checked = harness.step(ctx, flow, sub)
        except NamError as exc:
            todo.update(status="blocked", why=f"{exc.code}: {exc.message}"[:300])
            ran.append({"flow": flow, "status": exc.code, "verified": False})
            warnings += [f"[{flow}] {d}" for d in exc.details[:5]]
            stop = {"status": "needs_input", "questions": (), "why": todo["why"]}
            break
        case_id = case_id or outcome.case_id
        ran.append({"flow": flow, "status": outcome.status, "verified": checked["passed"]})
        warnings += [f"[{flow}] {w}" for w in outcome.warnings[:4]]
        need = need_store.load(ctx.conn, case_id) if case_id else None
        if flow == "precedent.select" and outcome.status == "ok" \
                and not (outcome.result or {}).get("selected"):
            _add_fallback(items, "S2")         # the officer said none fit
            continue
        if outcome.status == "needs_input" and checked["passed"] and need is not None \
                and _done_on_case(todo, need, orchestrator.workflow(need)):
            continue          # its work is on the case; what it asks is the next step's
        if outcome.status == "needs_input":
            todo.update(status="waiting_input",
                        why=(outcome.questions[0]["text"] if outcome.questions
                             else "; ".join(c["says"] for c in checked["checks"]
                                            if not c["ok"]) or "it needs input")[:300])
            stop = {"status": "needs_input", "questions": outcome.questions, "why": todo["why"]}
            break
        if outcome.status == "refused":
            todo.update(status="blocked", why="the step was refused")
            stop = {"status": "needs_input", "questions": (), "why": todo["why"]}
            break
        if not _done_on_case(todo, need, orchestrator.workflow(need)) and \
                not orchestrator.workflow(need).get("pending"):
            # it ran and verified, yet the case does not show it done: never
            # loop on it, say so
            todo.update(status="blocked", why="it ran, but the case does not show it done")
            stop = {"status": "needs_input", "questions": (), "why": todo["why"]}
            break
    else:
        warnings.append(f"Ran {MAX_STEPS} steps, the most in one call: send 'continue'.")

    need = need_store.load(ctx.conn, case_id) if case_id else None
    items = refresh(items, need)
    if need is not None:
        pend = orchestrator.workflow(need).get("pending")
        nxt = next((i for i in items if i["status"] != "done"), None)
        if pend and nxt is not None:
            nxt.update(status="waiting_approval",
                       why=f"{pend['kind']} awaits a user of {', '.join(pend['allowed_roles'])}")
            stop["why"] = nxt["why"]
    complete = all(i["status"] == "done" for i in items)
    nxt = next((i for i in items if i["status"] != "done"), None)
    value = {"summary": _summary(items, goal), "goal": goal, "items": items}
    result = {"goal": goal, "complete": complete, "todo": items, "ran": ran,
              "next_item": nxt["n"] if nxt else None,
              "stopped": ("done: every step is verified on the case" if complete
                          else stop["why"] or "")[:300]}
    return Outcome(case_id=case_id, status="ok" if complete or stop["status"] == "ok"
                   else stop["status"], result=result, questions=tuple(stop["questions"]),
                   warnings=tuple(dict.fromkeys(warnings))[:30],
                   next=() if complete else ("case.plan",),
                   artifacts={"todo": value} if case_id else {})
