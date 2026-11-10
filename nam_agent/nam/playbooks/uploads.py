"""document.update (FRS F39): documents edited outside NAM come back to the
case (S9, and S10 until the handoff).

The edited file is uploaded as a new version of its document through the
document endpoints (PUT /v1/nam/documents/{doc}); a corrigendum or another
new file is uploaded to the case (POST). This flow reads what came back
against the frozen package:

  - for each package document with a newer version, how much the edit
    changed it: accuracy 100% when the text is the same, else the share of
    the generated text the edited one kept (word by word);
  - each file added to the case after the freeze;
  - the documents the officer still awaits:
      answer {question_id: "expect", value: "scc, boq"}  ("none" clears it)
    A document awaited and not yet uploaded holds the stage, and the handoff
    (R32).

Every version stays in the document store, so what the officers changed is
kept for later learning (R40), under the store's own retention.
"""

from __future__ import annotations

import difflib
import re

from nam_agent.nam import need as need_store
from nam_agent.nam import orchestrator, store
from nam_agent.nam.docs import store as doc_store
from nam_agent.nam.errors import NamError
from nam_agent.nam.identity import Caller
from nam_agent.nam.playbooks import Outcome

EXPECT = "expect"


def _words(ctx, caller, doc_id: str, version: int | None) -> list[str]:
    body = doc_store.view(ctx, caller, doc_id, version)
    text = " ".join(p.get("text") or "" for p in body["content"]["pages"])
    return re.findall(r"\w+", text.lower())[:200000]


def accuracy(before: list[str], after: list[str]) -> float:
    """The share of the generated words the edited text kept, in percent."""
    if before == after:
        return 100.0
    if not before:
        return 0.0
    matched = sum(b.size for b in difflib.SequenceMatcher(None, before, after,
                                                          autojunk=False).get_matching_blocks())
    return round(100.0 * matched / max(len(before), len(after)), 1)


def handle(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    wf = orchestrator.workflow(need)
    package = orchestrator._artifact(wf, "package")
    if not package or not wf.get("frozen"):
        raise NamError("stage_gate", "there is no frozen package to update (package.freeze)",
                       case_id=case_id)
    answer = request.get("answer") or {}
    if answer and answer.get("question_id") != EXPECT:
        raise NamError("bad_request", f"document.update asks only '{EXPECT}'")
    stored = orchestrator._artifact(wf, "uploads") or {}
    actor = store.current()
    caller = Caller(tenant=actor.tenant, user=actor.user, roles=tuple(actor.roles))
    warnings: list[str] = []

    in_package = {d["doc_id"]: d for d in package["documents"] if d.get("doc_id")}
    kinds = {d["kind"] for d in package["documents"]}
    updates, added = [], []
    for doc in doc_store.listing(ctx, caller, case_id)["documents"]:
        frozen = in_package.get(doc["doc_id"])
        if frozen is None:
            added.append({"kind": doc["kind"], "doc_id": doc["doc_id"],
                          "version": doc["latest_version"], "filename": doc["filename"][:200]})
            continue
        if frozen.get("version") and doc["latest_version"] > frozen["version"]:
            pct = accuracy(_words(ctx, caller, doc["doc_id"], frozen["version"]),
                           _words(ctx, caller, doc["doc_id"], doc["latest_version"]))
            updates.append({"kind": frozen["kind"], "doc_id": doc["doc_id"],
                            "from_version": frozen["version"],
                            "to_version": doc["latest_version"], "accuracy_pct": pct,
                            "rework_pct": round(100.0 - pct, 1)})

    latest = {d["kind"]: d.get("version") for d in package["documents"]}
    for u in updates:
        latest[u["kind"]] = u["to_version"]
    expected = list(stored.get("expected") or [])
    given = str(answer.get("value") or "").strip().lower()
    if given:
        wanted = [] if given == "none" else [k.strip() for k in given.split(",") if k.strip()]
        unknown = [k for k in wanted if k not in kinds]
        if unknown:
            warnings.append(f"not in the package: {', '.join(unknown[:5])} (the package has "
                            f"{', '.join(sorted(kinds))})")
        else:
            # awaited: a version after the one the case holds now
            expected = [{"kind": k, "since_version": latest.get(k)} for k in wanted]
    awaiting = [e["kind"] for e in expected if latest.get(e["kind"]) == e["since_version"]]
    value = {"summary": (f"{len(updates)} document(s) updated, {len(added)} added"
                         + (f", awaiting {', '.join(awaiting)}" if awaiting else ""))[:300],
             "awaiting": bool(awaiting), "awaited": awaiting, "expected": expected,
             "updates": updates, "added": added[:100]}
    questions = ()
    if awaiting:
        questions = ({"id": EXPECT, "source": "harness", "options": [],
                      "text": (f"Upload the edited {', '.join(awaiting)} as a new version, or "
                               "say 'none' if no edit is coming."),
                      "why": "An awaited document holds the handoff to NIT (R32)."},)
    return Outcome(
        case_id=case_id, status="needs_input" if awaiting else "ok",
        result={"updates": updates, "added": added[:100], "awaiting": awaiting,
                "kept": "every version stays in the document store (R40)"},
        questions=questions, warnings=tuple(warnings),
        next=() if awaiting else ("handoff.nit",),
        artifacts={"uploads": value} if value != stored else {})
