"""docs.generate (FRS F17-F25, plan Step 15.6): the case's mandatory
documents from its approved estimate and its selected precedents, stored as
versioned .docx files, edited by the drafter with a reason, and put to the
clauses and documents gates only once complete."""

import pytest
from nam_fakes import OFFICER, case_record, ctx
from nam_walk import DRAFTER, call, confirm, goods_at_s4, goods_at_s5

from nam_agent.nam import audit, guardrails, need, orchestrator, store
from nam_agent.nam.docs import parse, render
from nam_agent.nam.docs import store as doc_store
from nam_agent.nam.playbooks import documents

D = "Generate the BOQ and SCC"
FIELDS = "all: delivery_period=8 weeks, delivery_location=NRL stores"


def _docs(c, case_id):
    return orchestrator._artifact(orchestrator.workflow(need.load(c.conn, case_id)), "documents")


def _doc(body, kind):
    return next(d for d in body["result"]["documents"] if d["kind"] == kind)


def test_the_categorys_mandatory_documents_are_drafted(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s5(c)
    body = call(c, case_id, D)
    kinds = [d["kind"] for d in body["result"]["documents"]]
    assert kinds == guardrails.policy()["mandatory_documents"]["goods"]
    assert body["status"] == "needs_input" and body["result"]["missing_fields"] == [
        "delivery_period", "delivery_location"]
    assert body["questions"][0]["id"] == "fields" and body["pending_action"] is None
    assert any("R18" in w for w in body["warnings"])
    calls = [e["name"] for e in audit.events(c.conn, run_id=body["run_id"], kind="tool")]
    assert calls == ["tender_record", "clauses", "clauses", "clauses"]


def test_the_boq_is_made_from_the_approved_estimate(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s5(c)
    call(c, case_id, D)
    est = orchestrator._artifact(orchestrator.workflow(need.load(c.conn, case_id)), "estimate")
    boq = next(d for d in _docs(c, case_id)["documents"] if d["kind"] == "boq")
    assert [(r["item"], r["qty"], r["rate"], r["amount"]) for r in boq["rows"]] == [
        (x["item"], x["qty"], x["rate"], x["amount"]) for x in est["lines"]]
    assert boq["total"] == est["total"]


def test_the_precedents_clauses_are_verbatim_and_suggestions_are_not_included(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s5(c)
    body = call(c, case_id, D)
    scc = _doc(body, "scc")["clauses"]
    record = case_record("PUR-4")
    precedent = [x for x in scc if x["source"] == "PUR-4"]
    assert [x["text"] for x in precedent] == [x["text"] for x in record["scc"]]
    assert all(x["selected"] for x in precedent)
    suggested = [x for x in scc if x["source"].startswith("suggested")]
    assert suggested and not any(x["selected"] for x in suggested)
    # the identical clause from the KG suggestion is not listed twice
    assert len({x["text"] for x in scc}) == len(scc)


@pytest.mark.parametrize(("text", "cls"), [
    ("Delivery within 8 weeks of the purchase order.", "contract_specific"),
    ("Liquidated damages at 0.5% per week, up to 10%.", "standard"),
    ("The contractor shall not sub-contract the work.", "standard"),
    ("The bidder shall have supplied similar gaskets.", "unclassified"),
])
def test_clauses_are_classed_by_their_words(text, cls):
    assert documents.classify(text)[0] == cls


def test_complete_documents_go_to_the_drafters_two_gates(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s5(c)
    body = call(c, case_id, D, answer="fields", value=FIELDS)
    assert body["status"] == "needs_approval" and body["pending_action"]["kind"] == "gate:clauses"
    refused = confirm(c, case_id, body, OFFICER.__class__(tenant="nrl", user="x",
                                                          roles=("U5",)))
    assert refused["error"]["code"] == "forbidden_role"
    body = confirm(c, case_id, body, DRAFTER)
    assert body["pending_action"]["kind"] == "gate:documents"
    # approved: validation is optional, so the case goes on to the PR (S7)
    assert confirm(c, case_id, body, DRAFTER)["stage"] == "S7"


def test_each_document_is_a_stored_docx_versioned_when_it_changes(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s5(c)
    first = call(c, case_id, D)
    scc = _doc(first, "scc")
    assert scc["doc_id"].startswith("DOC-") and scc["version"] == 1
    view = doc_store.view(c, OFFICER, scc["doc_id"])
    assert view["document"]["kind"] == "scc" and view["document"]["filename"].endswith(".docx")
    text = "\n".join(p["text"] for p in view["content"]["pages"])
    assert "Delivery within 8 weeks of the purchase order." in text
    assert "Clause selection" in text
    again = call(c, case_id, D)                                    # nothing changed
    assert _doc(again, "scc")["version"] == 1
    edited = call(c, case_id, D, answer="scc", value="3: keep=yes, why=LD clause needed")
    assert _doc(edited, "scc")["version"] == 2 and _doc(edited, "boq")["version"] == 1
    assert _doc(edited, "scc")["selection"]["suggested_kept"] == 1


def test_a_change_needs_its_reason_and_a_bad_answer_changes_nothing(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s5(c)
    call(c, case_id, D)
    version = store.version_of(c.conn, case_id)
    body = call(c, case_id, D, answer="scc", value="3: keep=yes, why=needed; 1: keep=no")
    assert any("needs why" in w and "R27" in w for w in body["warnings"])
    assert store.version_of(c.conn, case_id) == version
    assert _doc(body, "scc")["clauses"][2]["selected"] is False          # none of it applied
    body = call(c, case_id, D, answer="scc", value="1: keep=no, why=repeats the PO terms")
    kept = _docs(c, case_id)["documents"][-1]["clauses"][0]
    assert kept["selected"] is False and kept["why"] == "repeats the PO terms"
    assert _doc(body, "scc")["selection"]["dropped"] == 1


def test_the_drafter_adds_a_clause_with_its_reason(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s5(c)
    call(c, case_id, D)
    body = call(c, case_id, D, answer="pqc",
                value="add: text=The bidder shall hold a valid ISO 9001 certificate., "
                      "why=required by the department")
    added = _doc(body, "pqc")["clauses"][-1]
    assert added["source"] == "officer" and added["selected"]
    assert call(c, case_id, D, answer="pqc", value="add: text=Only the best bidders.")[
        "warnings"][-1].startswith("add: needs text=... and why=...")


def test_a_document_with_no_clause_holds_the_gates(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s5(c)
    call(c, case_id, D, answer="fields", value=FIELDS)
    body = call(c, case_id, D, answer="pqc", value="1: keep=no, why=not this time")
    assert body["status"] == "needs_input" and body["questions"][0]["id"] == "pqc"
    body = call(c, case_id, D, answer="pqc", value="all: required=no, why=proprietary item")
    assert body["status"] == "needs_approval"


def test_the_documents_need_the_approved_estimate(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    assert call(c, case_id, D)["error"]["code"] == "stage_gate"


def test_only_a_drafter_or_the_officer_generates(tmp_path):
    from nam_walk import BUYER

    c = ctx(tmp_path)
    case_id = goods_at_s5(c)
    assert call(c, case_id, D, caller=BUYER)["error"]["code"] == "forbidden_role"
    assert call(c, case_id, D, caller=DRAFTER)["flow"] == "docs.generate"


def test_the_docx_is_the_same_bytes_for_the_same_content():
    blocks = [("h", "Lines"), ("table", [["n", "item"], [1, "gasket"]]), ("p", "a <b> & c")]
    one, two = render.docx("BOQ", blocks), render.docx("BOQ", blocks)
    assert one == two
    parsed = parse.extract(one, parse.sniff(one, "x.docx"))
    assert "a <b> & c" in parsed.pages[0].text and "1: gasket" in parsed.pages[0].text
