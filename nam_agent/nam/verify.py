"""The feedback loop around every step: what a step claims is checked in code
against what the case now holds, before the reply goes out.

A flow says how it ended (ok, needs_input, ...). That is a claim. After the
guardrails took its artifacts and the stage machine settled the case, step()
reads the case back and checks:

  every step
    recorded        each artifact the step produced is on the case, written
                    by this flow in this call
    schema          each one is still inside its schema (schemas/artifact.*)
    cited           none of its facts is without a source (knowledge.uncited)
    stored          every document it names (doc_id, version) is in the
                    document store, and its file matches its hash
    answerable      a step that needs input says what: a question, a next
                    step or a warning, never a dead end
  a step that says ok, its own "done" (DONE below)
    e.g. estimate.goods: every line priced, of the quantity now finalised;
    pr.build: no mandatory field missing; validate: the validation passed and
    is of the documents as they are now

When a check fails, the step is not reported as done: "ok" becomes
"needs_input", the failed checks are warnings, and the step is offered
again in `next`. The reply always carries the result as `verification`:
{passed, checks: [{name, ok, says}], stage_needs: what the case's stage
still needs}, so the caller (and the officer) sees what was verified and
what is left, every step.

case() checks the whole case before the two acts nothing can follow back
(guardrails.before_action, freeze_package and handoff_nit): every artifact
inside its schema and cited, every document file intact, each artifact of
the inputs it was built on (estimate of the quantity, documents of the
estimate, validation of the documents), and the audit chain unbroken. Each
problem names the flow that owns it, to run again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from nam_agent.nam import audit, guardrails, knowledge, orchestrator, schema, store
from nam_agent.nam.errors import NamError

log = logging.getLogger("nam_agent.nam.verify")


@dataclass(frozen=True)
class Done:
    name: str
    says: str                                      # what is not done, in words
    test: Callable[..., bool]                      # (need, workflow, result) -> done


def _art(wf: dict, name: str) -> dict:
    return orchestrator._artifact(wf, name) or {}


def _at(wf: dict, name: str) -> int:
    return orchestrator._at(wf, name)


def _priced(need, wf, _r=None) -> bool:
    lines = _art(wf, "estimate").get("lines") or []
    return bool(lines) and all(x.get("rate") is not None for x in lines)


def _selection(need, wf, result) -> bool:
    shown = [c["case_key"] if isinstance(c, dict) else c for c in result.get("selected") or []]
    return list(need.selected_cases) == shown


_ESTIMATE = (
    Done("estimate_priced", "a line of the estimate has no rate (a New Item): give it",
         _priced),
    Done("estimate_current", "the estimate is of an earlier quantity: run it again",
         lambda need, wf, _r: _at(wf, "estimate") >= _at(wf, "quantity") > 0),
)

# what "ok" must mean, per flow; a flow not listed has only the checks of every step
DONE: dict[str, tuple[Done, ...]] = {
    "need.classify": (Done("classified", "the category, procurement type and tender type "
                           "are not all confirmed", lambda need, wf, _r: not need.unresolved),),
    "need.guided_form": (Done("form_complete", "the guided form has fields still to answer",
                              lambda need, wf, _r: bool(_art(wf, "guided_form").get("complete"))),),
    # what the step says is selected (none, when the officer said none fit)
    # is what the case holds
    "precedent.select": (Done("selection_recorded", "the selection the step reports is not "
                              "the one on the case", _selection),),
    "quantity.finalise": (Done("quantity_complete", "a line has no quantity or unit",
                               lambda need, wf, _r: bool(_art(wf, "quantity").get("complete"))
                               and all(x.get("qty") is not None and x.get("uom")
                                       for x in _art(wf, "quantity").get("lines") or [])),),
    "estimate.goods": _ESTIMATE, "estimate.wage": _ESTIMATE, "estimate.works": _ESTIMATE,
    "docs.generate": (Done("documents_complete", "a document misses a field or a clause",
                           lambda need, wf, _r: bool(_art(wf, "documents").get("complete"))),),
    "docs.review": (Done("no_open_conflict", "a conflict between clauses is still open",
                         lambda need, wf, _r: not _art(wf, "review").get("open")),),
    "compliance.build": (Done("compliance_decided", "a compliance requirement is undecided",
                              lambda need, wf, _r: bool(_art(wf, "compliance").get("complete"))),),
    "validate": (
        Done("validation_passed", "the validation did not pass: fix what it lists",
             lambda need, wf, _r: _art(wf, "validation").get("status") == "pass"),
        Done("validation_current", "the documents changed after this validation",
             lambda need, wf, _r: _at(wf, "validation") >= max(
                 _at(wf, n) for n in ("documents", "review", "compliance"))),
    ),
    "package.freeze": (Done("package_assembled", "the package is not assembled",
                            lambda need, wf, _r: bool(_art(wf, "package"))),),
    "pr.build": (Done("pr_complete", "the PR has mandatory fields missing",
                      lambda need, wf, _r: bool(_art(wf, "pr"))
                      and not _art(wf, "pr").get("missing")),),
    "pr.submit": (Done("pr_submitted", "the PR is not submitted",
                       lambda need, wf, _r: "submit_pr" in wf["actions"]),),
    "approval": (Done("approval_file_complete", "the approval file names no approving "
                      "authority", lambda need, wf, _r: bool(_art(wf, "approval_file")
                                                         .get("complete"))),),
    "package.publish": (Done("published", "the Aarohan upload list is not recorded",
                             lambda need, wf, _r: bool(_art(wf, "published"))),),
    # the list the reply shows is the one stored on the case, item by item
    "case.plan": (Done("todo_recorded", "the to-do list on the case is not the one reported",
                       lambda need, wf, r: [(i["flow"], i["status"]) for i in
                                            _art(wf, "todo").get("items") or []]
                       == [(i["flow"], i["status"]) for i in r.get("todo") or []]),),
    "handoff.nit": (Done("handoff_assembled", "the handoff package is not assembled",
                         lambda need, wf, _r: bool(_art(wf, "handoff"))),),
}


def _check(name: str, ok: bool, says: str) -> dict:
    return {"name": name, "ok": bool(ok), "says": ("ok" if ok else says)[:300]}


def _doc_refs(value) -> Iterator[tuple[str, int]]:
    """Every (doc_id, version) an artifact names, at any depth."""
    if isinstance(value, dict):
        doc_id, version = value.get("doc_id"), value.get("version")
        if isinstance(doc_id, str) and doc_id.startswith("DOC-") and isinstance(version, int):
            yield doc_id, version
        for v in value.values():
            yield from _doc_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from _doc_refs(v)


def _stored(conn, home, tenant: str, value) -> list[str]:
    """The documents an artifact names that are not in the store intact."""
    from nam_agent.nam.docs import store as docs

    bad = []
    for doc_id, version in sorted(set(_doc_refs(value)))[:200]:
        row = docs._version_row(conn, doc_id, version)
        if row is None:
            bad.append(f"{doc_id} v{version} is not in the document store")
            continue
        if home is None:
            continue
        try:
            docs.get_blob(home, tenant, row["sha256"])
        except NamError as exc:
            bad.append(f"{doc_id} v{version}: {exc.message}")
    return bad


def _artifact_checks(conn, home, tenant: str, wf: dict, name: str) -> list[dict]:
    value = orchestrator._artifact(wf, name)
    out = []
    if name in guardrails.SCHEMAS:
        problems = schema.validate(value, schema.load(guardrails.SCHEMAS[name]))
        out.append(_check("schema", not problems, f"the {name} is outside its schema: "
                          + "; ".join(problems[:3])))
    bare = knowledge.uncited({name: value})
    out.append(_check("cited", not bare, f"{len(bare)} fact(s) of the {name} have no source: "
                      + "; ".join(bare[:2])))
    missing = _stored(conn, home, tenant, value)
    out.append(_check("stored", not missing, "; ".join(missing[:3])))
    return out


def step(conn, home, flow_id: str, outcome, need, run_id: str, *, unapplied: str = ""
         ) -> dict:
    """The verification of one step, from the case as stored after it.
    `unapplied`: the user's words that looked like a change the harness could
    not read (intent.edit_like): whatever else the step did, it did not do
    what they asked, and must not read as done."""
    checks: list[dict] = []
    if unapplied:
        from nam_agent.nam import intent

        qid = intent.ANSWER_IDS.get(flow_id, "")
        checks.append(_check("message_applied", False,
                             f"the words '{unapplied[:80]}' changed nothing: give the change as "
                             f"the answer '{qid}' (e.g. '1: qty=35', '1: rate=520', "
                             "'all: cost_centre=4100'), naming the line when there are several"))
    wf = orchestrator.workflow(need) if need is not None else {"artifacts": {}, "actions": {}}
    tenant = store.current().tenant
    for name in sorted(outcome.artifacts):
        entry = (wf.get("artifacts") or {}).get(name) or {}
        recorded = entry.get("flow") == flow_id and entry.get("run_id") == run_id
        checks.append(_check("recorded", recorded, f"the {name} this step produced is not on "
                             "the case"))
        if recorded:
            checks += [{**c, "name": f"{c['name']}:{name}"}
                       for c in _artifact_checks(conn, home, tenant, wf, name)]
    if outcome.status == "needs_input":
        checks.append(_check("answerable", outcome.questions or outcome.next or outcome.warnings,
                             "the step needs input but says neither what nor why"))
    if outcome.status == "ok" and need is not None:
        for done in DONE.get(flow_id, ()):
            checks.append(_check(done.name, done.test(need, wf, outcome.result or {}),
                                 done.says))
    return _result(checks, need)


def confirmed(result: dict, need) -> dict:
    """The verification of a confirmation: the decision is on the case."""
    wf = orchestrator.workflow(need)
    kind, action_id = result.get("kind", ""), result.get("action_id", "")
    recorded = action_id in wf["confirmed"]
    if result.get("decision") == "approve" and not result.get("replay"):
        if kind.startswith("gate:"):
            recorded = recorded and kind.removeprefix("gate:") in wf["gates"]
        else:
            recorded = recorded and kind in wf["actions"]
    return _result([_check("recorded", recorded, f"{kind} is not recorded on the case")], need)


def nothing_ran(need) -> dict:
    """The verification of a reply that ran no step (routing asked or refused)."""
    return _result([], need)


def _result(checks: list[dict], need) -> dict:
    stage_needs = []
    if need is not None:
        wf = orchestrator.workflow(need)
        if not wf.get("closed"):
            stage_needs = [m["says"][:300] for m in orchestrator.missing(need, wf)][:20]
    return {"passed": all(c["ok"] for c in checks), "checks": checks[:60],
            "stage_needs": stage_needs}


def applied(outcome, verification: dict, flow_id: str):
    """The outcome as the reply states it: a step that said ok but failed a
    check is not done. It needs input, says why, and is offered again."""
    import dataclasses

    failed = [c for c in verification["checks"] if not c["ok"]]
    if not failed:
        return outcome
    log.warning("NAM %s did not verify: %s", flow_id, "; ".join(c["says"] for c in failed))
    said = tuple(f"Not verified ({c['name']}): {c['says']}" for c in failed)
    if outcome.status != "ok":
        return dataclasses.replace(outcome, warnings=(*outcome.warnings, *said))
    return dataclasses.replace(outcome, status="needs_input",
                               warnings=(*outcome.warnings, *said),
                               next=tuple(dict.fromkeys((flow_id, *outcome.next))))


# -- the whole case, before freeze and handoff --------------------------------------

# which flow to run again when an artifact is wrong
OWNER = {name: flows[0] for name, flows in orchestrator.ARTIFACTS.items()}
_BUILT_ON = (("estimate", "quantity"), ("documents", "estimate"), ("validation", "documents"),
             ("validation", "review"), ("validation", "compliance"))


def case(conn, home, need) -> list[str]:
    """Every problem of the whole case, each naming the flow to run again."""
    wf = orchestrator.workflow(need)
    tenant = store.current().tenant
    out = []
    for name in sorted(wf["artifacts"]):
        for c in _artifact_checks(conn, home, tenant, wf, name):
            if not c["ok"]:
                out.append(f"{c['says']} (run {OWNER.get(name, name)})")
    for later, earlier in _BUILT_ON:
        if later in wf["artifacts"] and earlier in wf["artifacts"] \
                and _at(wf, later) < _at(wf, earlier):
            out.append(f"the {later} is older than the {earlier} it is built on (run "
                       f"{OWNER.get(later, later)})")
    chain = audit.verify(conn)
    if not chain["ok"]:
        out.append(f"the audit log is broken: {chain['says']}")
    return out
