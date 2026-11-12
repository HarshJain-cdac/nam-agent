"""DETERMINISTIC EVAL: the server, every endpoint on one port.

A real server on a free port, with a scripted model, checks that:
  - the chat, the NAM screens' routes, /v1/nam/* and the page answer on the
    one port, and the default port is 7777;
  - a chat begun with an identity is that user's alone: another user, or a
    caller without identity, can neither send to it, read it nor list it;
  - NAM_SERVER_KEY, when set, closes /api/* and /v1/* to a caller without it;
  - the browser's preflight is answered with the identity headers allowed.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from evals.helpers import ScriptedClient, make_agent, response, text_block
from nam_agent.config import Settings
from nam_agent.server import agent as server_agent
from nam_agent.server import chat as chats
from nam_agent.server import http as server_http

ALICE = {"X-NAM-Tenant": "nrl", "X-NAM-User": "alice", "X-NAM-Roles": "U1"}
BOB = {"X-NAM-Tenant": "nrl", "X-NAM-User": "bob", "X-NAM-Roles": "U1"}


def _replies(n: int) -> ScriptedClient:
    # each turn: the reply alone (memory and the gate are off where NAM runs)
    return ScriptedClient([response([text_block(f"reply {i}")]) for i in range(n)])


@pytest.fixture
def server(tmp_path, monkeypatch):
    def start(replies: int = 4, key: str = ""):
        home = tmp_path / "home"
        monkeypatch.setenv("NAM_HOME", str(home))
        monkeypatch.setenv("NAM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "offline")
        monkeypatch.setenv("NAM_CHAT_MEMORY", "off")
        monkeypatch.delenv("NAM_KG_URL", raising=False)
        if key:
            monkeypatch.setenv("NAM_SERVER_KEY", key)
        else:
            monkeypatch.delenv("NAM_SERVER_KEY", raising=False)
            monkeypatch.delenv("NAM_DASHBOARD_KEY", raising=False)
        from nam_agent import db

        home.mkdir(parents=True, exist_ok=True)
        agent = make_agent(home, client=_replies(replies),
                           conn=db.connect(home, check_same_thread=False))
        monkeypatch.setattr(server_agent, "_agent", agent)
        srv = server_http.build("127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    started: list = []
    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def call(base, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw, status, hdrs = resp.read(), resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        raw, status, hdrs = exc.read(), exc.code, exc.headers
    ctype = hdrs.get("Content-Type", "")
    if "event-stream" in ctype:
        events = [json.loads(line[5:]) for line in raw.decode().split("\n\n")
                  if line.startswith("data:")]
        return status, events, hdrs
    return status, (json.loads(raw) if "json" in ctype else raw), hdrs


def test_the_default_port_is_7777(monkeypatch):
    for name in ("NAM_PORT", "NAM_DASHBOARD_PORT", "PORT"):
        monkeypatch.delenv(name, raising=False)
    assert Settings().nam_port == 7777 == server_http.PORT
    monkeypatch.setenv("NAM_PORT", "8123")
    assert Settings().nam_port == 8123


def test_every_endpoint_is_on_the_one_port(server):
    base = server()
    assert call(base, "GET", "/health")[:2] == (200, {"ok": True, "version": "nam.v1"})
    status, body, _ = call(base, "GET", "/v1/nam/health")
    assert status == 200 and body["ok"] is True
    status, body, _ = call(base, "GET", "/api/nam/workflow")
    assert status == 200 and body["stages"]
    status, page, _ = call(base, "GET", "/")
    assert status == 200 and b"Need Assessment" in page
    assert call(base, "GET", "/static/js/render.js")[0] == 200
    assert call(base, "GET", "/static/../http.py")[0] == 404
    assert call(base, "GET", "/api/nope")[0] == 404
    # /v1/nam/* needs the identity headers
    assert call(base, "GET", "/v1/nam/cases/NAM-2026-0001")[0] == 401


def test_a_turn_streams_and_returns_its_chat_id(server):
    base = server()
    status, events, hdrs = call(base, "POST", "/api/chat/stream", {"message": "hello"})
    assert status == 200 and hdrs["Content-Type"] == "text/event-stream"
    done = events[-1]
    assert done["kind"] == "done" and done["reply"] == "reply 0"
    assert done["session_id"].startswith("nam-chat-")
    assert done["case_id"] is None                  # no NAM case opened yet
    status, body, _ = call(base, "POST", "/api/chat",
                           {"message": "and again", "session_id": done["session_id"]})
    assert status == 200 and body["reply"] == "reply 1"
    assert body["session_id"] == done["session_id"]
    status, body, _ = call(base, "POST", "/api/session",
                           {"action": "history", "id": done["session_id"]})
    assert [m["content"] for m in body["history"]] == ["hello", "reply 0", "and again", "reply 1"]
    status, body, _ = call(base, "POST", "/api/session", {"action": "case", "id": done["session_id"]})
    assert status == 200 and body["case"] is None


def test_a_users_chat_is_theirs_alone(server):
    base = server()
    status, body, _ = call(base, "POST", "/api/chat", {"message": "gaskets"}, ALICE)
    sid = body["session_id"]
    for who in (BOB, None):
        assert call(base, "POST", "/api/chat", {"message": "hi", "session_id": sid}, who)[0] == 403
        assert call(base, "POST", "/api/session", {"action": "history", "id": sid}, who)[0] == 403
        assert call(base, "POST", "/api/session", {"action": "case", "id": sid}, who)[0] == 403
    status, body, _ = call(base, "POST", "/api/session", {"action": "list"}, BOB)
    assert body["chats"] == []
    status, body, _ = call(base, "POST", "/api/session", {"action": "list", "ids": [sid]})
    assert body["chats"] == []                      # no identity: an owned chat is not listed
    status, body, _ = call(base, "POST", "/api/session", {"action": "list"}, ALICE)
    assert [c["id"] for c in body["chats"]] == [sid]
    status, body, _ = call(base, "POST", "/api/session", {"action": "history", "id": sid}, ALICE)
    assert status == 200 and body["history"][0]["content"] == "gaskets"


def test_a_bad_identity_or_chat_id_is_refused(server):
    base = server()
    partial = {"X-NAM-Tenant": "nrl"}
    assert call(base, "POST", "/api/chat", {"message": "hi"}, partial)[0] == 401
    assert call(base, "POST", "/api/chat", {"message": "hi", "session_id": "a b"})[0] == 400
    assert call(base, "POST", "/api/chat", {"message": ""})[0] == 400


def test_the_server_key_closes_the_api(server):
    base = server(key="s3cret")
    assert call(base, "GET", "/api/nam/workflow")[0] == 401
    assert call(base, "GET", "/v1/nam/health")[0] == 401
    assert call(base, "POST", "/api/chat", {"message": "hi"})[0] == 401
    ok = {"Authorization": "Bearer s3cret"}
    assert call(base, "GET", "/api/nam/workflow", headers=ok)[0] == 200
    assert call(base, "GET", "/v1/nam/health", headers=ok)[0] == 200
    assert call(base, "GET", "/health")[0] == 200            # liveness stays open


def test_the_preflight_allows_the_identity_headers(server):
    base = server()
    status, _, hdrs = call(base, "OPTIONS", "/api/chat")
    assert status == 204
    allowed = hdrs["Access-Control-Allow-Headers"]
    for name in ("Authorization", "X-NAM-Tenant", "X-NAM-User", "X-NAM-Roles"):
        assert name in allowed


def test_ownership_rules_without_a_server(tmp_path):
    from nam_agent import db
    from nam_agent.nam.identity import Caller

    conn = db.connect(tmp_path)
    alice = Caller(tenant="nrl", user="alice", roles=("U1",))
    chats.check(conn, "c1", alice, claim=True)
    assert chats.owner_of(conn, "c1") == ("nrl", "alice")
    with pytest.raises(chats.ChatRefused):
        chats.check(conn, "c1", None)
    chats.check(conn, "open-chat", None, claim=True)          # no identity: no owner
    assert chats.owner_of(conn, "open-chat") is None


def test_the_client_file_drives_the_chat(server):
    from nam_agent.client import NamCallError, NamClient

    base = server(key="k1")
    alice = NamClient(base, tenant="nrl", user="alice", roles=["U1"], server_key="k1")
    turn = alice.chat("hello")
    assert turn["reply"] == "reply 0" and turn["session_id"]
    assert alice.chat_case(turn["session_id"]) is None
    assert [c["id"] for c in alice.chats()] == [turn["session_id"]]
    bob = NamClient(base, tenant="nrl", user="bob", roles=["U1"], server_key="k1")
    with pytest.raises(NamCallError) as refused:
        bob.chat_history(turn["session_id"])
    assert refused.value.status == 403
    assert alice.health()["ok"] is True


def test_broken_tool_arguments_from_the_model_are_an_error_it_can_correct():
    from nam_agent.loop.models import BAD_ARGUMENTS, _arguments
    from nam_agent.tools.registry import Tool, ToolRegistry

    assert _arguments('{"item": "valve", "key" "V-2"}') == {BAD_ARGUMENTS: '{"item": "valve", "key" "V-2"}'}
    assert _arguments('{"item": "valve"}') == {"item": "valve"}
    reg = ToolRegistry()
    reg.register(Tool(name="nam_pick", description="", input_schema={"type": "object"},
                      fn=lambda **a: "picked"))
    out = reg.execute("nam_pick", _arguments('{"item": "valve", "key" "V-2"}'))
    assert out.startswith("Error running nam_pick: the arguments were not valid JSON")


def test_a_failed_turn_is_an_error_reply_not_a_dropped_connection(server, monkeypatch):
    base = server()

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(chats, "chat", boom)
    status, body, _ = call(base, "POST", "/api/chat", {"message": "hello"})
    assert status == 500 and body["error"].startswith("the turn failed (RuntimeError)")
