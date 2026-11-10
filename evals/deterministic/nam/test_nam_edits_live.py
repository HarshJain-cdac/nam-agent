"""DETERMINISTIC EVAL — the Procurix screens' edits, run on a real case.

A Goods case is driven through the actual harness (nam_agent.nam.run, the scripted
KG of nam_fakes, no model) to the documents, and every kind of screen edit
goes through nam_agent/nam/edits.py exactly as POST /api/nam/edit sends it: the
harness takes each answer, and the case shows the change.
"""

from __future__ import annotations

import sqlite3

import pytest
from nam_fakes import ask, classified, fake_kg

import nam_agent.nam as harness
from nam_agent.nam import edits
from nam_agent.nam import need as nam

ROLES = ("U1", "U2", "U3", "U4", "U5", "U6", "U7")


@pytest.fixture()
def case(tmp_path):
    ctx = harness.Context(conn=sqlite3.connect(":memory:"), home=tmp_path, kg_url="http://kg",
                          fetch=fake_kg())
    who = {r: harness.Caller(tenant="nrl", user=f"user-{r.lower()}", roles=(r,)) for r in ROLES}

    def call(system, case_id=None, role="U1", **extra):
        reply = harness.run(ask(extra.pop("text", ""), system=system,
                                **({"case_id": case_id} if case_id else {}), **extra),
                            ctx, who[role])
        while reply.get("status") == "needs_approval":
            act = reply["pending_action"]
            reply = harness.run(ask("", system="Approve the pending step", case_id=case_id,
                                    confirm={"action_id": act["id"], "decision": "approve"}),
                                ctx, who[act["allowed_roles"][0]])
        return reply

    reply = call("Assess need for request #123", text="i need to procure gaskets")
    case_id = reply["case_id"]
    if reply.get("questions"):
        call("Assess need for request #123", case_id,
             answer={"question_id": reply["questions"][0]["id"], "value": "ring joint"})
    classified(ctx, case_id, who["U1"])
    call("Select the precedent", case_id, text="use PUR-4")
    call("Finalise the quantity", case_id)
    call("Prepare the estimate", case_id)

    def send(kind, payload, role="U1"):
        need = nam.load(ctx.conn, case_id)
        return edits.apply(need, kind, payload,
                           lambda req: call(req["system"], req["case_id"], role,
                                            answer=req["answer"]))

    return {"ctx": ctx, "id": case_id, "call": call, "send": send,
            "load": lambda: nam.load(ctx.conn, case_id)}


def _art(need, name):
    return ((need.workflow or {}).get("artifacts") or {}).get(name, {}).get("value") or {}


def test_quantity_and_rate_edits_reach_the_case(case):
    est = _art(case["load"](), "estimate")
    assert est["lines"], "the estimate built lines"
    out = case["send"]("quantity", {"edits": [{"line": 0, "qty": 99}]})
    assert out["status"] != "error", out
    qty = _art(case["load"](), "quantity")["lines"][0]["qty"]
    assert float(qty) == 99
    case["call"]("Prepare the estimate", case["id"])               # the totals follow
    out = case["send"]("rates", {"edits": [{"line": 0, "rate": 123.5}]})
    assert out["status"] != "error", out
    line = _art(case["load"](), "estimate")["lines"][0]
    assert float(line["rate"]) == 123.5


def test_clause_and_requirement_edits_reach_the_documents(case):
    case["call"]("Generate the BOQ and SCC", case["id"], role="U4",
                 answer={"question_id": "fields",
                         "value": "all: delivery_period=8 weeks, delivery_location=NRL stores"})
    docs = _art(case["load"](), "documents")
    scc = next(d for d in docs["documents"] if d["kind"] == "scc")
    assert scc["clauses"], "the SCC has clauses to edit"
    n = len(scc["clauses"])
    out = case["send"]("clauses", {"doc": "scc", "edits": [
        {"clause": 0, "text": "Contract period: 18 months", "why": "the unit asked for longer"},
        {"add": "Safety and security of contractor personnel", "why": "the checker found it missing"}],
    }, role="U4")
    assert out["status"] != "error", out
    scc = next(d for d in _art(case["load"](), "documents")["documents"] if d["kind"] == "scc")
    texts = [c["text"] for c in scc["clauses"] if c["selected"]]
    assert "Contract period: 18 months" in texts and len(scc["clauses"]) == n + 2
    assert scc["clauses"][0]["selected"] is False and scc["clauses"][0]["why"]

    case["call"]("Review the drafts", case["id"], role="U4")
    case["call"]("Build the compliance requirements", case["id"], role="U2",
                 answer={"question_id": "requirements", "value": "all: apply=yes"})
    reqs = _art(case["load"](), "compliance")["requirements"]
    assert reqs
    out = case["send"]("requirements", {"edits": [{"n": 0, "apply": False,
                                                   "why": "not for this item"}]}, role="U2")
    assert out["status"] != "error", out
    assert _art(case["load"](), "compliance")["requirements"][0]["apply"] is False


def test_a_refused_edit_is_reported_not_half_applied(case):
    with pytest.raises(edits.EditError, match="no line"):
        case["send"]("rates", {"edits": [{"line": 999, "rate": 1}]})
