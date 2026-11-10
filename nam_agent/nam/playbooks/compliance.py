"""compliance.build (FRS F26): the compliance requirements of the case, as
the user department endorses them (S5). NAM builds them; the NIT module
applies them with the commercial terms, and the bid stage only verifies.

  1. The requirements the category may need are proposed from
     policy/compliance.json (DRAFT): MSE purchase preference and EMD
     exemption, Make in India, OEM authorisation, startup relaxation, the
     technical compliance statement, labour laws.
  2. The department decides each one, and may add its own:
     answer {question_id: "requirements", value: "1: apply=yes; 2: apply=no,
             why=not divisible; add: text=..., why=...; all: apply=yes"}
     Not applying one, or adding one, needs its reason.
  2a. Beside a requirement NRL's standing NIT text exists for (MSE, PP-LC,
     PTR relaxation), the wording NRL actually issued is shown, verbatim
     with its tender (the KG's boilerplate, authority precedent): evidence
     for the department, never the requirement itself. A KG that cannot
     be reached leaves it out, with a warning.
  3. Once every requirement is decided the list is complete and stored as a
     .docx in the case's documents; it goes into the package and the NIT
     handoff. validate warns while it is missing or undecided.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from nam_agent.nam import need as need_store
from nam_agent.nam import orchestrator, store, tools
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome, documents, lines, need_classify

POLICY = Path(__file__).resolve().parents[1] / "policy" / "compliance.json"
ANSWER = "requirements"
MAX = 100


@cache
def policy() -> dict:
    return json.loads(POLICY.read_text(encoding="utf-8"))


ISSUED = 2                        # issued wordings shown per requirement


def _proposed(category: str) -> list[dict]:
    return [{"id": r["id"], "text": r["text"], "basis": r["basis"], "source": "policy",
             "apply": None, "why": "", "by": ""}
            for r in policy()["requirements"] if category in r["categories"]]


def _issued(ctx, rows: list[dict], warnings: list[str]) -> list[dict]:
    """Each policy requirement with NRL's issued wording for it, from the KG's
    standing NIT text, cited by tender. One KG call per section."""
    want = {r["id"]: r["kg"] for r in policy()["requirements"] if r.get("kg")}
    found: dict[str, list[dict]] = {}
    for section in sorted({want[r["id"]]["section"] for r in rows if r["id"] in want}):
        try:
            found[section] = tools.call(ctx, "boilerplate",
                                        {"section": section, "limit": 3})["clauses"]
        except NamError as exc:
            if exc.code != "kg_unavailable":
                raise
            warnings.append(f"NRL's issued wording for {section} is not shown: the "
                            "knowledge graph could not be reached.")
    out = []
    for r in rows:
        spec = want.get(r["id"])
        if spec and spec["section"] in found:
            hits = [c for c in found[spec["section"]]
                    if any(w in c["text"].lower() for w in spec["words"])][:ISSUED]
            r = {**r, "issued": [{"text": c["text"][:2000], "case_key": c["cite"]["case_key"]}
                                 for c in hits]}
        out.append(r)
    return out


def _apply(rows: list[dict], text: str, user: str) -> list[dict]:
    rows = [dict(r) for r in rows]
    for e in lines.parse(text, {"apply", "why", "text"}):
        why = e.fields.get("why", "").strip()[:300]
        if e.target == "add":
            body = e.fields.get("text", "").strip()
            if len(body) < 10 or not why:
                raise lines.LineError("add: needs text=... and why=...")
            rows.append({"id": f"dept_{len(rows) + 1}", "text": body[:2000],
                         "basis": "user department", "source": "department", "apply": True,
                         "why": why, "by": user})
            continue
        if e.remove:
            raise lines.LineError("a requirement is set aside with apply=no and why=...")
        if set(e.fields) - {"apply", "why"} or "apply" not in e.fields:
            raise lines.LineError(f"{e.target}: give apply=yes or apply=no")
        if e.fields["apply"] not in ("yes", "no"):
            raise lines.LineError("apply is yes or no")
        targets = range(len(rows)) if e.target == "all" else [e.index]
        for i in targets:
            if i >= len(rows):
                raise lines.LineError(f"there is no requirement {e.target}")
            if e.fields["apply"] == "no" and not why:
                raise lines.LineError(f"requirement {i + 1}: not applying it needs why=...")
            rows[i].update(apply=e.fields["apply"] == "yes", why=why, by=user)
    if len(rows) > MAX:
        raise lines.LineError(f"at most {MAX} requirements")
    return rows


def handle(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    category = need_classify.route(need)["category"]
    if not category:
        raise NamError("stage_gate", "the case has no category (need.classify)",
                       case_id=case_id)
    wf = orchestrator.workflow(need)
    stored = orchestrator._artifact(wf, "compliance") or {}
    answer = request.get("answer") or {}
    if answer and answer.get("question_id") != ANSWER:
        raise NamError("bad_request", f"compliance.build asks only '{ANSWER}'")
    warnings = [("policy/compliance.json is a DRAFT: the requirements proposed are for the "
                "department to confirm.")]
    rows = list(stored.get("requirements") or []) or _issued(ctx, _proposed(category), warnings)
    if answer.get("value"):
        try:
            rows = _apply(rows, str(answer["value"]), store.current().user)
        except lines.LineError as exc:
            warnings.append(str(exc))

    undecided = [i for i, r in enumerate(rows, 1) if r["apply"] is None]
    complete = not undecided
    value = {"summary": (f"{sum(bool(r['apply']) for r in rows)} of {len(rows)} requirement(s) "
                         "apply" + ("" if complete else f", {len(undecided)} undecided"))[:300],
             "complete": complete, "category": category, "requirements": rows,
             "doc_id": stored.get("doc_id"), "version": stored.get("version")}
    core = {k: v for k, v in value.items() if k not in ("doc_id", "version")}
    changed = core != {k: v for k, v in stored.items() if k not in ("doc_id", "version")}
    if changed and complete:
        applied = [r for r in rows if r["apply"]]
        blocks = [("p", (f"Case {case_id}: compliance requirements built by the user department "
                        "(FRS F26).")),
                  ("table", [["n", "requirement", "basis", "applies", "why"],
                             *[[i, r["text"], r["basis"], "yes" if r["apply"] else "no",
                                r["why"]] for i, r in enumerate(rows, 1)]]),
                  ("p", f"{len(applied)} requirement(s) apply.")]
        for i, r in enumerate(rows, 1):
            for c in r.get("issued") or []:
                said = f"Requirement {i}, as NRL issued it in {c['case_key']}: {c['text']}"
                blocks.append(("p", said))
        value["doc_id"], value["version"] = documents.store_docx(
            ctx, case_id, "compliance", "Compliance requirements", blocks,
            {"doc_id": stored.get("doc_id"), "version": stored.get("version")}, warnings,
            flow="compliance.build")
    questions = ()
    if undecided:
        questions = ({"id": ANSWER, "source": "harness", "options": [],
                      "text": (f"Decide requirement(s) {', '.join(map(str, undecided[:10]))}: "
                               f"'{undecided[0]}: apply=yes' or 'apply=no, why=...'."),
                      "why": "The department endorses each compliance requirement (F26)."},)
    return Outcome(
        case_id=case_id, status="ok" if complete else "needs_input",
        result={"category": category, "complete": complete,
                "requirements": [{"n": i, **{k: r[k] for k in ("id", "text", "basis", "source",
                                                               "apply", "why")},
                                  "issued": r.get("issued") or []}
                                 for i, r in enumerate(rows, 1)],
                "undecided": undecided},
        questions=questions, warnings=tuple(warnings),
        next=("validate",) if complete else (),
        artifacts={"compliance": value} if changed else {})
