"""Guardrails enforced outside the model (plan Step 14).

A flow, or the model inside it, proposes; this module decides whether what it
proposed may stand. It runs in code at three points, so a prompt can never
talk past it:

  before()         before a flow's handler runs (nam_agent/nam/__init__._run)
  after()          after the handler, before the stage machine takes its
                   artifacts (settle); a refusal reverts what the flow saved
  before_action()  before an irreversible action's effect runs
                   (orchestrator.confirm), and submit_pr's duplicate check
                   again inside the effect, under the write lock

Each guardrail is a rule id. A rule either blocks (422 `guardrail`, nothing
of the step is kept), corrects (the artifact is fixed in code and the
correction is a warning) or warns. Every outcome is an audit row
(nam_events, kind "guardrail").

  rule            FRS          what it stops
  duplicate_need  -            a second open case for the same need (warns)
  duplicate_pr    gap 5        a PR for an item another open PR already covers
  artifact        -            an artifact outside its schema (schemas/artifact.*)
  rate_source     R5-R8, R35   a rate, amount or component no tool result or
                               officer gave: the number must be the one at the
                               cited path of a tool result of this same run, a
                               number the officer typed (in this request, or
                               held by the stored estimate), a policy
                               percentage of earlier components, an earlier
                               line's quantity times its rate or a component
                               (line, component), or a rounding up (round_up,
                               R13); a base component is what a percentage is
                               of, not part of the rate
  arithmetic      R19          amount != qty x rate, total != sum of lines
  dsr_alert       R8, R9, R36  a DSR/DAR (non NRL SOR) rate without its alert,
                               or marked NRL-approved (corrects)
  last_resort     R6, R37      a PO or quotation rate without its alert (corrects)
  threshold       R4, R10,     a threshold result presented as final (corrects),
                  R34, R38     below its threshold, or a VARN/CAGR flag that is
                               not the one the rule gives (blocks)
  precedent       R2           a precedent no officer named, or an artifact
                               built on a tender that is not selected
  category        R1, R14, R28 an estimate flow for another category, a subtype
                               outside the category, a codified line without
                               its material code
  mandatory_docs  R24          a package without the case's mandatory documents
  handoff         R32          NIT hand-off while an approval, validation or
                               mandatory document is missing, or with a package
                               other than the frozen one
  citation        Step 13      a rate, clause, requirement or suggestion stored
                               without its source (knowledge.uncited)
  verification    -            freeze or handoff of a case that does not verify
                               as a whole: an artifact outside its schema or
                               uncited, a document file changed, an artifact
                               older than what it is built on, a broken audit
                               chain (verify.case)

The thresholds, mandatory documents and rule percentages are in
policy/guardrails.json (reviewed 2026-10-01).
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from functools import cache
from pathlib import Path

from nam_agent.nam import audit, knowledge, schema, store
from nam_agent.nam.errors import NamError

POLICY = Path(__file__).parent / "policy" / "guardrails.json"

FRS = {"duplicate_need": "", "duplicate_pr": "", "artifact": "",
       "rate_source": "R5-R8, R35", "arithmetic": "R19", "dsr_alert": "R8, R9, R36",
       "last_resort": "R6, R37", "threshold": "R4, R10, R34, R38", "precedent": "R2",
       "category": "R1, R14, R28", "mandatory_docs": "R24", "handoff": "R32",
       "citation": "Step 13", "verification": ""}

# artifacts with a schema of their own (schemas/artifact.<name>.json)
SCHEMAS = {"estimate": "artifact.estimate", "pr": "artifact.pr",
           "package": "artifact.package", "validation": "artifact.validation",
           "handoff": "artifact.handoff", "guided_form": "artifact.guided_form",
           "shortlist": "artifact.shortlist",
           "quantity": "artifact.quantity",
           "documents": "artifact.documents",
           "pr_reference": "artifact.pr_reference", "review": "artifact.review",
           "compliance": "artifact.compliance", "materials": "artifact.materials",
           "enrichment": "artifact.enrichment", "approval_file": "artifact.approval_file",
           "published": "artifact.published", "uploads": "artifact.uploads",
           "todo": "artifact.todo"}

NON_NRL = {"dsr": "DSR 2023", "dsr_em": "DSR E&M 2022", "dar": "DAR",
           "apwd_sor": "APWD SOR (NH) 2021", "sor_rr": "SOR RR 2020-21"}
COMPUTED = ("derived", "first_principle", "wage")
GATES = ("quantity", "estimate", "clauses", "documents")
ACTIONS = ("freeze_package", "submit_pr", "approve")
CENT = Decimal("0.01")


@cache
def policy() -> dict:
    return json.loads(POLICY.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Breach:
    rule: str
    says: str


def _refuse(conn, breaches: list[Breach], case_id: str | None, step: str) -> NamError:
    for b in breaches:
        audit.record(conn, kind="guardrail", name=b.rule, status="blocked",
                     case_id=case_id or "", detail={"step": step, "says": b.says[:300]})
    first = breaches[0]
    details = [f"{b.rule} ({FRS[b.rule]}): {b.says}" if FRS[b.rule] else f"{b.rule}: {b.says}"
               for b in breaches]
    return NamError("guardrail", f"{step} was refused by the {first.rule} guardrail: "
                    f"{first.says}", details=details, case_id=case_id)


def _note(conn, rule: str, status: str, says: str, case_id: str | None) -> str:
    audit.record(conn, kind="guardrail", name=rule, status=status, case_id=case_id or "",
                 detail={"says": says[:300]})
    return says


# -- the evidence of a run: what the tools answered ----------------------------

_LEDGER: ContextVar[list | None] = ContextVar("nam_evidence", default=None)


@contextmanager
def evidence() -> Iterator[list]:
    """The tool results of one run (nam_agent/nam/__init__._run opens it). A rate
    may only cite what is in here."""
    ledger: list = []
    token = _LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _LEDGER.reset(token)


def note(tool: str, result: dict, args: dict | None = None) -> None:
    """Keep a copy of a read tool's arguments and result (nam_agent/nam/tools.call).
    The copy is what a citation is checked against, so changing the result
    afterwards does not change the evidence."""
    ledger = _LEDGER.get()
    if ledger is not None:
        ledger.append({"id": f"ev_{len(ledger) + 1}", "tool": tool, "obj": id(result),
                       "args": copy.deepcopy(args or {}), "result": copy.deepcopy(result)})


def cite(result: dict, path: str) -> dict:
    """The source of a number taken from a tool result of this run:
    {"tool", "evidence", "path", "value"}. `path` is "rows/0/unit_price/median"."""
    for entry in reversed(_LEDGER.get() or []):
        if entry["obj"] == id(result):
            found, value = _at(entry["result"], path)
            if not found:
                raise NamError("internal", f"{entry['tool']} result has nothing at {path}")
            return {"tool": entry["tool"], "evidence": entry["id"], "path": path,
                    "value": str(value)}
    raise NamError("internal", "cite() takes a result tools.call returned in this run")


def _at(value, path: str):
    for part in [p for p in path.split("/") if p]:
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return False, None
    return True, value


def _dec(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def round_up(value: Decimal, step) -> Decimal:
    """`value` rounded up to a whole multiple of `step` (R13)."""
    step = Decimal(str(step))
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _same(a, b) -> bool:
    da, db = _dec(a), _dec(b)
    return da is not None and db is not None and abs(da - db) < CENT


_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def _officer_numbers(request: dict) -> set[Decimal]:
    """Every number the caller typed in this request: a rate the officer
    gives is one of these."""
    texts = [m.get("content", "") for m in request.get("messages") or []
             if m.get("role") == "user"]
    for part in (request.get("answer"), request.get("options")):
        if part:
            texts.append(json.dumps(part))
    out = set()
    for text in texts:
        for m in _NUMBER.findall(str(text)):
            d = _dec(m)
            if d is not None:
                out.add(d.quantize(CENT))
    return out


# -- before a flow --------------------------------------------------------------

def _category(need) -> str:
    from nam_agent.nam import need as need_store

    return need_store.category_id((getattr(need, "category", None) or {}).get("value")) or ""


def before(conn: sqlite3.Connection, flow, need, request: dict) -> None:
    """Refuse a flow the case may not run, before it does any work."""
    breaches = []
    allowed = policy()["estimate_flows"].get(flow.id)
    if allowed is not None and need is not None:
        cat = _category(need)
        if cat not in allowed:
            breaches.append(Breach("category", f"{flow.id} estimates {' or '.join(allowed)}; "
                                   f"{need.need_id} is {cat or 'not classified'}"))
    if flow.id == "handoff.nit" and need is not None:
        from nam_agent.nam import orchestrator

        breaches += _r32(need, orchestrator.workflow(need), with_handoff=False)
    if breaches:
        raise _refuse(conn, breaches, need.need_id if need else None, f"{flow.id}")


# -- after a flow ----------------------------------------------------------------

def after(conn: sqlite3.Connection, flow, request: dict, before_need, outcome
          ) -> tuple[dict, list[str]]:
    """Check what the flow left: the record it saved and the artifacts it
    hands to the stage machine. Returns (artifacts, warnings), the artifacts
    with the corrections made. Raises NamError guardrail after reverting the
    record when a rule blocks."""
    from nam_agent.nam import need as need_store
    from nam_agent.nam import orchestrator

    case_id = outcome.case_id
    need = need_store.load(conn, case_id) if case_id else None
    breaches: list[Breach] = []
    warnings: list[str] = []
    artifacts = copy.deepcopy(dict(outcome.artifacts or {}))
    if need is not None:
        breaches += _record(flow, request, before_need, need)
        wf = orchestrator.workflow(need)
        for name, value in artifacts.items():
            if flow.id not in orchestrator.ARTIFACTS.get(name, ()):
                continue                          # settle() refuses it as internal
            found, fixed = _artifact(conn, flow, request, need, wf, name, value)
            breaches += found
            warnings += fixed
        breaches += _precedents_used(need, artifacts)
        if before_need is None and not breaches:
            warnings += _duplicate_need(conn, need)
    if breaches:
        _revert(conn, before_need, need)
        raise _refuse(conn, breaches, case_id, flow.id)
    return artifacts, [_note(conn, w[0], w[1], w[2], case_id) for w in warnings]


def _record(flow, request: dict, before_need, need) -> list[Breach]:
    from nam_agent.nam import need as need_store

    out = []
    old = set(before_need.selected_cases) if before_need is not None else set()
    added = [k for k in need.selected_cases if k not in old]
    if set(need.selected_cases) != old:
        if flow.id not in policy()["precedent_flows"]:
            out.append(Breach("precedent", f"{flow.id} changed the selected precedent; only "
                              "an officer's selection may (precedent.select)"))
        else:
            # the caller's own words: its system prompt and the officer's messages
            said = f"{request.get('system', '')}\n{_request_text(request)}".lower()
            unnamed = [k for k in added if not _named(k, said)]
            if unnamed:
                out.append(Breach("precedent", f"{', '.join(unnamed)} selected without the "
                                  "officer naming it in the request"))
    label = (need.category or {}).get("value")
    subtype = (need.subtype or {}).get("value")
    changed = before_need is None or label != (before_need.category or {}).get("value") \
        or subtype != (before_need.subtype or {}).get("value")
    if changed and label and subtype and subtype not in need_store.SUBTYPES.get(label, ()):
        out.append(Breach("category", f"subtype {subtype!r} is not one of {label}'s: "
                          f"{', '.join(need_store.SUBTYPES.get(label, ()))}"))
    return out


def _named(case_key: str, said: str) -> bool:
    """The officer named this tender: its case key is in the request, or a
    reference in the request resolved to it and to nothing else in this run
    (resolve_reference, exact tiers or one core match)."""
    if re.search(rf"(?<![\w-]){re.escape(case_key.lower())}(?![\w-])", said):
        return True
    for entry in _LEDGER.get() or []:
        if entry["tool"] != "resolve_reference":
            continue
        ref = str(entry["args"].get("ref") or "").lower()
        matches = entry["result"].get("matches") or []
        if ref and ref in said and not entry["result"].get("ambiguous") and len(matches) == 1 \
                and matches[0]["cite"]["case_key"] == case_key:
            return True
    return False


def _request_text(request: dict) -> str:
    parts = [m.get("content", "") for m in request.get("messages") or []
             if m.get("role") == "user"]
    for key in ("answer", "options"):
        if request.get(key):
            parts.append(json.dumps(request[key]))
    return "\n".join(str(p) for p in parts)


def _precedents_used(need, artifacts: dict) -> list[Breach]:
    selected = set(need.selected_cases)
    out = []
    for name, value in artifacts.items():
        for key in _walk_precedents(value):
            if key not in selected:
                out.append(Breach("precedent", f"the {name} artifact is built on {key}, which "
                                  "the officer did not select"))
    return out


def _walk_precedents(value) -> Iterator[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ("precedent", "precedents"):
                yield from ([v] if isinstance(v, str) else
                            [x for x in v if isinstance(x, str)] if isinstance(v, list) else [])
            else:
                yield from _walk_precedents(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_precedents(v)


def _revert(conn, before_need, need) -> None:
    """Undo what a refused flow saved, as a new version (history is kept)."""
    from nam_agent.nam import need as need_store

    if need is None:
        return
    if before_need is None:
        if not need.selected_cases and not (need.subtype or {}).get("value"):
            return
        need.selected_cases = []
        need.subtype = need_store.Prediction().as_dict()
    else:
        if need.version == before_need.version:
            return
        version = need.version
        need = copy.deepcopy(before_need)
        need.version = version
    need_store.save(conn, need, reason="guardrail: reverted the refused step")


# -- artifacts -------------------------------------------------------------------

def _artifact(conn, flow, request, need, wf, name, value) -> tuple[list[Breach], list]:
    if name in SCHEMAS:
        problems = schema.validate(value, schema.load(SCHEMAS[name]))
        if problems:
            return [Breach("artifact", f"the {name} artifact is outside its schema: "
                           + "; ".join(problems[:5]))], []
    breaches, fixes = _domain(conn, flow, request, need, wf, name, value)
    if breaches:
        return breaches, fixes
    # Step 13: what the rules above do not trace (a clause, a requirement)
    # still needs a source
    bare = knowledge.uncited({name: value})
    if bare:
        return [Breach("citation", f"{len(bare)} fact(s) of the {name} have no source: "
                       + "; ".join(bare[:3]))], fixes
    return [], fixes


def _domain(conn, flow, request, need, wf, name, value) -> tuple[list[Breach], list]:
    if name == "estimate":
        return _estimate(flow, request, need, value,
                         prior=orchestrator_artifact(wf, "estimate"))
    if name == "pr":
        return _pr(conn, need, value), []
    if name == "package":
        return _mandatory(need, value), []
    if name == "handoff":
        return _r32(need, wf, handoff=value), []
    return [], []


def orchestrator_artifact(wf: dict, name: str):
    return ((wf.get("artifacts") or {}).get(name) or {}).get("value")


def _accepted(prior: dict | None) -> set:
    """(item, officer, value) of every officer number the stored estimate
    already carries: it passed this check when the officer typed it, so a
    re-run need not have it typed again."""
    out = set()
    for line in (prior or {}).get("lines") or []:
        for src, value in [(line.get("source") or {}, line.get("rate")),
                           *[(c.get("source") or {}, c.get("amount"))
                             for c in line.get("components") or []]]:
            d = _dec(value)
            if src.get("officer") and d is not None:
                out.add((line.get("item"), src["officer"], d.quantize(CENT)))
    return out


def _estimate(flow, request, need, est, prior: dict | None = None
              ) -> tuple[list[Breach], list]:
    """Every rate traced to its evidence, the sums checked, the threshold
    results recomputed, and the alerts added. Corrects `est` in place.
    `prior` is the estimate stored before this run."""
    out: list[Breach] = []
    fixes: list = []
    cat = _category(need)
    if est["category"] != cat:
        out.append(Breach("category", f"the estimate is for {est['category']}; the case is "
                          f"{cat or 'not classified'}"))
    allowed = policy()["estimate_flows"].get(flow.id)
    if allowed is not None and est["category"] not in allowed:
        out.append(Breach("category", f"{flow.id} cannot write a {est['category']} estimate"))
    codified = (need.subtype or {}).get("value") == "Codified"
    numbers = _officer_numbers(request)
    accepted = _accepted(prior)
    actor = store.current()
    alerts = list(est.get("alerts") or [])
    total = Decimal(0)

    def alert(code: str, i: int, says: str) -> None:
        if not any(a["code"] == code and a.get("line") == i for a in alerts):
            alerts.append({"code": code, "line": i, "says": says[:300]})
            fixes.append((_RULE_OF[code], "corrected", f"line {i + 1}: {says}"))

    for i, line in enumerate(est["lines"]):
        where = f"line {i + 1} ({line['item'][:60]})"
        if codified and not (line.get("material_code") or "").strip():
            out.append(Breach("category", f"{where}: a Codified item needs its material code"))
        out += _rate(where, line, numbers, actor, accepted, est["lines"][:i])
        rate, qty = _dec(line["rate"]), _dec(line["qty"])
        if rate is None:
            if line.get("amount") is not None:
                out.append(Breach("arithmetic", f"{where}: an amount without a rate"))
            alert("new_item", i, f"{line['item'][:80]}: no rate found; the officer gives it")
        else:
            amount = (rate * qty).quantize(CENT)
            if line.get("amount") is not None and not _same(line["amount"], amount):
                out.append(Breach("arithmetic", f"{where}: amount {line['amount']} is not "
                                  f"{line['qty']} x {line['rate']} = {amount}"))
            total += amount
        schedule = line.get("schedule")
        if schedule in NON_NRL:
            alert("dsr_fallback", i, f"{line['item'][:80]}: rate from {NON_NRL[schedule]}, "
                  "not NRL SOR; reference data, not an NRL-approved rate")
            if line.get("nrl_approved") is not False:
                line["nrl_approved"] = False
                fixes.append(("dsr_alert", "corrected", (f"line {i + 1}: {NON_NRL[schedule]} "
                              "rate marked not NRL-approved")))
        # a vendor's price is the last resort for works and services; for goods
        # the last PO price is the PR flow's own basis (2(a)), so only a quote is
        if line["basis"] == "quotation" or (line["basis"] == "po" and cat != "goods"):
            alert("last_resort", i, f"{line['item'][:80]}: {line['basis']} rate, the last "
                  "resort: a vendor's price, not an elemental cost")
        out += _threshold(where, line, i, alert, fixes)
    if est.get("total") is not None and not _same(est["total"], total):
        out.append(Breach("arithmetic", f"total {est['total']} is not the sum of the lines, "
                          f"{total}"))
    est["alerts"] = alerts
    if est.get("provisional") is not True:
        est["provisional"] = True
        fixes.append(("threshold", "corrected", ("the estimate is provisional until the "
                      "officer approves it")))
    return out, fixes


_RULE_OF = {"dsr_fallback": "dsr_alert", "not_nrl_approved": "dsr_alert",
            "last_resort": "last_resort", "new_item": "rate_source",
            "manual_review": "threshold", "high_rate": "threshold"}


def _rate(where: str, line: dict, numbers: set, actor, accepted: set = frozenset(),
          earlier_lines: list | None = None) -> list[Breach]:
    rate = line["rate"]
    ctx = {"accepted": accepted, "item": line["item"], "lines": earlier_lines or []}
    basis = line["basis"]
    if rate is None:
        return [] if basis == "new_item" else [
            Breach("rate_source", f"{where}: no rate, so its basis is new_item, not {basis}")]
    if basis in COMPUTED:
        comps = line.get("components") or []
        if not comps:
            return [Breach("rate_source", f"{where}: a {basis} rate needs its components")]
        out = []
        for j, comp in enumerate(comps):
            out += _sourced(f"{where} component {j} ({comp['name'][:40]})", comp["amount"],
                            comp["source"], numbers, actor, comps[:j], ctx)
        # a base component is what a rule is a percentage of, not part of the rate
        total = sum((_dec(c["amount"]) or Decimal(0) for c in comps if not c.get("base")),
                    Decimal(0))
        if not _same(rate, total):
            out.append(Breach("arithmetic", f"{where}: rate {rate} is not the sum of its "
                              f"components, {total.quantize(CENT)}"))
        return out
    if "source" not in line:
        return [Breach("rate_source", f"{where}: rate {rate} has no source")]
    return _sourced(where, rate, line["source"], numbers, actor, [], ctx)


def _sourced(where: str, value, source: dict, numbers: set, actor, earlier: list,
             ctx: dict | None = None) -> list[Breach]:
    ctx = ctx or {"accepted": set(), "item": "", "lines": []}
    if "evidence" in source:
        entry = next((e for e in _LEDGER.get() or [] if e["id"] == source["evidence"]), None)
        if entry is None:
            return [Breach("rate_source", f"{where}: {source['evidence']} is no tool result "
                           "of this run")]
        if source.get("tool") != entry["tool"]:
            return [Breach("rate_source", f"{where}: {source['evidence']} is {entry['tool']}, "
                           f"not {source.get('tool')}")]
        found, at = _at(entry["result"], source.get("path", ""))
        if not found or isinstance(at, (dict, list)):
            return [Breach("rate_source", f"{where}: {entry['tool']} has no value at "
                           f"{source.get('path')!r}")]
        if not _same(value, at):
            return [Breach("rate_source", f"{where}: {value} is not what {entry['tool']} "
                           f"gave at {source.get('path')}: {at}")]
        return []
    if "officer" in source:
        d = _dec(value)
        if d is None:
            return [Breach("rate_source", f"{where}: {value} is not a number")]
        if (ctx["item"], source["officer"], d.quantize(CENT)) in ctx["accepted"]:
            return []                     # typed by that officer in an earlier run
        if source["officer"] != actor.user:
            return [Breach("rate_source", f"{where}: the officer {source['officer']} is not "
                           "the caller of this run")]
        if d.quantize(CENT) not in numbers:
            return [Breach("rate_source", f"{where}: {value} is not a number the officer "
                           "gave in this request, nor one the estimate already held")]
        return []
    if "line" in source:
        # an earlier line's quantity times its rate, or times one of its components
        k, j = source["line"], source.get("component")
        lines = ctx["lines"]
        if not isinstance(k, int) or not 0 <= k < len(lines):
            return [Breach("rate_source", f"{where}: line {k} is not an earlier line")]
        ref = lines[k]
        comps = ref.get("components") or []
        if j is not None and not (isinstance(j, int) and 0 <= j < len(comps)):
            return [Breach("rate_source", f"{where}: line {k} has no component {j}")]
        per = _dec(comps[j]["amount"] if j is not None else ref["rate"])
        qty = _dec(ref["qty"])
        want = (per * qty).quantize(CENT) if per is not None and qty is not None else None
        if want is None or not _same(value, want):
            return [Breach("rate_source", f"{where}: line {k} gives {want}, not {value}")]
        return []
    if source.get("rule") == "escalation":
        # the last price brought up to the evaluation date (PR flow 2(b))
        pct = policy()["goods_price"]["escalation_pct"]
        years = source.get("years")
        if source.get("pct") != pct:
            return [Breach("rate_source", f"{where}: escalation is {pct}% a year, not "
                           f"{source.get('pct')}%")]
        if not isinstance(years, int) or years < 0:
            return [Breach("rate_source", f"{where}: escalation needs whole years")]
        base = source.get("base_value")
        out = _sourced(f"{where} last price", base, source.get("base") or {}, numbers, actor,
                       earlier, ctx)
        if out:
            return out
        want = escalate(_dec(base), pct, years)
        if not _same(value, want):
            return [Breach("rate_source", f"{where}: {base} + {pct}% a year for {years} "
                           f"year(s) is {want}, not {value}")]
        return []
    if source.get("rule") == "round_up":
        step = policy()["rounding"]["reimbursement_step"]
        of = source.get("of") or []
        if source.get("step") != step:
            return [Breach("rate_source", f"{where}: round_up is to {step}, not "
                           f"{source.get('step')}")]
        if not of or any(k >= len(earlier) for k in of):
            return [Breach("rate_source", f"{where}: round_up applies to earlier components "
                           "only")]
        base = sum((_dec(earlier[k]["amount"]) or Decimal(0) for k in of), Decimal(0))
        want = (round_up(base, step) - base).quantize(CENT)
        if not _same(value, want):
            return [Breach("rate_source", f"{where}: rounding {base} up to {step} adds "
                           f"{want}, not {value}")]
        return []
    if "rule" in source:
        rule = policy()["percentages"].get(source["rule"])
        pct, of = source.get("pct"), source.get("of") or []
        if rule is None:
            return [Breach("rate_source", f"{where}: no rule {source['rule']!r}")]
        lo, hi = (rule if isinstance(rule, list) else (rule, rule))
        if pct is None or not lo - 1e-9 <= pct <= hi + 1e-9:
            return [Breach("rate_source", f"{where}: {source['rule']} is {rule}%, not {pct}%")]
        if not of or any(k >= len(earlier) for k in of):
            return [Breach("rate_source", f"{where}: {source['rule']} applies to earlier "
                           "components only")]
        base = sum((_dec(earlier[k]["amount"]) or Decimal(0) for k in of), Decimal(0))
        want = (base * _dec(pct) / 100).quantize(CENT)
        if not _same(value, want):
            return [Breach("rate_source", f"{where}: {pct}% of {base} is {want}, not {value}")]
        return []
    return [Breach("rate_source", f"{where}: {value} has no source")]


def _threshold(where: str, line: dict, i: int, alert, fixes: list) -> list[Breach]:
    out = []
    limits = policy()
    match = line.get("match")
    if line["basis"] == "similar" and match is None:
        out.append(Breach("threshold", f"{where}: a Similar rate needs its match score, "
                          "threshold and basis"))
    if match is not None:
        floor = limits["similarity"]["similar_rate"]
        if match["threshold"] < floor:
            out.append(Breach("threshold", f"{where}: threshold {match['threshold']} is below "
                              f"the configured {floor}"))
        if line["basis"] == "similar" and match["score"] < max(floor, match["threshold"]):
            out.append(Breach("threshold", f"{where}: match {match['score']} is below the "
                              "Similar threshold; the rate is Derived, not Similar"))
        if match.get("final") is not False:
            match["final"] = False
            fixes.append(("threshold", "corrected", (f"line {i + 1}: the match is a suggestion, "
                          "not final")))
    comp = line.get("comparison")
    if comp is not None:
        flag, varn, cagr = _r10(comp)
        if comp["flag"] != flag:
            out.append(Breach("threshold", f"{where}: the comparison gives {flag}, not "
                              f"{comp['flag']}"))
        for key, want in (("varn", varn), ("cagr", cagr)):
            have = comp.get(key)
            if have is not None and (want is None or not _same(have, want)):
                out.append(Breach("threshold", f"{where}: {key} {have} is not {want}"))
        comp["varn"], comp["cagr"] = (float(varn) if varn is not None else None,
                                      float(cagr) if cagr is not None else None)
        if comp.get("final") is not False:
            comp["final"] = False
            fixes.append(("threshold", "corrected", (f"line {i + 1}: {flag} is a flag for the "
                          "officer, not a final decision")))
        if flag == "manual_review":
            alert("manual_review", i, f"{line['item'][:80]}: PO within a year; review the "
                  "rate by hand")
        elif flag == "AHR":
            alert("high_rate", i, f"{line['item'][:80]}: CAGR {cagr}% over the threshold")
    return out


def escalate(value: Decimal, pct, years: int) -> Decimal:
    """`value` plus `pct` a year for `years`, compounded and rounded to the
    paisa each year, as the PR flow's table does (100, 107, 114.49)."""
    for _ in range(years):
        value = (value * (100 + Decimal(str(pct))) / 100).quantize(CENT)
    return value.quantize(CENT)


