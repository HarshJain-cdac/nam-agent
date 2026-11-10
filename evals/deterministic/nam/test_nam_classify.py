"""need.classify (FRS F4, R1, R28, plan Step 15.2): the officer confirms the
category, procurement type and tender type one question at a time, and the
case cannot leave S1 before they are confirmed.

Only a field NAM could not confidently predict is asked about, a tender type
is never settled from words alone, an answer outside the closed vocabulary
changes nothing, and the confirmed category decides the route."""

import pytest
from nam_fakes import CLASSIFIED, ask, classified, ctx, run

from nam_agent.nam import guardrails, identity, need, router, schema, store
from nam_agent.nam.router import CaseState

VARIANT = {"question_id": "variant", "value": "ring joint"}
SYSTEM = "Confirm the classification"


def _valid(body):
    name = "error" if "error" in body else "response"
    assert schema.validate(body, schema.load(name)) == [], body
    if "error" not in body:
        assert schema.validate(body["result"], schema.load("flow.need.classify")) == [], body
    return body


def _captured(c, text="i need to procure gaskets"):
    """A case whose need capture is done: only the classification is left."""
    first = run(ask(text), c)
    done = run(ask("", case_id=first["case_id"], answer=VARIANT), c)
    assert done["status"] == "ok" and done["stage"] == "S1", done
    return first["case_id"]


def _classify(c, case_id, question_id=None, value=None, caller=None):
    extra = {"answer": {"question_id": question_id, "value": value}} if question_id else {}
    body = run(ask("", system=SYSTEM, case_id=case_id, **extra), c, *([caller] if caller else []))
    return _valid(body)


def test_the_case_stays_at_s1_until_the_three_are_confirmed(tmp_path):
    c = ctx(tmp_path)
    case_id = _captured(c)
    body = _classify(c, case_id)
    assert body["flow"] == "need.classify" and body["status"] == "needs_input"
    assert body["stage"] == "S1" and body["result"]["open"] == ["procurement_type",
                                                                "tender_type"]
    body = _classify(c, case_id, "procurement_type", "OPEX")
    assert body["stage"] == "S1" and body["result"]["open"] == ["tender_type"]
    body = _classify(c, case_id, "tender_type", "Open")
    assert body["status"] == "ok" and body["stage"] == "S2" and body["result"]["confirmed"]
    assert body["next"] == ["precedent.search"]


def test_the_s1_gate_names_the_classification(tmp_path):
    c = ctx(tmp_path)
    case_id = _captured(c)
    refused = _valid(run(ask("", system="run precedent.search", case_id=case_id), c))
    assert refused["error"]["code"] == "stage_gate"
    assert any("classification" in d for d in refused["error"]["details"]), refused


def test_only_what_is_not_confident_is_asked_in_order(tmp_path):
    c = ctx(tmp_path)
    case_id = _captured(c)                       # "gaskets": Goods, confidently
    asked = []
    body = _classify(c, case_id)
    while body["questions"]:
        q = body["questions"][0]
        asked.append(q["id"])
        body = _classify(c, case_id, q["id"], q["options"][0]["value"])
    assert asked == ["procurement_type", "tender_type", "subtype"]
    assert need.load(c.conn, case_id).category["basis"] != "you set this"


def test_the_options_are_the_whole_vocabulary(tmp_path):
    c = ctx(tmp_path)
    case_id = _captured(c)
    q = _classify(c, case_id)["questions"][0]
    assert q["id"] == "procurement_type" and q["source"] == "harness"
    assert [o["value"] for o in q["options"]] == list(need.PROCUREMENT_TYPES)


def test_a_suggested_tender_type_is_offered_first_and_still_asked(tmp_path):
    c = ctx(tmp_path)
    case_id = run(ask("open tender for supply of gaskets"), c)["case_id"]
    _classify(c, case_id, "procurement_type", "OPEX")
    body = _classify(c, case_id)
    q = body["questions"][0]
    assert q["id"] == "tender_type" and body["stage"] == "S1"
    assert q["options"][0]["value"] == "Open" and "suggested" in q["options"][0]["label"]
    assert "open tender" in q["why"]
    assert sorted(o["value"] for o in q["options"]) == sorted(need.TENDER_TYPES)


@pytest.mark.parametrize(("question_id", "value"), [
    ("tender_type", "open tender please"), ("procurement_type", "capital"),
    ("category", "stationery"), ("subtype", "New"),        # New is a Works sub-type
])
def test_an_answer_outside_the_vocabulary_changes_nothing(tmp_path, question_id, value):
    c = ctx(tmp_path)
    case_id = _captured(c)
    before = store.version_of(c.conn, case_id)
    body = _classify(c, case_id, question_id, value)
    assert body["error"]["code"] == "bad_request", body
    assert store.version_of(c.conn, case_id) == before


def test_an_answer_to_another_question_is_refused(tmp_path):
    c = ctx(tmp_path)
    case_id = _captured(c)
    body = _classify(c, case_id, "colour", "blue")
    assert body["error"]["code"] == "bad_request" and "colour" in body["error"]["message"]


def test_a_category_id_or_label_is_accepted_and_an_old_subtype_dropped(tmp_path):
    c = ctx(tmp_path)
    case_id = _captured(c)
    _classify(c, case_id, "subtype", "Codified")
    body = _classify(c, case_id, "category", "works")
    record = need.load(c.conn, case_id)
    assert record.category["value"] == need.WORKS and record.category["confident"]
    assert record.subtype["value"] is None                 # Codified is not a Works sub-type
    assert body["result"]["changed"] == "category"
    _classify(c, case_id, "category", need.SERVICES)
    assert need.load(c.conn, case_id).category["value"] == need.SERVICES


