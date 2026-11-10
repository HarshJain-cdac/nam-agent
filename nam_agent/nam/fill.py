"""The chat fills the case: what the harness still needs from the officer,
asked with its options and applied through the harness's own flows.

The chat beside a NAM screen (or the conversation that opened a case) is
where an officer says "OPEX", "accept line 2" or "line 1 rate is 450". The
model is not trusted to turn that into a change; this module does it in
code, the way the screens do (nam_agent/nam/edits.py), so a small model fills a
case as well as a large one.

What a case can still need, in the PR flow's order (journey.to_give reads
the same facts):

  capture    need capture's open question (asked by the driver, drive.py)
  classify   the category, procurement type or tender type not confirmed
  rate       an estimate line with no rate (a New Item)
  decision   a flagged estimate line with no Accept or Revise (2(e))
  pr         a mandatory PR field only the officer knows (3(a))

`turn()` takes one message for a case: an answer to the item asked last, an
explicit edit ("accept line 2", "line 3 rate 450"), or "what next" (the next
item, asked). Anything else is None: the model's turn.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import nam_agent.nam as harness
from nam_agent.nam import edits, orchestrator, scope
from nam_agent.nam import followup as nam_followup
from nam_agent.nam import need as nam

# the chat acts as the officer of the reserved "local" tenant, as the
# dashboard's screens do (plan D4: identity comes from another component)
CALLER = harness.Caller(tenant="local", user="chat", roles=("U1",))

_NEXT = {"next", "what next", "whats next", "what's next", "fill", "fill it", "fill the case",
         "fill the data", "continue", "go on", "what do i need to give",
         "what is missing", "whats missing", "what's missing", "what do you need",
         "ok", "okay", "proceed", "go ahead", "move to next", "move on", "next step",
         "move to the next step", "lets proceed", "let's proceed", "carry on",
         "generate the pr", "create the pr", "prepare the pr", "make the pr", "raise the pr",
         "submit the pr", "prepare the estimate", "generate the estimate", "do the estimate",
         "make the estimate", "finalise the quantity", "finalize the quantity"}
_YES = {"yes", "ok", "okay", "approve", "approved", "go ahead", "proceed", "confirm", "accept"}
RUNS_AT_ONCE = 4        # harness steps one "next" may run before it must ask
_ACCEPT = re.compile(r"\baccept\b.*?\bline\s*(\d+)|\bline\s*(\d+)\b.*?\baccept", re.IGNORECASE)
_RATE = re.compile(r"\bline\s*(\d+)\D{0,20}?\brate\b\D{0,10}?([\d,]+(?:\.\d+)?)"
                   r"|\brate\b\D{0,20}?\bline\s*(\d+)\D{0,10}?([\d,]+(?:\.\d+)?)"
                   r"|^\s*(\d+)\s*:\s*rate\s*=\s*([\d,]+(?:\.\d+)?)\s*$", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9']+")


def _norm(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def _estimate(need) -> dict:
    return orchestrator._artifact(orchestrator.workflow(need), "estimate") or {}


def _option(label: str, value: str = "") -> dict:
    return {"label": label, "value": value or label}


def needs(need) -> list[dict]:
    """What the case still needs from the officer, each as a question."""
    from nam_agent.nam.playbooks import need_classify, pr

    if scope.tender_only():
        return []                         # the chat stops at the matching tender (scope.py)
    out: list[dict] = []
    for field in need_classify.FIELDS:
        if field in need.unresolved:
            q = need_classify._question(need, field)
            out.append({"kind": "classify", "field": field, "question": q})
    wf = orchestrator.workflow(need)
    stage = wf["stage"]
    if out:
        return out                        # the classification first: the stages wait on it
    if stage == "S2" and not need.selected_cases:
        out.append(_precedent_item(need))
    sheet = orchestrator._artifact(wf, "quantity") or {}
    if stage == "S3" and not sheet.get("lines"):
        out.append(_run_item("quantity.finalise", "Finalise the quantity",
                             "NAM drafts the quantity sheet"))
    elif stage == "S3" and not sheet.get("complete"):
        # drafted, with lines still missing a quantity or unit: the officer
        # decides how they are filled (running the draft again changes nothing)
        gaps = [i for i, r in enumerate(sheet["lines"]) if r.get("qty") is None
                or not r.get("uom")]
        first = sheet["lines"][gaps[0]] if gaps else {}
        out.append({"kind": "quantity_gaps", "question": {
            "id": "quantity_gaps",
            "text": (f"The quantity sheet has {len(sheet['lines'])} line(s); {len(gaps)} still "
                     f"need a quantity or unit (line {gaps[0] + 1}: {first.get('item', '')[:60]})."
                     " Fill them from past tenders and the AI, or give them yourself "
                     "('2: qty=40, uom=Nos') or on the Quantity step?" if gaps else
                     "The quantity sheet needs checking on the Quantity step."),
            "options": [_option("Fill the missing from past tenders and AI", "fill"),
                        _option("I will fill them myself", "self")]}})
    stale = orchestrator._at(wf, "estimate") < orchestrator._at(wf, "quantity")
    if stage == "S4" and (not orchestrator._artifact(wf, "estimate") or stale):
        flow = need_classify.route(need)["estimate_flow"]
        if flow:
            out.append(_run_item(flow, _FLOW_PROMPT.get(flow, "Prepare the estimate"),
                                 "NAM prices each line from its last purchase"))
    if stage == "S7" and not orchestrator._artifact(wf, "pr"):
        out.append(_run_item("pr.build", "Prepare the PR",
                             "NAM fills the PR from the case and the estimate"))
    est = _estimate(need)
    for i, line in enumerate(est.get("lines") or []):
        if line.get("rate") is None:
            out.append({"kind": "rate", "line": i, "question": {
                "id": f"rate:{i}", "options": [],
                "text": (f"What is the rate of line {i + 1} ({line['item'][:60]})? No past price "
                         "was found for it.")}})
    for i in orchestrator.undecided(est):
        line = est["lines"][i]
        flag = (line.get("comparison") or {}).get("flag")
        out.append({"kind": "decision", "line": i, "question": {
            "id": f"decision:{i}",
            "text": (f"Line {i + 1} ({line['item'][:60]}) is flagged {flag} at rate "
                     f"{line['rate']}: accept the rate, or give a new one?"),
            "options": [_option(f"Accept line {i + 1}", "accept"),
                        _option(f"Revise line {i + 1}", "revise")]}})
    pr_art = orchestrator._artifact(wf, "pr") or {}
    if pr_art.get("missing"):
        spec = pr.policy()["header"]
        # what the officer may give: their own fields, and the form's
        by_label = {rule["label"]: key for key, rule in spec.items()
                    if rule.get("from") in ("officer", "form")}
        for label in pr_art["missing"]:
            if label in by_label:
                key = by_label[label]
                options = []
                if spec[key].get("type") == "date" or key.endswith("_date"):
                    from datetime import date, timedelta
                    options = [_option(f"In {d // 30} month{'s' if d >= 60 else ''} "
                                       f"({(date.today() + timedelta(days=d)).isoformat()})",
                                       (date.today() + timedelta(days=d)).isoformat())
                               for d in (30, 60, 90)]
                out.append({"kind": "pr", "field": key, "question": {
                    "id": f"pr:{key}", "options": options,
                    "text": f"The PR needs {label.split(' (')[0]}. What is it?"}})
    pending = wf.get("pending")
    if pending and not any(x["kind"] in ("rate", "decision", "pr") for x in out):
        out.append({"kind": "approval", "pending": orchestrator.public(pending), "question": {
            "id": f"approval:{pending['id']}",
            "text": f"{pending['summary']}. Approve it?",
            "options": [_option("Approve", "approve"), _option("Reject", "reject")]}})
    return out


_FLOW_PROMPT = {"estimate.goods": "Goods estimate", "estimate.wage": "Estimate the manpower cost",
                "estimate.works": "Works estimate from the SOR"}


def _run_item(flow: str, system: str, says: str) -> dict:
    """A step the harness does itself on the officer's "next": no input."""
    return {"kind": "run", "flow": flow, "system": system,
            "question": {"id": f"run:{flow}", "text": says, "options": []}}


