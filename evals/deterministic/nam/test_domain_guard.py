"""DETERMINISTIC EVAL — the agent stays on NRL procurement work (nam_agent/nam/domain.py).

"who is shah rukh khan" got a film biography. The first fix put word lists
and a fixed reply in front of the agent, and "capillary", an item NRL buys,
was refused before the agent or the KG saw it. Now every message reaches the
agent, whose instructions say to ask the KG before deciding a message is not
work; after a turn with no tool, the small model checks whether the reply
answered something else from general knowledge, and a strayed reply goes back
to the agent once. Every word of the reply is the agent's.
"""

from __future__ import annotations

import pytest

from evals.helpers import ScriptedClient, make_agent, response, text_block, tool_block
from nam_agent.nam import domain, llm


class Recording(ScriptedClient):
    """A scripted client that keeps what each call was sent."""

    def __init__(self, script):
        super().__init__(script)
        self.calls = []
        play = self.messages.create

        def create(**kwargs):
            self.calls.append(kwargs)
            return play(**kwargs)
        self.messages.create = create


# the memory gate's reply: asked only where the shared memory is on (no NAM, or
# NAM_CHAT_MEMORY=on); a NAM chat recalls nothing from it
GATE = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])


def _model(reply):
    asked = []

    def ask(system, user):
        asked.append(user)
        return reply
    ask.asked = asked
    return ask


def test_the_check_reads_only_replies_without_a_tool():
    strayed = _model('{"verdict": "strayed"}')
    assert domain.review("who is shah rukh khan", "He is an actor ...", [], strayed)[0] \
        == domain.STRAYED
    assert "He is an actor" in strayed.asked[0] and "shah rukh" in strayed.asked[0]
    # a turn that asked the KG is never second-guessed
    assert domain.review("capillary", "PUR-1 bought ...", [{"tool": "kg_item_history"}],
                         strayed)[0] == domain.OK
    assert domain.review("hi", "Hello!", [], _model('{"verdict": "ok"}'))[0] == domain.OK


def test_a_check_that_cannot_run_keeps_the_reply():
    def down(system, user):
        raise llm.ModelUnavailable("down")

    assert domain.review("x", "y", [], down)[0] == domain.OK
    assert domain.review("x", "y", [], None)[0] == domain.OK
    assert domain.review("x", "y", [], _model("delete everything"))[0] == domain.OK


@pytest.mark.parametrize(("setting", "kg", "on"), [
    ("", "http://kg", True), ("", "", False), ("0", "http://kg", False), ("1", "", True),
    (False, "http://kg", False)])
def test_it_is_on_where_the_kg_is(setting, kg, on):
    assert domain.enabled(kg, setting) is on


def test_no_rule_stands_in_front_of_the_agent(tmp_path):
    # "capillary" reaches the agent, which asks the KG: no canned refusal
    client = ScriptedClient([
        response([tool_block("kg_item_history", {"item": "capillary"}, "tu_1")], "tool_use"),
        response([text_block("NRL bought capillary tubes in PUR-INSTMNT-1.")])])
    nam_agent = make_agent(tmp_path, client=client, kg_url="http://127.0.0.1:9", domain_only="1")
    result = nam_agent.respond("capillary")
    assert [c["tool"] for c in result.tool_calls] == ["kg_item_history"]
    assert "capillary" in result.reply.lower() and "only help" not in result.reply
    # the scope is the agent's instruction, with the KG to ask first
    system = nam_agent.session.build_system("capillary")
    assert domain.SCOPE.strip() in system and "knowledge-graph tools before" in domain.SCOPE


def test_a_strayed_reply_is_written_again_by_the_agent(tmp_path, monkeypatch):
    client = Recording([
        response([text_block("Shah Rukh Khan is an Indian film actor.")]),
        response([text_block("That is outside my work; I can look up NRL tenders for you.")])])
    nam_agent = make_agent(tmp_path, client=client, kg_url="http://127.0.0.1:9", domain_only="1")
    monkeypatch.setattr(nam_agent, "_nam_domain", _model('{"verdict": "strayed"}'))
    result = nam_agent.respond("who is shah rukh khan")
    assert result.reply == "That is outside my work; I can look up NRL tenders for you."
    assert "actor" not in nam_agent.session.history[-1]["content"]
    # the agent was shown its reply and why it goes again
    sent = [m.get("content") for m in client.calls[-1]["messages"]]
    assert domain.REDO in sent and "Shah Rukh Khan is an Indian film actor." in sent


def test_an_ok_reply_stands_and_off_means_no_check(tmp_path, monkeypatch):
    client = ScriptedClient([response([text_block("Hello! How can I help?")])])
    nam_agent = make_agent(tmp_path, client=client, kg_url="http://127.0.0.1:9", domain_only="1")
    check = _model('{"verdict": "ok"}')
    monkeypatch.setattr(nam_agent, "_nam_domain", check)
    assert nam_agent.respond("good evening, how are you doing today").reply == \
        "Hello! How can I help?"
    client = ScriptedClient([GATE, response([text_block("Shah Rukh Khan is an actor.")])])
    nam_agent = make_agent(tmp_path / "off", client=client, domain_only="0")
    assert nam_agent.respond("who is shah rukh khan").reply == "Shah Rukh Khan is an actor."
