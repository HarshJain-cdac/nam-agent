"""need.guided_form (FRS F8, R1, R28, plan Step 15.3): when no past tender
fits, the need is captured through the guided form for its category.

The category (and for Works, New or Repetitive) picks the form; only the
next field still needing an answer is asked; a wrong answer is asked again
and stores nothing; a half-filled form resumes and captures nothing; a
complete one takes the place of a precedent."""

import dataclasses

import pytest
from nam_fakes import ctx, run

import nam_agent.nam as harness
from nam_agent.nam import identity, need, playbooks, schema, store
from nam_agent.nam.playbooks import need_classify, need_guided_form

SYSTEM = "Fill the guided form"
GOODS = {"codified": "Codified", "material_code": "MC-GV-2", "description": "gate valve 2 inch",
         "quantity": "20", "uom": "Nos", "required_by": "2026-12-01", "location": "Plant 2",
         "justification": "replacement of worn valves", "purpose": ""}
CLASSIFY = {"procurement_type": "OPEX", "tender_type": "Open"}


def _valid(body):
    name = "error" if "error" in body else "response"
    assert schema.validate(body, schema.load(name)) == [], body
    if "error" not in body and body["flow"] == "need.guided_form":
        assert schema.validate(body["result"], schema.load("flow.need.guided_form")) == [], body
    return body


def _form(c, case_id=None, text="", question_id=None, value=None, system=SYSTEM, caller=None):
    req = {"system": system, "messages": [{"role": "user", "content": text}]}
    if case_id:
        req["case_id"] = case_id
    if question_id:
        req["answer"] = {"question_id": question_id, "value": value}
    return _valid(run(req, c, *([caller] if caller else [])))


def _walk(c, case_id, answers):
    """Answer each question asked from `answers`; return (asked ids, last body)."""
    asked, body = [], _form(c, case_id)
    while body.get("questions") and body["questions"][0]["id"] in answers:
        q = body["questions"][0]["id"]
        asked.append(q)
        body = _form(c, case_id, question_id=q, value=answers[q])
    return asked, body


def _opened(c, text="supply of gate valves for plant 2"):
    body = _form(c, text=text)
    assert body["flow"] == "need.guided_form" and body["case_id"], body
    return body["case_id"]


def test_a_need_with_no_precedent_is_captured_through_its_form(tmp_path):
    c = ctx(tmp_path)
    case_id = _opened(c)
    asked, body = _walk(c, case_id, GOODS)
    assert asked == list(GOODS)                      # in the form's order, one at a time
    assert body["status"] == "ok" and body["result"]["complete"] and body["result"]["missing"] == []
    assert body["result"]["answers"]["purpose"] == "NA"
    assert body["next"] == ["need.classify"] and body["stage"] == "S1"
    record = need.load(c.conn, case_id)
    assert record.subtype == {"value": "Codified", "basis": "you set this", "confident": True}
    assert (record.quantity, record.location) == ("20 Nos", "Plant 2")
    assert "gate valve 2 inch" in record.raw_text and record.selected_cases == []
    _valid(run({"system": "Confirm the classification", "messages": [{"role": "user", "content": ""}],
                       "case_id": case_id, "answer": {"question_id": "procurement_type",
                                                      "value": "OPEX"}}, c))
    body = _valid(run({"system": "Confirm the classification", "messages": [{"role": "user", "content": ""}],
                       "case_id": case_id, "answer": {"question_id": "tender_type",
                                                      "value": "Open"}}, c))
    assert body["stage"] == "S3"                     # the form stood in for a precedent


def test_an_uncodified_item_needs_its_specification_not_a_code(tmp_path):
    c = ctx(tmp_path)
    case_id = _opened(c)
    asked, body = _walk(c, case_id, {"codified": "Uncodified", "description": "valve"})
    assert asked == ["codified", "description"]
    assert body["questions"][0]["id"] == "specification"
    assert "Required" in body["questions"][0]["why"]
    skipped = _form(c, case_id, question_id="specification", value="")
    assert skipped["questions"][0]["id"] == "specification"
    assert any("required" in w for w in skipped["warnings"])
    assert skipped["result"]["answers"]["material_code"] == "NA"      # ruled out, never asked


