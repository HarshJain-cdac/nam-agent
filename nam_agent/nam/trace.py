"""A case's trace (plan Step 17): any output traced back to its inputs and
its approvals, from what the harness already keeps.

    GET /v1/nam/cases/{id}/trace                 the trace as one JSON body
    GET /v1/nam/cases/{id}/trace?format=jsonl    one line per event, for export
    GET /v1/nam/runs/{run_id}                    one call: its audit rows

  calls       every harness call on the case: who (tenant, user, roles),
              which flow, how it was routed, how it ended (audit kind "call")
  history     every version of the Need Record: who wrote it, from which
              flow and call, and why (store.versions)
  artifacts   per artifact (estimate, documents, pr, ...): the flow, call,
              user and case version that wrote it, and the tool calls, KG
              fetches and model attempts of that call (the audit rows of its
              run_id), so each value leads to what it was made from
  evidence    why each rate and clause is there, with its authority
              (knowledge.py: precedent, nrl_schedule, reference, policy,
              officer, derived): per estimate line its
              basis and source (a KG tool result, the officer who typed it,
              a policy rule); per document clause its source and the reason
              for a change; the precedents and who selected them; the review
              findings accepted and why
  approvals   every gate and irreversible action: kind, decision, reason,
              who, when, in which call
  events      every audit row of the case's calls (tool, kg_fetch, model,
              document, guard)
  integrity   the audit log's hash chain (audit.verify)

Everything here is read from the case's tenant only, and the prompts are
not in it (audit.py stores none).
"""

from __future__ import annotations

import json
import sqlite3

from nam_agent.nam import audit, knowledge, orchestrator, store
from nam_agent.nam import need as need_store
from nam_agent.nam.errors import NamError

_EVENT_KEYS = ("id", "at", "user", "run_id", "flow", "kind", "name", "effect", "status",
               "error_code", "attempts", "ms")


def _event(e: dict) -> dict:
    out = {k: e[k] for k in _EVENT_KEYS}
    detail = e.get("detail") or {}
    for key in ("model", "fallback", "outcome", "system", "roles", "route", "pending",
                "confirm", "kind", "filename"):
        if key in detail:
            out[key] = detail[key]
    return out


def _line_evidence(est: dict) -> list[dict]:
    out = []
    for n, x in enumerate(est.get("lines") or [], 1):
        src = x.get("source") or {}
        if src.get("rule") == "escalation":
            # the last price (cited) brought up to the evaluation date by policy
            base = src.get("base") or {}
            how = (f"{base.get('tool', '')} {base.get('evidence', '')}, "
                   f"+{src.get('pct')}% a year for {src.get('years')} year(s)").strip()
        elif src.get("tool"):
            how = f"{src['tool']} {src.get('evidence', '')}".strip()
        elif src.get("officer"):
            how = f"typed by {src['officer']}"
        elif x.get("components"):
            parts = []
            for comp in x["components"]:
                cs = comp.get("source") or {}
                parts.append(cs.get("rule") or (f"typed by {cs['officer']}" if cs.get("officer")
                                                else cs.get("tool") or "line"))
            how = "built from " + ", ".join(dict.fromkeys(parts))
        else:
            how = "no rate: a New Item"
        auths = sorted({knowledge.authority(c.get("source"), x.get("schedule"))
                        for c in x.get("components") or []}
                       or {knowledge.authority(src, x.get("schedule"))} - {""})
        out.append({"line": n, "item": x["item"][:200], "rate": x.get("rate"),
                    "basis": x.get("basis"), "source": how[:300],
                    "authority": ", ".join(a for a in auths if a)})
    return out


def _clause_evidence(docs: dict) -> list[dict]:
    out = []
    for doc in docs.get("documents") or []:
        for n, c in enumerate(doc.get("clauses") or [], 1):
            if c.get("selected") or c.get("why"):
                out.append({"doc": doc["kind"], "clause": n, "selected": c["selected"],
                            "source": c["source"],
                            "authority": knowledge.authority(c["source"]), "class": c["class"],
                            "why": c.get("why") or "", "text": c["text"][:300]})
    return out[:2000]


