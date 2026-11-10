"""The NAM harness: the Need Assessment Module as one LLM-like call.

    from nam_agent import nam
    body = nam.run({"system": "Assess need for request #123",
                    "messages": [{"role": "user", "content": "We need 20 gate valves"}]},
                   ctx, nam.Caller(tenant="nrl", user="u123", roles=("U1",)))

`run(request, ctx, caller)` is the only entry point. `caller` is the trusted
identity context (tenant, user, roles) that the upstream auth component
supplies; the harness checks it against the NAM permissions
(nam_agent/nam/permissions.py) and never authenticates anyone itself.

The server's /v1/nam/* (nam_agent/nam/api.py, served by `nam-agent serve`) and
its /api/nam/* routes (nam_agent/server/nam_routes.py) both go through it.
Every body it returns is either a response that passed schemas/response.json
and the flow's own result schema, or an error body (schemas/error.json).
It never raises.

Two ways in, one package:

  the harness API   run() above: the stage machine, its flows and gates
                    (orchestrator.py, playbooks/), for Procurix screens and any
                    other client (api.py, the server's /api/nam/* routes)
  the chat          the PR Creation Flow (flow.py, case.py, stage_tools.py,
                    narrow.py, estimate.py, pr.py): the agent works the
                    manual's steps with its tools in the NAM harness's chat
                    (/api/chat, /api/chat/stream); docs/nam/integration.md

docs/backend-integration.md describes every endpoint.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import secrets
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from nam_agent.nam import (
    guard,
    guardrails,
    intent,
    llm,
    orchestrator,
    record,
    router,
    schema,
    store,
    tools,
    verify,
)
from nam_agent.nam.errors import VERSION, NamError, status_of
from nam_agent.nam.identity import Caller

__all__ = [
    "VERSION",
    "Caller",
    "Context",
    "NamError",
    "case",
    "case_trace",
    "history",
    "run",
    "run_trace",
    "status_of",
]

log = logging.getLogger("nam_agent.nam")


def _kg_get(kg_url: str, path: str, params: dict) -> dict:
    from nam_agent.nam import need

    return need._get(kg_url, path, params)


@dataclass
class Context:
    """What a flow may use. Evals build one with a scripted KG and model."""

    conn: sqlite3.Connection
    home: Path
    kg_url: str = ""
    # the model per task (nam_agent/nam/llm.py); None = no model
    models: object | None = None
    # or one (system, user) -> text for every task: evals and simple callers
    ask_model: Callable[[str, str], str] | None = None
    # (kg_url, path, params) -> dict, the KG's HTTP GET
    fetch: Callable[[str, str, dict], dict] = field(default=_kg_get)
    # the OCR engine (nam_agent/nam/ocr/base.py); None: NAM_OCR_BACKEND, if set
    ocr: object | None = None
    max_doc_bytes: int = 25 * 1024 * 1024
    ocr_min_chars: int = 20          # letters and digits a page needs to count as text


def new_run_id() -> str:
    return f"run_{secrets.token_hex(8)}"


def run(request, ctx: Context, caller: Caller | None) -> dict:
    """One call. `caller` is the trusted identity context (nam_agent/nam/identity.py);
    without one the call is refused."""
    run_id = new_run_id()
    started = time.monotonic()
    try:
        body = _run(request, ctx, caller, run_id)
    except NamError as exc:
        body = exc.body(run_id)
    except Exception:  # noqa: BLE001 - the caller gets a clean error, the log gets the trace
        log.exception("NAM run %s failed", run_id)
        body = NamError("internal", "The harness failed on this request; see the "
                        f"server log for {run_id}.").body(run_id)
    _record_call(ctx, caller, request, body, run_id, int((time.monotonic() - started) * 1000))
    return body


def _record_call(ctx: Context, caller, request, body: dict, run_id: str, ms: int) -> None:
    """One audit row per call (kind "call"): who asked, what for, what came
    back. The prompt and messages are not stored, only the system prompt's
    first words, because the messages carry the officer's text."""
    from nam_agent.nam import audit

    who = caller if isinstance(caller, Caller) else Caller(tenant="", user="", roles=())
    error = body.get("error") or {}
    system = request.get("system") if isinstance(request, dict) else ""
    detail = {"system": guard.redact_secrets(str(system or ""))[:120],
              "roles": list(who.roles), "stage": body.get("stage"),
              "route": body.get("route"), "pending": (body.get("pending_action") or {}).get("kind"),
              "documents": list(request.get("documents") or [])[:20]
              if isinstance(request, dict) and isinstance(request.get("documents"), list)
              else [],
              "confirm": (request.get("confirm") or {}).get("action_id")
              if isinstance(request, dict) and isinstance(request.get("confirm"), dict) else None}
    actor = store.Actor(tenant=who.tenant, user=who.user, flow=body.get("flow") or "",
                        run_id=run_id, case_id=body.get("case_id") or "", roles=who.roles)
    try:
        with store.acting(actor):
            audit.record(ctx.conn, kind="call", name=body.get("flow") or "error",
                         status=error.get("code") and "error" or body.get("status") or "ok",
                         error_code=error.get("code") or "", ms=ms, detail=detail)
    except Exception:  # noqa: BLE001 - the reply is already made; the server log keeps it
        log.exception("NAM run %s: the call could not be audited", run_id)