@pytest.mark.parametrize(("field", "value", "says"), [
    ("quantity", "-3", "positive number"), ("quantity", "twenty", "positive number"),
    ("quantity", "inf", "positive number"), ("required_by", "next week", "YYYY-MM-DD"),
    ("required_by", "2026-02-30", "YYYY-MM-DD"), ("codified", "Maybe", "allowed choices"),
])
def test_a_wrong_answer_is_asked_again_and_stores_nothing(tmp_path, field, value, says):
    c = ctx(tmp_path)
    case_id = _opened(c)
    before = store.version_of(c.conn, case_id)
    body = _form(c, case_id, question_id=field, value=value)
    assert body["questions"][0]["id"] == field and body["status"] == "needs_input"
    assert any(says in w for w in body["warnings"]), body["warnings"]
    assert store.version_of(c.conn, case_id) == before


def test_numbers_and_dates_are_stored_in_one_form(tmp_path):
    c = ctx(tmp_path)
    case_id = _opened(c)
    _form(c, case_id, question_id="quantity", value="1,200.50")
    body = _form(c, case_id, question_id="required_by", value="2026-12-01")
    assert body["result"]["answers"]["quantity"] == "1200.5"
    assert body["result"]["answers"]["required_by"] == "2026-12-01"


def test_an_answer_to_another_forms_field_is_refused(tmp_path):
    c = ctx(tmp_path)
    case_id = _opened(c)
    body = _form(c, case_id, question_id="budget", value="Capital")
    assert body["error"]["code"] == "bad_request" and "Goods" in body["error"]["message"]


def test_the_category_picks_the_form(tmp_path):
    c = ctx(tmp_path)
    case_id = _opened(c, "i need something for the site")
    body = _form(c, case_id)
    assert body["questions"][0]["id"] == "category" and body["result"]["form"] is None
    body = _form(c, case_id, question_id="category", value="works")
    assert body["questions"][0]["id"] == "works_kind"
    wrong = _form(c, case_id, question_id="works_kind", value="Capital / Revenue")
    assert wrong["questions"][0]["id"] == "works_kind" and wrong["warnings"]
    body = _form(c, case_id, question_id="works_kind", value="Repetitive")
    assert body["result"]["form"] == "Repetitive Works"
    assert body["questions"][0]["id"] == "budget"
    assert need.load(c.conn, case_id).subtype["value"] == "Repetitive"


@pytest.mark.parametrize(("text", "form", "first"), [
    ("supply of gate valves", "Goods", "codified"),
    ("annual maintenance of the cooling towers", "Services", "service_type"),
])
def test_each_category_has_its_own_form(tmp_path, text, form, first):
    c = ctx(tmp_path)
    body = _form(c, text=text)
    assert body["result"]["form"] == form and body["questions"][0]["id"] == first


def test_a_manpower_service_needs_its_workers(tmp_path):
    c = ctx(tmp_path)
    case_id = _opened(c, "manpower for housekeeping")
    form = _form(c, case_id)
    if form["questions"][0]["id"] == "service_type":
        _form(c, case_id, question_id="service_type", value="Repetitive (AMC, manpower)")
    for field in ("service_code", "description", "scope_of_work"):
        _form(c, case_id, question_id=field, value="x" if field != "service_code" else "")
    body = _form(c, case_id)
    assert body["questions"][0]["id"] == "worker_categories"
    assert "worker_categories" in body["result"]["missing"] and "head_count" in body["result"][
        "missing"]


