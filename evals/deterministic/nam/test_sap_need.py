"""DETERMINISTIC EVAL — need capture on SAP's lines (NAM_SOURCE=sap).

The officer states a need; the harness reads sap_search, asks questions built
from the materials' own descriptions, and ends at one material with its last
PR, PO and PUR case. The model may propose a question; the harness checks it
against the list and counts every option from the data. The lines below are
trimmed from a real sap_search reply.
"""

from __future__ import annotations

import json
import sqlite3

from evals.helpers import make_agent
from nam_agent.nam import chatstate, sap_need

URL = "http://127.0.0.1:9/mcp"     # nothing listens there


def _line(kind, doc, date, code, rate, pur, pr="10073969", po="4500024964"):
    return {"kind": kind, "doc_no": doc, "date": date, "material": code, "quantity": 2.0,
            "unit": "NOS", "rate_inr": rate, "pur_case_id": pur, "pr_no": pr, "po_no": po}


def _res(code, desc, pur="PUR-INSTMNT-1002364"):
    return {"description": desc, "codes": {code: desc}, "lines": [
        _line("PR", "10073969", "2021-09-28", code, 15000.0, pur),
        _line("PO", "4500024964", "2022-07-13", code, 18835.35, pur)]}


GAUGES = {"status": "ok", "results": [
    _res("I-TG-ARC-G100-01", "TG Bimetallic U320mm 6mm dia 0-100Cel"),
    _res("I-TG-ARC-G200-01", "TG Bimetallic U320mm 6mm dia 0-200Cel"),
    _res("I-TG-ARC-G200-02", "TG Bimetallic U400mm 6mm dia 0-200Cel"),
    _res("I-TE-TC-K0320-06-1", "Thermocouple , K ,U-320mm, 6mm", "PUR-INSTMNT-1000001"),
    _res("I-TW-05TE1102-00", "Thermowell for TIs 05-TE-1102", "PUR-INSTMNT-1000002"),
]}


def _conn():
    return sqlite3.connect(":memory:")


def _search(data):
    sent = []

    def search(url, need):
        sent.append(need)
        return data
    return search, sent


def _asker(*replies):
    seen = []

    def ask(system, user):
        seen.append(user)
        return replies[min(len(seen), len(replies)) - 1]
    return ask, seen


def test_source_is_tender_unless_sap(monkeypatch):
    monkeypatch.delenv("NAM_SOURCE", raising=False)
    assert sap_need.source() == "tender" and sap_need.source("SAP") == "sap"
    assert sap_need.source("other") == "tender"


def test_lines_group_by_material_newest_first():
    mats = sap_need.materials(GAUGES)
    assert [m["code"] for m in mats][:2] == ["I-TG-ARC-G100-01", "I-TG-ARC-G200-01"]
    assert mats[0]["lines"][0]["kind"] == "PO"          # 2022 before 2021
    service = sap_need.materials({"results": [{"description": "Gasket replacement of HE cover",
                                               "codes": {}, "lines": [
        {"kind": "SERVICE", "doc_no": "4300098995", "date": "2026-08-31", "material": None,
         "pr_no": "10082938", "po_no": "4300098995", "long_text": "Gasket changing"}]}]})
    assert service[0]["code"] == "" and service[0]["description"].startswith("Gasket")