def _r10(comp: dict) -> tuple[str, Decimal | None, Decimal | None]:
    """R10/R38: the flag the rate comparison gives, with VARN and CAGR."""
    rules = policy()["rate_comparison"]
    quoted, po = _dec(comp["quoted"]), _dec(comp["po_rate"])
    if po is None or po == 0:
        return "new_item", None, None
    ratio = quoted / po
    varn = ((ratio - 1) * 100).quantize(CENT)
    if varn < rules["varn_pct"]:
        return "ALR", varn, None
    years = comp.get("years")
    if years is None or years <= rules["min_years"]:
        return "manual_review", varn, None
    cagr = Decimal(str((float(ratio) ** (1 / years) - 1) * 100)).quantize(CENT)
    return ("AHR" if cagr > rules["cagr_pct"] else "Regular"), varn, cagr


# -- duplicates ----------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")


# what every request says, whatever it is for
_FILLER = frozenset(["we", "need", "needs", "needed", "require", "required", "requirement", "want", "to", "the", "of", "for", "and", "a", "an", "procure", "procurement", "purchase", "buy", "supply", "provide", "please", "some", "new", "with", "in", "on", "at"])


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if len(w) > 1 and w not in _FILLER}


def _duplicate_need(conn, need) -> list:
    """A new case whose requirement is another open case's: warn, with its id.
    Two needs for the same thing can be real (another plant, another year),
    so this does not block; the PR check does."""
    mine = _words(need.raw_text)
    if not mine:
        return []
    actor = store.current()
    rows = conn.execute("SELECT case_id, record_json FROM nam_cases WHERE tenant = ? AND "
                        "case_id != ? ORDER BY updated_at DESC LIMIT 500",
                        (actor.tenant, need.need_id)).fetchall()
    floor = policy()["duplicates"]["need_overlap"]
    same = []
    for case_id, text in rows:
        rec = json.loads(text)
        if (rec.get("workflow") or {}).get("closed"):
            continue
        theirs = _words(rec.get("raw_text", ""))
        # the share of the shorter need's words the other has too
        if theirs and len(mine & theirs) / min(len(mine), len(theirs)) >= floor:
            same.append(case_id)
    if not same:
        return []
    return [("duplicate_need", "warned", (f"{need.need_id} looks like the open case(s) "
             f"{', '.join(same[:5])}; continue only if it is a separate need"))]


