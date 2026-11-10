"""S8 to S10 (FRS F37-F41, F43-F45, F50): the approval file and its
authority, the Aarohan upload list, documents edited outside NAM, and the
structured handoff to NIT that closes the case. Each irreversible step is
confirmed by its own role; the S8 PR comes from the real flows."""

import pytest
from nam_fakes import OFFICER, ctx
from nam_walk import (
    APPROVER,
    BUYER,
    COMMERCIAL,
    call,
    confirm,
    goods_approved,
    goods_submitted,
)

from nam_agent.nam import audit, need, orchestrator, store
from nam_agent.nam.docs import render
from nam_agent.nam.docs import store as doc_store
from nam_agent.nam.playbooks import approval, uploads

A = "Route for approval"
H = "Hand off to the NIT module"
U = "Upload the edited document"


def _wf(c, case_id):
    return orchestrator.workflow(need.load(c.conn, case_id))


# -- approval ---------------------------------------------------------------------------------

def test_without_a_doa_the_approver_names_the_authority_and_gets_the_ann(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_submitted(c)
    body = call(c, case_id, A, caller=APPROVER)
    assert body["status"] == "needs_input" and body["questions"][0]["id"] == "authority"
    assert body["pending_action"] is None
    assert any("delegation of authority is not configured" in w for w in body["warnings"])
    bad = call(c, case_id, A, caller=APPROVER, answer="authority", value="<b>")
    assert any("3 to 120 characters" in w for w in bad["warnings"])
    body = call(c, case_id, A, caller=APPROVER, answer="authority", value="GM (Materials)")
    assert body["result"]["authority"] == "GM (Materials)"
    assert body["pending_action"]["kind"] == "approve"
    assert body["pending_action"]["allowed_roles"] == ["U7"]
    ann = doc_store.view(c, OFFICER, body["result"]["ann"]["doc_id"])
    text = " ".join(p["text"] for p in ann["content"]["pages"])
    assert "Administrative Approval Note" in text and "GM (Materials)" in text
    assert "PRX-" in text and "(provisional)" in text
    done = confirm(c, case_id, body, APPROVER)
    assert done["result"]["result"]["authority"] == "GM (Materials)"


def test_a_doa_level_picks_the_authority(tmp_path, monkeypatch):
    monkeypatch.setattr(approval, "policy", lambda: {"levels": [
        {"up_to": 5000, "authority": "DGM"}, {"up_to": None, "authority": "Director"}]})
    c = ctx(tmp_path)
    case_id = goods_submitted(c)                  # Rs 9,000
    body = call(c, case_id, A, caller=APPROVER, answer="authority", value="DGM")
    assert body["result"]["authority"] == "Director"
    assert any("is not chosen by hand" in w for w in body["warnings"])
    assert body["result"]["authority_basis"] == "DoA level up to any value"


def test_an_aarohan_file_made_by_hand_is_linked(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_submitted(c)
    body = call(c, case_id, A, caller=APPROVER, answer="aarohan_file", value="NRL/MAT/2026/9")
    assert body["result"]["aarohan_file"] == "NRL/MAT/2026/9"
    assert orchestrator._artifact(_wf(c, case_id), "approval_file")["route"] == "aarohan"


def test_a_rejection_keeps_the_case_at_s8(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_submitted(c)
    body = call(c, case_id, A, caller=APPROVER, answer="authority", value="GM (Materials)")
    pending = body["pending_action"]["id"]
    import nam_agent.nam as harness
    out = harness.run({"system": "confirm", "messages": [{"role": "user", "content": ""}],
                       "case_id": case_id, "confirm": {"action_id": pending,
                                                       "decision": "reject",
                                                       "reason": "justify the quantity"}},
                      c, APPROVER)
    assert out["stage"] == "S8" and "approve" not in _wf(c, case_id)["actions"]


def test_only_the_approver_routes_and_only_after_the_pr(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_submitted(c)
    assert call(c, case_id, A, caller=BUYER)["error"]["code"] == "forbidden_role"


# -- publish, uploads, handoff ------------------------------------------------------------------

def test_the_aarohan_list_is_in_the_frs_order_and_says_nothing_was_sent(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_approved(c)
    body = call(c, case_id, "Push the documents to Aarohan", caller=COMMERCIAL)
    kinds = [x["kind"] for x in body["result"]["items"]]
    assert kinds[:4] == ["scc", "pqc", "boq", "specification"]
    assert body["result"]["sent"] is False and "no Aarohan connector" in body["result"]["aarohan"]
    assert body["questions"][0]["id"] == "aarohan_ref"
    bad = call(c, case_id, "Push the documents to Aarohan", caller=COMMERCIAL,
               answer="aarohan_ref", value="x")
    assert any("not an Aarohan file reference" in w for w in bad["warnings"])
    ok = call(c, case_id, "Push the documents to Aarohan", caller=COMMERCIAL,
              answer="aarohan_ref", value="NRL/MAT/2026/77")
    assert ok["result"]["aarohan_ref"] == "NRL/MAT/2026/77" and ok["questions"] == []


def test_accuracy_of_an_edited_document():
    words = ["delivery", "within", "eight", "weeks", "of", "the", "order"]
    assert uploads.accuracy(words, words) == 100.0
    assert uploads.accuracy(words, [*words[:3], "ten", *words[4:]]) == pytest.approx(85.7, 0.1)
    assert uploads.accuracy([], ["x"]) == 0.0


def _scc(c, case_id):
    return next(d for d in orchestrator._artifact(_wf(c, case_id), "package")["documents"]
                if d["kind"] == "scc")


def test_an_awaited_edit_holds_the_handoff_until_it_is_uploaded(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_approved(c)
    body = call(c, case_id, U, answer="expect", value="scc")
    assert body["status"] == "needs_input" and body["result"]["awaiting"] == ["scc"]
    assert body["stage"] == "S10"
    refused = call(c, case_id, H, caller=COMMERCIAL)
    assert refused["error"]["code"] == "guardrail"
    assert any("still awaited" in d for d in refused["error"]["details"])
    scc = _scc(c, case_id)
    doc_store.new_version(c, OFFICER, scc["doc_id"], filename="scc.docx",
                          expected=scc["version"], reason="approver's comments",
                          data=render.docx("Special conditions of contract",
                                           [("p", "Delivery within 10 weeks of the order.")]))
    body = call(c, case_id, U)
    update = body["result"]["updates"][0]
    assert update["kind"] == "scc" and update["to_version"] == scc["version"] + 1
    assert 0 < update["accuracy_pct"] < 100 and body["result"]["awaiting"] == []
    assert call(c, case_id, U, answer="expect", value="colour")["warnings"]


def test_the_handoff_carries_the_structured_data_and_closes_the_case(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_approved(c)
    call(c, case_id, "Push the documents to Aarohan", caller=COMMERCIAL, answer="aarohan_ref",
         value="NRL/MAT/2026/77")
    body = call(c, case_id, H, caller=COMMERCIAL)
    data = body["result"]["data"]
    assert data["category"] == "goods" and data["tender_type"] == "Open"
    # the estimate's own line, as the harness priced it: the last PO price,
    # brought up to today (policy goods_price)
    est = need.load(c.conn, case_id).workflow["artifacts"]["estimate"]["value"]["lines"][0]
    assert data["items"] == [{"item": "ring joint gasket R24", "material_code": "MC-PUR-4-1",
                              "qty": 20, "uom": "Nos", "rate": est["rate"],
                              "amount": est["amount"], "basis": "po"}]
    assert data["pr"]["pr_number"].startswith("PRX-")
    assert data["approval"] == {"file": f"FILE-{case_id}", "authority": "GM (Materials)",
                                "approved_by": "a7",
                                "approved_at": _wf(c, case_id)["actions"]["approve"]["at"]}
    assert data["fields"]["delivery_period"] == "8 weeks" and data["aarohan_ref"]
    assert data["compliance"] and data["precedents"] == ["PUR-4"]
    assert body["pending_action"]["kind"] == "handoff_nit"
    done = confirm(c, case_id, body, COMMERCIAL)
    assert done["result"]["result"]["nit"].startswith("recorded")
    assert _wf(c, case_id)["closed"] is True
    after = call(c, case_id, "Prepare the PR", caller=BUYER)
    assert after["error"]["code"] == "stage_gate" and "closed" in after["error"]["message"]


def test_a_goods_case_runs_from_need_to_nit_through_the_real_flows(tmp_path):
    """S1 to S10: every flow real, every gate and action confirmed by its role,
    every step in the case history and the audit chain intact."""
    c = ctx(tmp_path)
    case_id = goods_approved(c)
    body = call(c, case_id, H, caller=COMMERCIAL)
    confirm(c, case_id, body, COMMERCIAL)
    wf = _wf(c, case_id)
    assert sorted(wf["gates"]) == ["clauses", "documents", "estimate", "quantity"]
    assert sorted(wf["actions"]) == ["approve", "freeze_package", "handoff_nit", "submit_pr"]
    history = " ".join(f"{e['flow']} {e['reason']}" for e in store.versions(c.conn, case_id))
    for flow in ("need.capture", "need.classify", "precedent.select", "quantity.finalise",
                 "estimate.goods", "docs.generate", "docs.review", "compliance.build",
                 "validate", "package.freeze", "pr.build", "approval", "handoff.nit"):
        assert flow in history, flow
    by = {wf["actions"][a]["user"] for a in wf["actions"]}
    assert by == {"v5", "b1", "a7", "c6"}
    assert audit.verify(c.conn)["ok"]
    calls = audit.events(c.conn, case_id=case_id, kind="call")
    assert {e["name"] for e in calls} >= {"handoff.nit", "nam.confirm", "validate"}


@pytest.mark.parametrize("category", ["services", "works"])
def test_services_and_works_cases_also_run_to_nit(tmp_path, category):
    from nam_walk import services_at_s4, to_nit, works_at_s4
    from test_nam_estimate import INPUTS, LINES, MANPOWER

    c = ctx(tmp_path)
    if category == "services":
        case_id = services_at_s4(c, MANPOWER)
        body = call(c, case_id, "Wage estimate", answer="rates", value=INPUTS)
        fields, docs = "all: contract_period=12 months, location=Plant 1", ("pqc", "scc")
    else:
        case_id = works_at_s4(c, LINES)
        body = call(c, case_id, "Works estimate", answer="rates",
                    value="2: rate=245, schedule=nrl_sor, ref=2.1.4; 3: rate=7800, "
                          "schedule=dsr; 4: rate=6100, basis=similar, score=0.97, ref=4.1.2; "
                          "5: c=frame:9000|shutter:14000|fixing:1500")
        fields, docs = ("all: completion_period=6 months, location=Township",
                        ("specification", "pqc", "scc"))
    assert confirm(c, case_id, body)["stage"] == "S7"      # S5, S6 optional
    done = to_nit(c, case_id, fields, docs)
    assert done["result"]["kind"] == "handoff_nit" and _wf(c, case_id)["closed"]
    handoff = orchestrator._artifact(_wf(c, case_id), "handoff")["payload"]
    assert handoff["category"] == category and handoff["items"]
    assert audit.verify(c.conn)["ok"]
