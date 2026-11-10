"""The NAM stage machine (plan D5, D6): which stage a case is in, what it
needs to leave that stage, and who may approve it. Code decides; a flow only
does the work inside its stage.

Each stage S1-S10 (FRS 2.5, SRS 2.4.3) is left only when all of these hold,
in order:

  checks   the work of the stage is on the record: the need is complete and
           its classification confirmed (F4), a precedent is chosen, the flow's artifact is there
  gates    a human approval by a role policy/permissions.json allows
           (quantity, estimate, clauses, documents)
  action   an irreversible act, confirmed by an allowed role: freeze the
           package, submit the PR, approve, hand off to NIT

When the checks pass and a gate or action is still open, the orchestrator
proposes it as the case's one pending action (response `pending_action`,
status needs_approval). It runs only when a caller sends
`confirm: {action_id}` with an allowed role, and only if the case has not
changed since it was proposed; otherwise it is proposed again under a new id.
A flow can never propose one: approvals and irreversible actions come from
this module only (D6).

A stage may be optional (`optional_unless`): while none of its artifacts is
on the case it is passed through, so the PR follows the approved estimate as
the PR Creation Flow says; documents and validation (S5, S6) are the
officer's to start, and once started they are checked like any stage.

A flow may not run ahead of the case's stage (409 stage_gate, saying what is
missing). A flow of an earlier stage may run before the package is frozen;
if it changes the case, the case goes back to that stage and the approvals
from there on must be given again. After the freeze nothing goes back.

The state lives in the Need Record (`workflow`), so a case resumes where it
was after a restart.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from nam_agent.nam import guardrails, permissions, store
from nam_agent.nam import need as need_store
from nam_agent.nam.errors import NamError
from nam_agent.nam.identity import Caller
from nam_agent.nam.playbooks import ANY_STAGE, Flow

CLAIM_TIMEOUT_S = 300            # a confirmation that died mid-way may be retried after this


# -- artifacts: what each flow may leave on the record -------------------------

ARTIFACTS = {
    "guided_form": ("need.guided_form",),
    "shortlist": ("precedent.search",),
    "pr_reference": ("pr.submit",),
    "replica": ("need.replicate",),
    "enrichment": ("need.enrich",),
    "materials": ("material.suggest",),
    "quantity": ("quantity.finalise",),
    "estimate": ("estimate.goods", "estimate.wage", "estimate.works"),
    "documents": ("docs.generate",),
    "review": ("docs.review",),
    "compliance": ("compliance.build",),
    "validation": ("validate",),
    "package": ("package.freeze",),
    "pr": ("pr.build",),
    "approval_file": ("approval",),
    "published": ("package.publish",),
    "uploads": ("document.update",),
    "handoff": ("handoff.nit",),
    "todo": ("case.plan",),
}


def _artifact(wf: dict, name: str):
    return ((wf.get("artifacts") or {}).get(name) or {}).get("value")


# -- the stages ----------------------------------------------------------------

@dataclass(frozen=True)
class Check:
    name: str
    says: str                                   # what is missing, in words
    test: Callable[[need_store.NeedRecord, dict], bool]


@dataclass(frozen=True)
class Stage:
    id: str
    name: str
    checks: tuple[Check, ...] = ()
    gates: tuple[str, ...] = ()
    action: str = ""
    # an optional stage is passed through while none of these artifacts is on
    # the case; once one is (the officer started its work), its checks, gates
    # and action apply like any stage's
    optional_unless: tuple[str, ...] = ()


def undecided(estimate: dict | None) -> list[int]:
    """The flagged lines (policy rate_comparison.decide) the officer has not
    accepted at their current rate (PR flow 2(e)), 0-based."""
    decide = set(guardrails.policy()["rate_comparison"].get("decide") or [])
    out = []
    for i, x in enumerate((estimate or {}).get("lines") or []):
        if (x.get("comparison") or {}).get("flag") not in decide:
            continue
        d = x.get("decision") or {}
        if d.get("value") != "accept" or d.get("rate") != x.get("rate"):
            out.append(i)
    return out


def _form_complete(wf) -> bool:
    # the guided form counts once every field is settled; a half-filled one
    # is saved so it resumes, but captures nothing yet
    return bool((_artifact(wf, "guided_form") or {}).get("complete"))


def _need_complete(need, wf) -> bool:
    # need capture says "references" only when related tenders with material
    # exist and no follow-up question is open
    done = need.stage == "references" or bool(need.selected_cases) \
        or _form_complete(wf)
    return done and bool((need.category or {}).get("value"))


def _at(wf: dict, name: str) -> int:
    """The case version an artifact was stored at (0: not stored)."""
    return int(((wf.get("artifacts") or {}).get(name) or {}).get("at_version") or 0)


def _has(name: str, says: str) -> Check:
    return Check(name, says, lambda need, wf: bool(_artifact(wf, name)))


STAGES: tuple[Stage, ...] = (
    Stage("S1", "Need capture and classification", (
        Check("need", "the need is not complete: need capture has open questions or found "
              "nothing to build from, or there is no category", _need_complete),
        Check("classification", "the category, procurement type and tender type are not all "
              "confirmed (need.classify)", lambda need, wf: not need.unresolved),)),
    Stage("S2", "Enterprise and precedent enrichment", (
        Check("precedent", "no precedent is selected (precedent.select), and the guided form "
              "was not completed", lambda need, wf: bool(need.selected_cases)
              or _form_complete(wf)),)),
    Stage("S3", "Requirement and quantity finalisation",
          (Check("quantity", "the quantity is not finalised (quantity.finalise): a line has "
                 "no quantity or unit", lambda need, wf: bool(
                     (_artifact(wf, "quantity") or {}).get("complete"))),),
          gates=("quantity",)),
    Stage("S4", "Price evidence and estimate",
          (_has("estimate", "there is no estimate (estimate.goods, .wage or .works)"),
           Check("estimate_current", "the estimate is of an earlier quantity: run the "
                 "estimate again", lambda need, wf: _at(wf, "estimate") >= _at(wf, "quantity")),
           Check("estimate_priced", "a line of the estimate has no rate (a New Item): the "
                 "officer gives it", lambda need, wf: all(
                     x.get("rate") is not None
                     for x in (_artifact(wf, "estimate") or {}).get("lines") or [])),
           Check("estimate_decided", "a flagged line has no decision: the officer accepts its "
                 "rate or revises it (estimate.goods: N: decision=accept)",
                 lambda need, wf: not undecided(_artifact(wf, "estimate")))),
          gates=("estimate",)),
    Stage("S5", "Document generation and checking",
          (Check("documents", "the documents are not generated, or are drafts missing a "
                 "field or a clause (docs.generate)", lambda need, wf: bool(
                     (_artifact(wf, "documents") or {}).get("complete"))),),
          gates=("clauses", "documents"),
          # the PR Creation Flow goes from the locked estimate to the PR: the
          # documents are the officer's choice, not a step the PR waits for
          optional_unless=("documents", "review", "compliance")),
    Stage("S6", "Internal validation and freeze", (
        Check("validation", "validation has not passed (validate)",
              lambda need, wf: (_artifact(wf, "validation") or {}).get("status") == "pass"),
        _has("package", "the package is not assembled (package.freeze)")),
          action="freeze_package", optional_unless=("validation", "package")),
    Stage("S7", "PR creation and reference capture", (
        Check("pr", "the PR is not built, or has missing mandatory fields (pr.build)",
              lambda need, wf: bool(_artifact(wf, "pr"))
              and not (_artifact(wf, "pr") or {}).get("missing")),),
          action="submit_pr"),
    Stage("S8", "Approval file and workflow",
          (Check("approval_file", "the approval file is not prepared, or names no approving "
                 "authority (approval)", lambda need, wf: bool(
                     (_artifact(wf, "approval_file") or {}).get("complete"))),),
          action="approve"),
    Stage("S9", "User upload", (
        Check("uploads", "an edited document is still awaited (document.update)",
              lambda need, wf: not (_artifact(wf, "uploads") or {}).get("awaiting")),)),
    Stage("S10", "Package routing to NIT",
          (_has("handoff", "the handoff package is not assembled (handoff.nit)"),),
          action="handoff_nit"),
)
_INDEX = {s.id: i for i, s in enumerate(STAGES)}
BY_ID = {s.id: s for s in STAGES}


# -- irreversible actions --------------------------------------------------------
# Each is an effect, run once per action id (store.once), then its mark on the
# workflow. An effect that calls a connector passes action_id as its
# idempotency key.

def _submit_pr(conn, need, action_id) -> dict:
    # FRS F27: the PR goes to SAP MM, or is created in Procurix.ai with its
    # own file number. There is no SAP connector yet, so it is the latter.
    # The duplicate-PR guardrail registers its items under the write lock first.
    done = guardrails.register_pr(conn, need, action_id, store.next_pr_number)
    return {**done, "sap": "not sent: no SAP connector"}


def _approve(conn, need, action_id) -> dict:
    # FRS F45: the approval in Procurix.ai's own e-office (no Aarohan
    # connector), recorded against the approval file
    art = _artifact(workflow(need), "approval_file") or {}
    return {"approved": True, "file": art.get("aarohan_file") or art.get("file_number"),
            "authority": art.get("authority"), "aarohan": "not sent: no Aarohan connector"}


EFFECTS: dict[str, Callable] = {
    "freeze_package": lambda conn, need, action_id: {"frozen_at_version": need.version},
    "submit_pr": _submit_pr,
    "approve": _approve,
    "handoff_nit": lambda conn, need, action_id: {
        "package_version": (workflow(need).get("frozen") or {}).get("at_version"),
        "nit": "recorded: no NIT connector"},
}


def _mark(kind: str, wf: dict, result: dict) -> None:
    if kind == "freeze_package":
        wf["frozen"] = {"at_version": result["frozen_at_version"]}
    elif kind == "handoff_nit":
        wf["closed"] = True


# -- reading the state -----------------------------------------------------------

def workflow(need: need_store.NeedRecord) -> dict:
    wf = dict(need.workflow or {})
    wf.setdefault("stage", "S1")
    for key in ("gates", "actions", "artifacts", "confirmed", "rejected"):
        wf[key] = dict(wf.get(key) or {})
    wf.setdefault("pending", None)
    return wf


def _stamp(actor: store.Actor, **extra) -> dict:
    return {"tenant": actor.tenant, "user": actor.user, "run_id": actor.run_id,
            "at": datetime.now(UTC).isoformat(timespec="seconds"), **extra}


def skipped(wf: dict, stage: Stage) -> bool:
    """An optional stage the officer has not started: the case passes it."""
    return bool(stage.optional_unless) and not any(
        _artifact(wf, name) for name in stage.optional_unless)


def missing(need, wf: dict, stage: Stage | None = None) -> list[dict]:
    """What the stage still needs, checks first, then gates, then its action."""
    stage = stage or BY_ID[wf["stage"]]
    out = [{"kind": "check", "name": c.name, "says": c.says}
           for c in stage.checks if not c.test(need, wf)]
    out += [{"kind": "gate", "name": g, "says": f"the {g} approval is not given"}
            for g in stage.gates if g not in wf["gates"]]
    if stage.action and stage.action not in wf["actions"]:
        out.append({"kind": "action", "name": stage.action,
                    "says": f"{stage.action} is not confirmed"})
    return out


def public(pending: dict | None) -> dict | None:
    """The pending action as the response shows it."""
    if not pending:
        return None
    return {k: pending[k] for k in ("id", "kind", "summary", "allowed_roles")}


def _allowed(item: dict) -> list[str]:
    section = "gates" if item["kind"] == "gate" else "actions"
    return list(permissions.policy()[section].get(item["name"], []))


def _kind(item: dict) -> str:
    return f"gate:{item['name']}" if item["kind"] == "gate" else item["name"]


# -- admission -------------------------------------------------------------------

def _stage_gate(message: str, need, wf, case_id: str) -> NamError:
    details = [f"case stage: {wf['stage']} ({BY_ID[wf['stage']].name})"]
    details += [f"missing: {m['says']}" for m in missing(need, wf)]
    if wf.get("pending"):
        details.append(f"pending: {wf['pending']['kind']} as {wf['pending']['id']}, "
                       f"roles {', '.join(wf['pending']['allowed_roles'])}")
    return NamError("stage_gate", message, details=details, case_id=case_id)


def admit(flow: Flow, need: need_store.NeedRecord | None) -> None:
    """Refuse a flow the case's stage does not allow (409 stage_gate)."""
    if need is None:
        if not flow.at("S1"):
            raise NamError("stage_gate", f"{flow.id} runs on a case past S1; a new case "
                           "starts with need.capture, need.replicate or need.guided_form")
        return
    wf = workflow(need)
    case_id, stage = need.need_id, wf["stage"]
    if flow.id in ("case.status", "case.plan", "case.ask"):
        # case.plan changes no record itself: each step it runs is admitted
        # here on its own (nam_agent/nam/__init__._execute)
        return
    if wf.get("closed"):
        raise _stage_gate(f"{case_id} is handed off to NIT and closed", need, wf, case_id)
    if ANY_STAGE in flow.stages:
        if wf.get("frozen"):
            raise _stage_gate(f"{case_id}'s package is frozen: {flow.id} can no longer change "
                              "it", need, wf, case_id)
        return
    if flow.at(stage):
        return
    if max(_INDEX[s] for s in flow.stages) < _INDEX[stage]:
        if wf.get("frozen"):
            raise _stage_gate(f"{case_id}'s package is frozen: it cannot go back to "
                              f"{flow.id} ({', '.join(flow.stages)})", need, wf, case_id)
        return                                    # rework: settle() takes the case back
    raise _stage_gate(f"{flow.id} belongs to {', '.join(flow.stages)}; {case_id} is at {stage} "
                      "and cannot skip ahead", need, wf, case_id)