def item_key(item: dict) -> str:
    code = (item.get("material_code") or "").strip().upper()
    if code:
        return f"mc:{code}"
    desc = " ".join(_WORD.findall(item["description"].lower()))
    return f"d:{desc}|{(item.get('uom') or '').strip().lower()}"


_REGISTER = """
CREATE TABLE IF NOT EXISTS nam_pr_register (
    tenant TEXT NOT NULL,
    item_key TEXT NOT NULL,
    case_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    pr_number TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL,
    PRIMARY KEY (tenant, item_key, case_id)
);
"""


def ensure(conn: sqlite3.Connection) -> None:
    """Create or migrate these tables, once per database file per process
    (nam_agent.db.once): each call re-running the schema took the write lock."""
    from nam_agent import db

    db.once(conn, "nam.guardrails", _setup, table="nam_pr_register")


def _setup(conn: sqlite3.Connection) -> None:
    conn.executescript(_REGISTER)


def _conflicts(conn, tenant: str, case_id: str, keys: list[str]) -> list[tuple]:
    """Other cases' PRs, registered within the open window, with these items."""
    since = (datetime.now(UTC) - timedelta(days=policy()["duplicates"]["pr_open_days"])
             ).isoformat(timespec="seconds")
    found = []
    for k in keys:
        found += conn.execute("SELECT item_key, case_id, pr_number FROM nam_pr_register WHERE "
                              "tenant = ? AND item_key = ? AND case_id != ? AND at >= ?",
                              (tenant, k, case_id, since)).fetchall()
    return found


