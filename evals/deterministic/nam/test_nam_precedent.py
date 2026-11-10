"""precedent.search (FRS F6) and precedent.select (FRS F7), plan Step 15.4:
NAM ranks the past tenders like the need; the officer picks the precedent.

A search selects nothing and records every tender it showed; only a tender
shown and named by the officer can be selected; an ambiguous or unknown
reference selects nothing; a new selection past S2 is rework."""

import dataclasses
import urllib.error

import pytest
from nam_fakes import ask, classified, ctx, fake_kg, run

from nam_agent.nam import audit, guardrails, need, orchestrator, schema

VARIANT = {"question_id": "variant", "value": "ring joint"}


def _valid(body):
    name = "error" if "error" in body else "response"
    assert schema.validate(body, schema.load(name)) == [], body
    if "error" not in body and body["flow"].startswith("precedent."):
        assert schema.validate(body["result"], schema.load(f"flow.{body['flow']}")) == [], body
    return body


def _at_s2(c):
    case_id = run(ask("i need to procure gaskets"), c)["case_id"]
    run(ask("", case_id=case_id, answer=VARIANT), c)
    assert classified(c, case_id)["stage"] == "S2"
    return case_id


def _call(c, case_id, system, text="", **extra):
    return _valid(run({"system": system, "messages": [{"role": "user", "content": text}],
                       "case_id": case_id, **extra}, c))


def _search(c, case_id, text=""):
    return _call(c, case_id, "Find similar tenders", text)


def _select(c, case_id, text="", value=None):
    extra = {"answer": {"question_id": "precedent", "value": value}} if value is not None else {}
    return _call(c, case_id, "Select the precedent", text, **extra)


def _shortlist(c, case_id):
    return orchestrator._artifact(orchestrator.workflow(need.load(c.conn, case_id)), "shortlist")


# -- search -------------------------------------------------------------------------

def test_a_search_ranks_with_reasons_and_selects_nothing(tmp_path):
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    body = _search(c, case_id)
    assert body["flow"] == "precedent.search" and body["status"] == "needs_input"
    cards = body["result"]["candidates"]
    assert [x["case_key"] for x in cards][:2] == ["PUR-4", "PUR-5"]       # ring joint first
    assert [x["rank"] for x in cards] == list(range(1, len(cards) + 1))
    assert all(x["match_basis"] and 0 <= x["match_strength"] <= 100 for x in cards)
    strengths = [x["match_strength"] for x in cards]
    assert strengths == sorted(strengths, reverse=True)
    question = body["questions"][0]
    assert question["id"] == "precedent" and question["options"][-1]["value"] == "none"
    assert need.load(c.conn, case_id).selected_cases == []
    assert body["stage"] == "S2" and body["next"] == ["precedent.select", "need.guided_form"]
    assert {x["ref"] for x in body["citations"]} == {f"tender:{x['case_key']}" for x in cards}


def test_every_search_is_an_audited_tool_call(tmp_path):
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    body = _search(c, case_id)
    calls = audit.events(c.conn, run_id=body["run_id"], kind="tool")
    assert [e["name"] for e in calls] == ["tenders"]


def test_try_another_adds_the_officers_words(tmp_path):
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    first = _search(c, case_id)["result"]["query"]
    body = _call(c, case_id, "Try another search", "try another with SS316 windings")
    assert body["flow"] == "precedent.search"
    assert body["result"]["added_terms"] == "SS316 windings"
    assert body["result"]["query"].startswith(first) and "SS316" in body["result"]["query"]
    assert _shortlist(c, case_id)["searches"] == 2


def test_a_named_identifier_is_put_on_the_list(tmp_path):
    def fetch(kg_url, path, params):
        if path == "/api/kg/tenders":
            return {"count": 0, "terms": [], "tenders": []}
        return base(kg_url, path, params)
    base = fake_kg()
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    c = dataclasses.replace(c, fetch=fetch)
    body = _search(c, case_id, "look at T-PUR-6 too")
    assert body["result"]["candidates"][0]["case_key"] == "PUR-6"
    assert "PUR-6" in _shortlist(c, case_id)["shown"]


