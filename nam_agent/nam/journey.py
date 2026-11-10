"""Where a case is on the officer's journey: what was done, where it is now,
what the user still has to give, and what comes next.

Everything here is read from the harness, never written for a stage:

  stages      orchestrator.STAGES, each check re-run on the case now
  flows       playbooks.registry(): which flows work in a stage, and which
              flow a missing check names
  approvers   policy/permissions.json: who gives each approval or action
  produced    the artifacts on the case (orchestrator.ARTIFACTS), with the
              summary each flow left
  to give     the case itself: the open follow-up question, the fields not
              confidently classified, every artifact's `missing` list, lines
              with no rate and lines flagged for review
  the steps   policy/journey.json: the PR Creation Flow's phases and steps
              (Need Identification, Estimate creation, PR Creation) laid
              over those stages, each with the conditions that make it done

journey.json is data: changing the document's steps, or which stage a step
belongs to, needs no code. `problems()` checks that every stage, check,
gate, action and artifact it names exists in the harness, so a typo there
fails an eval instead of silently marking a step done.

The chat beside a Procurix screen, the main chat once a case is open in
the conversation, case.status and /api/nam/workflow all show the same
journey, so the model (large or small) is told the facts, not asked to
work them out.
"""

from __future__ import annotations

import dataclasses
import json
from functools import cache
from pathlib import Path

from nam_agent.nam import need as need_store
from nam_agent.nam import orchestrator, permissions, playbooks

POLICY = Path(__file__).parent / "policy" / "journey.json"
MAX_TO_GIVE = 12                  # what the user still has to give, at most, in one view


@cache
def policy() -> dict:
    return json.loads(POLICY.read_text(encoding="utf-8"))


def end_stage() -> str:
    """Where NAM ends: the last stage of the PR flow's phases (the PR)."""
    order = [s.id for s in orchestrator.STAGES]
    stages = [sid for ph in policy().get("phases") or [] for sid in ph.get("stages") or []]
    return max(stages, key=order.index) if stages else order[-1]


def problems(data: dict | None = None) -> list[str]:
    """Every name journey.json uses that the harness does not have."""
    data = data or policy()
    stage_ids = {s.id for s in orchestrator.STAGES}
    names = {
        "check": {c.name for s in orchestrator.STAGES for c in s.checks},
        "gate": {g for s in orchestrator.STAGES for g in s.gates},
        "action": {s.action for s in orchestrator.STAGES if s.action},
        "artifact": set(orchestrator.ARTIFACTS),
    }
    fields = {f.name for f in dataclasses.fields(need_store.NeedRecord)}
    out, seen = [], set()
    for phase in data.get("phases") or []:
        for sid in phase.get("stages") or []:
            if sid not in stage_ids:
                out.append(f"phase {phase.get('id')}: no stage {sid}")
        for step in phase.get("steps") or []:
            sid = step.get("id")
            if sid in seen:
                out.append(f"step {sid} is listed twice")
            seen.add(sid)
            if step.get("stage") not in (phase.get("stages") or []):
                out.append(f"step {sid}: stage {step.get('stage')} is not in its phase")
            for cond in _flat(step.get("done_when") or []):
                kind, _, name = cond.lstrip("!").partition(":")
                head = name.split(".")[0]
                if kind == "record":
                    ok = head in fields
                elif kind == "flagged":
                    ok = head in names["artifact"]
                elif kind in names:
                    ok = head in names[kind]
                else:
                    ok = False
                if not ok:
                    out.append(f"step {sid}: unknown condition {cond}")
    return out


def _flat(conds: list) -> list[str]:
    """The conditions of a done_when, any-of groups opened up."""
    return [c for item in conds for c in (item if isinstance(item, list) else [item])]


# -- reading the case ----------------------------------------------------------------

def _dig(value, path: list[str]):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _flagged(value) -> bool:
    flags = policy().get("flags") or {}
    return any(isinstance(x, dict) and str((x.get("comparison") or {}).get("flag") or "") in flags
               for x in (value or {}).get("lines") or []) if isinstance(value, dict) else False