def _dup_breaches(found: list[tuple]) -> list[Breach]:
    return [Breach("duplicate_pr", f"{key.split(':', 1)[1]} is already on the open PR "
                   f"{pr or '(being submitted)'} of {case_id}") for key, case_id, pr in found]


def _pr(conn, need, pr: dict) -> list[Breach]:
    ensure(conn)
    keys = sorted({item_key(i) for i in pr["items"]})
    return _dup_breaches(_conflicts(conn, store.current().tenant, need.need_id, keys))


def register_pr(conn, need, action_id: str, draw) -> dict:
    """submit_pr's effect, under the write lock: refuse an item another open
    PR covers, else register this PR's items and draw its number. Two cases
    submitting the same item at once: the second is refused."""
    from nam_agent.nam import orchestrator

    pr = orchestrator._artifact(orchestrator.workflow(need), "pr") or {}
    if schema.validate(pr, schema.load("artifact.pr")):
        raise _refuse(conn, [Breach("artifact", "the PR artifact is outside its schema")],
                      need.need_id, "submit_pr")
    tenant = store.current().tenant
    keys = sorted({item_key(i) for i in pr["items"]})
    at = datetime.now(UTC).isoformat(timespec="seconds")
    ensure(conn)
    with store._write(conn):
        found = _conflicts(conn, tenant, need.need_id, keys)
        if not found:
            conn.executemany("INSERT OR REPLACE INTO nam_pr_register (tenant, item_key, case_id, "
                             "action_id, pr_number, at) VALUES (?, ?, ?, ?, '', ?)",
                             [(tenant, k, need.need_id, action_id, at) for k in keys])
    if found:
        raise _refuse(conn, _dup_breaches(found), need.need_id, "submit_pr")
    number = draw(conn)
    with store._write(conn):
        conn.execute("UPDATE nam_pr_register SET pr_number = ? WHERE tenant = ? AND "
                     "case_id = ? AND action_id = ?", (number, tenant, need.need_id, action_id))
    return {"pr_number": number, "items": len(keys)}


