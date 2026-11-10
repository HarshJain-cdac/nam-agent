"""A vLLM server started without --tool-call-parser returns the model's tool
call as plain text with `tool_calls` empty. NAM harness must still run the call,
instead of ending the turn and showing the user only the model's reasoning."""

from types import SimpleNamespace

from nam_agent.loop.models import OpenAICompatClient, _OpenAIStream, recover_text_tool_calls

TOOLS = {"kg_find_tenders"}

# verbatim shape of a qwen3.8-flash-next reply from an unparsed vLLM endpoint
QWEN_XML = (
    "Need use tool kg_find_tenders query. Likely call with \"gaskets\".\n</think>\n\n"
    "<tool_call>\n<function=kg_find_tenders>\n<parameter=query>\ngaskets\n</parameter>\n"
    "<parameter=limit>\n5\n</parameter>\n</function>\n</tool_call>"
)


def test_qwen_xml_call_is_recovered_and_reasoning_dropped():
    text, calls = recover_text_tool_calls(QWEN_XML, TOOLS)
    assert calls == [{"name": "kg_find_tenders", "input": {"query": "gaskets", "limit": 5}}]
    assert text == ""


def test_hermes_json_call_is_recovered():
    raw = 'Sure.<tool_call>\n{"name": "kg_find_tenders", "arguments": {"query": "valves"}}\n</tool_call>'
    text, calls = recover_text_tool_calls(raw, TOOLS)
    assert calls == [{"name": "kg_find_tenders", "input": {"query": "valves"}}]
    assert text == "Sure."


def test_unknown_tool_and_plain_prose_are_left_alone():
    raw = "<tool_call>\n<function=rm_rf>\n</function>\n</tool_call>"
    assert recover_text_tool_calls(raw, TOOLS) == (raw, [])
    assert recover_text_tool_calls("thinking...\n</think>\nNo tenders found.", TOOLS) == (
        "No tenders found.", [])


def _tool_defs():
    return [{"name": "kg_find_tenders", "description": "d",
             "input_schema": {"type": "object", "properties": {}}}]


def test_create_turns_a_text_call_into_tool_use():
    client = OpenAICompatClient.__new__(OpenAICompatClient)   # skip __init__ (no network)
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=QWEN_XML, tool_calls=None))], usage=None)
    client._call = lambda kwargs, **extra: reply
    out = client._create(model="m", messages=[{"role": "user", "content": "hi"}],
                         max_tokens=10, tools=_tool_defs())
    assert out.stop_reason == "tool_use"
    [call] = [b for b in out.content if b.type == "tool_use"]
    assert call.name == "kg_find_tenders" and call.input["query"] == "gaskets"
    assert not [b for b in out.content if b.type == "text"]


def test_stream_hides_call_markup_and_returns_tool_use():
    client = OpenAICompatClient.__new__(OpenAICompatClient)
    pieces = ["done.\n</think>\n", "<tool_call>\n<function=kg_find_tenders>\n",
              "<parameter=query>\ngaskets\n</parameter>\n</function>\n</tool_call>"]
    chunks = [SimpleNamespace(usage=None, choices=[SimpleNamespace(
        delta=SimpleNamespace(content=p, tool_calls=None))]) for p in pieces]
    client._call = lambda kwargs, **extra: iter(chunks)
    kwargs = client._to_openai(model="m", messages=[], max_tokens=10, tools=_tool_defs())
    stream = _OpenAIStream(client, kwargs)
    shown = "".join(stream.text_stream)
    assert "<tool_call>" not in shown
    final = stream.get_final_message()
    assert final.stop_reason == "tool_use"
    assert [b.name for b in final.content if b.type == "tool_use"] == ["kg_find_tenders"]