def _holds(cond, need, wf: dict, record: dict) -> bool:
    if isinstance(cond, list):                  # an any-of group
        return any(_holds(c, need, wf, record) for c in cond)
    negate = cond.startswith("!")
    kind, _, name = cond.lstrip("!").partition(":")
    head, *path = name.split(".")
    if kind == "record":
        value = _dig(record.get(head), path)
    elif kind == "artifact":
        value = _dig(orchestrator._artifact(wf, head), path)
    elif kind == "check":
        check = next((c for s in orchestrator.STAGES for c in s.checks if c.name == head), None)
        value = bool(check and check.test(need, wf))
    elif kind == "flagged":
        value = _flagged(orchestrator._artifact(wf, head))
    elif kind == "gate":
        value = head in wf["gates"]
    elif kind == "action":
        value = head in wf["actions"]
    else:
        value = None
    return (not value) if negate else bool(value)


def _roles(names: list[str]) -> list[str]:
    labels = permissions.policy().get("roles") or {}
    return [labels.get(r, r) for r in names]


def _approvers(section: str, name: str) -> list[str]:
    return _roles(list(permissions.policy()[section].get(name, [])))


def _flows_named(text: str, flows: dict) -> list[str]:
    """The ids of the flows a check's words name, e.g. '(quantity.finalise)'."""
    return [f.id for f in flows.values() if f.id in text]


def _stage_flows(stage_id: str, flows: dict) -> list[str]:
    return [f"{f.id} ({f.title})" for f in flows.values()
            if stage_id in f.stages and playbooks.ANY_STAGE not in f.stages]


def _produced(wf: dict, flows: dict) -> dict[str, list[dict]]:
    """The artifacts on the case, by the stage of the flow that left them."""
    out: dict[str, list[dict]] = {}
    for name, art in (wf.get("artifacts") or {}).items():
        flow = flows.get(str((art or {}).get("flow") or ""))
        value = (art or {}).get("value")
        if flow is None or not value:
            continue
        summary = value.get("summary") if isinstance(value, dict) else ""
        out.setdefault(flow.stage, []).append(
            {"artifact": name, "flow": flow.id, "summary": str(summary or "")[:200]})
    return out


def _line_name(line: dict, i: int) -> str:
    item = " ".join(str(line.get("item") or line.get("description") or "").split())[:60]
    return f"line {i + 1}" + (f" ({item})" if item else "")


def to_give(need, wf: dict) -> list[str]:
    """What the user still has to give on this case, read from the case."""
    flags = policy().get("flags") or {}
    out: list[str] = []
    pending = (need.followups or {}).get("pending")
    if pending:
        options = ", ".join(str(o.get("label") or o.get("value"))
                            for o in pending.get("options") or [])
        out.append(f"answer the open question \"{pending.get('text')}\""
                   + (f" (options: {options})" if options else ""))
    if need.unresolved:
        out.append("confirm the " + ", ".join(f.replace("_", " ") for f in need.unresolved))
    pending_decisions = set(orchestrator.undecided(orchestrator._artifact(wf, "estimate")))
    for name, art in (wf.get("artifacts") or {}).items():
        value = (art or {}).get("value")
        if not isinstance(value, dict):
            continue
        missing = [str(m) for m in value.get("missing") or [] if m]
        if missing:
            out.append(f"{name.replace('_', ' ')}: give " + ", ".join(missing[:8]))
        for i, line in enumerate(value.get("lines") or []):
            if not isinstance(line, dict):
                continue
            if "rate" in line and line.get("rate") is None:
                out.append(f"{name}: the rate of {_line_name(line, i)}, which has no past price")
            flag = str((line.get("comparison") or {}).get("flag") or "")
            if name == "estimate" and i not in pending_decisions:
                continue
            if flag in flags:
                out.append(f"{name}: accept or revise {_line_name(line, i)}, flagged "
                           f"{flag}: {flags[flag]}")
    return out[:MAX_TO_GIVE]


# -- the journey -------------------------------------------------------------------------

