"""The model per task (nam_agent/nam/llm.py, plan Step 16): each task's models
come from NAM_MODELS, every attempt is in the audit log, an outage or a
cut-off reply hands over to the next model, and a cut-off reply is never
acted on."""

import dataclasses
import http.client
import json
import threading
import time
from types import SimpleNamespace

import pytest
from nam_fakes import OFFICER, ask, ctx, run

from nam_agent.config import Settings
from nam_agent.loop.models import OpenAICompatClient
from nam_agent.nam import api, audit, llm, router

AMBIGUOUS = {"system": "Help with request #123",
             "messages": [{"role": "user", "content": "20 gate valves for the crude unit"}]}


class Script:
    """A transport: per model, a list of what each call returns or raises."""

    def __init__(self, **plan):
        self.plan = {k.replace("_", "-"): list(v) for k, v in plan.items()}
        self.calls = []

    def __call__(self, call):
        self.calls.append(call)
        step = self.plan[call.model].pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, float):
            time.sleep(step)
            return llm.Reply(text="need.capture")
        return step


def _ctx(tmp_path, script, **chains):
    c = ctx(tmp_path)
    c.models = llm.Models(transport=script, chains={
        "route": ("small-a", "small-b"), "followup": ("main-a",),
        **{k: tuple(v) for k, v in chains.items()}})
    return c


def _rows(c, task="route"):
    return [(e["detail"]["model"], e["status"], e["error_code"], e["attempts"],
             e["detail"]["fallback"])
            for e in audit.events(c.conn, kind="model") if e["name"] == task]


# -- configuration ----------------------------------------------------------------------

def test_each_task_gets_its_default_models():
    chains = llm.parse_chains("", "big", "small")
    assert chains == {"route": ("small", "big"), "followup": ("big", "small"),
                      "review": ("big", "small"), "quantity": ("big", "small"),
                      "intent": ("small", "big"), "domain": ("small", "big")}
    assert llm.parse_chains("", "same", "same")["route"] == ("same",)
    assert llm.parse_chains("", "", "")["route"] == ()


def test_the_configuration_names_the_models_in_order():
    chains = llm.parse_chains("route = r1, r2 ,r1; followup=f1", "big", "small")
    assert chains == {"route": ("r1", "r2"), "followup": ("f1",), "review": ("big", "small"),
                      "quantity": ("big", "small"), "intent": ("small", "big"),
                      "domain": ("small", "big")}


def test_an_unknown_task_is_warned_and_left_out(caplog):
    # a setting written for another version never stops the server
    assert "draft" not in llm.parse_chains("draft=m1", "big", "small")
    assert "no task 'draft'" in caplog.text


@pytest.mark.parametrize(("spec", "says"), [("route", "is not task=model"),
                                           ("route=, ,", "names no model")])
def test_a_bad_configuration_is_refused(spec, says):
    with pytest.raises(ValueError, match=says):
        llm.parse_chains(spec, "big", "small")


def test_the_settings_read_the_models_per_task(monkeypatch):
    monkeypatch.setenv("NAM_MODELS",
                       "route=r1;followup=f1,f2;review=v1;quantity=q1;intent=i1;domain=d1")
    settings = Settings()
    models = llm.Models.from_settings(SimpleNamespace(messages=None), settings)
    assert models.chains == {"route": ("r1",), "followup": ("f1", "f2"), "review": ("v1",),
                             "quantity": ("q1",), "intent": ("i1",), "domain": ("d1",)}


# -- each task's model is used and logged -------------------------------------------------