def test_a_need_reads_sap_and_the_models_question_is_checked_and_counted():
    conn = _conn()
    search, sent = _search(GAUGES)
    ask, seen = _asker(json.dumps({"question": "Which instrument is it?", "options": [
        {"label": "Bimetallic gauge", "words": ["TG", "Bimetallic"]},
        {"label": "Thermocouple", "words": ["thermocouple"]},
        {"label": "Thermowell", "words": ["Thermowell"]}]}))
    out = sap_need.turn(conn, "s1", URL, "i need to procure bimetallic temperature gauge",
                        ask_model=ask, search=search)
    assert sent == ["bimetallic temperature gauge"]
    assert out["ask"]["question"] == "Which instrument is it?"
    assert out["ask"]["options"] == ["Bimetallic gauge (3 materials)",
                                     "Thermocouple (1 material)", "Thermowell (1 material)"]
    assert out["tool_calls"][0]["tool"] == "sap_search" and "<untrusted" in seen[0]
    # a click narrows to three; with three left the model is asked again
    ask2, _ = _asker(json.dumps({"question": "Which range?", "options": [
        {"label": "0-100 Cel", "words": ["0‑100"]},
        {"label": "0-200 Cel", "words": ["0-200"]}]}))
    out = sap_need.turn(conn, "s1", URL, "Bimetallic gauge (3 materials)", ask_model=ask2,
                        search=search)
    assert out["ask"]["options"] == ["0-100 Cel (1 material)", "0-200 Cel (2 materials)"]
    out = sap_need.turn(conn, "s1", URL, "0-100 Cel (1 material)", ask_model=ask2,
                        search=search)
    assert out["ask"] is None
    assert out["reply"].startswith("Final material: I-TG-ARC-G100-01")
    assert "Last PR: 10073969 (2021-09-28)" in out["reply"]
    assert "Last PO: 4500024964 (2022-07-13), 2.0 NOS at INR 18,835.35" in out["reply"]
    assert "PUR case: PUR-INSTMNT-1002364" in out["reply"]
    assert chatstate.get(conn, "s1", sap_need.SLOT) is None
    assert len(sent) == 1                               # SAP was read once


def test_a_question_the_list_does_not_bear_out_is_refused_then_the_data_asks():
    conn = _conn()
    search, _ = _search(GAUGES)
    tg_only = json.dumps({"question": "Which?", "options": [{"label": "TG", "words": ["TG"]}]})
    nowhere = json.dumps({"question": "Which?", "options": [
        {"label": "Titanium", "words": ["titanium"]}, {"label": "TG", "words": ["TG"]}]})
    ask, seen = _asker(nowhere, tg_only, nowhere)
    out = sap_need.turn(conn, "s2", URL, "i need to procure temperature gauge", ask_model=ask,
                        search=search)
    assert len(seen) == 3
    assert "no candidate carries ['titanium']" in seen[1]
    # five materials: the data's own question names each, with its code
    assert out["ask"]["question"] == "Which of these is it?"
    assert "TG Bimetallic U320mm 6mm dia 0-100Cel [I-TG-ARC-G100-01]" in out["ask"]["options"]


def test_without_a_model_a_long_list_splits_on_its_descriptions():
    many = {"results": [_res(f"I-TG-{i:02d}", f"TG Bimetallic U{200 + 40 * i}mm 0-200Cel")
                        for i in range(6)]
            + [_res(f"I-TW-{i:02d}", f"Thermowell U-{200 + 40 * i} SS316") for i in range(5)]}
    out = sap_need.turn(_conn(), "s3", URL, "i need to procure temperature gauge",
                        search=_search(many)[0])
    assert out["ask"]["options"] == ["TG (6 materials)", "Thermowell (5 materials)"]


def test_typed_words_narrow_and_unknown_words_read_sap_again():
    conn = _conn()
    search, sent = _search(GAUGES)
    sap_need.turn(conn, "s4", URL, "i need to procure temperature gauge", search=search)
    out = sap_need.turn(conn, "s4", URL, "u400", search=search)
    assert out["reply"].startswith("Final material: I-TG-ARC-G200-02")
    sap_need.turn(conn, "s4", URL, "i need to procure temperature gauge", search=search)
    sap_need.turn(conn, "s4", URL, "titanium", search=search)
    assert sent[-1] == "temperature gauge titanium"


def test_the_officers_own_question_goes_to_the_agent_and_undo_stop_work():
    conn = _conn()
    search, _ = _search(GAUGES)
    sap_need.turn(conn, "s5", URL, "i need to procure temperature gauge", search=search)
    assert sap_need.turn(conn, "s5", URL, "what is a thermowell?", search=search) is None
    assert chatstate.get(conn, "s5", sap_need.SLOT)["pending"]      # still open
    sap_need.turn(conn, "s5", URL, "bimetallic", search=search)
    back = sap_need.turn(conn, "s5", URL, "undo", search=search)
    assert back["reply"].startswith("Took the last answer back. 5 SAP materials")
    stop = sap_need.turn(conn, "s5", URL, "stop", search=search)
    assert "5 SAP material(s) are still in" in stop["reply"] and stop["ask"] is None