def _precedent_item(need) -> dict:
    """1(j): the matching past tenders, in the flow's order, to pick from."""
    f = need.followups or {}
    subjects = {}
    for x in [*[i.get("related") or [] for i in f.get("items") or []],
              (f.get("scan") or {}).get("related") or []]:
        for r in x:
            subjects.setdefault(r["case_key"], " ".join(str(r.get("subject") or "").split()))
    keys = [k for k in (f.get("ranked") or need.related_cases) if k in need.related_cases]
    options = [_option(f"{k}: {subjects.get(k, '')[:60]}".rstrip(": "), k) for k in keys[:6]]
    return {"kind": "precedent", "question": {
        "id": "precedent", "text": "Which past tender should this need be built from?",
        "options": options + [_option("None of these fit", "none")]}}


def recommend(need, question: dict) -> tuple[str, str] | None:
    """(the option NAM recommends, why) when the officer asks NAM to choose
    ("choose the one that matches best"), or None when only they can answer.
    The precedent: the related tender whose line items and subject share the
    most words with what the officer described. Any other question: the
    answer most matching past tenders carry."""
    options = [o for o in question.get("options") or []
               if o.get("value") not in ("none", nam_followup.UNKNOWN, "")]
    if not options:
        return None
    if question.get("id") == "precedent":
        f = need.followups or {}
        said = " ".join([need.raw_text, *[str(v) for k, v in (f.get("answers") or {}).items()
                                          if not str(k).startswith("d:")]])
        words = {w[:5] for w in re.findall(r"[a-z0-9]{2,}", said.lower())}
        rows = {}
        for x in [*[i.get("related") or [] for i in f.get("items") or []],
                  (f.get("scan") or {}).get("related") or []]:
            for r in x:
                rows.setdefault(r["case_key"], r)

        def score(key: str) -> tuple[int, int]:
            r = rows.get(key) or {}
            lines = [str(t) for t in (r.get("matching_items") or r.get("items_sample") or [])]
            hits = [t for t in lines if len(words & {w[:5] for w in
                                                     re.findall(r"[a-z0-9]{2,}", t.lower())}) >= 2]
            subject = len(words & {w[:5] for w in re.findall(r"[a-z0-9]{2,}",
                                                               str(r.get("subject", "")).lower())})
            return len(hits), subject
        best = max(options, key=lambda o: score(o["value"]))
        hits, subject = score(best["value"])
        if not hits and not subject:
            return None
        why = (f"{hits} of its line items name what you described" if hits else
               "its subject names what you described")
        return best["label"], f"{best['value']} matches your need best: {why}."
    counted = [o for o in options if o.get("count")]
    if not counted:
        return None
    best = max(counted, key=lambda o: o["count"])
    return best["label"], (f"Most of the matching past tenders ({best['count']}) carry "
                           f"\"{best['label']}\".")