def test_routing_uses_its_model_and_says_which(tmp_path):
    script = Script(small_a=[llm.Reply(text="need.capture", tokens_in=50, tokens_out=3)])
    c = _ctx(tmp_path, script)
    body = run(AMBIGUOUS, c)
    assert body["flow"] == "need.capture" and body["route"]["by"] == "model"
    assert "small-a" in body["route"]["why"]
    assert [x.model for x in script.calls] == ["small-a"]
    assert script.calls[0].max_tokens == llm.TASKS["route"].max_tokens
    row = audit.events(c.conn, kind="model")[0]
    assert (row["name"], row["status"], row["run_id"]) == ("route", "ok", body["run_id"])
    assert row["user"] == OFFICER.user
    assert row["detail"]["tokens_in"] == 50 and row["detail"]["position"] == 0


def test_need_capture_asks_the_followup_model(tmp_path):
    question = ('{"question": "Which type of gasket?", "options": ['
                '{"label": "Spiral wound", "match": "spiral wound"}, '
                '{"label": "Ring joint", "match": "ring joint"}]}')
    script = Script(main_a=[llm.Reply(text=question)] * 5)
    c = _ctx(tmp_path, script)
    run(ask("i need to procure gaskets"), c)
    assert {x.model for x in script.calls} == {"main-a"}
    assert _rows(c, "followup")[0][:2] == ("main-a", "ok")


def test_the_prompt_is_not_stored_in_the_log(tmp_path):
    script = Script(small_a=[llm.Reply(text="need.capture")])
    c = _ctx(tmp_path, script)
    run({**AMBIGUOUS, "system": "Help with request #123 secret-word-zq9"}, c)
    row = audit.events(c.conn, kind="model")[0]
    assert "secret-word-zq9" not in json.dumps(row)
    assert row["detail"]["prompt_chars"] > 0 and len(row["detail"]["prompt_sha256"]) == 16


# -- fallback -------------------------------------------------------------------------------

def test_an_outage_goes_straight_to_the_next_model(tmp_path):
    script = Script(small_a=[ConnectionError("down")], small_b=[llm.Reply(text="need.capture")])
    c = _ctx(tmp_path, script)
    body = run(AMBIGUOUS, c)
    assert body["flow"] == "need.capture" and "small-b" in body["route"]["why"]
    assert _rows(c) == [("small-a", "error", "model_unavailable", 1, False),
                        ("small-b", "ok", "", 1, True)]


def test_a_model_that_takes_too_long_hands_over(tmp_path, monkeypatch):
    monkeypatch.setitem(llm.TASKS, "route", dataclasses.replace(llm.TASKS["route"],
                                                                timeout_s=0.05))
    script = Script(small_a=[0.5], small_b=[llm.Reply(text="need.capture")])
    c = _ctx(tmp_path, script)
    assert run(AMBIGUOUS, c)["flow"] == "need.capture"
    assert [r[2] for r in _rows(c)] == ["timeout", ""]


def test_every_model_down_leaves_the_question_to_the_caller(tmp_path):
    script = Script(small_a=[OSError("x")], small_b=[OSError("y")])
    c = _ctx(tmp_path, script)
    body = run(AMBIGUOUS, c)
    assert body["status"] == "needs_input" and body["questions"][0]["id"] == "flow"
    assert [r[1] for r in _rows(c)] == ["error", "error"]


def test_no_model_means_no_model_call(tmp_path):
    c = ctx(tmp_path)
    assert llm.asker(c, "route") is None
    with pytest.raises(llm.ModelUnavailable, match="no model configured"):
        llm.complete(c, "route", "s", "u")
    assert run(AMBIGUOUS, c)["status"] == "needs_input"


# -- a cut-off reply ------------------------------------------------------------------------

def test_a_cut_off_reply_is_asked_again_shorter(tmp_path):
    script = Script(small_a=[llm.Reply(text="need.", truncated=True),
                             llm.Reply(text="need.capture")])
    c = _ctx(tmp_path, script)
    assert run(AMBIGUOUS, c)["flow"] == "need.capture"
    assert script.calls[1].user.endswith(llm.SHORTER) and not script.calls[0].user.endswith(
        llm.SHORTER)
    assert _rows(c) == [("small-a", "error", "truncated", 1, False),
                        ("small-a", "ok", "", 2, False)]