_SAP_PR = """CREATE TABLE IF NOT EXISTS nam_sap_pr (
    tenant TEXT NOT NULL, sap_pr TEXT NOT NULL, case_id TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (tenant, sap_pr))"""


def sap_pr_owner(conn, number: str, case_id: str) -> str | None:
    """The other case a SAP PR number is already recorded on (F29), or None."""
    conn.execute(_SAP_PR)
    row = conn.execute("SELECT case_id FROM nam_sap_pr WHERE tenant = ? AND sap_pr = ?",
                       (store.current().tenant, number)).fetchone()
    return row[0] if row and row[0] != case_id else None


def record_sap_pr(conn, number: str, case_id: str) -> None:
    """Claim a SAP PR number for this case, under the write lock; a number
    another case claimed first is refused (422 guardrail)."""
    conn.execute(_SAP_PR)
    tenant = store.current().tenant
    with store._write(conn):
        conn.execute("INSERT OR IGNORE INTO nam_sap_pr (tenant, sap_pr, case_id, at) "
                     "VALUES (?, ?, ?, ?)", (tenant, number, case_id,
                                             datetime.now(UTC).isoformat(timespec="seconds")))
        row = conn.execute("SELECT case_id FROM nam_sap_pr WHERE tenant = ? AND sap_pr = ?",
                           (tenant, number)).fetchone()
    if row[0] != case_id:
        raise _refuse(conn, [Breach("duplicate_pr", f"SAP PR {number} is already recorded "
                                    f"on {row[0]}")], case_id, "pr.submit")