def _run(request, ctx: Context, caller: Caller | None, run_id: str) -> dict:
    from nam_agent.nam import permissions, playbooks

    if not isinstance(caller, Caller):
        raise NamError("identity_missing", "no identity context for this call")
    if not isinstance(request, dict):
        raise NamError("bad_request", "the body must be a JSON object")
    problems = schema.validate(request, schema.load("request"))
    if problems:
        raise NamError("schema", "the request does not match the nam.v1 request schema",
                       details=problems)

    # a case named only in the prompt ("Status of NAM-2026-0012") is that case
    found = router.refs(request["system"])
    case_id = request.get("case_id")
    if found.get("case_id") and case_id and found["case_id"] != case_id:
        raise NamError("bad_request", f"the prompt names {found['case_id']} but case_id is "
                       f"{case_id}")
    if not case_id and found.get("case_id"):
        case_id = found["case_id"]
        request = {**request, "case_id": case_id}
    if case_id:
        permissions.require_case(ctx.conn, caller, case_id)
        seen, now = request.get("case_version"), store.version_of(ctx.conn, case_id)
        if seen is not None and seen != now:
            raise store.stale(case_id, seen, now)
    if request.get("confirm") and not case_id:
        raise NamError("bad_request", "confirm needs the case_id of the pending action")

    # every write to the Need Record in this call is recorded as the caller's,
    # and a case it opens belongs to the caller's tenant
    actor = store.Actor(tenant=caller.tenant, user=caller.user, flow="nam.orchestrator",
                        run_id=run_id, case_id=case_id or "", seen=request.get("case_version"),
                        roles=caller.roles)
    with store.acting(actor):
        # the stage first: a change made outside a harness call (the
        # dashboard) may have moved the case on
        current = orchestrator.settle(ctx.conn, case_id) if case_id else None
        if request.get("confirm"):
            return _confirmed(request, caller, case_id, ctx, run_id)
        state = router.case_state(ctx.conn, case_id) if case_id else None

        chosen = router.route(request, state, llm.asker(ctx, "route"))
        if chosen.flow is None:
            return _routed(chosen, current, case_id, ctx, run_id)
        flow = playbooks.registry()[chosen.flow]
        request, read_as, unapplied = _worded(flow.id, request, current)
        if unapplied and current is not None:
            # the words name lines the case does not hold yet (the step builds
            # them from the precedent): build them, then read the words again
            _first, after, _checked_first = _execute(flow, request, ctx, caller, actor, run_id,
                                                    current)
            again, read_as, unapplied = _worded(flow.id, request, after)
            if read_as:
                request, current = again, after
        if read_as:
            chosen = dataclasses.replace(chosen, why=f"{chosen.why}; {read_as}")
        outcome, after, checked_step = _execute(flow, request, ctx, caller, actor, run_id,
                                                current, unapplied=unapplied)
    body = _envelope(run_id, ctx, flow.id, list(flow.frs), outcome,
                     {"by": chosen.by, "why": chosen.why[:500]}, after, flow.stage,
                     checked_step)
    return _checked(body, flow.id, flow.result_schema, run_id)


# flows that run other flows, each through _execute (playbooks/plan.py)
RUNS_STEPS = frozenset({"case.plan"})


def _worded(flow_id: str, request: dict, current) -> tuple[dict, str, str]:
    """A change written in words ("make it 35 nos"), as the step's own answer
    (intent.answer_for). Returns (request, how it was read, the words when
    they look like a change but could not be read: the step must then say
    it applied nothing, verify.py)."""
    if request.get("answer") or flow_id not in intent.ANSWER_IDS:
        return request, "", ""
    message = router._last_user(request["messages"])
    if not message.strip():
        return request, "", ""
    wf = orchestrator.workflow(current) if current is not None else {"artifacts": {}}
    lines = len((orchestrator._artifact(wf, "quantity") or {}).get("lines") or [])
    answer = intent.answer_for(flow_id, message, lines)
    if answer:
        return ({**request, "answer": answer},
                f"the message read as '{answer['question_id']}': {answer['value']}", "")
    return request, "", message if intent.edit_like(message) else ""