def test_a_search_with_nothing_found_offers_the_guided_form(tmp_path):
    def fetch(kg_url, path, params):
        if path == "/api/kg/tenders":
            return {"count": 0, "terms": [], "tenders": []}
        if path == "/api/kg/precedent":
            return {"best_similarity": None, "tenders": []}
        return base(kg_url, path, params)
    base = fake_kg()
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    c = dataclasses.replace(c, fetch=fetch)
    body = _search(c, case_id)
    assert body["result"]["candidates"] == [] and body["result"]["mode"] == "similar"
    assert body["questions"][0]["id"] == "terms" and body["next"] == ["need.guided_form"]
    calls = [e["name"] for e in audit.events(c.conn, run_id=body["run_id"], kind="tool")]
    assert calls == ["tenders", "tenders"]                 # lexical, then by meaning


def test_a_weak_match_says_so(tmp_path):
    def fetch(kg_url, path, params):
        if path == "/api/kg/tenders":
            return {"count": 0, "terms": [], "tenders": []}
        if path == "/api/kg/precedent":
            return {"best_similarity": 0.41, "precedent_gap": "no tender bought this item",
                    "tenders": [{"pur_case_id": "PUR-6", "tender_no": "T-PUR-6",
                                 "job_name": "Supply of Rubber Gaskets",
                                 "matched_items": ["rubber gasket sheet"]}]}
        return base(kg_url, path, params)
    base = fake_kg()
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    c = dataclasses.replace(c, fetch=fetch)
    body = _search(c, case_id)
    assert body["result"]["best_similarity"] == 0.41
    assert [x["case_key"] for x in body["result"]["candidates"]] == ["PUR-6"]
    assert any("weak match" in w for w in body["warnings"])


def test_the_kg_down_is_not_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setattr("nam_agent.nam.tools._sleep", lambda s: None)
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    c = dataclasses.replace(c, fetch=fake_kg(down=True))
    assert _search(c, case_id)["error"]["code"] == "kg_unavailable"


# -- select -------------------------------------------------------------------------

def test_the_officer_selects_from_the_list_and_sees_what_it_carries(tmp_path):
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    _search(c, case_id)
    body = _select(c, case_id, value="PUR-4, PUR-1")
    assert body["status"] == "ok" and body["stage"] == "S3", body
    assert need.load(c.conn, case_id).selected_cases == ["PUR-4", "PUR-1"]
    assert [x["case_key"] for x in body["result"]["selected"]] == ["PUR-4", "PUR-1"]
    assert body["result"]["selected"][0]["counts"] == {"boq": 1, "pqc": 1, "scc": 2,
                                                       "tech_spec": 1, "sor_references": 0}
    assert body["next"] == ["quantity.finalise"]
    calls = [e["name"] for e in audit.events(c.conn, run_id=body["run_id"], kind="tool")]
    assert calls == ["tender_record", "tender_record"]


def test_a_related_tender_from_capture_counts_as_shown(tmp_path):
    c = ctx(tmp_path)
    case_id = _at_s2(c)                                  # capture related PUR-4, PUR-5
    body = _select(c, case_id, "use PUR-5 as the precedent")
    assert body["status"] == "ok" and need.load(c.conn, case_id).selected_cases == ["PUR-5"]


def test_a_tender_not_shown_is_refused_and_nothing_selected(tmp_path):
    c = ctx(tmp_path)
    case_id = _at_s2(c)                                  # PUR-6 was never shown
    body = _select(c, case_id, "go with PUR-6")
    assert body["status"] == "needs_input" and body["result"]["refused"][0]["ref"] == "PUR-6"
    assert "not among the tenders shown" in body["result"]["refused"][0]["why"]
    assert need.load(c.conn, case_id).selected_cases == []
    _search(c, case_id)                                  # now it has been shown
    assert _select(c, case_id, "go with PUR-6")["status"] == "ok"


