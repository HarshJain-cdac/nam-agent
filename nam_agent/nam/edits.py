"""The Procurix screens' edits, as the harness flows take them.

A table edit on a screen (a quantity, a rate, a clause dropped, a compliance
requirement changed) is not sent as a sentence for a model to interpret. Each
flow already takes the officer's edits as one structured answer in the plain
format of playbooks/lines.py ("2: qty=40; 3: remove; add: item=..."). This
module turns the screen's edits into exactly that answer, names the flow
explicitly (the router's "explicit" rule, so no model picks it), and runs it
through nam_agent.nam.run: the same gates, checks, audit and stage rules as every
other call.

  quantity      quantity.finalise   lines          qty, uom, item, code; remove; add
  rates         the estimate flow   rates          rate (per estimate line)
  clauses       docs.generate       scc|pqc|specification   keep=no + why; add text + why
  requirements  compliance.build    requirements   apply, text, why; add
  classify      need.classify       category | procurement_type | tender_type
  form          need.guided_form    one answer per field of the case's form

An edit the format cannot carry is refused here, naming it, before anything
runs: nothing is half-applied by a guess.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from nam_agent.nam import need as need_store
from nam_agent.nam import orchestrator

CLAUSE_DOCS = ("scc", "pqc", "specification")
CLASSIFY = ("category", "procurement_type", "tender_type")
MAX_EDITS = 400


class EditError(ValueError):
    pass


def value(text) -> str:
    """One value for the lines.py format: no ';' or newline (they end an edit),
    no double quote (it delimits); quoted when it holds a comma."""
    s = " ".join(str(text if text is not None else "").replace(";", ",").split())
    s = s.replace('"', "'")
    if not s:
        raise EditError("an empty value")
    return f'"{s}"' if "," in s else s


def _number(x, what: str) -> str:
    try:
        n = float(x)
    except (TypeError, ValueError):
        raise EditError(f"{what} must be a number, not {x!r}") from None
    if n < 0:
        raise EditError(f"{what} cannot be negative")
    return str(int(n)) if n == int(n) else repr(n)


def _why(edit: dict, what: str) -> str:
    why = str(edit.get("why") or "").strip()
    if len(why) < 3:
        raise EditError(f"{what} needs a reason (why)")
    return value(why)


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _quantity_line(need: need_store.NeedRecord, est_index: int) -> int:
    """The quantity line (1-based) an estimate line came from, matched by its
    item text: the estimate adds lines of its own (wage lump sums) that have
    no quantity line and cannot be changed there."""
    wf = orchestrator.workflow(need)
    est = orchestrator._artifact(wf, "estimate") or {}
    qty = orchestrator._artifact(wf, "quantity") or {}
    lines = est.get("lines") or []
    if not 0 <= est_index < len(lines):
        raise EditError(f"the estimate has no line {est_index + 1}")
    item = _norm(lines[est_index].get("item", ""))
    for i, q in enumerate(qty.get("lines") or []):
        if _norm(q.get("item", "")) == item:
            return i + 1
    raise EditError(f"estimate line {est_index + 1} ('{lines[est_index].get('item', '')[:50]}') "
                    "is worked out by the estimate, not a quantity line; change its rate instead")


def form_fields(need: need_store.NeedRecord) -> set[str]:
    """The fields of the case's guided form (none until its category is set)."""
    from nam_agent.nam.playbooks import need_classify, need_guided_form

    form = need_classify.route(need)["guided_form"]
    return {q["key"] for q in need_guided_form.questions(form)} if form else set()