def change_item(need, field: str) -> dict | None:
    """The question for a classification field already given, to give it
    again (the officer asked to change it), or None."""
    from nam_agent.nam.playbooks import need_classify

    if field not in need_classify.FIELDS:
        return None
    return {"kind": "classify", "field": field,
            "question": need_classify._question(need, field)}


def _ask(item: dict) -> dict:
    q = item["question"]
    from nam_agent.tools import clarify

    return {"question": q["text"],
            "options": clarify.clean_options([o["label"] for o in q.get("options") or []]),
            "field": q.get("id") or item.get("field")}


def _caller(role: str = "") -> harness.Caller:
    """The chat as the officer, or in the role a step needs, as the screens'
    role switch does on this local tool (in production the role comes from
    the identity component, plan D4)."""
    if not role or role in CALLER.roles:
        return CALLER
    return harness.Caller(tenant=CALLER.tenant, user=f"chat-{role.lower()}", roles=(role,))


def _role_for(flow: str) -> str:
    from nam_agent.nam import permissions

    allowed = permissions.policy()["flows"].get(flow) or []
    return "" if not allowed or set(allowed) & set(CALLER.roles) else allowed[0]


def _run(conn: sqlite3.Connection, home: Path, kg_url: str, role: str = ""):
    ctx = harness.Context(conn=conn, home=home, kg_url=kg_url)

    def run(req: dict) -> dict:
        request = {"system": req["system"], "case_id": req["case_id"],
                   "messages": [{"role": "user", "content": req.get("text", "")}]}
        for key in ("answer", "confirm"):
            if req.get(key):
                request[key] = req[key]
        return harness.run(request, ctx, _caller(req.get("role") or role))
    return run