def test_no_line_is_said_and_an_outage_is_not_a_negative():
    conn = _conn()
    out = sap_need.turn(conn, "s6", URL, "i need to procure unobtainium",
                        search=_search({"results": []})[0])
    assert out["reply"].startswith("SAP has no PR, PO or service line for 'unobtainium'")

    def down(url, need):
        raise OSError("refused")
    out = sap_need.turn(conn, "s6", URL, "i need to procure gasket", search=down)
    assert out["reply"].startswith("SAP could not be read just now")
    assert sap_need.turn(conn, "s6", URL, "hello there") is None


def test_the_app_runs_sap_capture_only_when_the_source_is_sap(tmp_path, monkeypatch):
    seen = []

    def turn(conn, session_id, kg_url, message, ask_model=None, search=None):
        seen.append(message)
        return {"reply": "SAP has 2 line(s).", "ask": None,
                "tool_calls": [{"tool": "sap_search", "args": {}, "output": "x"}]}
    monkeypatch.setattr(sap_need, "turn", turn)
    a = make_agent(tmp_path / "a", kg_url=URL, kg_tools="sap_search", nam_source="sap",
                  nam_chat="driver")
    assert a._drive_nam("i need to procure gaskets", lambda *x: None, False).reply \
        == "SAP has 2 line(s)."
    b = make_agent(tmp_path / "b", kg_url=URL, kg_tools="sap_search",
                  nam_chat="driver")       # Option B
    assert b._drive_nam("i need to procure gaskets", lambda *x: None, False) is None
    assert seen == ["i need to procure gaskets"]


def test_the_data_places_each_material_and_checks_the_labels():
    from nam_agent.nam import llm

    cands = sap_need.materials(GAUGES)[:3]          # 0-100 U320, 0-200 U320, 0-200 U400

    def ask(options, among=cands):
        return sap_need.check(json.dumps({"question": "Which?", "options": options}), among)

    def refused(options, among=cands):
        try:
            ask(options, among)
        except llm.Rejected as exc:
            return str(exc)
        return ""
    # "0-100" is not the start of "0-1000"; each material goes to its first option
    q = ask([{"label": "U320", "words": ["u320"]}, {"label": "0-200", "words": ["0-200"]}])
    assert [o["items"] for o in q["options"]] == [[0, 1], [2]]
    assert "does not carry 100" in refused([{"label": "0-100 Cel", "words": ["U320"]},
                                            {"label": "U400", "words": ["U400"]}])
    assert "tells none apart" in refused([{"label": "Gauge", "words": ["TG"]},
                                          {"label": "U400", "words": ["U400"]}])
    # one left out of three: it stays in, under "Something else"
    q = ask([{"label": "0-100 Cel", "words": ["0-100"]}, {"label": "U400", "words": ["U400"]}])
    assert q["options"][-1] == {"label": sap_need.OTHER, "items": [1]}
    # most of the list left out: refused
    assert "carry no option's words" in refused(
        [{"label": "0-100 Cel", "words": ["0-100"]}, {"label": "U400", "words": ["U400"]}],
        sap_need.materials(GAUGES))
    assert not sap_need._carry(cands[0], ["0-10"])


def test_a_side_question_knows_the_materials_still_in_and_the_question_comes_back(
        tmp_path, monkeypatch):
    nam_agent = make_agent(tmp_path, kg_url=URL, kg_tools="sap_search", nam_source="sap",
                  nam_chat="driver")
    sid = nam_agent.session.session_id
    real = sap_need.turn
    monkeypatch.setattr(sap_need, "turn", lambda conn, s, url, msg, ask_model=None: real(
        conn, s, url, msg, search=_search(GAUGES)[0]))
    first = nam_agent._drive_nam("i need to procure temperature gauge", lambda *x: None, False)
    assert first.reply.startswith("SAP has 10 line(s)")
    assert nam_agent._drive_nam("what is the difference between them?", lambda *x: None,
                           False) is None
    system = nam_agent.session.build_system("what is the difference between them?")
    assert "SAP need capture is open" in system and "I-TG-ARC-G200-02" in system
    from nam_agent.loop.agent import LoopResult
    result = LoopResult(reply="They differ in range.", tool_calls=[], iterations=1)
    nam_agent._nam_reminder(result, lambda *x: None, False)
    assert "Back to the need: Which of these is it?" in result.reply
    assert result.tool_calls[-1]["args"]["field"] == "sap"
    assert sap_need.context(nam_agent.conn, "another chat") is None