def _execute(flow, request: dict, ctx: Context, caller: Caller, actor, run_id: str, current,
             *, unapplied: str = ""):
    """Run one flow on the case as a call would: its role, its stage, the
    guardrails before and after it, the stage machine, and the verification
    of what it claims. Returns (outcome as verified, case after, verification).
    The plan (playbooks/plan.py) runs each of its steps through here too."""
    from nam_agent.nam import permissions

    case_id = request.get("case_id")
    permissions.require_flow(caller, flow.id)
    if flow.needs_case and not case_id:
        raise NamError("bad_request", f"{flow.id} works on an existing case: send its "
                       "case_id")
    orchestrator.admit(flow, current)
    if flow.handler is None:
        raise NamError("flow_not_built", f"The call routes to {flow.id} ({flow.title}), "
                       "which is not built yet.",
                       details=[f"flow: {flow.id}", f"frs: {', '.join(flow.frs)}"],
                       case_id=case_id)
    guardrails.before(ctx.conn, flow, current, request)
    before = current.version if current else None
    snapshot = copy.deepcopy(current)
    # the flow's own KG fetches are audited like its tool calls, and its
    # tool results are the only evidence a rate may cite
    fetches = tools.FetchLog(ctx.fetch)
    with store.acting(dataclasses.replace(actor, flow=flow.id)), guardrails.evidence():
        try:
            outcome = flow.handler(request, dataclasses.replace(ctx, fetch=fetches))
        finally:
            fetches.flush(ctx.conn, case_id or "")
        if outcome.case_id and case_id and outcome.case_id != case_id:
            raise NamError("internal", f"{flow.id} was called on {case_id} and answered "
                           f"for {outcome.case_id}")
        if flow.id in RUNS_STEPS and outcome.case_id:
            # its steps were each guarded and verified as they ran (step());
            # its own output is checked from where they left the case
            from nam_agent.nam import need as need_store

            snapshot = need_store.load(ctx.conn, outcome.case_id)
        artifacts, checked = guardrails.after(ctx.conn, flow, request, snapshot, outcome)
    if checked:
        outcome = dataclasses.replace(outcome, warnings=(*outcome.warnings, *checked))
    after = (orchestrator.settle(ctx.conn, outcome.case_id, ran=flow, version_before=before,
                                 artifacts=artifacts)
             if outcome.case_id else None)
    # the feedback loop: what the step claims, checked against the case as
    # stored (verify.py); a step that is not done is not reported done
    checked_step = verify.step(ctx.conn, ctx.home, flow.id, outcome, after, run_id,
                               unapplied=unapplied)
    return verify.applied(outcome, checked_step, flow.id), after, checked_step


def step(ctx: Context, flow_id: str, request: dict):
    """One flow run inside the current call, for the plan's step-by-step
    execution: the caller and run are the current actor's (store.current()).
    Returns (outcome, case after, verification); raises NamError."""
    from nam_agent.nam import playbooks

    actor = store.current()
    caller = Caller(tenant=actor.tenant, user=actor.user, roles=tuple(actor.roles))
    case_id = request.get("case_id")
    current = orchestrator.settle(ctx.conn, case_id) if case_id else None
    sub = dataclasses.replace(actor, flow="nam.orchestrator", case_id=case_id or "")
    with store.acting(sub):
        return _execute(playbooks.registry()[flow_id], request, ctx, caller, sub,
                        actor.run_id, current)


def _confirmed(request: dict, caller: Caller, case_id: str, ctx: Context, run_id: str) -> dict:
    """confirm: {action_id}: the pending gate or irreversible action, run by
    nam_agent/nam/orchestrator.py for an allowed role. No flow is routed."""
    from nam_agent.nam import playbooks

    result, after = orchestrator.confirm(ctx.conn, caller, case_id, request["confirm"],
                                         home=ctx.home)
    outcome = playbooks.Outcome(
        case_id=case_id, status="ok", result=result,
        warnings=(f"{result['action_id']} was already confirmed; nothing was run again.",)
        if result["replay"] else ())
    body = _envelope(run_id, ctx, "nam.confirm", [], outcome,
                     {"by": "none", "why": "a confirmation runs no flow"}, after, "S1",
                     verify.confirmed(result, after))
    return _checked(body, "nam.confirm", "flow.nam.confirm", run_id)


