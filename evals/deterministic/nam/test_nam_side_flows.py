"""need.enrich (FRS F9), material.suggest (F11) and case.edit (F12, F42):
the record read but never selected, the items suggested but picked only by
the officer, and every edit kept with its reason."""

from nam_fakes import ask, classified, ctx, run
from nam_walk import call, goods_at_s3, goods_frozen

from nam_agent.nam import need, orchestrator, store


def _s2(c):
    case_id = run(ask("i need to procure gaskets"), c)["case_id"]
    run(ask("", case_id=case_id, answer={"question_id": "variant", "value": "ring joint"}), c)
    assert classified(c, case_id)["stage"] == "S2"
    return case_id


# -- need.enrich -------------------------------------------------------------------------------

def test_enrichment_reads_the_related_tenders_and_selects_none(tmp_path):
    c = ctx(tmp_path)
    case_id = _s2(c)
    body = call(c, case_id, "Enrich the case from SAP")
    keys = [s["case_key"] for s in body["result"]["sources"]]
    assert keys == ["PUR-4", "PUR-5"]          # the related tenders left after the answer
    assert body["result"]["sources"][0]["fields"]["emd_amount"] == "9,000"
    assert "cost_centre" in body["result"]["missing"] and body["next"] == ["precedent.select"]
    assert need.load(c.conn, case_id).selected_cases == []
    assert any("not connected" in w for w in body["warnings"])


def test_a_named_tender_is_read_and_an_ambiguous_one_is_asked_about(tmp_path):
    c = ctx(tmp_path)
    case_id = _s2(c)
    body = call(c, case_id, "Enrich the case from SAP", answer="ref", value="PUR-1")
    assert "PUR-1" in [s["case_key"] for s in body["result"]["sources"]]
    vague = call(c, case_id, "Enrich the case from SAP", answer="ref", value="OC01000435")
    assert any("names 2 tenders" in w for w in vague["warnings"])


# -- material.suggest ----------------------------------------------------------------------------

def test_items_are_suggested_and_a_picked_one_becomes_a_quantity_line(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    body = call(c, case_id, "Suggest materials and specifications")
    items = [s["item"] for s in body["result"]["suggestions"]]
    assert "ring joint gasket R31" in items and body["result"]["picked"] == 0
    assert body["result"]["suggestions"][0]["spec"] == ["ASME B16.20, SS316 windings."]
    n = items.index("ring joint gasket R31") + 1
    body = call(c, case_id, "Suggest materials and specifications", answer="pick",
                value=f"{n}: use=yes")
    assert body["result"]["picked"] == 1 and body["next"] == ["quantity.finalise"]
    q = call(c, case_id, "Finalise the quantity")
    lines = q["result"]["lines"]
    assert lines[-1]["item"] == "ring joint gasket R31" and lines[-1]["qty"] is None
    assert lines[-1]["source"].startswith("suggested from PUR-")
    assert q["status"] == "needs_input"          # the officer gives its quantity
    again = call(c, case_id, "Finalise the quantity")
    assert len(again["result"]["lines"]) == len(lines)     # added once


def test_a_bad_pick_changes_nothing(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    call(c, case_id, "Suggest materials and specifications")
    body = call(c, case_id, "Suggest materials and specifications", answer="pick",
                value="1: use=yes; 99: use=yes")
    assert body["result"]["picked"] == 0 and any("no suggestion 99" in w
                                                 for w in body["warnings"])


# -- case.edit ------------------------------------------------------------------------------------

def test_an_edit_needs_its_reason_and_is_in_the_history(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    body = call(c, case_id, "Edit the case details", answer="edit", value="all: location=Plant 2")
    assert any("needs its reason" in w for w in body["warnings"])
    version = store.version_of(c.conn, case_id)
    body = call(c, case_id, "Edit the case details", answer="edit",
                value="all: location=Plant 2, why=the unit moved")
    assert body["result"]["changed"] == [{"field": "location", "from": "", "to": "Plant 2"}]
    assert store.version_of(c.conn, case_id) > version
    reasons = [h["reason"] for h in body["result"]["history"]]
    assert any("location '' -> 'Plant 2' because the unit moved" in r for r in reasons)


def test_a_field_another_flow_owns_is_refused_with_its_flow(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    body = call(c, case_id, "Edit the case details", answer="edit",
                value="all: category=works, why=wrong")
    assert any("category is changed in need.classify" in w for w in body["warnings"])
    bad = call(c, case_id, "Edit the case details", answer="edit",
               value="all: subtype=Capital, why=x y")
    assert any("not a sub-type" in w for w in bad["warnings"])


def test_nothing_is_edited_after_the_freeze(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_frozen(c)
    body = call(c, case_id, "Edit the case details", answer="edit",
                value="all: location=Plant 2, why=moved")
    assert body["error"]["code"] == "stage_gate" and "frozen" in body["error"]["message"]
    assert orchestrator.workflow(need.load(c.conn, case_id))["frozen"]
