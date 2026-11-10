"""DETERMINISTIC EVAL — need capture follows the PR Creation Flow's Stage 1.

  1(a)  the category picked before typing is the officer's
  1(c)  a request naming several items is completed one item at a time
  1(e)  after the questions that tell the tenders apart, the details every
        need carries (the guided form's "capture" fields) are asked
  1(f)  an answer that does not fit is asked again with the reason
  1(g)  "I don't know" takes the answer most related tenders carry, or the
        NRL standard, and marks it a default
  1(i)  the related tenders are ranked: an order placed first, latest first
        undo  takes an answer back and asks the same question again
  2(b)  the escalation compounds a year at a time

and the chat: "Hii" on the entry screen is the NAM harness's to answer, the driver's
undo and "I don't know" words, and the classification asked as the
harness's own question with options. Nothing here is a scripted answer: the
fake KG's tenders decide every question.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from nam_fakes import fake_kg

from nam_agent.nam import capture, drive, followup, guardrails
from nam_agent.nam import need as nam


def _cap(conn, tmp_path, text="", **kw):
    return capture.capture(conn, tmp_path, "http://kg", text, fetch=fake_kg(), **kw)


def _answer(conn, tmp_path, out, value):
    q = out["followups"]["pending"]
    return _cap(conn, tmp_path, need_id=out["need_id"],
                answer={"question_id": q["id"], "value": value})


# -- several items (1(c)) ----------------------------------------------------------------

def test_a_list_of_items_is_split_and_one_item_is_not():
    assert capture.split_items("Supply of ring joint gaskets, spiral wound gaskets and "
                               "rubber sheets") == ["ring joint gaskets",
                                                    "spiral wound gaskets", "rubber sheets"]
    assert capture.split_items("pressure gauge, 0-100 bar") == []      # a range, not an item
    assert capture.split_items("Supply, installation and commissioning of pumps") == []
    assert capture.split_items("i need to procure gaskets") == []


def test_the_model_splits_only_into_the_requests_own_words():
    text = "valves and gaskets"
    made_up = lambda system, user: '{"items": ["valves", "pumps"]}'   # noqa: E731
    assert capture.split_items(text, made_up) == ["valves", "gaskets"]   # the rule decides
    calls = []

    def model(system, user):
        calls.append(user)
        return '{"items": ["valves", "gaskets"]}'
    assert capture.split_items(text, model) == ["valves", "gaskets"]
    assert capture.split_items("i need gaskets", model) == [] and len(calls) == 1  # no list


def test_each_item_is_completed_in_turn(tmp_path):
    conn = sqlite3.connect(":memory:")
    out = _cap(conn, tmp_path, "gaskets, rubber sheets")
    f = out["followups"]
    assert [i["text"] for i in f["items"]] == ["gaskets", "rubber sheets"]
    assert f["item"] == 0 and f["pending"]["id"] == "variant"
    out = _answer(conn, tmp_path, out, "ring joint")
    f = out["followups"]
    assert f["items"][0]["status"] == "done" and f["item"] == 1
    # the first item's tenders stay with the need
    assert {"PUR-4", "PUR-5"} <= set(nam.load(conn, out["need_id"]).related_cases)


# -- undo, "I don't know", answers that do not fit ----------------------------------------

def test_undo_asks_the_same_question_again_and_goes_on_from_it(tmp_path):
    conn = sqlite3.connect(":memory:")
    first = _cap(conn, tmp_path, "i need to procure gaskets")
    asked = first["followups"]["pending"]
    text_before = nam.load(conn, first["need_id"]).raw_text
    out = _answer(conn, tmp_path, first, "ring joint")
    assert out["followups"]["answered"][0]["answer"] == "ring joint"
    back = _cap(conn, tmp_path, need_id=first["need_id"], undo=asked["id"])
    f = back["followups"]
    assert f["undone"] == asked["id"] and f["answered"] == []
    assert f["pending"]["id"] == asked["id"] and f["pending"]["options"] == asked["options"]
    assert nam.load(conn, first["need_id"]).raw_text == text_before
    assert f["narrowed_to"] == f["total_related"]               # the answer no longer narrows
    again = _answer(conn, tmp_path, back, "spiral wound")
    assert again["followups"]["answered"][0]["answer"] == "spiral wound"
    assert set(nam.load(conn, first["need_id"]).related_cases) == {"PUR-1", "PUR-2", "PUR-3"}


def test_i_dont_know_takes_what_most_tenders_carry_and_marks_it(tmp_path):
    conn = sqlite3.connect(":memory:")
    first = _cap(conn, tmp_path, "i need to procure gaskets")
    options = first["followups"]["pending"]["options"]
    assert options[-1] == {"label": followup.UNKNOWN_LABEL, "value": followup.UNKNOWN,
                           "count": 0}
    most = max(options[:-1], key=lambda o: o["count"])
    out = _answer(conn, tmp_path, first, followup.UNKNOWN)
    done = out["followups"]["answered"][0]
    assert done["answer"] == most["value"] and done["default"]["value"] == most["label"]
    assert "not known" in done["default"]["basis"]


@pytest.mark.capture_details
def test_the_details_are_asked_after_the_tender_questions(tmp_path):
    conn = sqlite3.connect(":memory:")
    out = _answer(conn, tmp_path, _cap(conn, tmp_path, "i need to procure gaskets"),
                  "ring joint")
    asked = []
    while out["followups"]["pending"]:
        q = out["followups"]["pending"]
        asked.append(q["id"])
        assert q["narrows"] is False and q["options"][-1]["value"] == followup.UNKNOWN
        # choices to pick, not examples in the words; "I don't know" one of them
        assert len(q["options"]) >= 2 and "(" not in q["text"].split("?")[0][-25:]
        assert "{item}" not in q["text"]
        if q["per_item"]:
            assert "gaskets" in q["text"]                       # it names the item
        if q.get("type") == "date":
            wrong = _cap(conn, tmp_path, need_id=out["need_id"],
                         answer={"question_id": q["id"], "value": "next month"})
            assert "2027-04-12" in wrong["followups"]["ignored"]
            assert wrong["followups"]["pending"]["id"] == q["id"]     # asked again
            value = "31/01/2027"                                  # as an officer types it
        elif q["id"] == "d:quantity":
            value = "20"
        elif q["id"] == "d:location":
            value = followup.UNKNOWN
        else:
            value = "Nos" if q["id"] == "d:uom" else "maintenance spares"
        out = _cap(conn, tmp_path, need_id=out["need_id"],
                   answer={"question_id": q["id"], "value": value})
        assert len(asked) < 10
    # the guided form's capture fields for Goods, in its order (policy/guided_forms.json)
    from nam_agent.nam.playbooks import need_guided_form
    want = [f"d:{e if isinstance(e, str) else e['key']}"
            for e in need_guided_form.policy()["capture"]["Goods"]]
    assert asked == want
    need = nam.load(conn, out["need_id"])
    # not known: the department the matching tenders carry, marked default
    assert need.quantity == "20 Nos" and need.location == "Materials"
    assert need.followups["details"]["location"] == "Materials"
    assert "d:location" in need.followups["defaults"]
    assert out["next_step"] == "references"                      # 1(h): nothing left to ask
    assert need.followups["details"]["required_by"] == "2027-01-31"   # stored as ISO


def test_the_category_picked_before_typing_is_the_officers(tmp_path):
    conn = sqlite3.connect(":memory:")
    out = _cap(conn, tmp_path, "i need to procure gaskets", category="services")
    cat = nam.load(conn, out["need_id"]).category
    assert cat["value"] == nam.SERVICES and cat["basis"] == "you set this"


def test_the_related_tenders_are_ranked_ordered_first_then_latest():
    related = [{"case_key": "A", "bid_opening_date": "2025-01-01", "awarded_to": []},
               {"case_key": "B", "bid_opening_date": "2023-01-01", "awarded_to": ["X"]},
               {"case_key": "C", "bid_opening_date": "2024-06-01", "awarded_to": ["Y"]},
               {"case_key": "D", "bid_opening_date": None, "awarded_to": []}]
    assert capture.rank(related) == ["C", "B", "A", "D"]


def test_a_model_answer_shown_as_one_thing_and_applied_as_another_is_dropped():
    related = [{"case_key": f"T{i}", "subject": s, "matching_items": [s]}
               for i, s in enumerate(["temperature gauge 0-200", "temperature gauge 0-200",
                                      "pressure gauge", "pressure gauge"])]
    raw = {"question": "Which range?", "options": [
        {"label": "0-200 C", "match": "pressure gauge"},          # the label is not the match
        {"label": "Temperature", "match": "temperature gauge"},
        {"label": "Pressure", "match": "pressure gauge"}]}
    q = followup.check_model_question(raw, related, 0)["question"]
    assert [o["label"] for o in q["options"]] == ["Temperature", "Pressure"]


def test_the_escalation_compounds_a_year_at_a_time():
    assert guardrails.escalate(Decimal(100), 7, 0) == Decimal("100.00")
    assert guardrails.escalate(Decimal(100), 7, 1) == Decimal("107.00")
    assert guardrails.escalate(Decimal(100), 7, 2) == Decimal("114.49")


# -- the chat ---------------------------------------------------------------------------

def test_words_that_are_not_a_need_go_to_nam_agent_not_to_the_kg():
    for words in ("Hii", "hello", "what did we pay for gaskets?", "thanks", "undo"):
        assert drive.entry_kind(words) == "chat", words
    for words in ("i need gaskets", "Bimetallic temperature gauge", "procure binding wires"):
        assert drive.entry_kind(words) == "need", words


def _turn(conn, tmp_path, message, awaiting):
    return drive.turn(conn, tmp_path, "http://kg", "chat-1", message, fetch=fake_kg(),
                      awaiting=awaiting)


def test_the_chat_undoes_and_the_classification_comes_with_options(tmp_path):
    conn, awaiting = sqlite3.connect(":memory:"), {}
    first = _turn(conn, tmp_path, "i need gaskets", awaiting)
    question = first["ask"]["question"]
    _turn(conn, tmp_path, "ring joint", awaiting)
    back = _turn(conn, tmp_path, "undo", awaiting)
    assert back["ask"]["question"] == question                   # the same question again
    out = _turn(conn, tmp_path, "i don't know", awaiting)
    case_id = out["tool_calls"][0]["args"]["need_id"]
    assert nam.load(conn, case_id).followups["defaults"]          # marked a default
    # the classification: the harness's question, its vocabulary as the options
    while out["ask"] and out["ask"].get("field") in ("procurement_type", "tender_type"):
        field = out["ask"]["field"]
        pick = nam.PROCUREMENT_TYPES[1] if field == "procurement_type" else nam.TENDER_TYPES[0]
        assert pick in out["ask"]["options"] or any(o.startswith(pick)
                                                    for o in out["ask"]["options"])
        out = _turn(conn, tmp_path, pick, awaiting)
        assert getattr(nam.load(conn, case_id), field)["basis"] == "you set this"
    assert not nam.load(conn, case_id).unresolved
    # classified: the case moves on and its next question comes at once
    assert out["ask"]["field"] == "precedent"


# -- speed: a slow meaning lane is not waited for ------------------------------------------

def test_a_slow_meaning_search_does_not_hold_the_officer_up(tmp_path, monkeypatch):
    import threading
    import time

    release = threading.Event()
    base = fake_kg()

    def slow(kg_url, path, params):
        if path == "/api/kg/precedent":
            release.wait(5)
            return {"best_similarity": 0.8, "tenders": []}
        return base(kg_url, path, params)

    monkeypatch.setattr(capture, "SEMANTIC_WAIT_S", 0.2)
    conn = sqlite3.connect(":memory:")
    started = time.monotonic()
    out = capture.capture(conn, tmp_path, "http://kg", "i need to procure gaskets", fetch=slow)
    assert time.monotonic() - started < 3
    assert out["semantic"]["pending"] and out["followups"]["pending"]   # asked at once
    release.set()
    for _ in range(50):
        if all(f.done() for f in capture._LATE.values()):
            break
        time.sleep(0.05)
    nxt = capture.capture(conn, tmp_path, "http://kg", "", need_id=out["need_id"], fetch=slow,
                          answer={"question_id": out["followups"]["pending"]["id"],
                                  "value": "ring joint"})
    assert nxt["semantic"]["on"] and not nxt["semantic"]["pending"]    # merged now


def test_the_models_question_has_a_time_budget(monkeypatch):
    calls = []

    def slow_model(system, user):
        calls.append(1)
        return "not json"

    monkeypatch.setattr(followup, "ASK_BUDGET_S", -1)        # already spent after one try
    out = followup.ask_model_for_question(slow_model, "gaskets", "gaskets", [], [])
    assert "rejected" in out and len(calls) == 1


def test_an_option_the_officer_already_named_is_taken_not_asked(tmp_path):
    conn = sqlite3.connect(":memory:")
    out = _cap(conn, tmp_path, "i need to procure ring joint gaskets")
    f = out["followups"]
    taken = f["answered"][0]
    assert taken["answer"] == "ring joint" and taken["from_your_words"]
    assert f["pending"] is None or f["pending"]["id"] != taken["id"]
    assert set(nam.load(conn, out["need_id"]).related_cases) == {"PUR-4", "PUR-5"}


def test_the_entry_hands_nam_agent_only_what_is_not_the_captures():
    from nam_agent.server.nam_routes import entry_chat_words

    assert entry_chat_words({"text": "Hii"}) == "Hii"
    assert entry_chat_words({"text": "i need gaskets"}) == ""
    ask = {"need_id": "NAM-2026-0001"}
    # an open question: a greeting or a question is the NAM harness's, any answer the capture's
    assert entry_chat_words({**ask, "answer": {"question_id": "q1", "value": "hello"}}) == "hello"
    assert entry_chat_words({**ask, "answer": {"question_id": "q1", "value": "why?"}}) == "why?"
    for value in (followup.UNKNOWN, "ring joint", "", "2027-01-31"):
        assert entry_chat_words({**ask, "answer": {"question_id": "q1", "value": value}}) == ""
    assert entry_chat_words({**ask, "undo": "q1", "text": "Hii"}) == ""


@pytest.mark.capture_details
def test_each_item_is_completed_with_its_own_details_before_the_next(tmp_path):
    conn = sqlite3.connect(":memory:")
    out = _cap(conn, tmp_path, "gaskets, rubber sheets")
    nid = out["need_id"]
    assert out["followups"]["item"] == 0 and out["followups"]["pending"]["id"] == "variant"
    out = _cap(conn, tmp_path, need_id=nid, skip_followups=True)      # this item's questions
    f = out["followups"]
    assert f["item"] == 0 and f["items"][0]["status"] == "current"   # not yet the next item
    assert f["pending"]["id"] == "d:quantity" and f["pending"]["per_item"]
    out = _cap(conn, tmp_path, need_id=nid, answer={"question_id": "d:quantity", "value": "5"})
    assert out["followups"]["pending"]["id"] == "d:uom"
    out = _cap(conn, tmp_path, need_id=nid, answer={"question_id": "d:uom", "value": "Nos"})
    f = out["followups"]
    assert f["items"][0]["status"] == "done" and f["item"] == 1       # now the second item
    assert f["pending"]["id"] == "d:quantity"                          # its own quantity
    details = nam.load(conn, nid).followups["item_details"]
    assert details[0] == {"text": "gaskets", "quantity": "5", "uom": "Nos"}
    out = _cap(conn, tmp_path, need_id=nid, skip_followups=True)      # on a detail: all end
    assert out["followups"]["pending"] is None and out["followups"]["done"]


@pytest.mark.capture_details
def test_detail_options_come_from_the_tenders_data_and_i_dont_know_takes_the_commonest(
        tmp_path):
    import time

    conn = sqlite3.connect(":memory:")
    out = _answer(conn, tmp_path, _cap(conn, tmp_path, "i need to procure gaskets"), "ring joint")
    for _ in range(50):                                   # the price records come in
        if all(f.done() for f in capture._LATE.values()):
            break
        time.sleep(0.05)
    out = _cap(conn, tmp_path, need_id=out["need_id"],      # read again: now with suggestions
               answer={"question_id": "none", "value": ""})
    q = out["followups"]["pending"]
    assert q["id"] == "d:quantity" and not q["closed"]
    assert [o["value"] for o in q["options"]][:1] == ["20"]           # what NRL ordered before
    assert q["options"][-1]["value"] == followup.UNKNOWN
    typed = _cap(conn, tmp_path, need_id=out["need_id"],
                 answer={"question_id": "d:quantity", "value": "35"})   # a value of their own
    assert typed["followups"]["answered"][-1]["answer"] == "35"
    q = typed["followups"]["pending"]
    assert q["id"] == "d:uom" and [o["value"] for o in q["options"]][:1] == ["Nos"]
    out = _cap(conn, tmp_path, need_id=out["need_id"],
               answer={"question_id": "d:uom", "value": followup.UNKNOWN})
    last = out["followups"]["answered"][-1]
    assert last["answer"] == "Nos" and "not known" in last["default"]["basis"]


def test_quantity_and_unit_options_count_tenders_and_line_item_records_apart(monkeypatch):
    # live, 2026-11-03: "m (216 tenders)" when 15 tenders matched: every BOQ
    # line was counted as a tender
    from nam_agent.nam import capture as cap
    from nam_agent.nam import followup

    records = {"PUR-1": {"boq": [{"item": "steel pipe 150 mm", "qty": 10, "uom": "m"}] * 3},
               "PUR-2": {"boq": [{"item": "steel pipe 100 mm", "qty": 5, "uom": "m"},
                                 {"item": "steel pipe bend", "qty": 2, "uom": "Nos"}]}}
    monkeypatch.setattr(cap, "_CASES", {})
    fetch = lambda url, path, params: records[params["case_key"]]   # noqa: E731
    related = [{"case_key": "PUR-1"}, {"case_key": "PUR-2"}]
    opts = cap._data_options("past_quantities.uom", "steel pipes", related, "http://kg", fetch)
    m = next(o for o in opts if o["value"] == "m")
    assert (m["count"], m["lines"]) == (2, 4)
    assert followup.option_label(m) == "m (4 line-item records in 2 tenders)"
    nos = next(o for o in opts if o["value"] == "Nos")
    assert followup.option_label(nos) == "Nos (1 tender)"
    assert drive.answer_to("m (4 line-item records in 2 tenders)",
                           {"options": [{"label": "m", "value": "m"}]}) == ("answer", "m")


def test_one_quantity_written_two_ways_is_one_option(monkeypatch):
    # live, 2026-11-03: "10.0" and "10" were offered as two quantities
    from nam_agent.nam import capture as cap

    records = {"PUR-1": {"boq": [{"item": "cpvc pipe", "qty": "10.0", "uom": "m"}]},
               "PUR-2": {"boq": [{"item": "cpvc pipe", "qty": "10", "uom": "m"}]}}
    monkeypatch.setattr(cap, "_CASES", {})
    fetch = lambda url, path, params: records[params["case_key"]]   # noqa: E731
    opts = cap._data_options("past_quantities.qty", "cpvc pipes",
                             [{"case_key": "PUR-1"}, {"case_key": "PUR-2"}], "http://kg", fetch)
    assert [(o["value"], o["count"]) for o in opts] == [("10", 2)]


def test_a_yes_no_question_never_offers_no_twice():
    # live, 2026-11-03: "Do you also need thermocouples and thermowells?" offered
    # "No (7 tenders)" and "No (6 tenders)": the model's one option was "No"
    related = [{"case_key": f"T{i}", "subject": s, "matching_items": [s]}
               for i, s in enumerate(["gauge with thermocouple"] * 6 + ["pressure gauge"] * 7)]
    raw = {"question": "Do you also need thermocouples and thermowells?",
           "options": [{"label": "No", "match": "thermocouple"}]}
    q = followup.check_model_question(raw, related, 0)["question"]
    labels = [o["label"] for o in q["options"]]
    assert len(set(labels)) == len(labels) and labels[1] == "No"
    assert labels[0].startswith("Yes")


def test_an_either_or_question_gets_a_named_other_answer():
    related = [{"case_key": f"T{i}", "subject": s, "matching_items": [s]}
               for i, s in enumerate(["exposed cpvc pipe"] * 2 + ["concealed cpvc pipe"] * 3)]
    raw = {"question": "Do you need the pipe exposed or concealed?",
           "options": [{"label": "Exposed", "match": "exposed"}]}
    q = followup.check_model_question(raw, related, 0)["question"]
    assert [o["label"] for o in q["options"]] == ["Exposed", "Not exposed"]


def test_a_qualified_item_keeps_only_the_tenders_with_that_exact_item():
    # live, 2026-11-03: "CPVC pipes" searched "pipes" alone: 59 related tenders,
    # and the case ended on civil-works and electrical tenders; the KG had 15
    # tenders whose line items are CPVC pipes (exact_items)
    lexical = [
        {"case_key": "A", "job_name": "Annual rate contract for civil works",
         "matching_item_count": 40, "matched_by_name": False,
         "exact_items": ["Providing CPVC pipes 20 mm", "CPVC pipes 25 mm"]},
        {"case_key": "B", "job_name": "Procurement of Seamless, ERW and EFSW Pipes",
         "matching_item_count": 47, "matched_by_name": True, "exact_items": []},
        {"case_key": "C", "job_name": "Supply of CPVC pipes", "matching_item_count": 3,
         "matched_by_name": True, "exact_items": ["CPVC pipe 100 mm"]}]
    semantic = [{"pur_case_id": "D", "job_name": "PVC hose", "matched_items": ["PVC hose 2in"]},
                {"pur_case_id": "E", "job_name": "Plumbing", "matched_items": ["cpvc pipe 50"]}]
    related, incidental = capture.classify_tenders(lexical, semantic, ["cpvc", "pipes"],
                                                   exact=True)
    assert {r["case_key"] for r in related} == {"A", "C", "E"}
    assert "B" in {r["case_key"] for r in incidental}
    a = next(r for r in related if r["case_key"] == "A")
    assert a["items_sample"][0].startswith("Providing CPVC pipes")   # the exact lines, read
    # no exact match: the tiers are as before
    related, _ = capture.classify_tenders(lexical, [], ["cpvc", "pipes"])
    assert "B" in {r["case_key"] for r in related}


def test_a_goods_need_is_narrowed_to_supply_tenders_not_service_contracts():
    # live, 2026-11-03: "temperature gauge", Goods, ended on three instrument
    # maintenance contracts whose items say "Installation of ... gauge"
    entries = [{"case_key": "S", "subject": "Supply of Temperature Gauge, Thermocouple spares"},
               {"case_key": "M", "subject": "Instrument Maintenance Assistance Contract"},
               {"case_key": "R", "subject": "Rate contract for Instrumentation Miscellaneous Jobs"},
               {"case_key": "U", "subject": "Annual rate contract for Gaskets"}]
    kept = [e["case_key"] for e in capture.fit_category(entries, nam.GOODS)]
    assert kept == ["S", "U"]                                      # "U" says no kind: kept
    assert [e["case_key"] for e in capture.fit_category(entries, nam.SERVICES)] == ["M", "R", "U"]
    assert capture.fit_category(entries[1:3], nam.GOODS) == entries[1:3]   # none fits: all


def test_done_is_not_taken_while_several_tenders_still_differ():
    # live, 2026-11-03: "all remaining tenders include a temperature gauge"
    # stopped the questions with 8 different tenders in view
    related = [{"case_key": f"T{i}", "subject": "gauge", "matching_items": ["gauge"]}
               for i in range(8)]
    out = followup.check_model_question({"done": True, "reason": "all are gauges"}, related, 1)
    assert "rejected" in out
    assert "done" in followup.check_model_question({"done": True}, related[:2], 1)


def test_a_capture_with_exact_item_tenders_passes_its_response_schema():
    # live, 2026-11-03: need.capture response failed its schema:
    # "$.related[4].tier: must be one of ['direct', 'line items']"
    import json
    from pathlib import Path

    schema = json.loads((Path(capture.__file__).parent / "schemas" /
                         "flow.need.capture.json").read_text())
    text = json.dumps(schema)
    assert '"exact items"' in text and all(t in text for t in capture._TIERS)