def test_skipping_keeps_the_gate_closed_and_asks_the_next(tmp_path):
    c = ctx(tmp_path)
    case_id = _captured(c)
    body = _classify(c, case_id, "procurement_type", "")
    assert body["questions"][0]["id"] == "tender_type" and body["stage"] == "S1"
    assert any("procurement type is still open" in w for w in body["warnings"])
    body = _classify(c, case_id, "tender_type", "Limited")
    assert body["questions"][0]["id"] == "procurement_type"     # the skipped one, again
    body = _classify(c, case_id, "procurement_type", "")      # the optional one meanwhile
    assert body["questions"][0]["id"] == "subtype" and body["status"] == "needs_input"
    assert body["result"]["open"] == ["procurement_type"] and body["stage"] == "S1"
    body = _classify(c, case_id, "subtype", "")
    assert body["questions"][0]["id"] == "procurement_type"     # never left unasked


def test_the_subtype_is_offered_but_not_part_of_the_gate(tmp_path):
    c = ctx(tmp_path)
    case_id = _captured(c)
    body = classified(c, case_id)
    assert body["status"] == "ok" and body["stage"] == "S2"
    assert body["questions"][0]["id"] == "subtype"
    assert [o["value"] for o in body["questions"][0]["options"]] == list(need.SUBTYPES[need.GOODS])
    assert any("optional" in w for w in body["warnings"])
    skipped = _classify(c, case_id, "subtype", "")
    assert skipped["questions"] == [] and skipped["status"] == "ok"


@pytest.mark.parametrize(("category", "subtype", "estimate", "form"), [
    ("goods", "Codified", "estimate.goods", "Goods"),
    ("services", "Consultancy", "estimate.wage", "Services"),
    ("works", "New", "estimate.works", "New Works"),
    ("works", "Repetitive", "estimate.works", "Repetitive Works"),
    ("works", "Capital / Revenue", "estimate.works", None),
])
def test_the_category_decides_the_route(tmp_path, category, subtype, estimate, form):
    c = ctx(tmp_path)
    case_id = _captured(c)
    body = classified(c, case_id, values={**CLASSIFIED, "category": category})
    _classify(c, case_id, "category", category)
    route = _classify(c, case_id, "subtype", subtype)["result"]["route"]
    assert route == {"category": category, "estimate_flow": estimate,
                     "mandatory_documents": guardrails.policy()["mandatory_documents"][category],
                     "guided_form": form}
    assert body["result"]["confirmed"]


def test_the_route_is_empty_until_the_category_is_confirmed(tmp_path):
    c = ctx(tmp_path)
    case_id = run(ask("i need something"), c)["case_id"]
    route = _classify(c, case_id)["result"]["route"]
    assert route == {"category": None, "estimate_flow": None, "mandatory_documents": [],
                     "guided_form": None}
    assert _classify(c, case_id)["questions"][0]["id"] == "category"


def test_every_change_is_recorded_once_with_its_field(tmp_path):
    c = ctx(tmp_path)
    case_id = _captured(c)
    _classify(c, case_id, "procurement_type", "CAPEX")
    version = store.version_of(c.conn, case_id)
    again = _classify(c, case_id, "procurement_type", "CAPEX")      # already so
    assert store.version_of(c.conn, case_id) == version and again["result"]["changed"] is None
    reasons = [v["reason"] for v in store.versions(c.conn, case_id)]
    assert reasons.count("classification: procurement_type set to CAPEX") == 1


def test_more_need_text_keeps_what_the_officer_confirmed(tmp_path):
    c = ctx(tmp_path)
    case_id = _captured(c)
    _classify(c, case_id, "tender_type", "Limited")
    run(ask("for the AMC of the crude unit", case_id=case_id), c)
    assert need.load(c.conn, case_id).tender_type["value"] == "Limited"


def test_after_s1_a_change_is_rework_and_says_so(tmp_path):
    c = ctx(tmp_path)
    case_id = _captured(c)
    classified(c, case_id)
    body = _classify(c, case_id, "category", "works")
    assert body["status"] == "ok" and need.load(c.conn, case_id).category["value"] == need.WORKS
    assert any("changed after S1" in w and "rework" in w for w in body["warnings"])
    assert body["result"]["route"]["estimate_flow"] == "estimate.works"


def test_continue_after_capture_goes_to_classify():
    case = CaseState(stage="S1", last_flow="need.capture", unclassified=True)
    request = {"system": "continue", "messages": []}
    assert router.route(request, case).flow == "need.classify"
    asking = CaseState(stage="S1", last_flow="need.capture", open_question="variant",
                       unclassified=True)
    assert router.route(request, asking).flow == "need.capture"
    assert router.route(request, CaseState(stage="S1")).flow == "need.capture"


def test_capture_and_replicate_point_to_classify(tmp_path):
    c = ctx(tmp_path)
    first = run(ask("i need to procure gaskets"), c)
    done = run(ask("", case_id=first["case_id"], answer=VARIANT), c)
    assert done["next"] == ["need.classify"]
    copied = run({"system": "Create by copy", "messages": [{"role": "user",
                                                            "content": "replicate PUR-4"}]}, c)
    assert copied["next"] == ["need.classify"]


def test_classify_needs_a_case_and_the_officer(tmp_path):
    c = ctx(tmp_path)
    assert _valid(run(ask("", system=SYSTEM), c))["error"]["code"] in ("bad_request",
                                                                      "stage_gate")
    case_id = _captured(c)
    drafter = identity.Caller(tenant="nrl", user="d1", roles=("U4",))
    assert _classify(c, case_id, caller=drafter)["error"]["code"] == "forbidden_role"