def test_a_half_filled_form_resumes_and_captures_nothing(tmp_path):
    c = ctx(tmp_path)
    case_id = _opened(c)
    _form(c, case_id, question_id="codified", value="Codified")
    refused = _valid(run({"system": "run precedent.search", "messages": [{"role": "user", "content": ""}], "case_id": case_id},
                         c))
    assert refused["error"]["code"] == "stage_gate"
    again = _valid(run({"system": "continue", "messages": [{"role": "user", "content": ""}], "case_id": case_id}, c))
    assert again["flow"] == "need.guided_form", again
    assert again["questions"][0]["id"] == "material_code"
    assert again["result"]["answers"]["codified"] == "Codified"


def test_a_change_of_category_keeps_the_shared_answers(tmp_path):
    c = ctx(tmp_path)
    case_id = _opened(c)
    _walk(c, case_id, {k: GOODS[k] for k in ("codified", "material_code", "description")})
    body = _form(c, case_id, question_id="category", value="services")
    assert body["result"]["form"] == "Services"
    assert body["result"]["answers"] == {"description": "gate valve 2 inch"}
    assert body["questions"][0]["id"] == "service_type"
    assert need.load(c.conn, case_id).subtype["value"] is None    # Codified is not a service


def test_every_answer_is_in_the_history(tmp_path):
    c = ctx(tmp_path)
    case_id = _opened(c)
    _walk(c, case_id, GOODS)
    reasons = [e["reason"] for e in harness.history(c, identity.Caller(
        tenant="nrl", user="u1", roles=("U1",)), case_id)["entries"]]
    # an answer that goes onto the Need Record is saved under its field; every
    # answer is a new version of the form artifact
    assert [r for r in reasons if r.startswith("guided form: ")] == [
        f"guided form: {k}" for k in ("codified", "description", "quantity", "uom", "location")]
    assert sum("guided_form from need.guided_form" in r for r in reasons) == len(GOODS) + 1


def test_a_new_case_needs_some_text_and_the_officer(tmp_path):
    c = ctx(tmp_path)
    assert _form(c)["error"]["code"] == "bad_request"
    store.ensure(c.conn)
    assert c.conn.execute("SELECT COUNT(*) FROM nam_cases").fetchone()[0] == 0
    drafter = identity.Caller(tenant="nrl", user="d1", roles=("U4",))
    assert _form(c, text="supply of valves", caller=drafter)["error"]["code"] == "forbidden_role"


def test_a_form_outside_its_schema_is_refused(tmp_path, monkeypatch):
    table = playbooks.registry()

    def sneaky(request, context):
        made = need_guided_form.handle(request, context)
        return dataclasses.replace(made, artifacts={"guided_form": {"complete": "yes"}})

    table["need.guided_form"] = dataclasses.replace(table["need.guided_form"], handler=sneaky)
    monkeypatch.setattr("nam_agent.nam.playbooks.registry", lambda: table)
    c = ctx(tmp_path)
    body = _form(c, text="supply of gate valves")
    assert body["error"]["code"] == "guardrail", body


def test_the_forms_are_well_formed():
    policy = need_guided_form.policy()
    assert set(policy["forms"]) == {"Goods", "Services", "New Works", "Repetitive Works"}
    assert policy["status"].startswith("DRAFT")
    for name, keys in policy["forms"].items():
        assert len(keys) == len(set(keys)), name
        for i, key in enumerate(keys):
            field = policy["fields"][key]
            assert field["type"] in ("text", "number", "date", "radio"), key
            assert (field["type"] == "radio") == bool(field.get("options")), key
            for cond in ("mandatory_if", "na_if"):
                if cond in field:                      # depends on an earlier field
                    assert field[cond]["field"] in keys[:i], (name, key)
            if field.get("subtype"):
                category = {"Goods": need.GOODS, "Services": need.SERVICES}[name]
                assert set(field["options"]) <= set(need.SUBTYPES[category])
    # need.classify's route names exactly these forms
    for cat, sub in (("goods", None), ("services", None), ("works", "New"),
                     ("works", "Repetitive")):
        assert need_classify._guided_form(cat, sub) in policy["forms"]

