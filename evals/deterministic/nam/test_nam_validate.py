"""compliance.build (FRS F26), validate (F30-F34, F46) and package.freeze
(F35, F36): the department decides each requirement, the checker fails what
is inconsistent or tampered with, and only a current, passed validation is
frozen."""

import copy
import sqlite3

from nam_fakes import OFFICER, ctx
from nam_walk import (
    CHECKER,
    DEPT,
    DRAFTER,
    approve_documents,
    call,
    confirm,
    goods_at_s5,
    goods_drafted,
    goods_validated,
)

from nam_agent.nam import audit, need, orchestrator
from nam_agent.nam.docs import render
from nam_agent.nam.docs import store as doc_store
from nam_agent.nam.playbooks import checker

V = "Validate the PR and documents"
C = "Build the compliance requirements"


def _wf(c, case_id):
    return orchestrator.workflow(need.load(c.conn, case_id))


# -- compliance ------------------------------------------------------------------------------

def test_the_department_decides_each_requirement(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s5(c)
    call(c, case_id, "Generate the BOQ and SCC", answer="fields",
         value="all: delivery_period=8 weeks, delivery_location=NRL stores")
    body = call(c, case_id, C, caller=DEPT)
    ids = [r["id"] for r in body["result"]["requirements"]]
    assert ids == ["mse_preference", "mse_emd_exemption", "make_in_india", "oem_authorisation",
                   "startup_relaxation", "technical_compliance"]       # goods only
    assert body["status"] == "needs_input" and body["result"]["undecided"] == list(range(1, 7))
    refused = call(c, case_id, C, caller=DEPT, answer="requirements", value="1: apply=no")
    assert any("needs why" in w for w in refused["warnings"])
    body = call(c, case_id, C, caller=DEPT, answer="requirements",
                value="all: apply=yes; 4: apply=no, why=the bidders are OEMs; add: "
                      "text=Bidders hold a valid PESO licence, why=pressure parts")
    rows = body["result"]["requirements"]
    assert body["result"]["complete"] and rows[3]["apply"] is False and rows[-1]["source"] == \
        "department"
    art = orchestrator._artifact(_wf(c, case_id), "compliance")
    assert art["doc_id"] and doc_store.view(c, OFFICER, art["doc_id"])["document"]["kind"] == \
        "compliance"
    assert call(c, case_id, C)["error"]["code"] == "forbidden_role"


# -- validate ---------------------------------------------------------------------------------

def test_a_consistent_case_passes_with_its_report_stored(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_drafted(c)
    body = call(c, case_id, V, caller=CHECKER)
    assert body["result"]["status"] == "pass" and body["result"]["failures"] == 0
    checks = {x["check"] for x in body["result"]["checks"]}
    assert {"manual", "completeness"} <= checks
    assert any("No Procurement Manual" in x["says"] for x in body["result"]["checks"])
    report = body["result"]["report"]
    view = doc_store.view(c, OFFICER, report["doc_id"])
    assert view["document"]["kind"] == "validation_report"
    again = call(c, case_id, V, caller=CHECKER)
    assert again["result"]["report"] == report             # nothing changed: no new version


def test_a_conflict_fails_until_it_is_accepted_in_review(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_drafted(c, "all: delivery_period=12 weeks, delivery_location=NRL stores")
    body = call(c, case_id, V, caller=CHECKER)
    assert body["result"]["status"] == "fail"
    assert any(x["check"] == "consistency" and "delivery" in x["says"]
               for x in body["result"]["checks"])
    review = call(c, case_id, "Review the drafts", caller=DRAFTER)
    n = next(f["n"] for f in review["result"]["findings"] if f["kind"] == "conflict")
    call(c, case_id, "Review the drafts", caller=DRAFTER, answer="accept",
         value=f"{n}: why=12 weeks is agreed")
    assert call(c, case_id, V, caller=CHECKER)["result"]["status"] == "pass"


def test_the_boq_and_the_quantity_must_match_the_estimate(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_drafted(c)
    rec = need.load(c.conn, case_id)
    wf = orchestrator.workflow(rec)
    docs = copy.deepcopy(orchestrator._artifact(wf, "documents"))
    boq = next(d for d in docs["documents"] if d["kind"] == "boq")
    boq["rows"][0]["qty"] = 25
    qty = copy.deepcopy(orchestrator._artifact(wf, "quantity"))
    qty["lines"][0]["uom"] = "Sets"
    r = checker.Report()
    checker._consistency(r, rec, wf, docs, orchestrator._artifact(wf, "estimate"), qty, {})
    says = [x["says"] for x in r.items if x["severity"] == "fail"]
    assert any("boq line 1: qty is 25" in s for s in says)
    assert any("the approved quantity says 20 Sets" in s for s in says)


def test_a_tampered_file_or_audit_log_fails_integrity(tmp_path):
    c = ctx(tmp_path, conn=sqlite3.connect(tmp_path / "s.db"))
    case_id = goods_drafted(c)
    scc = next(d for d in orchestrator._artifact(_wf(c, case_id), "documents")["documents"]
               if d["kind"] == "scc")
    sha = doc_store.view(c, OFFICER, scc["doc_id"])["document"]["sha256"]
    blob = doc_store._blob(tmp_path, "nrl", sha)
    blob.write_bytes(blob.read_bytes() + b"x")
    body = call(c, case_id, V, caller=CHECKER)
    assert body["result"]["status"] == "fail"
    assert any(x["doc"] == "scc" and x["check"] in ("integrity", "format")
               for x in body["result"]["checks"] if x["severity"] == "fail")
    # the audit log, edited round its triggers
    c.conn.execute("DROP TRIGGER nam_events_no_update")
    c.conn.execute("UPDATE nam_events SET user = 'someone' WHERE id = 2")
    c.conn.commit()
    assert audit.verify(c.conn)["broken_at"] == 2
    body = call(c, case_id, V, caller=CHECKER)
    assert any(x["check"] == "integrity" and "audit row 2" in x["says"]
               for x in body["result"]["checks"])


def test_validate_answers_nothing_and_needs_its_roles(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_drafted(c)
    assert call(c, case_id, V, caller=CHECKER, answer="x", value="y")["error"]["code"] == \
        "bad_request"
    assert call(c, case_id, V)["error"]["code"] == "forbidden_role"


# -- package.freeze -------------------------------------------------------------------------------

def test_the_package_needs_a_current_passed_validation(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_drafted(c)
    assert approve_documents(c, case_id)["stage"] == "S7"      # validation is optional
    body = call(c, case_id, "Freeze the package", caller=CHECKER)
    assert body["error"]["code"] == "stage_gate" and "validation has not passed" in \
        body["error"]["message"]


def test_the_package_holds_the_documents_compliance_report_and_attachments(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_validated(c)
    doc_store.upload(c, OFFICER, case_id, filename="datasheet.docx", kind="datasheet",
                     data=render.docx("Datasheet", [("p", "Gasket datasheet, ASME B16.20.")]))
    body = call(c, case_id, "Freeze the package", caller=CHECKER)
    kinds = [d["kind"] for d in body["result"]["documents"]]
    assert kinds == ["estimate", "boq", "specification", "pqc", "scc", "compliance",
                     "validation_report", "datasheet"]
    assert body["pending_action"]["kind"] == "freeze_package"
    assert body["pending_action"]["allowed_roles"] == ["U5"]
    done = confirm(c, case_id, body, CHECKER)
    assert done["stage"] == "S7"
    # frozen: the stages before it cannot run again
    assert call(c, case_id, "Generate the BOQ and SCC")["error"]["code"] == "stage_gate"
