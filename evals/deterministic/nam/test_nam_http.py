"""The harness API (/v1/nam/*) over real HTTP on a loopback port (nam_agent/nam/api.py).

Every reply is JSON with the status its code names: a bad body is a 400 and
not a dropped connection, an oversized one is a 413 refused before it is read,
the wrong content type is a 415, and no body carries a stack trace."""

import http.client
import json
import sqlite3
import threading

import pytest
from nam_fakes import IDENTITY, ask, fake_kg

import nam_agent.nam as harness
from nam_agent.nam import api, schema

MAX_BODY = 4096


@pytest.fixture()
def server(tmp_path):
    db = tmp_path / "state.db"

    def make_ctx():
        conn = sqlite3.connect(db)
        return harness.Context(conn=conn, home=tmp_path, kg_url="http://kg",
                               fetch=fake_kg()), conn.close

    srv = api.serve("127.0.0.1", 0, make_ctx, max_body=MAX_BODY, model_configured=False,
                    kg_url="")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def call(port, method, path, body=None, headers=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    hdrs = {"Content-Type": "application/json", **IDENTITY, **(headers or {})}
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    assert resp.getheader("Content-Type", "").startswith("application/json")
    text = payload.decode()
    assert "Traceback" not in text
    return resp.status, json.loads(text), resp


def _error(body):
    assert schema.validate(body, schema.load("error")) == [], body
    return body["error"]["code"]


def test_run_returns_a_valid_response(server):
    status, body, resp = call(server, "POST", "/v1/nam/run", ask("i need to procure gaskets"),
                              headers={"X-Request-Id": "req-42"})
    assert status == 200
    assert schema.validate(body, schema.load("response")) == []
    assert body["status"] == "needs_input" and body["case_id"]
    assert resp.getheader("X-Request-Id") == "req-42"
    assert resp.getheader("Cache-Control") == "no-store"


def test_a_case_continues_across_calls(server):
    _, first, _ = call(server, "POST", "/v1/nam/run", ask("i need to procure gaskets"))
    status, second, _ = call(server, "POST", "/v1/nam/run", ask(
        "", case_id=first["case_id"], answer={"question_id": "variant", "value": "ring joint"}))
    assert status == 200 and second["status"] == "ok"
    assert second["case_id"] == first["case_id"]


@pytest.mark.parametrize("raw, ctype, status, code", [
    (b"{not json", "application/json", 400, "bad_request"),
    (b"\xff\xfe", "application/json", 400, "bad_request"),
    (b'{"system": "x"}', "application/json", 400, "schema"),
    (b'{"system": "x"}', "text/plain", 415, "unsupported_type"),
    (b"x" * (MAX_BODY + 1), "application/json", 413, "too_large"),
], ids=["bad-json", "not-utf8", "schema", "content-type", "too-large"])
def test_bad_bodies_get_json_errors(server, raw, ctype, status, code):
    got, body, _ = call(server, "POST", "/v1/nam/run", raw=raw, headers={"Content-Type": ctype})
    assert got == status and _error(body) == code


def test_oversized_body_is_refused_before_it_is_read(server):
    # claims 1 GB and sends nothing more: the answer must come at once
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    conn.putrequest("POST", "/v1/nam/run")
    conn.putheader("Content-Type", "application/json")
    for name, value in IDENTITY.items():
        conn.putheader(name, value)
    conn.putheader("Content-Length", str(1 << 30))
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 413 and _error(json.loads(resp.read())) == "too_large"
    conn.close()


@pytest.mark.parametrize("method, path, status, code", [
    ("GET", "/v1/nam/run", 405, "method_not_allowed"),
    ("POST", "/v1/nam/health", 405, "method_not_allowed"),
    ("DELETE", "/v1/nam/run", 405, "method_not_allowed"),
    ("GET", "/api/data", 404, "not_found"),        # no dashboard admin route here
    ("POST", "/api/settings", 404, "not_found"),
    ("GET", "/v1/nam/../../etc/passwd", 404, "not_found"),
])
def test_unknown_routes_and_methods(server, method, path, status, code):
    got, body, _ = call(server, method, path, body={} if method == "POST" else None)
    assert got == status and _error(body) == code


def test_health_and_schema(server):
    status, body, _ = call(server, "GET", "/v1/nam/health")
    assert status == 200 and body["ok"] and body["kg"] == {"configured": False,
                                                          "reachable": None}
    status, body, _ = call(server, "GET", "/v1/nam/schema")
    assert status == 200 and set(body["schemas"]) >= {"request", "response", "error"}


def test_a_hostile_request_id_is_not_echoed(server):
    _, _, resp = call(server, "GET", "/v1/nam/health", headers={"X-Request-Id": "a b<script>"})
    assert resp.getheader("X-Request-Id") is None
