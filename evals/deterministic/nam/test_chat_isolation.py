"""DETERMINISTIC EVAL — one officer's chat never reaches another's.

Where NAM runs, many officers use one server: each chat is its own (the
client keeps its chat id, or the harness makes one), nothing of a chat feeds
the shared memory, and no turn recalls from it. A question names which of the
chosen materials it narrows.
"""

from __future__ import annotations

import re
from pathlib import Path

from evals.helpers import ScriptedClient, make_agent, response, text_block
from nam_agent.nam import case, chatstate

ROOT = Path(__file__).resolve().parents[3]


def test_shared_memory_is_off_where_nam_runs_unless_asked_for(tmp_path):
    nam = make_agent(tmp_path / "a", kg_url="http://kg/mcp")
    assert not chatstate.shared_memory(nam.settings)
    on = make_agent(tmp_path / "b", kg_url="http://kg/mcp", nam_chat_memory="on")
    assert chatstate.shared_memory(on.settings)
    plain = make_agent(tmp_path / "c")
    assert chatstate.shared_memory(plain.settings)


def test_a_nam_chat_neither_recalls_nor_feeds_the_shared_memory(tmp_path):
    client = ScriptedClient([response([text_block("Noted.")])] * 8)
    agent = make_agent(tmp_path, client=client, kg_url="http://127.0.0.1:9/mcp",
                       consolidate_every=1)
    agent.memory.facts.add("officer", "The other officer's plant is 2200.")
    seen = []
    for msg in ("hello", "what plant do I use?"):
        agent.respond(msg, observer=lambda k, e: seen.append(k))
    assert "gate" not in seen and "consolidation" not in seen       # no recall, no feeding
    system = agent.session.build_system("what plant do I use?")
    assert "2200" not in system
    rows = agent.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    assert rows == 1                                                 # nothing new learnt


def test_a_caller_without_a_chat_id_gets_its_own_new_chat():
    one, two = chatstate.new_chat_id(), chatstate.new_chat_id()
    assert re.fullmatch(r"nam-chat-\d{8}-[0-9a-f]{8}", one) and one != two
    src = (ROOT / "nam_agent" / "server" / "chat.py").read_text()
    stream = src[src.index("def chat_stream("):src.index("def chat(")]
    assert "chatstate.new_chat_id()" in stream and '"session_id": session_id' in stream


def test_the_page_keeps_its_own_chat_and_lists_only_its_chats():
    js = {f.name: f.read_text() for f in (ROOT / "nam_agent" / "server" / "static" / "js").glob("*.js")}
    assert 'let SESSION = store.get("nam-chat") || "";' in js["dock.js"]
    # the history asks the server only for the chats this browser started
    assert '{action: "list", ids: myChats()}' in js["dock.js"]
    assert "session_id:SESSION" in js["render.js"] and "rememberChat(ev.session_id)" in js["render.js"]
    assert "if (SESSION) loadChat(SESSION)" in js["main.js"]


def test_a_question_names_which_of_the_chosen_materials_it_narrows(tmp_path):
    from evals.deterministic.nam.test_cart_flow import SAP, _kg
    import sqlite3

    from nam_agent.nam import stage_tools

    conn = sqlite3.connect(":memory:")
    call, _ = _kg(SAP)
    t = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, "http://x/mcp", call)}
    shown = []
    token = chatstate.enter("c")
    try:
        t["nam_need"].fn(need="job", items=["gasket", "valve", "thermowell"], category="goods")
        t["nam_choose"].fn(items=["All of them"])
        t["nam_ask"].fn(item="valve", question="Which body?", options=[
            {"label": "CS", "words": ["cs"]}, {"label": "SS", "words": ["ss"]}],
            _notify=lambda k, e: shown.append(e))
    finally:
        chatstate.leave(token)
    assert shown[-1]["progress"] == {"n": 2, "of": 3, "item": "valve"}
    assert case.progress(case.for_session(conn, "c"), {"name": "nope"}) is None
    js = (ROOT / "nam_agent" / "server" / "static" / "js" / "render.js").read_text()
    assert "Material ${pg.n} of ${pg.of}" in js