def _apply(conn, home, kg_url, need, item: dict, value: str) -> str:
    """Apply one answer; returns what was done, or raises ValueError(why)."""
    kind = item["kind"]
    if kind == "classify":
        q = item["question"]
        options = {o["value"] for o in q.get("options") or []}
        if value in (nam_followup.UNKNOWN, ""):
            value = (getattr(need, item["field"]) or {}).get("value") or ""
        if value not in options:
            raise ValueError(f"answer one of: {', '.join(sorted(options))}")
        # through the harness's own flow, so the stage machine moves the case on
        reply = _run(conn, home, kg_url)({"system": "Confirm the classification",
                                          "case_id": need.need_id,
                                          "answer": {"question_id": item["field"],
                                                     "value": value}})
        if "error" in reply:
            raise ValueError(reply["error"]["message"])
        return f"{item['field'].replace('_', ' ').capitalize()}: {value}."
    if kind == "quantity_edit":
        reply = _run(conn, home, kg_url)({"system": "Finalise the quantity",
                                          "case_id": need.need_id,
                                          "answer": {"question_id": "lines",
                                                     "value": value[:500]}})
        if "error" in reply:
            raise ValueError(reply["error"]["message"])
        refused = [w for w in reply.get("warnings") or [] if "not" in w.lower()][:1]
        return f"Quantity sheet updated. {' '.join(refused)}".strip()
    if kind == "quantity_gaps":
        if value != "fill":
            return ("Give the missing quantities here ('2: qty=40, uom=Nos') or on the "
                    "Quantity step.")
        reply = _run(conn, home, kg_url)({"system": "Finalise the quantity",
                                          "case_id": need.need_id,
                                          "answer": {"question_id": "fill", "value": "missing"}})
        if "error" in reply:
            raise ValueError(reply["error"]["message"])
        return " ".join(reply.get("warnings") or ["Filled."])[:400]
    if kind == "precedent":
        run = _run(conn, home, kg_url)
        words = "none of these fit" if value == "none" else f"use {value}"
        reply = run({"system": "Select the precedent", "case_id": need.need_id, "text": words})
        if "error" in reply:
            raise ValueError(reply["error"]["message"])
        refused = (reply.get("result") or {}).get("refused") or []
        if refused:
            raise ValueError(refused[0].get("why") or "not taken")
        return ("No precedent: the guided form describes the need instead." if value == "none"
                else f"Precedent: {value}.")
    if kind == "run":
        run = _run(conn, home, kg_url, _role_for(item["flow"]))
        reply = run({"system": item["system"], "case_id": need.need_id})
        if "error" in reply:
            raise ValueError(reply["error"]["message"])
        summary = str((reply.get("result") or {}).get("summary") or "").strip()
        return f"{item['question']['text']}: done" + (f" ({summary})." if summary else ".")
    if kind == "approval":
        pending = item["pending"]
        if value not in ("approve", "reject"):
            raise ValueError("answer Approve or Reject")
        run = _run(conn, home, kg_url, (pending.get("allowed_roles") or [""])[0])
        reply = run({"system": "Approve the pending step", "case_id": need.need_id,
                     "confirm": {"action_id": pending["id"], "decision": value,
                                 **({"reason": "rejected in the chat"} if value == "reject"
                                    else {})}})
        if "error" in reply:
            raise ValueError(reply["error"]["message"])
        return "Approved." if value == "approve" else "Rejected."
    run = _run(conn, home, kg_url)
    lines = _estimate(need).get("lines") or []
    if kind in ("rate", "decision") and not 0 <= item["line"] < len(lines):
        raise ValueError(f"the estimate has no line {item['line'] + 1}")
    if kind == "decision" and item["line"] not in orchestrator.undecided(_estimate(need)):
        raise ValueError(f"line {item['line'] + 1} has no flag waiting for a decision")
    if kind in ("rate", "decision"):
        if kind == "decision" and value in ("accept", "revise"):
            if value == "revise":
                return (f"Give the new rate of line {item['line'] + 1}, e.g. \"line "
                        f"{item['line'] + 1} rate <amount>\".")
            payload = {"edits": [{"line": item["line"], "decision": "accept"}]}
        else:
            try:
                rate = float(value.replace(",", ""))
            except ValueError:
                raise ValueError("give the rate as a number") from None
            payload = {"edits": [{"line": item["line"], "rate": rate}]}
        out = _edit(need, "rates", payload, run)
        return (f"Line {item['line'] + 1}: accepted." if "decision" in payload["edits"][0]
                else f"Line {item['line'] + 1}: rate {payload['edits'][0]['rate']:g}.") + out
    if kind == "pr":
        run = _run(conn, home, kg_url, _role_for("pr.build"))
        reply = run({"system": "Prepare the PR", "case_id": need.need_id,
                     "answer": {"question_id": "fields",
                                "value": f"all: {item['field']}={edits.value(value)}"}})
        if "error" in reply:
            raise ValueError(reply["error"]["message"])
        bad = [w for w in reply.get("warnings") or [] if item["field"] in w]
        if bad:
            raise ValueError(bad[0])
        return f"PR {item['field'].replace('_', ' ')}: {value}."
    raise ValueError(f"cannot fill {kind}")