# -- settling: artifacts in, rewind, advance, propose --------------------------------

def settle(conn: sqlite3.Connection, case_id: str, *, ran: Flow | None = None,
           version_before: int | None = None, artifacts: dict | None = None) -> need_store.NeedRecord:
    """Bring the case's stage up to date after a flow ran (or before one runs),
    and propose the next gate or action. Saves only if something changed."""
    need = need_store.load(conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    wf = workflow(need)
    before = dict(need.workflow or {})
    actor = store.current()
    notes = []

    wrote = ran is not None and version_before is not None and need.version != version_before
    for name, value in (artifacts or {}).items():
        if ran is None or ran.id not in ARTIFACTS.get(name, ()):
            raise NamError("internal", f"{ran.id if ran else 'nothing'} may not write the "
                           f"artifact {name!r}")
        wf["artifacts"][name] = {"value": value, "flow": ran.id, "at_version": need.version,
                                 **_stamp(actor)}
        notes.append(f"{name} from {ran.id}")
        wrote = True

    if wrote and ran is not None and ANY_STAGE not in ran.stages and not ran.at(wf["stage"]):
        target = max((s for s in ran.stages if _INDEX[s] < _INDEX[wf["stage"]]), key=_INDEX.get)
        cleared = [g for s in STAGES[_INDEX[target]:_INDEX[wf["stage"]] + 1] for g in s.gates
                   if wf["gates"].pop(g, None)]
        notes.append(f"back to {target} after {ran.id}"
                     + (f"; approvals to give again: {', '.join(cleared)}" if cleared else ""))
        wf["stage"], wf["pending"] = target, None

    while not wf.get("closed"):
        stage = BY_ID[wf["stage"]]
        if missing(need, wf, stage) and not skipped(wf, stage):
            break
        if _INDEX[stage.id] + 1 == len(STAGES):
            break
        wf["stage"] = STAGES[_INDEX[stage.id] + 1].id
        wf["pending"] = None
        notes.append(f"{stage.id} -> {wf['stage']}")

    _propose(need, wf, notes, changed=wrote)
    if wf != workflow_of(before):
        need.workflow = wf
        need_store.save(conn, need, reason="orchestrator: " + ("; ".join(notes) or "updated"))
    return need


def workflow_of(raw: dict) -> dict:
    return workflow(need_store.NeedRecord(need_id="", created_at="", raw_text="", workflow=raw))


def _propose(need, wf: dict, notes: list[str], changed: bool = False) -> None:
    """The one pending action: the stage's first open gate or action, once
    its checks pass. Kept while the case is unchanged since it was made;
    `changed` says this call changed it (a flow's write or artifact)."""
    todo = missing(need, wf) if not wf.get("closed") else []
    first = todo[0] if todo else None
    if wf["pending"] and wf["pending"].get("claim") and not _expired(wf["pending"]["claim"]):
        return                                    # a confirmation is running on it
    if first is None or first["kind"] == "check":
        if wf["pending"]:
            notes.append(f"withdrew {wf['pending']['id']}")
        wf["pending"] = None
        return
    kind = _kind(first)
    pending = wf["pending"]
    unchanged_at = {pending["version"], (pending.get("claim") or {}).get("version")} \
        if pending else set()
    if pending and pending["kind"] == kind and need.version in unchanged_at and not changed:
        return                                    # still valid: nothing changed since
    rejected = wf["rejected"].get(kind)
    if rejected and rejected["version"] >= need.version and not changed:
        wf["pending"] = None                      # rejected, and nothing changed since
        return
    wf["pending"] = {
        "id": f"act_{secrets.token_hex(8)}", "kind": kind, "stage": wf["stage"],
        "summary": _summary(need, wf, first), "allowed_roles": _allowed(first),
        # the version this proposal is saved as: confirming it later is only
        # valid if the case is still at that version
        "version": need.version + 1,
    }
    notes.append(f"proposed {kind} as {wf['pending']['id']}")


def _summary(need, wf: dict, item: dict) -> str:
    source = {"quantity": "quantity", "estimate": "estimate", "clauses": "documents",
              "documents": "documents", "freeze_package": "package", "submit_pr": "pr",
              "approve": "approval_file", "handoff_nit": "handoff"}.get(item["name"])
    art = _artifact(wf, source) if source else None
    detail = art.get("summary") if isinstance(art, dict) else None
    verb = "Approve the" if item["kind"] == "gate" else "Confirm:"
    text = f"{verb} {item['name'].replace('_', ' ')} for {need.need_id}"
    return (f"{text}: {detail}" if detail else text)[:1000]


# -- confirming --------------------------------------------------------------------

def confirm(conn: sqlite3.Connection, caller: Caller, case_id: str,
            request: dict, *, home=None) -> tuple[dict, need_store.NeedRecord]:
    """Run the pending action `request['action_id']` for an allowed caller.
    Returns (result, record after settling). Confirming an action already
    confirmed returns its first result again and does nothing."""
    action_id = request["action_id"]
    decision = request.get("decision") or "approve"
    reason = (request.get("reason") or "").strip()
    need = need_store.load(conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    wf = workflow(need)

    done = wf["confirmed"].get(action_id)
    if done:
        _may_confirm(caller, done["kind"])
        return {**done, "action_id": action_id, "replay": True}, need
    pending = wf["pending"]
    if not pending:
        raise NamError("not_found", f"{case_id} has no pending action {action_id}",
                       case_id=case_id)
    if pending["id"] != action_id:
        raise NamError("stale_version", f"{action_id} is not the pending action of {case_id}: "
                       f"it was replaced by {pending['id']} when the case changed",
                       case_id=case_id, details=[f"pending: {pending['id']}"])
    claim = pending.get("claim")
    if claim and not _expired(claim):
        raise NamError("stale_version", f"{action_id} is being confirmed by another call",
                       case_id=case_id)
    # unchanged since it was proposed, or since a confirmation that died
    # after claiming it
    if need.version not in (pending["version"], (claim or {}).get("version")):
        raise NamError("stale_version", f"{case_id} changed since {action_id} was proposed; "
                       "run the case again for a fresh proposal", case_id=case_id)
    gate = pending["kind"].removeprefix("gate:") if pending["kind"].startswith("gate:") else ""
    _may_confirm(caller, pending["kind"])
    if decision == "reject" and not reason:
        raise NamError("bad_request", "a rejection needs a reason (confirm.reason)",
                       case_id=case_id)
    if decision == "approve" and not gate:
        guardrails.before_action(conn, pending["kind"], need, wf, home=home)

    actor = store.current()
    # claim it first: of two confirmations at once, one saves and the other
    # gets stale_version, so the action below runs once
    pending = {**pending, "claim": _stamp(actor, version=need.version + 1)}
    wf["pending"] = pending
    need.workflow = wf
    need_store.save(conn, need, reason=f"confirming {pending['kind']}")

    entry = {"kind": pending["kind"], "decision": decision, "reason": reason,
             "stage": pending["stage"], **_stamp(actor)}
    if decision == "reject":
        wf["rejected"][pending["kind"]] = {"version": need.version + 1, "reason": reason,
                                           **_stamp(actor)}
        entry["result"] = {}
    elif gate:
        wf["gates"][gate] = {"action_id": action_id, **_stamp(actor)}
        entry["result"] = {}
    else:
        kind = pending["kind"]
        try:
            entry["result"] = store.once(conn, action_id, case_id, kind,
                                         lambda: EFFECTS[kind](conn, need, action_id))
        except NamError:
            # refused inside the effect (a duplicate PR submitted at the same
            # moment): release the claim; the next call proposes it again
            wf["pending"] = None
            need.workflow = wf
            need_store.save(conn, need, reason=f"{kind} refused")
            raise
        _mark(kind, wf, entry["result"])
        wf["actions"][pending["kind"]] = {"action_id": action_id, "result": entry["result"],
                                          **_stamp(actor)}
    wf["confirmed"][action_id] = entry
    wf["pending"] = None
    need.workflow = wf
    need_store.save(conn, need, reason=f"{decision} {pending['kind']}"
                    + (f": {reason}" if reason else ""))
    after = settle(conn, case_id)
    return {**entry, "action_id": action_id, "replay": False}, after


def _may_confirm(caller: Caller, kind: str) -> None:
    if kind.startswith("gate:"):
        permissions.require_gate(caller, kind.removeprefix("gate:"))
    else:
        permissions.require_action(caller, kind)


def _expired(claim: dict) -> bool:
    try:
        at = datetime.fromisoformat(claim["at"])
    except (KeyError, ValueError):
        return True
    return (datetime.now(UTC) - at).total_seconds() > CLAIM_TIMEOUT_S
