"""pr.build (FRS F27, R20) and pr.submit (FRS F28, F29): the purchase
requisition, from what the case already holds, and its number.

pr.build
  1. The header (policy/pr_fields.json, a draft until the bulk-PR-upload
     Excel is in hand): the category, procurement and tender type and the
     date opened from the case; the estimated value from the approved
     estimate; the justification and delivery date from the guided form;
     the rest (cost centre, plant, purchasing group, ...) from the officer:
     answer {question_id: "fields", value: "all: cost_centre=4100, plant=NR01"}.
  2. The items: every line of the approved estimate, with its material code.
     A Codified item without its code is a missing field.
  3. Every mandatory field still empty is listed in `missing` and asked for
     (R20). The stage machine proposes the PR's submission only when nothing
     is missing.

pr.submit
  The submission itself is an irreversible action, so it is the stage
  machine's (submit_pr, confirmed by an allowed role; the duplicate-PR check
  runs again under the write lock). There is no SAP connector: the PR is
  created in Procurix.ai with its own PR number (PRX-...). This flow shows
  where that stands, and records the SAP PR number once the PR has been
  created in SAP by hand: answer {question_id: "sap_pr", value: "1000012345"}.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path

from nam_agent.nam import guardrails, orchestrator, store
from nam_agent.nam import need as need_store
from nam_agent.nam.errors import NamError
from nam_agent.nam.playbooks import Outcome, lines

POLICY = Path(__file__).resolve().parents[1] / "policy" / "pr_fields.json"
FIELDS = "fields"
SAP_PR = "sap_pr"
SAP_STATUS = "sap_status"
_SAP_NUMBER = re.compile(r"^\d{8,12}$")


@cache
def policy() -> dict:
    return json.loads(POLICY.read_text(encoding="utf-8"))


def _from_case(need, wf, est: dict) -> dict:
    form = (orchestrator._artifact(wf, "guided_form") or {}).get("answers") or {}
    # need capture asks the same details (PR flow 1(e)): its answers stand in
    # for the form's; one the officer did not know is left for the PR to ask
    details = {k: v for k, v in ((need.followups or {}).get("details") or {}).items()
               if v and not str(v).startswith("not known")}
    details.setdefault("justification", details.get("purpose"))

    def answer(key):
        value = form.get(key) if form.get(key) not in (None, "NA", "") else details.get(key)
        return None if value in (None, "NA", "") else value

    proposal = need.raw_text.split(";")[0].strip()[:200]
    delivery = answer("required_by")
    return {
        "category": (need.category or {}).get("value"),
        "procurement_type": (need.procurement_type or {}).get("value"),
        "tender_type": (need.tender_type or {}).get("value"),
        "date_opened": need.created_at[:10],
        "proposal_name": proposal or None,
        "estimated_value": f"{est['total']:.2f}" if est.get("total") is not None else None,
        "justification": answer("justification"),
        "delivery_date": delivery,
        "requisitioner": store.current().user,
        # F13: the estimate carries the cost centre; the officer may give another
        "cost_centre": (est.get("header") or {}).get("cost_centre"),
    }


def build(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    wf = orchestrator.workflow(need)
    est = orchestrator._artifact(wf, "estimate")
    if not est or "estimate" not in wf.get("gates", {}):
        raise NamError("stage_gate", "the estimate is not approved (gate estimate)",
                       case_id=case_id)
    stored = orchestrator._artifact(wf, "pr") or {}
    spec = policy()["header"]
    officer = dict(stored.get("officer_fields") or {})
    warnings: list[str] = []

    answer = request.get("answer") or {}
    if answer and answer.get("question_id") != FIELDS:
        raise NamError("bad_request", f"pr.build asks only '{FIELDS}'")
    if answer.get("value"):
        try:
            edits = lines.parse(answer["value"], set(spec))
            if any(e.target != "all" for e in edits):
                raise lines.LineError("PR fields are set with all: name=value")
            given = {k: v.strip()[:120] for e in edits for k, v in e.fields.items()}
            for key, value in given.items():
                if spec[key]["from"] in ("case", "estimate"):
                    raise lines.LineError(f"{key} comes from the case, not from an answer: "
                                          "change it where it is decided")
                if key == "delivery_date":
                    try:
                        date.fromisoformat(value)
                    except ValueError:
                        raise lines.LineError("delivery_date is YYYY-MM-DD") from None
            officer.update({k: v for k, v in given.items() if v})
        except lines.LineError as exc:
            warnings.append(str(exc))

    case = _from_case(need, wf, est)
    header = {}
    for key, rule in spec.items():
        value = officer.get(key) if rule["from"] == "officer" or key in officer else None
        header[key] = value if value is not None else case.get(key)
    codified = (need.subtype or {}).get("value") == "Codified"
    items = [{"material_code": x.get("material_code"), "description": x["item"][:300],
              "qty": x["qty"], "uom": x.get("uom"), "rate": x.get("rate"),
              "amount": x.get("amount")} for x in est["lines"]]
    missing = [rule["label"] for k, rule in spec.items()
               if rule["mandatory"] and not header.get(k)]
    if codified:
        missing += [f"material code of item {i}" for i, x in enumerate(items, 1)
                    if not (x["material_code"] or "").strip()][:50]
    new_items = [i for i, x in enumerate(est["lines"], 1) if x.get("rate") is None]
    if new_items:
        warnings.append(f"Items {', '.join(map(str, new_items[:20]))} are New Items without "
                        "an estimated rate: the estimated value leaves them out.")

    value = {"summary": (f"PR for {len(items)} item(s), Rs {header['estimated_value'] or '?'}"
                         + (f"; {len(missing)} field(s) missing" if missing else ""))[:300],
             "missing": missing[:100], "items": items, "header": header,
             "officer_fields": officer, "precedents": list(need.selected_cases)}
    changed = value != stored
    questions = ()
    if missing:
        first = next((k for k in spec if spec[k]["label"] == missing[0]), None)
        questions = ({"id": FIELDS, "source": "harness", "options": [],
                      "text": f"Give the missing PR fields: {', '.join(missing[:8])}"
                              + (f" (all: {first}=...)" if first else ""),
                      "why": "A PR is submitted only with every mandatory field (R20)."},)
    return Outcome(case_id=case_id, status="needs_input" if missing else "ok",
                   result={"header": header, "items": items, "missing": missing[:100],
                           "new_items": new_items},
                   questions=questions, warnings=tuple(warnings),
                   next=() if missing else ("pr.submit",),
                   artifacts={"pr": value} if changed else {})


def submit(request: dict, ctx) -> Outcome:
    case_id = request["case_id"]
    need = need_store.load(ctx.conn, case_id)
    if need is None:
        raise NamError("not_found", f"no case {case_id}")
    wf = orchestrator.workflow(need)
    pr = orchestrator._artifact(wf, "pr")
    if not pr:
        raise NamError("stage_gate", "there is no PR (pr.build)", case_id=case_id)
    done = wf["actions"].get("submit_pr")
    ref = orchestrator._artifact(wf, "pr_reference") or {}
    answer = request.get("answer") or {}
    warnings: list[str] = []
    artifacts = {}

    if answer and answer.get("question_id") not in (SAP_PR, SAP_STATUS):
        raise NamError("bad_request", f"pr.submit asks only '{SAP_PR}' or '{SAP_STATUS}'")
    value = str(answer.get("value") or "").strip()
    if value and done is None:
        warnings.append("The PR is not submitted yet: confirm its submission first.")
    elif value and answer["question_id"] == SAP_STATUS:
        if not ref.get("sap_pr"):
            warnings.append("Give the SAP PR number first (sap_pr), then its status.")
        elif ref.get("sap_status") != value[:60]:
            ref = {**ref, "sap_status": value[:60], "by": store.current().user,
                   "at": datetime.now(UTC).isoformat(timespec="seconds")}
            artifacts["pr_reference"] = ref
    elif value:
        number = value
        other = (guardrails.sap_pr_owner(ctx.conn, number, case_id)
                 if _SAP_NUMBER.match(number) else None)
        if not _SAP_NUMBER.match(number):
            warnings.append(f"'{number[:40]}' is not a SAP PR number (8 to 12 digits).")
        elif other:
            warnings.append(f"SAP PR {number} is already recorded on {other}: one SAP PR "
                            "belongs to one case.")
        elif ref.get("sap_pr") != number:
            actor = store.current()
            guardrails.record_sap_pr(ctx.conn, number, case_id)
            ref = {"pr_number": done["result"]["pr_number"], "sap_pr": number,
                   "by": actor.user, "at": datetime.now(UTC).isoformat(timespec="seconds")}
            artifacts["pr_reference"] = ref

    if done is None:
        pending = wf.get("pending") or {}
        status = "needs_approval" if pending.get("kind") == "submit_pr" else "needs_input"
        if pr.get("missing"):
            warnings.append("The PR has missing fields: complete them with pr.build.")
        result = {"submitted": False, "pr_number": None, "sap_pr": None, "sap_status": None,
                  "items": len(pr["items"]),
                  "sap": "no SAP connector: the PR is created in Procurix.ai with its own "
                         "number once its submission is confirmed"}
        return Outcome(case_id=case_id, status=status, result=result,
                       warnings=tuple(warnings))
    result = {"submitted": True, "pr_number": done["result"]["pr_number"],
              "sap_pr": ref.get("sap_pr"), "sap_status": ref.get("sap_status"),
              "items": len(pr["items"]),
              "sap": done["result"].get("sap") or ""}
    questions = ()
    if not ref.get("sap_pr"):
        questions = ({"id": SAP_PR, "source": "harness", "options": [],
                      "text": "If the PR was also created in SAP by hand, give its SAP PR "
                              "number.", "why": "It links the case to SAP (F29)."},)
    elif not ref.get("sap_status"):
        questions = ({"id": SAP_STATUS, "source": "harness", "options": [],
                      "text": "Give the SAP PR's status as SAP shows it (e.g. Released).",
                      "why": "F29 records the SAP PR number and its status."},)
    return Outcome(case_id=case_id, status="ok", result=result, questions=questions,
                   warnings=tuple(warnings), next=("approval",), artifacts=artifacts)
