"""DETERMINISTIC EVAL — the chat stops at the matching tender (nam_agent/nam/scope.py).

The officers' ask: "first give a final tender which matches the requirement;
remove the questions it asks, what type of tender and codified or non
codified, we are not up to that step right now". With NAM_STOP_AT=tender
(the default) the chat asks only what tells the past tenders apart, then
shows what is left; no tender type, sub-type, detail, quantity, estimate or
PR question follows. NAM_STOP_AT=pr gives the whole flow back.
"""

from __future__ import annotations

import re
import sqlite3

import pytest
from nam_fakes import fake_kg

from nam_agent.nam import drive, fill, scope, understand
from nam_agent.nam import need as nam

LATER = ("tender type", "sub-type", "type of procurement", "codified", "how many",
         "which unit", "by when", "estimate", "purchase request", "quantity")


@pytest.fixture
def tender_scope(monkeypatch):
    monkeypatch.setenv("NAM_STOP_AT", "tender")


def _walk(tmp_path):
    conn, fetch, awaiting = sqlite3.connect(":memory:"), fake_kg(), {}
    out = drive.turn(conn, tmp_path, "http://kg", "chat-1", "i need gaskets", fetch=fetch,
                     awaiting=awaiting)
    replies = [out]
    for _ in range(12):                     # pick the first option until nothing is asked
        if not out or not out.get("ask"):
            break
        out = drive.turn(conn, tmp_path, "http://kg", "chat-1", out["ask"]["options"][0],
                         fetch=fetch, awaiting=awaiting)
        replies.append(out)
    return conn, replies


@pytest.mark.usefixtures("tender_scope")
def test_the_chat_ends_at_the_matching_tender_without_later_questions(tmp_path):
    conn, replies = _walk(tmp_path)
    last = replies[-1]
    assert last is not None and last["ask"] is None
    asked = " ".join((r.get("ask") or {}).get("question", "") for r in replies if r).lower()
    assert not any(word in asked for word in LATER), asked
    assert "Final tender: PUR-" in last["reply"] or "past tenders match what you gave" in \
        last["reply"]
    assert scope.END in last["reply"] and "Need NAM-" not in last["reply"]
    case = nam.load(conn, drive.session_case(conn, "chat-1"))
    assert fill.needs(case) == []                 # "ok" / "next" leads nowhere further
    nxt = drive.turn(conn, tmp_path, "http://kg", "chat-1", "ok", fetch=fake_kg(), awaiting={})
    assert nxt is None or not nxt.get("ask")
    # a request about the tender found is the model's (kg_tender_record), not
    # the flow's "next": it once got "NAM stops here" back
    key = case.related_cases[0] if case.related_cases else "PUR-X-1"
    def model(system, user):            # the intent model reads it as "go on"
        return understand.scripted(intent="next")

    ask = drive.turn(conn, tmp_path, "http://kg", "chat-1", f"get the PR of {key}",
                     fetch=fake_kg(), awaiting={}, intent_ask=model)
    assert ask is None


def test_the_whole_flow_comes_back_with_pr(tmp_path, monkeypatch):
    monkeypatch.setenv("NAM_STOP_AT", "pr")
    _, replies = _walk(tmp_path)
    asked = [(r.get("ask") or {}).get("question", "") for r in replies if r]
    assert any("tender type" in q.lower() or "category" in q.lower()
               or "type of procurement" in q.lower() for q in asked), asked


def test_the_scope_reads_its_setting(monkeypatch):
    monkeypatch.delenv("NAM_STOP_AT", raising=False)
    assert scope.stop_at() == "tender"
    monkeypatch.setenv("NAM_STOP_AT", "PR")
    assert scope.stop_at() == "pr"
    monkeypatch.setenv("NAM_STOP_AT", "nonsense")
    assert scope.tender_only()


def _t(key, subject, dept, items=()):
    return {"case_key": key, "subject": subject, "items_sample": list(items), "department": dept,
            "bid_opening_date": "2024-05-01", "tier": "direct",
            "counts": {"boq_lines": 5, "pqc_clauses": 2, "scc_clauses": 1, "spec_clauses": 1}}


