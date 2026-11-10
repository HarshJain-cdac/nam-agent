"""docs.review (FRS F24): the drafter's review of the generated documents,
before they are approved: the clauses that conflict, and the wording that
leaves a term open (S5).

  1. Code reads every selected clause of every required document, and the
     contract-specific fields (playbooks/checks.py):
       conflict    two places state the same term with different figures
                   (delivery 8 weeks in the SCC, 12 weeks in a field)
       vague       wording that leaves a term to be decided later
                   ("as required", "approx.", "to be decided")
       unclassified a clause neither standard nor contract-specific by its
                   words, for the drafter to place (F22)
  2. The model of the "review" task (nam_agent/nam/llm.py) may add findings the
     code cannot see. It is shown the clauses as data and may only name a
     clause it was shown; a finding that names anything else, or fails the
     output check (guard.py), is dropped. Without a model, the code's
     findings stand alone.
  3. The drafter fixes a finding in docs.generate, or accepts it with a
     reason: answer {question_id: "accept", value: "2: why=both periods are
     intended"}. A conflict not fixed or accepted keeps validation from
     passing (validate, F32).

Findings carry over between runs by their id, with what the drafter decided.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path

from nam_agent.nam import extract, guard, llm, orchestrator, skillset
from nam_agent.nam import need as need_store
from nam_agent.nam.errors import NamError
from nam_agent.nam.followup import _json_object
from nam_agent.nam.playbooks import Outcome, checks, lines

ACCEPT = "accept"
MODEL_FINDINGS = 10
MODEL_SYSTEM = (
    "You review the clauses of a procurement tender's draft documents for Numaligarh Refinery "
    "Limited. Each clause is shown with its reference, such as [scc 2]. Find clauses that are "
    "ambiguous (a reader could take them two ways) or that conflict with another clause. Do "
    "not rewrite clauses, do not judge prices, and do not repeat a problem already listed as "
    "found. Reply with one JSON object only: {\"findings\": [{\"ref\": \"scc 2\", \"issue\": "
    "\"<one sentence>\"}]}, or {\"findings\": []} when there is nothing. " + guard.RULE)


def _id(kind: str, refs: list[str], says: str) -> str:
    digest = hashlib.sha256(json.dumps([kind, refs, says]).encode()).hexdigest()[:8]
    return f"R-{digest}"


def _finding(kind: str, severity: str, refs: list[str], says: str, source: str) -> dict:
    return {"id": _id(kind, refs, says), "kind": kind, "severity": severity,
            "refs": refs[:20], "says": says[:300], "source": source,
            "accepted": False, "why": ""}


NORMS = Path(__file__).resolve().parents[1] / "policy" / "pqc_norms.json"


@cache
def norms() -> dict:
    return json.loads(NORMS.read_text(encoding="utf-8"))


def _near(have: float, want: float) -> bool:
    return abs(have - want) <= norms()["tolerance"] * want


def _pqc_findings(ref: str, text: str, estimate: float | None) -> list[dict]:
    """A PQC criterion against the norms (policy/pqc_norms.json, DRAFT) and
    this case's estimate (skill nam-documents/pqc): warnings, for the officer."""
    out = []
    for c in extract.pqc(text):
        if c["kind"] == "experience":
            for alt in c.get("alternatives") or []:
                want = dict(map(tuple, norms()["experience"]["alternatives"])).get(alt["count"])
                if want and not _near(alt["share"], want):
                    out.append(f"{ref}: {alt['count']} work(s) of {alt['share']}% each, against "
                               f"the norm of {want}%")
            period = c.get("period") or {}
            if period and "year" in period["unit"] and \
                    period["value"] != norms()["experience"]["years"]:
                out.append(f"{ref}: experience over {period['value']} years, against the norm "
                           f"of {norms()['experience']['years']}")
            if not period and c.get("alternatives"):
                out.append(f"{ref}: the experience criterion states no period")
        if c["kind"] == "turnover" and c.get("share") is not None \
                and not _near(c["share"], norms()["turnover"]["share"]):
            out.append(f"{ref}: turnover of {c['share']}% of the estimate, against the norm of "
                       f"{norms()['turnover']['share']}%")
        if c.get("amount") and c["kind"] in ("experience", "turnover"):
            amount = c["amount"]
            out.append(f"{ref}: a fixed threshold of Rs {amount['value']} {amount['unit']}"
                       + (f", while this case's estimate is Rs {estimate:,.2f}: set it as a "
                          "share of this estimate" if estimate else "") + " (a precedent's "
                       "figure is not this case's)")
    return [_finding("pqc_threshold", "warn", [ref], says, "harness") for says in out]


