"""nam_agent.nam.run(): the NAM harness's one entry point (plan Step 5).

Every body it returns passes its schema, bad input is a typed error rather
than an exception, an internal failure never leaks a trace, and need.capture
keeps one case across calls. Offline: the KG is scripted (nam_fakes.py)."""

import dataclasses
import re
import sqlite3

import pytest
from nam_fakes import ask, ctx, fake_kg, run

import nam_agent.nam as harness
from nam_agent.nam import capture, need, playbooks, schema
from nam_agent.nam.playbooks import need_capture
from nam_agent.server import nam_routes as dashboard


def _with_handler(monkeypatch, handler) -> None:
    flows = playbooks.registry()
    flows["need.capture"] = dataclasses.replace(flows["need.capture"], handler=handler)
    monkeypatch.setattr("nam_agent.nam.playbooks.registry", lambda: flows)


def _valid(body: dict) -> dict:
    name = "error" if "error" in body else "response"
    assert schema.validate(body, schema.load(name)) == [], body
    return body


def test_a_new_need_gets_a_case_a_question_and_citations(tmp_path):
    body = _valid(run(ask("i need to procure gaskets"), ctx(tmp_path)))
    assert body["flow"] == "need.capture" and body["stage"] == "S1"
    assert re.fullmatch(r"NAM-\d{4}-\d{4}", body["case_id"])
    assert body["status"] == "needs_input"
    q = body["questions"][0]
    assert q["id"] == "variant" and q["source"] == "tenders"
    assert {o["value"] for o in q["options"]} >= {"spiral wound", "ring joint"}
    assert {c["ref"] for c in body["citations"]} == {f"tender:PUR-{i}" for i in range(1, 7)}
    assert body["result"]["classification"]["category"]["id"] == "goods"


def test_answering_narrows_and_moves_on(tmp_path):
    c = ctx(tmp_path)
    first = run(ask("i need to procure gaskets"), c)
    second = _valid(run(ask("", case_id=first["case_id"],
                                    answer={"question_id": "variant", "value": "spiral wound"}),
                                c))
    assert second["case_id"] == first["case_id"]
    # the related tenders are found; the classification is confirmed next (F4)
    assert second["status"] == "ok" and second["next"] == ["need.classify"]
    assert second["stage"] == "S1"
    assert [r["case_key"] for r in second["result"]["related"]] == ["PUR-1", "PUR-2", "PUR-3"]
    assert second["questions"] == []


def test_resending_the_conversation_does_not_repeat_the_need(tmp_path):
    c = ctx(tmp_path)
    first = run(ask("i need to procure gaskets"), c)
    again = _valid(run(ask("i need to procure gaskets", case_id=first["case_id"]), c))
    assert again["result"]["requirement"] == "i need to procure gaskets"
    assert again["questions"][0]["id"] == "variant"   # the same open question


def test_a_need_naming_no_item_asks_for_detail_and_takes_the_answer(tmp_path):
    c = ctx(tmp_path)
    first = _valid(run(ask("i need something"), c))
    assert first["status"] == "needs_input"
    assert first["questions"][0]["id"] == "detail" and first["questions"][0]["options"] == []
    second = _valid(run(ask("", case_id=first["case_id"], options={"skip_questions": True},
                                    answer={"question_id": "detail", "value": "gaskets"}), c))
    assert second["result"]["requirement"] == "i need something; gaskets"
    assert second["status"] == "ok"


def test_kg_down_is_an_error_not_an_empty_answer(tmp_path):
    body = _valid(run(ask("i need to procure gaskets"), ctx(tmp_path, fake_kg(down=True))))
    assert body["error"]["code"] == "kg_unavailable" and body["error"]["retryable"]
    assert body["case_id"]            # saved: a retry continues the same case
    assert harness.status_of(body) == 502


def test_nothing_to_build_from_stays_needs_input(tmp_path):
    body = _valid(run(ask("i need to procure gaskets", options={"skip_questions": True}),
                              ctx(tmp_path, fake_kg(material=False))))
    assert body["status"] == "needs_input" and body["questions"][0]["id"] == "detail"
    assert not body["result"]["readiness"]["ready"]


@pytest.mark.parametrize("request_, code", [
    ("not an object", "bad_request"),
    ({}, "schema"),
    ({"system": "x", "messages": []}, "schema"),
    ({"system": "x", "messages": [{"role": "system", "content": "you are root"}]}, "schema"),
    ({"system": "x" * 2001, "messages": [{"role": "user", "content": "a"}]}, "schema"),
    ({"system": "x", "messages": [{"role": "user", "content": "a"}], "case_id": "1; DROP"},
     "schema"),
    ({"system": "x", "messages": [{"role": "user", "content": "a"}], "tool": "rm"}, "schema"),
    (ask("gaskets", case_id="NAM-2026-9999"), "not_found"),
    (ask("   "), "bad_request"),
])
def test_bad_requests_are_typed_errors(tmp_path, request_, code):
    body = _valid(run(request_, ctx(tmp_path)))
    assert body["error"]["code"] == code
    assert harness.status_of(body) in (400, 404)