def test_stream_that_lost_the_call_is_retried_without_streaming():
    """This vLLM build counts the call tokens in a stream but never sends them:
    the stream ends with reasoning and '</think>' only. Retry non-streaming."""
    client = OpenAICompatClient.__new__(OpenAICompatClient)
    chunks = [SimpleNamespace(usage=None, choices=[SimpleNamespace(
        delta=SimpleNamespace(content="Use kg_find_tenders.\n</think>\n\n", tool_calls=None))])]
    unstreamed = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=QWEN_XML, tool_calls=None))], usage=None)
    calls = []

    def fake_call(kwargs, **extra):
        calls.append(extra.get("stream", False))
        return iter(chunks) if extra.get("stream") else unstreamed

    client._call = fake_call
    kwargs = client._to_openai(model="m", messages=[], max_tokens=10, tools=_tool_defs())
    stream = _OpenAIStream(client, kwargs)
    list(stream.text_stream)
    final = stream.get_final_message()
    assert calls == [True, False]
    assert final.stop_reason == "tool_use"
    assert [b.input["query"] for b in final.content if b.type == "tool_use"] == ["gaskets"]


# -- a server that refuses `tools`, and a model the server does not serve ------------

class _Refuses:
    """chat.completions.create that 400s on `tools` (vLLM without
    --enable-auto-tool-choice) and 404s on an unknown model."""

    def __init__(self, reply_text="", served=("main",)):
        self.calls, self.reply_text, self.served = [], reply_text, served

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["model"] not in self.served:
            raise RuntimeError(f"Error code: 404 - The model `{kwargs['model']}` does not exist.")
        if "tools" in kwargs:
            raise RuntimeError('Error code: 400 - "auto" tool choice requires '
                               '--enable-auto-tool-choice and --tool-call-parser to be set')
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content=self.reply_text, tool_calls=None), finish_reason="stop")], usage=None)


def _client(fake, fallback="main"):
    client = OpenAICompatClient.__new__(OpenAICompatClient)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    client.fallback_model = fallback
    return client


def test_a_server_that_refuses_tools_gets_them_in_the_prompt_once():
    fake = _Refuses('<tool_call>{"name": "kg_find_tenders", "arguments": {"query": "gaskets"}}'
                    "</tool_call>")
    client = _client(fake)
    out = client._create(model="main", messages=[{"role": "user", "content": "find gaskets"}],
                         max_tokens=10, system="You are NAM harness.", tools=_tool_defs())
    [call] = [b for b in out.content if b.type == "tool_use"]
    assert call.name == "kg_find_tenders" and call.input == {"query": "gaskets"}
    assert client.text_tools is True and len(fake.calls) == 2
    sent = fake.calls[1]
    assert "tools" not in sent
    system = sent["messages"][0]["content"]
    assert system.startswith("Tools.") and "kg_find_tenders(" in system   # stable prefix first
    assert system.endswith("You are NAM harness.")
    client._create(model="main", messages=[{"role": "user", "content": "again"}],
                   max_tokens=10, tools=_tool_defs())
    assert len(fake.calls) == 3                       # remembered: no second refusal


def test_text_mode_carries_earlier_calls_and_results_as_text():
    client = _client(_Refuses("done"))
    client.text_tools = True
    call = SimpleNamespace(type="tool_use", id="c1", name="kg_find_tenders",
                           input={"query": "x"})
    kwargs = client._to_openai(model="main", max_tokens=5, tools=_tool_defs(), messages=[
        {"role": "user", "content": "find x"},
        {"role": "assistant", "content": [call]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1",
                                      "content": "3 tenders"}]}])
    roles = [m["role"] for m in kwargs["messages"]]
    assert roles == ["system", "user", "assistant", "user"] and "tools" not in kwargs
    assert '"name": "kg_find_tenders"' in kwargs["messages"][2]["content"]
    assert kwargs["messages"][3]["content"] == "<tool_result>3 tenders</tool_result>"


def test_a_model_the_server_does_not_serve_falls_back_to_the_main_model():
    fake = _Refuses("ok")
    client = _client(fake)
    out = client._create(model="small-missing", messages=[{"role": "user", "content": "hi"}],
                         max_tokens=5)
    assert out.content[0].text == "ok"
    assert [c["model"] for c in fake.calls] == ["small-missing", "main"]
    client._create(model="small-missing", messages=[{"role": "user", "content": "hi"}],
                   max_tokens=5)
    assert fake.calls[-1]["model"] == "main" and len(fake.calls) == 3   # remembered


def test_without_a_fallback_the_404_is_raised():
    import pytest

    client = _client(_Refuses("ok"), fallback=None)
    with pytest.raises(RuntimeError, match="does not exist"):
        client._create(model="small-missing", messages=[{"role": "user", "content": "hi"}],
                       max_tokens=5)