def test_the_tenders_own_question_follows_a_model_question(monkeypatch):
    # live, "procure gasket" -> spiral wound -> 300# left a gasket rate contract
    # and a bio-diesel project: the model's next question did not hold up, and
    # asking stopped at two because the model had asked before. Now the
    # tenders' own question (here the department) takes them to one.
    from nam_agent.nam import capture

    two = [_t("PUR-MECHMNT-1", "Annual rate contract for Gaskets", "Maintenance Mechanical",
              ["Spiral wound gasket 300#"]),
           _t("PUR-SMT-2", "Provision for Bio-diesel facility", "SMT Operation",
              ["Spiral wound gasket 300#"])]
    need = nam.NeedRecord(need_id="NAM-1", raw_text="gasket", created_at="2026-11-03")

    def rejected(system, user):         # every reply fails the check
        return "not json"

    answers = {"q1": "spiral wound", "q2": "300#"}
    monkeypatch.setenv("NAM_STOP_AT", "tender")
    q, why, _ = capture._next_question(need, "gasket", two, answers, [], rejected)
    assert q is not None and why == "", why
    assert {o["label"].split(" (")[0] for o in q["options"]} >= {"Maintenance Mechanical",
                                                                   "SMT Operation"}
    monkeypatch.setenv("NAM_STOP_AT", "pr")
    q, why, _ = capture._next_question(need, "gasket", two, answers, [], rejected)
    assert q is None


# -- several items, "which matches best", ids typed with other dashes ------------------

def _walk_text(tmp_path, text, monkeypatch):
    monkeypatch.setenv("NAM_STOP_AT", "tender")
    conn, fetch, awaiting = sqlite3.connect(":memory:"), fake_kg(), {}
    out = drive.turn(conn, tmp_path, "http://kg", "chat-1", text, fetch=fetch, awaiting=awaiting)
    replies = [out]
    for _ in range(12):
        if not out or not out.get("ask"):
            break
        out = drive.turn(conn, tmp_path, "http://kg", "chat-1", out["ask"]["options"][0],
                         fetch=fetch, awaiting=awaiting)
        replies.append(out)
    return conn, replies


def test_several_items_end_with_each_items_own_result(tmp_path, monkeypatch):
    # "thermowell spares; 4 match" read "found 2; 4 match": the counts of the
    # item asked about and every item's tenders were mixed
    _, replies = _walk_text(tmp_path, "i need gaskets, rubber sheets", monkeypatch)
    last = replies[-1]["reply"]
    assert "Your request has 2 items" in last
    gaskets = next(ln for ln in last.splitlines() if ln.startswith("- gaskets:"))
    assert "final tender PUR-" in gaskets or "tenders left" in gaskets
    assert "- rubber sheets: no past tender matched it" in last
    for r in replies:
        m = re.search(r"I found (\d+) .*?; (\d+) match", r["reply"])
        assert not m or int(m.group(2)) <= int(m.group(1)), r["reply"]


def test_which_tender_matches_best_is_answered_from_the_case(tmp_path, monkeypatch):
    # the model answered with a "Match Count" column no tool gave
    conn, _ = _walk_text(tmp_path, "i need gaskets", monkeypatch)
    out = drive.turn(conn, tmp_path, "http://kg", "chat-1", "which tenders matchees the most",
                     fetch=fake_kg(), awaiting={})
    assert out is not None and out["tool_calls"][0]["tool"] == "nam_best_match"
    case = nam.load(conn, drive.session_case(conn, "chat-1"))
    assert all(k in out["reply"] for k in case.related_cases)
    assert "every answer you gave" in out["reply"] and "Match Count" not in out["reply"]
    for other in ("what did we pay for gaskets", "show me PUR-1", "which gaskets did we buy"):
        assert not drive._BEST_MATCH.search(other), other