def _edit(need, kind: str, payload: dict, run) -> str:
    try:
        out = edits.apply(need, kind, payload, run)
    except edits.EditError as exc:
        raise ValueError(str(exc)) from None
    if out.get("status") == "error":
        raise ValueError(out.get("note") or (out.get("reply") or {}).get("error", {})
                         .get("message") or "the harness refused it")
    return ""


def _value_for(item: dict, message: str) -> str | None:
    """The officer's answer to `item`, or None when the words are not one."""
    from nam_agent.nam import drive

    q = item["question"]
    if item["kind"] == "approval" and _norm(message) in _YES:
        return "approve"
    kind, value = drive.answer_to(message, q)
    if kind != "answer":
        return None
    options = {o["value"] for o in q.get("options") or []}
    if options and value not in options | {nam_followup.UNKNOWN, ""}:
        return None
    return value


_QTY_EDIT = re.compile(r"^\s*(?:\d{1,4}(?:\s*-\s*\d{1,4})?(?:\s*,\s*\d{1,4})*|add|incomplete)"
                       r"\s*:\s*(?:remove|[a-z_]+\s*=)", re.IGNORECASE)


def _explicit(need, message: str) -> tuple[dict, str] | None:
    """An edit the officer stated outright: "accept line 2", "line 3 rate 450",
    and on the quantity sheet "2: qty=40, uom=Nos" (lines.py)."""
    if _QTY_EDIT.match(message or "") and orchestrator.workflow(need)["stage"] == "S3":
        return {"kind": "quantity_edit", "question": {"id": "quantity_edit"}}, message.strip()
    m = _ACCEPT.search(message)
    if m:
        i = int(m.group(1) or m.group(2)) - 1
        return {"kind": "decision", "line": i, "question": {"id": f"decision:{i}"}}, "accept"
    m = _RATE.search(message)
    if m:
        line = next(g for g in (m.group(1), m.group(3), m.group(5)) if g)
        rate = next(g for g in (m.group(2), m.group(4), m.group(6)) if g)
        i = int(line) - 1
        return {"kind": "rate", "line": i, "question": {"id": f"rate:{i}"}}, rate
    return None