def build(need) -> dict:
    """The case's journey, as a JSON-safe dict."""
    wf = orchestrator.workflow(need)
    flows = playbooks.registry()
    record = dataclasses.asdict(need)
    order = [s.id for s in orchestrator.STAGES]
    closed = bool(wf.get("closed"))
    at = order.index(wf["stage"]) if wf["stage"] in order else 0
    phase_of = {sid: p for p in policy().get("phases") or [] for sid in p.get("stages") or []}
    produced = _produced(wf, flows)

    stages = []
    for i, st in enumerate(orchestrator.STAGES):
        state = "done" if closed or i < at else "current" if i == at else "ahead"
        entry = {"id": st.id, "name": st.name, "state": state,
                 "phase": (phase_of.get(st.id) or {}).get("name", ""),
                 "produced": produced.get(st.id, [])}
        if state == "done":
            entry["approved"] = [
                {"name": k, "by": str((wf[sec].get(k) or {}).get("user") or "")}
                for sec, keys in (("gates", st.gates), ("actions", (st.action,) if st.action else ()))
                for k in keys if k in wf[sec]]
        else:
            open_items = orchestrator.missing(need, wf, st) if not closed else []
            entry["needs"] = [
                {"kind": m["kind"], "name": m["name"], "says": m["says"],
                 "flows": [f"{fid} ({flows[fid].title})" for fid in named],
                 "flow_ids": named,
                 "by": _approvers("gates" if m["kind"] == "gate" else "actions", m["name"])
                 if m["kind"] != "check" else []}
                for m in open_items
                for named in [_flows_named(m["says"], flows) if m["kind"] == "check" else []]]
            entry["flows"] = _stage_flows(st.id, flows)
        stages.append(entry)

    steps, current_step = [], None
    for phase in policy().get("phases") or []:
        for step in phase.get("steps") or []:
            idx = order.index(step["stage"])
            conds = step.get("done_when") or []
            if not conds:
                state = "with_stage"          # a behaviour of the stage, not a milestone
            elif closed or idx < at:
                state = "done"
            elif idx > at:
                state = "ahead"
            elif all(_holds(c, need, wf, record) for c in conds):
                state = "done"
            else:
                state = "current" if current_step is None else "ahead"
            if state == "current":
                current_step = step["id"]
            steps.append({"id": step["id"], "phase": phase["name"], "stage": step["stage"],
                          "name": step["name"], "says": step.get("says", ""),
                          "user": step.get("user", ""), "nam": step.get("nam", ""),
                          "state": state})

    milestones = [s for s in steps if s["state"] != "with_stage"]
    done = [s for s in milestones if s["state"] == "done"]
    cur = next((s for s in milestones if s["state"] == "current"), None)
    nxt = next((s for s in milestones if s["state"] == "ahead"), None)
    stage = stages[at]
    # NAM ends where the PR flow does (its last phase's last stage): the
    # harness's stages after it are the next module's, not a next step here
    end = order.index(end_stage())
    following = stages[at + 1] if at + 1 <= end and not closed else None
    return {
        "case_id": need.need_id,
        "closed": closed,
        "stage": {"id": stage["id"], "name": stage["name"]},
        "phase": stage["phase"],
        "stages": stages,
        "steps": steps,
        "previous_step": done[-1]["id"] if done else None,
        "step": cur["id"] if cur else None,
        "next_step": nxt["id"] if nxt else None,
        "next_stage": ({"id": following["id"], "name": following["name"],
                        "phase": following["phase"]} if following else None),
        "to_give": to_give(need, wf) if not closed else [],
        # the estimate lines whose flag needs the officer's Accept or Revise (2(e))
        "undecided": orchestrator.undecided(orchestrator._artifact(wf, "estimate")),
        "pending": orchestrator.public(wf.get("pending")),
    }


def _step_line(s: dict) -> str:
    return f"{s['id']} {s['name']} ({s['phase']}, {s['stage']})"