def build(conn: sqlite3.Connection, case_id: str) -> dict:
    need = need_store.load(conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    wf = orchestrator.workflow(need)
    events = audit.events(conn, case_id=case_id)
    # a call that opened the case ran before it had an id: its rows are
    # found by the call's run_id
    have = {e["id"] for e in events}
    for run_id in {e["run_id"] for e in events if e["kind"] == "call"}:
        events += [e for e in audit.events(conn, run_id=run_id) if e["id"] not in have]
    events.sort(key=lambda e: e["id"])
    by_run: dict[str, list[dict]] = {}
    for e in events:
        if e["kind"] != "call":
            by_run.setdefault(e["run_id"], []).append(_event(e))
    history = store.versions(conn, case_id)

    artifacts = {}
    for name, entry in sorted(wf["artifacts"].items()):
        run_id = entry.get("run_id") or ""
        artifacts[name] = {"flow": entry.get("flow"), "user": entry.get("user"),
                           "at": entry.get("at"), "run_id": run_id,
                           "case_version": entry.get("at_version"),
                           "inputs": by_run.get(run_id, [])[:200]}
    selected = [{"case_version": v["case_version"], "user": v["by"]["user"], "flow": v["flow"],
                 "reason": v["reason"][:300]} for v in history
                if v["flow"] in ("precedent.select", "need.replicate")]
    review = orchestrator._artifact(wf, "review") or {}
    evidence = {
        "estimate": _line_evidence(orchestrator._artifact(wf, "estimate") or {}),
        "clauses": _clause_evidence(orchestrator._artifact(wf, "documents") or {}),
        "precedents": {"selected": list(need.selected_cases), "by": selected},
        "accepted_findings": [{"says": f["says"], "why": f["why"]}
                              for f in review.get("findings") or [] if f.get("accepted")],
        "validation": (orchestrator._artifact(wf, "validation") or {}).get("status"),
        # Step 13: every fact with its authority; "uncited" lists any without one
        "facts": knowledge.facts({n: orchestrator._artifact(wf, n) for n in
                                  ("estimate", "documents", "compliance", "materials")})[:4000],
        "uncited": knowledge.uncited({n: orchestrator._artifact(wf, n) for n in
                                      ("estimate", "documents", "compliance", "materials")}),
    }
    approvals = [{"action_id": aid, "kind": c["kind"], "decision": c["decision"],
                  "reason": c.get("reason") or "", "user": c.get("user"), "at": c.get("at"),
                  "run_id": c.get("run_id"), "stage": c.get("stage")}
                 for aid, c in wf["confirmed"].items()]
    calls = [_event(e) for e in events if e["kind"] == "call"]
    return {"case_id": case_id, "stage": wf["stage"], "closed": bool(wf.get("closed")),
            "frozen_at_version": (wf.get("frozen") or {}).get("at_version"),
            "calls": calls, "history": history, "artifacts": artifacts,
            "events": [_event(e) for e in events if e["kind"] != "call"][-2000:],
            "evidence": evidence, "approvals": approvals, "integrity": audit.verify(conn)}


def jsonl(trace: dict) -> str:
    """One JSON object per line: each call, version, approval and audit row,
    with what it is, in time order, for export to another system."""
    rows = [{"type": "call", **c} for c in trace["calls"]]
    rows += [{"type": "version", **v} for v in trace["history"]]
    rows += [{"type": "approval", **a} for a in trace["approvals"]]
    for name, art in trace["artifacts"].items():
        rows.append({"type": "artifact", "name": name,
                     **{k: v for k, v in art.items() if k != "inputs"}})
    rows += [{"type": "event", **e} for e in trace["events"]]
    rows.sort(key=lambda r: str(r.get("at") or ""))
    rows.append({"type": "integrity", **trace["integrity"]})
    return "".join(json.dumps(r, default=str, sort_keys=True) + "\n" for r in rows)


def for_run(conn: sqlite3.Connection, tenant: str, run_id: str) -> dict:
    """One call's audit rows, the caller's tenant only."""
    rows = [_event(e) for e in audit.events(conn, run_id=run_id) if e["tenant"] == tenant]
    if not rows:
        raise NamError("not_found", f"no call {run_id}")
    return {"run_id": run_id, "case_id": next((e["case_id"] for e in audit.events(
        conn, run_id=run_id) if e["case_id"]), None), "events": rows}