def test_a_cut_off_reply_is_never_acted_on(tmp_path):
    # the cut-off text happens to be a valid flow id: it is still not used
    cut = llm.Reply(text="need.capture", truncated=True)
    script = Script(small_a=[cut, cut], small_b=[cut, llm.Reply(text="")])
    c = _ctx(tmp_path, script)
    body = run(AMBIGUOUS, c)
    assert body["status"] == "needs_input" and body.get("flow") != "need.capture"
    assert [r[2] for r in _rows(c)] == ["truncated", "truncated", "truncated", "empty"]
    with pytest.raises(llm.ModelUnavailable, match="truncated"):
        llm.complete(_ctx(tmp_path, Script(small_a=[cut, cut], small_b=[OSError()])),
                     "route", "s", "u")


def test_an_answer_outside_the_list_is_still_ignored(tmp_path):
    script = Script(small_a=[llm.Reply(text="delete_everything")])
    c = _ctx(tmp_path, script)
    chosen = router.route(AMBIGUOUS, None, llm.asker(c, "route"))
    assert chosen.flow is None and chosen.status == "needs_input"


# -- the client ------------------------------------------------------------------------------

def _message(text, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=7, output_tokens=2))


def test_the_reasoning_hint_is_dropped_once_refused():
    sent = []

    class Client:
        def create(self, extra=None, **kw):
            sent.append(("create", extra))
            raise ValueError("unknown parameter reasoning_effort")

        messages = SimpleNamespace(create=lambda **kw: sent.append(("plain", None))
                                   or _message("ok", "max_tokens"))

    send = llm.client_transport(Client())
    call = llm.Call(model="m", system="s", user="u", max_tokens=10, reasoning="low")
    first = send(call)
    send(call)
    assert sent == [("create", {"reasoning_effort": "low"}), ("plain", None), ("plain", None)]
    assert first == llm.Reply(text="ok", truncated=True, tokens_in=7, tokens_out=2)


def test_another_client_error_is_not_mistaken_for_the_hint():
    class Client:
        def create(self, **kw):
            raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        llm.client_transport(Client())(llm.Call("m", "s", "u", 10, "low"))


def test_the_openai_client_reports_a_cut_off_reply_and_passes_options():
    client = object.__new__(OpenAICompatClient)
    seen = {}

    def fake_call(kwargs, **extra):
        seen.update(extra)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="length", message=SimpleNamespace(content="half", tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=9))

    client._call = fake_call
    out = client.create(model="m", messages=[{"role": "user", "content": "q"}], max_tokens=5,
                        extra={"reasoning_effort": "low"})
    assert out.stop_reason == "max_tokens" and seen == {"reasoning_effort": "low"}
    assert llm._reply(out).truncated


# -- health ------------------------------------------------------------------------------------

def test_health_shows_the_model_per_task(tmp_path):
    models = llm.Models(transport=lambda call: llm.Reply(text=""),
                        chains={"route": ("small-a",), "followup": ("main-a", "small-a"),
                                "review": ("main-a",), "quantity": ("main-a",),
                                "intent": ("small-a",), "domain": ("small-a",)})
    shown = llm.configured(models)
    srv = api.serve("127.0.0.1", 0, lambda: (ctx(tmp_path), lambda: None), max_body=4096,
                    model_configured=shown["configured"], kg_url="", model_tasks=shown["tasks"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        conn.request("GET", "/v1/nam/health")
        body = json.loads(conn.getresponse().read())
    finally:
        srv.shutdown()
        srv.server_close()
    assert body["model"] == {"configured": True,
                             "tasks": {"route": ["small-a"], "followup": ["main-a", "small-a"],
                                       "review": ["main-a"], "quantity": ["main-a"],
                                       "intent": ["small-a"], "domain": ["small-a"]}}
    assert llm.configured(None) == {"configured": False, "tasks": {}}
