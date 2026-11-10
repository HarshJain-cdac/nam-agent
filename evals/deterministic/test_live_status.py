"""DETERMINISTIC EVAL — the chat's status line says what the turn is doing.

While a reply was pending the chat always said "thinking…", and after 20s
added a note meant for developers ("slow models (free tiers especially) can
queue ... NAM_LLM_TIMEOUT"). Now it says "Reasoning" only when the model
streams its reasoning, names the tool that is running, and otherwise says it
is waiting for the model.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nam_agent.loop.models import OpenAICompatClient, _OpenAIStream

ROOT = Path(__file__).resolve().parents[2]
RENDER = ROOT / "nam_agent" / "server" / "static" / "js" / "render.js"


def _stream(deltas):
    client = OpenAICompatClient.__new__(OpenAICompatClient)   # skip __init__ (no network)
    chunks = [SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(
        tool_calls=None, **d))]) for d in deltas]
    client._call = lambda kwargs, **extra: iter(chunks)
    stream = _OpenAIStream(client, client._to_openai(model="m", messages=[], max_tokens=10))
    seen = []
    stream.on_reasoning = lambda: seen.append("reasoning")
    return stream, seen


def test_reasoning_is_signalled_once_and_never_shown():
    stream, seen = _stream([{"reasoning_content": "weigh the bids", "content": None},
                            {"reasoning_content": "then pick", "content": None},
                            {"content": "Pick vendor A."}])
    assert "".join(stream.text_stream) == "Pick vendor A."
    assert seen == ["reasoning"]


def test_no_reasoning_no_signal():
    stream, seen = _stream([{"content": "Hello."}])
    assert "".join(stream.text_stream) == "Hello."
    assert seen == []


def test_the_loop_announces_reasoning_and_the_running_tool():
    """One real loop turn: the model reasons, calls a tool, then answers."""
    from nam_agent.loop.agent import run_loop
    from nam_agent.tools.registry import Tool, ToolRegistry

    replies = iter([
        [{"reasoning_content": "check the KG", "content": None},
         {"content": None, "tool_calls": [SimpleNamespace(index=0, id="c1", function=SimpleNamespace(
             name="kg_find_tenders", arguments="{}"))]}],
        [{"content": "Two tenders."}],
    ])
    client = OpenAICompatClient.__new__(OpenAICompatClient)

    def fake_call(kwargs, **extra):
        return iter([SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(
            **{"tool_calls": None, **d}))]) for d in next(replies)])

    client._call = fake_call
    client._with_tools = lambda build, send: send(build())
    client.messages = SimpleNamespace(stream=client._stream)
    tools = ToolRegistry()
    tools.register(Tool("kg_find_tenders", "d", {"type": "object", "properties": {}},
                        lambda: "2 tenders"))
    kinds = []
    out = run_loop(client, "m", "s", [{"role": "user", "content": "gauges"}], tools,
                   observer=lambda kind, ev: kinds.append((kind, ev.get("tool"))), stream=True)
    assert out.reply == "Two tenders."
    order = [k for k in kinds if k[0] in ("reasoning", "tool_start", "tool", "text")]
    assert order == [("reasoning", None), ("tool_start", "kg_find_tenders"),
                     ("tool", "kg_find_tenders"), ("text", None)]


def test_the_page_shows_the_real_phase_not_the_developer_note():
    js = RENDER.read_text(encoding="utf-8")
    assert "NAM_LLM_TIMEOUT" not in js and "free tiers" not in js
    assert "thinking&hellip;" not in js
    assert 'ev.kind === "reasoning") pending.phase = "reasoning"' in js
    assert 'm.phase === "reasoning" ? "Reasoning"' in js
    assert 'ev.kind === "tool_start"' in js and '"Waiting for the model"' in js


def test_status_signals_stay_out_of_the_trace(tmp_path):
    from nam_agent.config import Settings
    from nam_agent.runtime.tracing import Tracer

    tracer = Tracer.__new__(Tracer)
    tracer.settings = Settings(home=tmp_path)
    written = []
    tracer._write = written.append
    for kind in ("reasoning", "tool_start", "text"):
        tracer.event(kind, {"tool": "x"})
    assert written == []