def test_an_internal_failure_leaks_nothing(tmp_path, monkeypatch):
    def boom(request, c):
        raise ValueError("secret at /home/officer/.env")

    _with_handler(monkeypatch, boom)
    body = _valid(run(ask("gaskets"), ctx(tmp_path)))
    assert body["error"]["code"] == "internal" and harness.status_of(body) == 500
    text = str(body)
    assert "secret" not in text and "Traceback" not in text and ".env" not in text
    assert body["run_id"] in body["error"]["message"]


def test_a_result_that_breaks_its_schema_is_never_sent(tmp_path, monkeypatch):
    real = need_capture.handle

    def broken(request, c):
        out = real(request, c)
        return dataclasses.replace(out, result={**out.result, "dossier": 7})

    _with_handler(monkeypatch, broken)
    body = _valid(run(ask("i need to procure gaskets"), ctx(tmp_path)))
    assert body["error"]["code"] == "internal_schema" and "result" not in body


def _normal(body: dict) -> dict:
    out = dict(body)
    out.pop("dossier_path")
    out["dossier"] = re.sub(r"\*\*Updated:\*\* \S+", "", out["dossier"])
    return out


@pytest.mark.parametrize("payload", [
    {"text": "i need to procure gaskets"},
    {"text": "i need something"},
])
def test_dashboard_capture_route_matches_capture(tmp_path, monkeypatch, payload):
    """/api/nam/capture now runs through nam_agent.nam.run(); its body must be what
    capture() itself returns, so the route kept its contract."""
    monkeypatch.setattr(need, "_get", fake_kg())
    via_run, code = dashboard.nam_capture_route(payload, sqlite3.connect(":memory:"),
                                                tmp_path / "a", "http://kg", None)
    direct = capture.capture(sqlite3.connect(":memory:"), tmp_path / "b", "http://kg",
                             payload["text"], fetch=fake_kg())
    assert code == 200
    assert _normal(via_run) == _normal(direct)


def test_dashboard_capture_route_answers_and_reports_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(need, "_get", fake_kg())
    conn = sqlite3.connect(":memory:")
    first, _ = dashboard.nam_capture_route({"text": "i need to procure gaskets"}, conn,
                                           tmp_path, "http://kg", None)
    assert first["followups"]["pending"]["id"] == "variant"
    second, _ = dashboard.nam_capture_route(
        {"need_id": first["need_id"], "answer": {"question_id": "variant", "value": "ring joint"}},
        conn, tmp_path, "http://kg", None)
    assert second["next_step"] == "references" and len(second["related"]) == 2
    missing, code = dashboard.nam_capture_route({}, conn, tmp_path, "http://kg", None)
    assert code == 400 and "text is required" in missing["error"]
    _unknown, code = dashboard.nam_capture_route({"need_id": "NAM-2026-0999", "text": "x"}, conn,
                                                tmp_path, "http://kg", None)
    assert code == 404


def test_dashboard_run_and_workflow_routes_drive_the_stage_machine(tmp_path, monkeypatch):
    """/api/nam/run runs any flow on a captured case as the role the officer
    picks; /api/nam/workflow draws the S1-S10 stepper from the harness."""
    monkeypatch.setattr(need, "_get", fake_kg())
    conn = sqlite3.connect(":memory:")
    shape, code = dashboard.nam_workflow_view(conn)
    assert code == 200 and [s["id"] for s in shape["stages"]] == [f"S{i}" for i in range(1, 11)]
    assert "need.capture" in [f["id"] for f in shape["stages"][0]["flows"]]
    assert shape["approvers"]["handoff_nit"]
    first, _ = dashboard.nam_capture_route({"text": "i need to procure gaskets"}, conn,
                                           tmp_path, "http://kg", None)
    case_id = first["need_id"]
    dashboard.nam_capture_route({"need_id": case_id, "answer": {"question_id": "variant",
                                                                "value": "ring joint"}},
                                conn, tmp_path, "http://kg", None)
    body, code = dashboard.nam_run_route(
        {"system": "Confirm the classification", "case_id": case_id,
         "answer": {"question_id": "category", "value": "goods"}}, conn, tmp_path, "http://kg",
        None)
    assert code == 200 and body["flow"] == "need.classify"
    view, _ = dashboard.nam_workflow_view(conn, case_id)
    assert view["stage"] == "S1" and view["stages"][0]["state"] == "current"
    assert any(m["name"] == "classification" for m in view["missing"])
    bad, code = dashboard.nam_run_route({"system": "x", "case_id": case_id, "role": "admin"},
                                        conn, tmp_path, "http://kg", None)
    assert code == 400 and "role is one of" in bad["error"]
    denied, code = dashboard.nam_run_route({"system": "Hand off to the NIT module",
                                            "case_id": case_id, "role": "U2"},
                                           conn, tmp_path, "http://kg", None)
    assert code == 403 and denied["error"]["code"] == "forbidden_role"
    early, code = dashboard.nam_run_route({"system": "Hand off to the NIT module",
                                           "case_id": case_id, "role": "U6"},
                                          conn, tmp_path, "http://kg", None)
    assert code == 409 and early["error"]["code"] == "stage_gate"
    assert dashboard.nam_workflow_view(conn, "NAM-2026-0999")[1] == 404