def test_ids_typed_with_another_dash_are_the_same_id(monkeypatch):
    # the model wrote "PUR‑INSTMNT‑1002364" with U+2011; the KG was asked for
    # that string and the agent said the tender does not exist
    from nam_agent.nam import answer_data, guard, kg_tools

    odd = "PUR\u2011INSTMNT\u20111002364"
    assert guard.plain_ids(f"get the PR of {odd}, range 0\u2013400") == \
        "get the PR of PUR-INSTMNT-1002364, range 0\u2013400"       # labels keep their dash
    urls = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"error": "none"}'

    def urlopen(req, timeout):
        urls.append((req.data or b"").decode())
        return Reply()

    monkeypatch.setattr(kg_tools.urllib.request, "urlopen", urlopen)
    kg_tools.make_tender_record_tool("http://kg/mcp").fn(tender=odd)
    sent = "".join(urls)
    assert "PUR-INSTMNT-1002364" in sent and "\u2011" not in sent    # every KG call: plain ids
    assert answer_data.complete(f"Here is {odd}.", []) == "Here is PUR-INSTMNT-1002364."


def test_the_kg_item_check_decides_which_tenders_bought_the_item(tmp_path):
    # "temperature gauge": the tender search marked no exact line, and the
    # scan fell back to all 18 tenders naming any "gauge" (wire-gauge shutters,
    # painting works); the KG's item check knows the 10 that bought one
    from nam_agent.nam import capture

    rows = [{"case_key": "PUR-INSTMNT-1", "item": "TG BiMet 0-400 DegC", "tier": "exact"},
            {"case_key": "PUR-INSTMNT-1", "item": "Temperature gauge 0-200", "tier": "exact"},
            {"case_key": "PUR-PRJ-9", "item": "Temperature Gauge, Thermocouple", "tier": "exact"}]
    checked = {"answer": "yes", "exact": rows, "similar": [],
               "tenders": {"PUR-PRJ-9": {"tender_no": "T9", "job_name": "Supply of instruments"}}}
    lexical = [{"case_key": "PUR-CIVIL-2", "job_name": "Wire gauge shutters"},
               {"case_key": "PUR-INSTMNT-1", "job_name": "ARC for gauges", "tender_no": "T1"}]
    out = capture.item_tenders(checked, lexical)
    assert [r["case_key"] for r in out] == ["PUR-INSTMNT-1", "PUR-PRJ-9"]
    assert out[0]["exact_items"] == ["TG BiMet 0-400 DegC", "Temperature gauge 0-200"]
    assert out[1]["job_name"] == "Supply of instruments"
    assert capture.item_tenders({"answer": "no"}, lexical) == []
    assert capture.item_tenders({}, lexical) == []

    calls = []

    def fetch(url, path, params):
        calls.append((path, params.get("query")))
        if path == "/api/kg/tenders":
            return {"count": 2, "terms": ["gauge"], "tenders": [
                {**t, "matched_by_name": True, "matching_item_count": 1,
                 "matching_items": [t["job_name"]]} for t in lexical]}
        if path == "/api/kg/items":
            return checked
        if path == "/api/kg/case_summary":
            return {"cases": []}
        return {}

    need = nam.NeedRecord(need_id="NAM-1", raw_text="temperature gauge", created_at="x")
    result = capture.scan(need, "http://kg", fetch, "temperature gauge")
    assert result["exact"] is True
    assert [r["case_key"] for r in result["related"]] == ["PUR-INSTMNT-1", "PUR-PRJ-9"]
    assert ("/api/kg/tenders", "gauge") not in calls          # no fall back to the head word


def test_several_items_name_the_tender_that_carried_them_all():
    items = [{"text": "temperature gauge", "found": ["PUR-1", "PUR-2"], "related": ["PUR-2"]},
             {"text": "thermowell spares", "found": ["PUR-1", "PUR-3"], "related": ["PUR-3"]}]
    assert drive.all_items(items) == ["PUR-1"]
    out = {"related": [{"case_key": "PUR-2", "subject": "Gauges"},
                       {"case_key": "PUR-3", "subject": "Spares"}]}
    text = drive._per_item(out, items, "")
    assert "Bought together before, in one tender: PUR-1." in text
    assert drive.all_items(items[:1]) == [] and drive.all_items(
        [items[0], {**items[1], "found": []}]) == []