def turn(conn: sqlite3.Connection, home: Path, kg_url: str, need, message: str,
         waiting: dict | None) -> tuple[str, dict | None, dict] | None:
    """(reply, ask, the state to wait on) for one message about `need`, or
    None when the message is not this module's to act on."""
    open_items = needs(need)
    item, value = None, None
    if waiting and not waiting.get("change") and _norm(message) in _NEXT:
        # "next" while a question only the officer can answer is open (a rate
        # no past price gives, a PR field): the same question, saying so
        open_q = next((x for x in open_items
                       if (x["question"].get("id") or x.get("field")) == waiting.get("field")
                       and x["kind"] in ("rate", "pr")), None)
        if open_q is not None:
            ask = _ask(open_q)
            return (f"This needs your answer before NAM can go on. {open_q['question']['text']}",
                    ask, {"field": ask["field"]})
    if waiting and waiting.get("change"):
        # the officer asked to change a value already given (drive._ask_change)
        item = change_item(need, waiting.get("field") or "")
        value = _value_for(item, message) if item else None
        if value is None:
            item = None
    elif waiting:
        item = next((x for x in open_items
                     if (x["question"].get("id") or x.get("field")) == waiting.get("field")),
                    None)
        value = _value_for(item, message) if item else None
        if value is None:
            item = None
    if item is None:
        stated = _explicit(need, message)
        if stated is not None:
            item, value = stated
    if item is None:
        if _norm(message) not in _NEXT:
            return None
        return _go_on(conn, home, kg_url, need, open_items)
    try:
        done = _apply(conn, home, kg_url, need, item, value)
    except ValueError as exc:
        q = item["question"]
        ask = _ask(item) if q.get("text") else None
        return f"Not taken: {exc}.", ask, ({"field": ask["field"]} if ask else {})
    need = nam.load(conn, need.need_id) or need
    reply, ask, state = _go_on(conn, home, kg_url, need, needs(need))
    return f"{done}\n\n{reply}".strip(), ask, state


def _go_on(conn, home, kg_url, need, open_items: list[dict]
           ) -> tuple[str, dict | None, dict]:
    """The case's next step: the steps the harness does itself are run (at
    most RUNS_AT_ONCE), then the first thing the officer must give is asked,
    with its options. Nothing left: where the case stands."""
    from nam_agent.nam import drive

    said: list[str] = []
    for _ in range(RUNS_AT_ONCE):
        if not open_items or open_items[0]["kind"] != "run":
            break
        try:
            said.append(_apply(conn, home, kg_url, need, open_items[0], ""))
        except ValueError as exc:
            said.append(f"{open_items[0]['question']['text']}: not done ({exc}).")
            break
        need = nam.load(conn, need.need_id) or need
        open_items = needs(need)
    head = "\n".join(said)
    asks = [x for x in open_items if x["kind"] != "run"]
    if asks:
        first = asks[0]
        text = f"{head}\n\n{first['question']['text']}".strip()
        return text, _ask(first), {"field": _ask(first)["field"]}
    wf = orchestrator.workflow(need)
    if "submit_pr" in wf["actions"]:
        end = ("The Purchase Request is submitted: NAM is done for this case. Its outputs "
               "(requirement sheet, estimate sheet, PR) are on the Outputs step.")
    else:
        end = drive._hint(conn, need.need_id) or "Nothing is waiting for you on this case."
    return f"{head}\n\n{end}".strip(), None, {}