# -- the package and the hand-off ----------------------------------------------------

def _mandatory(need, package: dict | None) -> list[Breach]:
    cat = _category(need)
    if not cat:
        return [Breach("mandatory_docs", "the case has no category, so no mandatory "
                       "document list")]
    have = {d["kind"] for d in (package or {}).get("documents") or []}
    lacking = [k for k in policy()["mandatory_documents"][cat] if k not in have]
    return [Breach("mandatory_docs", f"the {cat} package lacks {', '.join(lacking)}")] \
        if lacking else []


def _at_version(wf: dict, name: str) -> int:
    return int(((wf.get("artifacts") or {}).get(name) or {}).get("at_version") or 0)


def _validated(wf: dict) -> list[Breach]:
    from nam_agent.nam import orchestrator

    if (orchestrator._artifact(wf, "validation") or {}).get("status") != "pass":
        return [Breach("handoff", "validation has not passed")]
    changed = max(_at_version(wf, n) for n in ("documents", "review", "compliance"))
    if _at_version(wf, "validation") < changed:
        return [Breach("handoff", "the documents changed after the last validation")]
    return []


def _r32(need, wf: dict, *, with_handoff: bool = True, handoff: dict | None = None
         ) -> list[Breach]:
    """R32: nothing goes to NIT while an approval, a validation or a
    mandatory document is missing, or with a package that is not the frozen one."""
    from nam_agent.nam import orchestrator

    out = [Breach("handoff", f"the {g} approval is not given")
           for g in GATES if g not in wf["gates"]]
    out += [Breach("handoff", f"{a} is not confirmed")
            for a in ACTIONS if a not in wf["actions"]]
    out += _validated(wf)
    out += [Breach("handoff", b.says) for b in
            _mandatory(need, orchestrator._artifact(wf, "package"))]
    if (orchestrator._artifact(wf, "uploads") or {}).get("awaiting"):
        out.append(Breach("handoff", "an edited document is still awaited"))
    frozen = (wf.get("frozen") or {}).get("at_version")
    if not frozen:
        out.append(Breach("handoff", "the package is not frozen"))
    if with_handoff:
        handoff = handoff if handoff is not None else orchestrator._artifact(wf, "handoff")
        if not handoff:
            out.append(Breach("handoff", "the hand-off package is not assembled"))
        elif frozen and handoff.get("package_version") != frozen:
            out.append(Breach("handoff", f"the hand-off carries package version "
                              f"{handoff.get('package_version')}, not the frozen {frozen}"))
    return out


def before_action(conn: sqlite3.Connection, kind: str, need, wf: dict, *,
                  home=None) -> None:
    """Refuse an irreversible action whose rule does not hold, before it is
    claimed and run. Before the freeze and the handoff the whole case is
    verified again (verify.case)."""
    from nam_agent.nam import orchestrator, verify

    breaches: list[Breach] = []
    if kind in ("freeze_package", "handoff_nit"):
        breaches += [Breach("verification", p) for p in verify.case(conn, home, need)]
    if kind == "freeze_package":
        breaches += _mandatory(need, orchestrator._artifact(wf, "package")) + _validated(wf)
    elif kind == "submit_pr":
        pr = orchestrator._artifact(wf, "pr") or {}
        problems = schema.validate(pr, schema.load("artifact.pr"))
        breaches = [Breach("artifact", "the PR artifact is outside its schema: "
                           + "; ".join(problems[:5]))] if problems else _pr(conn, need, pr)
    elif kind == "handoff_nit":
        breaches += _r32(need, wf)
    if breaches:
        raise _refuse(conn, breaches, need.need_id, kind)