def _scc_findings(ref: str, text: str) -> list[dict]:
    """An SCC term missing the figure it needs (skill nam-documents/scc)."""
    out = []
    for c in extract.scc(text):
        units = [f["unit"] for f in c["figures"]]
        if c["term"] == "liquidated_damages" and units and \
                not (any("per" in u for u in units) and len(units) >= 2):
            out.append(f"{ref}: liquidated damages need both a rate per week and a cap")
        if c["term"] == "performance_security" and units and "%" not in units:
            out.append(f"{ref}: the performance security states no percentage")
    return [_finding("incomplete_term", "warn", [ref], says, "harness") for says in out]


def _spec_findings(ref: str, text: str) -> list[dict]:
    return [_finding("brand", "warn", [ref], f"{ref} names a make without 'or equivalent': it "
                     "restricts competition unless the tender is OEM or proprietary", "harness")
            for c in extract.spec(text) if c.get("brand_without_equivalent")]


def code_findings(docs: list[dict], fields: dict, estimate: float | None = None) -> list[dict]:
    out = [_finding("conflict", "fail", c["refs"], c["says"], "harness")
           for c in checks.conflicts(checks.places(docs, fields))]
    for doc in docs:
        if doc.get("required") is False:
            continue
        for n, clause in enumerate(doc.get("clauses") or [], 1):
            if not clause.get("selected"):
                continue
            ref = f"{doc['kind']} {n}"
            words = checks.vague(clause["text"])
            if words:
                out.append(_finding("vague", "warn", [ref], f"{ref} leaves a term open: "
                                    f"'{words}'", "harness"))
            if doc["kind"] == "pqc":
                out += _pqc_findings(ref, clause["text"], estimate)
            elif doc["kind"] == "scc":
                out += _scc_findings(ref, clause["text"])
            elif doc["kind"] == "specification":
                out += _spec_findings(ref, clause["text"])
            if clause.get("class") == "unclassified":
                out.append(_finding("unclassified", "info", [ref], f"{ref} is neither standard "
                                    "nor contract-specific by its words: place it", "harness"))
    return out


def _model_findings(ctx, docs: list[dict], known: list[dict]) -> tuple[list[dict], str]:
    """(findings, note). Never raises: no model or a bad reply is a note."""
    ask = llm.asker(ctx, "review")
    if ask is None:
        return [], "no model for the review task: the findings are the harness's checks"
    refs, blocks = set(), []
    for doc in docs:
        for n, clause in enumerate(doc.get("clauses") or [], 1):
            if clause.get("selected") and doc.get("required") is not False:
                ref = f"{doc['kind']} {n}"
                refs.add(ref)
                blocks.append(f"[{ref}] " + guard.wrap(clause["text"][:1500], "document", ref))
    if not blocks:
        return [], "no clause to review"
    already = "\n".join(f"- {f['says']}" for f in known[:20]) or "- none"
    # what each kind of document must state (nam_agent/nam/skills/), so the model
    # reviews against NRL's checks rather than its own idea of them
    rules = skillset.checks_for(sorted({d["kind"] for d in docs}))
    user = ("Clauses:\n" + "\n".join(guard.cap_prompt(blocks)) +
            f"\n\nAlready found:\n{already}" +
            (f"\n\nWhat these documents must state:\n{rules}" if rules else ""))
    names = guard.tool_names()

    def accept(text: str) -> tuple[list[dict], int]:
        """The findings that name a shown clause and pass the output check,
        and how many were dropped; a reply with none usable is sent back."""
        reply = _json_object(text)
        if reply is None or not isinstance(reply.get("findings"), list):
            raise llm.Rejected('the reply was not a JSON object {"findings": [...]}')
        out, dropped = [], 0
        for item in reply["findings"][:MODEL_FINDINGS * 2]:
            ref = str((item or {}).get("ref") or "").strip().lower() \
                if isinstance(item, dict) else ""
            issue = str((item or {}).get("issue") or "").strip() \
                if isinstance(item, dict) else ""
            if ref not in refs or not 10 <= len(issue) <= 300 \
                    or guard.check_output(issue, tool_names=names):
                dropped += 1
                continue
            out.append(_finding("model", "warn", [ref], f"{ref}: {issue}", "model"))
        if dropped and not out:
            raise llm.Rejected("no finding named a clause ref shown (like 'scc 2') with an "
                               "issue of 10 to 300 characters")
        return out, dropped

    try:
        out, dropped = llm.checked(ask, MODEL_SYSTEM, user, accept)
    except llm.ModelUnavailable as exc:
        return [], f"the review model was unavailable ({str(exc)[:120]})"
    except llm.Rejected as exc:
        return [], f"the review model's answers were refused: {str(exc)[:150]}"
    note = f"the review model added {len(out[:MODEL_FINDINGS])} finding(s)" + (
        f"; {dropped} of its findings were dropped by the check" if dropped else "")
    return out[:MODEL_FINDINGS], note