@pytest.mark.parametrize(("text", "says"), [
    ("use OC01000435", "matches PUR-4, PUR-5"), ("use PUR-99", "no past tender matches"),
])
def test_an_ambiguous_or_unknown_reference_selects_nothing(tmp_path, text, says):
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    _search(c, case_id)
    body = _select(c, case_id, text)
    assert body["status"] == "needs_input" and any(says in w for w in body["warnings"]), body
    assert need.load(c.conn, case_id).selected_cases == []


def test_one_bad_name_holds_the_whole_selection(tmp_path):
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    body = _select(c, case_id, value="PUR-4,PUR-6")
    assert body["status"] == "needs_input" and need.load(c.conn, case_id).selected_cases == []


def test_a_tender_number_resolved_to_one_tender_selects_it(tmp_path):
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    _search(c, case_id)
    body = _select(c, case_id, "go with T-PUR-2")
    assert body["status"] == "ok" and need.load(c.conn, case_id).selected_cases == ["PUR-2"]


def test_naming_nothing_asks_with_the_shown_tenders(tmp_path):
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    body = _select(c, case_id, "pick the best one for me")
    assert body["status"] == "needs_input" and body["questions"][0]["id"] == "precedent"
    assert [o["value"] for o in body["questions"][0]["options"]] == ["PUR-4", "PUR-5", "none"]
    assert need.load(c.conn, case_id).selected_cases == []


def test_none_clears_the_selection_and_offers_the_form(tmp_path):
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    _select(c, case_id, value="PUR-4")
    body = _select(c, case_id, value="none")
    assert body["status"] == "ok" and body["result"]["previous"] == ["PUR-4"]
    assert need.load(c.conn, case_id).selected_cases == []
    assert body["stage"] == "S2" and body["next"] == ["need.guided_form", "precedent.search"]


def test_a_new_selection_past_s2_is_rework(tmp_path):
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    _search(c, case_id)
    _select(c, case_id, value="PUR-4")
    body = _select(c, case_id, value="PUR-5")
    assert body["status"] == "ok" and need.load(c.conn, case_id).selected_cases == ["PUR-5"]
    assert any("rework" in w for w in body["warnings"])
    again = _select(c, case_id, value="PUR-5")              # the same: nothing changes
    assert again["warnings"] == [] and again["case_version"] == body["case_version"]


def test_a_tender_without_a_record_selects_nothing(tmp_path):
    def fetch(kg_url, path, params):
        if path == "/api/kg/case" and params["case_key"] == "PUR-5":
            raise urllib.error.HTTPError(kg_url + path, 404, "not found", {}, None)
        return base(kg_url, path, params)
    base = fake_kg()
    c = ctx(tmp_path)
    case_id = _at_s2(c)
    c = dataclasses.replace(c, fetch=fetch)
    body = _select(c, case_id, value="PUR-4,PUR-5")
    assert body["status"] == "needs_input" and body["result"]["refused"][0]["ref"] == "PUR-5"
    assert need.load(c.conn, case_id).selected_cases == []


def test_search_and_select_need_the_officer(tmp_path):
    from nam_agent.nam import identity

    c = ctx(tmp_path)
    case_id = _at_s2(c)
    drafter = identity.Caller(tenant="nrl", user="d1", roles=("U4",))
    for system in ("Find similar tenders", "Select the precedent"):
        body = run({"system": system, "messages": [{"role": "user", "content": "PUR-4"}],
                    "case_id": case_id}, c, drafter)
        assert body["error"]["code"] == "forbidden_role"


@pytest.mark.parametrize(("said", "named"), [
    ("use pur-1", True), ("use pur-12", False), ("use pur-1x", False), ("t-pur-1", False),
])
def test_a_case_key_is_named_only_as_a_whole_word(said, named):
    assert guardrails._named("PUR-1", said) is named