def text(j: dict) -> str:
    """The journey in a few lines, for the model's system prompt."""
    by_id = {s["id"]: s for s in j["steps"]}
    phases = [p["name"] for p in policy().get("phases") or []]
    lines = [(f"Where NAM case {j['case_id']} is on the PR journey, from the NAM harness "
              f"(phases: {' -> '.join(phases)}):")]
    if j["closed"]:
        lines.append("The case is closed: every stage is done.")
        return "\n".join(lines)
    st = j["stage"]
    lines.append(f"Stage now: {st['id']} {st['name']}"
                 + (f", in the {j['phase']} phase." if j["phase"] else
                    ", a harness stage outside the three phases of the PR flow."))
    done = [s for s in j["stages"] if s["state"] == "done"]
    if done:
        made = [f"{s['id']} {s['name']}" + (
            " (" + "; ".join(f"{p['artifact']}: {p['summary']}" if p["summary"] else p["artifact"]
                             for p in s["produced"]) + ")" if s["produced"] else "")
            for s in done]
        lines.append("Done before: " + "; ".join(made) + ".")
    if j["previous_step"]:
        lines.append(f"Last step done: {_step_line(by_id[j['previous_step']])}.")
    if j["step"]:
        s = by_id[j["step"]]
        lines.append(f"Step now: {_step_line(s)}: {s['says']}"
                     + (f" The user gives: {s['user']}." if s["user"] else ""))
    current = next(s for s in j["stages"] if s["state"] == "current")
    if current.get("needs"):
        lines.append("This stage still needs: " + "; ".join(
            n["says"] + (f" [by {', '.join(n['by'])}]" if n["by"] else "")
            + (f" [flow: {', '.join(n['flows'])}]" if n["flows"] else "")
            for n in current["needs"]) + ".")
    if j["to_give"]:
        lines.append("The user still has to give: " + "; ".join(j["to_give"]) + ".")
    if j["pending"]:
        p = j["pending"]
        lines.append(f"Waiting for approval: {p['summary']} (by "
                     f"{', '.join(_roles(p.get('allowed_roles') or []))}).")
    if j["next_step"]:
        s = by_id[j["next_step"]]
        lines.append(f"Next step: {_step_line(s)}: {s['says']}"
                     + (f" The user will give: {s['user']}." if s["user"] else ""))
    if j["next_stage"]:
        nxt = next(s for s in j["stages"] if s["id"] == j["next_stage"]["id"])
        needs = "; ".join(n["says"] for n in nxt.get("needs") or [])
        lines.append(f"Next stage: {nxt['id']} {nxt['name']}"
                     + (f"; it will need: {needs}" if needs else "") + ".")
    lines.append("Guide the officer from these facts: say which step this is, ask for what is "
                 "still to give one thing at a time, and say what comes next. Do not invent "
                 "steps, values or approvals the harness did not report. NAM ends with the "
                 "Purchase Request and its sheets (requirement, estimate, PR); NIT generation "
                 "is not part of NAM, so never offer or mention a NIT.")
    return "\n".join(lines)


def next_hint(j: dict) -> str:
    """One line: what the user does next, for a harness-written reply."""
    by_id = {s["id"]: s for s in j["steps"]}
    if j["to_give"]:
        return f"Next: {j['to_give'][0]}."
    for key in ("step", "next_step"):
        if j.get(key):
            s = by_id[j[key]]
            return (f"Next: step {s['id']} {s['name']} ({s['phase']})"
                    + (f": {s['user']}." if s["user"] else "."))
    if j["next_stage"]:
        return f"Next: {j['next_stage']['id']} {j['next_stage']['name']}."
    return ""


def focus(conn, case_id: str, screen: str = "") -> dict | None:
    """The chat's focus on a case: its id, title, stage and journey."""
    need = need_store.load(conn, case_id)
    if need is None:
        return None
    j = build(need)
    pending = j["pending"] or {}
    current = next(s for s in j["stages"] if s["id"] == j["stage"]["id"])
    return {"case_id": case_id, "screen": screen, "title": need.raw_text[:160],
            "stage": j["stage"]["id"], "stage_name": j["stage"]["name"],
            "needs": [n["says"] for n in current.get("needs") or []][:8],
            "pending": (f"{pending.get('summary')} (by "
                        f"{', '.join(_roles(pending.get('allowed_roles') or []))})"
                        if pending else ""),
            "journey": text(j)}