def handle(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    wf = orchestrator.workflow(need)
    documents = orchestrator._artifact(wf, "documents")
    if not documents:
        raise NamError("stage_gate", "there are no documents to review (docs.generate)",
                       case_id=case_id)
    stored = orchestrator._artifact(wf, "review") or {}
    docs_version = wf["artifacts"]["documents"]["at_version"]
    answer = request.get("answer") or {}
    if answer and answer.get("question_id") != ACCEPT:
        raise NamError("bad_request", f"docs.review asks only '{ACCEPT}'")
    warnings: list[str] = []

    est = orchestrator._artifact(wf, "estimate") or {}
    findings = code_findings(documents["documents"], documents.get("fields") or {},
                             est.get("total"))
    same_docs = stored.get("documents_version") == docs_version
    if same_docs and any(f["source"] == "model" for f in stored.get("findings") or []):
        model = [f for f in stored["findings"] if f["source"] == "model"]   # asked once
        note = "the review model's findings for these documents, from the last review"
    elif same_docs and stored.get("model_note"):
        model, note = [], stored["model_note"]
    else:
        model, note = _model_findings(ctx, documents["documents"], findings)
    findings += [m for m in model if m["id"] not in {f["id"] for f in findings}]
    decided = {f["id"]: f for f in stored.get("findings") or []}
    for f in findings:
        if f["id"] in decided:
            f["accepted"], f["why"] = decided[f["id"]]["accepted"], decided[f["id"]]["why"]

    if answer.get("value"):
        try:
            for e in lines.parse(str(answer["value"]), {"why"}):
                if e.target in ("add", "all") or e.remove:
                    raise lines.LineError("accept a finding by its number: '2: why=...'")
                if e.index >= len(findings):
                    raise lines.LineError(f"there is no finding {e.target}")
                why = e.fields.get("why", "").strip()
                if len(why) < 3:
                    raise lines.LineError(f"finding {e.target}: accepting it needs why=...")
                findings[e.index]["accepted"], findings[e.index]["why"] = True, why[:300]
        except lines.LineError as exc:
            warnings.append(str(exc))
    if note:
        warnings.append(note[:300])

    open_conflicts = [f for f in findings if f["severity"] == "fail" and not f["accepted"]]
    value = {"summary": (f"{len(findings)} finding(s), {len(open_conflicts)} open conflict(s)"
                         )[:300], "documents_version": docs_version,
             "findings": findings[:200], "open": len(open_conflicts), "model_note": note[:300]}
    questions = ()
    if open_conflicts:
        first = findings.index(open_conflicts[0]) + 1
        questions = ({"id": ACCEPT, "source": "harness", "options": [],
                      "text": (f"Finding {first} is a conflict: fix it in docs.generate, or "
                               f"accept it with '{first}: why=...'."),
                      "why": open_conflicts[0]["says"][:500]},)
    return Outcome(
        case_id=case_id, status="needs_input" if open_conflicts else "ok",
        result={"findings": [{"n": i, **{k: f[k] for k in ("kind", "severity", "refs", "says",
                                                           "source", "accepted", "why")}}
                             for i, f in enumerate(findings, 1)][:200],
                "open": len(open_conflicts), "documents_version": docs_version},
        questions=questions, warnings=tuple(warnings),
        next=("docs.generate",) if open_conflicts else ("validate",),
        artifacts={"review": value} if value != stored else {})