def _envelope(run_id: str, ctx: Context, flow_id: str, frs: list, outcome, route: dict,
              case=None, stage: str = "S1", verification: dict | None = None) -> dict:
    """The response body. With a case, the stage and pending action are the
    case's after the call, and ok becomes needs_approval while an approval
    or irreversible action awaits confirmation."""
    wf = orchestrator.workflow(case) if case is not None else None
    pending = orchestrator.public(wf["pending"]) if wf else None
    status = outcome.status
    if pending and status == "ok":
        status = "needs_approval"
    return {
        "version": VERSION,
        "run_id": run_id,
        "case_id": outcome.case_id,
        "case_version": store.version_of(ctx.conn, outcome.case_id) if outcome.case_id else None,
        "flow": flow_id,
        "frs": frs,
        "stage": wf["stage"] if wf else stage,
        "status": status,
        "result": outcome.result,
        "questions": list(outcome.questions),
        "pending_action": pending,
        "citations": list(outcome.citations),
        "warnings": list(outcome.warnings),
        "next": [] if pending else list(outcome.next),
        "route": route,
        # what was checked after the step, and what the case's stage still needs
        "verification": verification or verify.nothing_ran(case),
    }


def _routed(chosen, state, case_id: str | None, ctx: Context, run_id: str) -> dict:
    """The router's own reply, when no flow runs: needs_input with the
    candidate flows as a question, or refused. Nothing is written."""
    from nam_agent.nam import playbooks

    flows = playbooks.registry()
    result = {"reason": chosen.why[:500], "refs": chosen.refs,
              "candidates": [{"flow": i, "title": flows[i].title, "frs": list(flows[i].frs),
                              "stage": flows[i].stage} for i in chosen.candidates]}
    outcome = playbooks.Outcome(
        case_id=case_id, status=chosen.status, result=result,
        questions=(router.question(chosen),) if chosen.status == "needs_input" else (),
        warnings=(chosen.why[:500],) if chosen.status == "refused" else ())
    body = _envelope(run_id, ctx, "nam.route", [], outcome,
                     {"by": "none", "why": chosen.why[:500]}, state)
    return _checked(body, "nam.route", "flow.nam.route", run_id)


def _checked(body: dict, flow_id: str, result_schema: str, run_id: str) -> dict:
    leaked = guard.leaks(body)
    if leaked:
        # the system prompt or a server secret found its way into the reply
        log.error("NAM run %s: %s response withheld: it held %s", run_id, flow_id, leaked)
        raise NamError("internal", f"The {flow_id} response was withheld by the output check; "
                       f"see the server log for {run_id}.", case_id=body["case_id"])
    problems = (schema.validate(body, schema.load("response"))
                + schema.validate(body["result"], schema.load(result_schema)))
    if problems:
        # the harness broke its own contract: never send the half-valid body
        log.error("NAM run %s: %s response failed its schema: %s", run_id, flow_id, problems)
        raise NamError("internal_schema", f"The {flow_id} response failed its own schema; "
                       f"see the server log for {run_id}.", case_id=body["case_id"])
    return body


def get_case(ctx: Context, caller: Caller | None, case_id: str, version: int | None = None) -> dict:
    """GET /v1/nam/cases/{id}: the Need Record (nam_agent/nam/record.py), as the
    caller's tenant may see it. Never raises, like run()."""
    return _read(ctx, caller, case_id, lambda: record.view(ctx.conn, case_id, version))


def history(ctx: Context, caller: Caller | None, case_id: str) -> dict:
    """GET /v1/nam/cases/{id}/history: every version, who wrote it and why."""
    return _read(ctx, caller, case_id, lambda: record.history(ctx.conn, case_id))


def case_trace(ctx: Context, caller: Caller | None, case_id: str) -> dict:
    """GET /v1/nam/cases/{id}/trace: the case traced from each output to its
    inputs and approvals (nam_agent/nam/trace.py)."""
    from nam_agent.nam import trace as trace_mod

    return _read(ctx, caller, case_id, lambda: {"version": VERSION,
                                                **trace_mod.build(ctx.conn, case_id)})


def run_trace(ctx: Context, caller: Caller | None, run_id: str) -> dict:
    """GET /v1/nam/runs/{run_id}: one call's audit rows, the caller's tenant only."""
    from nam_agent.nam import permissions
    from nam_agent.nam import trace as trace_mod

    try:
        if not isinstance(caller, Caller):
            raise NamError("identity_missing", "no identity context for this call")
        permissions.require_flow(caller, "case.status")
        return {"version": VERSION, **trace_mod.for_run(ctx.conn, caller.tenant, run_id)}
    except NamError as exc:
        return exc.body(new_run_id())


def _read(ctx: Context, caller: Caller | None, case_id: str, read) -> dict:
    from nam_agent.nam import permissions

    run_id = new_run_id()
    try:
        if not isinstance(caller, Caller):
            raise NamError("identity_missing", "no identity context for this call")
        permissions.require_flow(caller, "case.status")
        permissions.require_case(ctx.conn, caller, case_id)
        return read()
    except NamError as exc:
        return exc.body(run_id)
    except Exception:  # noqa: BLE001 - as in run()
        log.exception("NAM read %s failed", run_id)
        return NamError("internal", "The harness failed on this request; see the "
                        f"server log for {run_id}.").body(run_id)