def plan(need: need_store.NeedRecord, kind: str, payload: dict) -> list[tuple[str, str, str]]:
    """[(flow, question_id, answer value)] for one screen edit, in run order."""
    edits = list(payload.get("edits") or [])
    if len(edits) > MAX_EDITS:
        raise EditError(f"at most {MAX_EDITS} edits at once")

    if kind == "quantity":
        parts = []
        for e in edits:
            if e.get("add"):
                a = e["add"]
                fields = [f"item={value(a.get('item'))}", f"qty={_number(a.get('qty'), 'qty')}"]
                if a.get("uom"):
                    fields.append(f"uom={value(a['uom'])}")
                if a.get("code"):
                    fields.append(f"code={value(a['code'])}")
                parts.append("add: " + ", ".join(fields))
                continue
            line = _quantity_line(need, int(e.get("line", -1)))
            if e.get("remove"):
                parts.append(f"{line}: remove")
                continue
            fields = []
            if "qty" in e:
                fields.append(f"qty={_number(e['qty'], 'qty')}")
            for key in ("uom", "item", "code"):
                if e.get(key):
                    fields.append(f"{key}={value(e[key])}")
            if not fields:
                raise EditError(f"nothing to change on line {line}")
            parts.append(f"{line}: " + ", ".join(fields))
        return [("quantity.finalise", "lines", "; ".join(parts))] if parts else []

    if kind == "rates":
        est_entry = (orchestrator.workflow(need).get("artifacts") or {}).get("estimate") or {}
        flow = est_entry.get("flow")
        if flow not in ("estimate.goods", "estimate.wage", "estimate.works"):
            raise EditError("there is no estimate yet to change a rate in")
        n_lines = len((est_entry.get("value") or {}).get("lines") or [])
        parts = []
        for e in edits:
            i = int(e.get("line", -1))
            if not 0 <= i < n_lines:
                raise EditError(f"the estimate has no line {i + 1}")
            fields = []
            if e.get("rate") not in (None, ""):
                fields.append(f"rate={_number(e.get('rate'), 'rate')}")
            if e.get("decision"):
                # Accept or Revise on a flagged line (PR flow 2(e)); goods only
                decision = str(e["decision"]).strip().lower()
                if decision not in ("accept", "revise"):
                    raise EditError("a decision is accept or revise")
                if flow != "estimate.goods":
                    raise EditError("decisions are taken on a goods estimate's flagged lines")
                fields.append(f"decision={decision}")
            if not fields:
                raise EditError(f"nothing to change on line {i + 1}")
            parts.append(f"{i + 1}: " + ", ".join(fields))
        return [(flow, "rates", "; ".join(parts))] if parts else []

    if kind == "clauses":
        doc = payload.get("doc")
        if doc not in CLAUSE_DOCS:
            raise EditError(f"doc is one of {', '.join(CLAUSE_DOCS)}")
        docs = orchestrator._artifact(orchestrator.workflow(need), "documents") or {}
        stored = next((d for d in docs.get("documents") or [] if d.get("kind") == doc), None)
        if stored is None:
            raise EditError(f"there is no {doc} drafted yet")
        n = len(stored.get("clauses") or [])
        parts = []
        for e in edits:
            if e.get("add"):
                parts.append(f"add: text={value(e['add'])}, why={_why(e, 'adding a clause')}")
                continue
            i = int(e.get("clause", -1))
            if not 0 <= i < n:
                raise EditError(f"the {doc} has no clause {i + 1}")
            if "text" in e:
                # a changed clause: the old one dropped and the new one added,
                # both with the reason (the format keeps every change's why)
                why = _why(e, f"changing clause {i + 1}")
                parts += [f"{i + 1}: keep=no, why={why}", f"add: text={value(e['text'])}, why={why}"]
            elif "keep" in e:
                keep = "yes" if e["keep"] else "no"
                why = _why(e, f"{'keeping' if e['keep'] else 'dropping'} clause {i + 1}")
                parts.append(f"{i + 1}: keep={keep}, why={why}")
            else:
                raise EditError(f"clause {i + 1}: say keep, text or add")
        return [("docs.generate", doc, "; ".join(parts))] if parts else []

    if kind == "requirements":
        comp = orchestrator._artifact(orchestrator.workflow(need), "compliance") or {}
        n = len(comp.get("requirements") or [])
        parts = []
        for e in edits:
            if e.get("add"):
                parts.append(f"add: text={value(e['add'])}, why={_why(e, 'adding a requirement')}")
                continue
            i = int(e.get("n", -1))
            if not 0 <= i < n:
                raise EditError(f"there is no requirement {i + 1}")
            fields = []
            if "apply" in e:
                fields.append(f"apply={'yes' if e['apply'] else 'no'}")
            if e.get("text"):
                fields.append(f"text={value(e['text'])}")
            if not fields:
                raise EditError(f"requirement {i + 1}: say apply or text")
            fields.append(f"why={_why(e, f'changing requirement {i + 1}')}")
            parts.append(f"{i + 1}: " + ", ".join(fields))
        return [("compliance.build", "requirements", "; ".join(parts))] if parts else []

    if kind == "classify":
        out = []
        for key in CLASSIFY:
            if payload.get(key):
                out.append(("need.classify", key, str(payload[key])))
        return out

    if kind == "form":
        fields = payload.get("fields") or {}
        if not isinstance(fields, dict):
            raise EditError("fields is an object {field: value}")
        own = form_fields(need)
        return [("need.guided_form", str(k), " ".join(str(v).split()))
                for k, v in fields.items() if str(v or "").strip() and k in own]

    raise EditError(f"unknown edit kind {kind!r}")


def apply(need: need_store.NeedRecord, kind: str, payload: dict,
          run: Callable[[dict], dict]) -> dict:
    """Run the edit's answers in order through `run` (one nam.v1 request ->
    reply). Stops at the first reply that is not ok, and returns it with
    the answers that were sent, so the screen shows what the harness said."""
    steps = plan(need, kind, payload)
    skipped = sorted(set(payload.get("fields") or {}) - form_fields(need)) \
        if kind == "form" else []
    if not steps:
        return {"status": "ok", "sent": [], "replies": [], "skipped": skipped,
                "note": "nothing to change"}
    replies, sent = [], []
    for flow, qid, val in steps:
        reply = run({"system": flow, "case_id": need.need_id,
                     "answer": {"question_id": qid, "value": val}})
        sent.append({"flow": flow, "question_id": qid, "value": val})
        replies.append(reply)
        if reply.get("error") or reply.get("status") == "refused":
            break
    last = replies[-1]
    return {"status": "error" if last.get("error") else last.get("status", "ok"),
            "sent": sent, "replies": replies, "reply": last, "skipped": skipped}
