"""DETERMINISTIC EVAL — NAM reads SAP alone unless the knowledge graph is
asked for, and the dashboard keeps how a turn was made in a closed dropdown."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from evals.helpers import make_agent
from nam_agent.config import Settings
from nam_agent.nam import case, chatstate, flow, kg_tools, stage_tools

ROOT = Path(__file__).resolve().parents[3]
URL = "http://127.0.0.1:9/mcp"


def test_the_default_gives_sap_search_and_the_nam_tools_only(monkeypatch, tmp_path):
    monkeypatch.delenv("NAM_KG_TOOLS", raising=False)
    assert Settings(home=tmp_path).kg_tools == "sap_search,nam"
    agent = make_agent(tmp_path, kg_url=URL, kg_tools="sap_search,nam")
    names = {t["name"] for t in agent.tools.schemas()}
    assert "sap_search" in names and "nam_need" in names
    assert not any(n.startswith("kg_") for n in names)
    assert kg_tools.allowed("all") is None and kg_tools.kg_on(None)
    assert not kg_tools.kg_on({"sap_search", "nam"})


def test_with_sap_alone_the_guide_and_the_tools_never_point_to_the_kg(tmp_path):
    g = flow.guide(None, kg=False)
    assert "One source: SAP" in g and "kg_tender_record" not in g
    assert "kg_tender_record" in flow.guide(None, kg=True)
    from evals.deterministic.nam.test_pr_flow import _kg

    conn = sqlite3.connect(":memory:")
    call, _ = _kg()
    t = {x.name: x for x in stage_tools.make_tools(conn, tmp_path, URL, call, kg=False)}
    assert "kg_tender_record" not in t["nam_tenders"].description
    token = chatstate.enter("c")
    try:
        t["nam_need"].fn(need="temperature gauge", category="goods")
        out = t["nam_tenders"].fn()
    finally:
        chatstate.leave(token)
    assert "PUR-PRJNUM-1008552" in out and "kg_tender_record" not in out
    assert case.for_session(conn, "c") is not None


def test_how_a_turn_was_made_is_a_dropdown_that_starts_closed():
    js = (ROOT / "nam_agent" / "server" / "static" / "js" / "render.js").read_text()
    card = js[js.index("const chatTurnCard"):js.index("// While a turn runs")]
    assert "turnDetails(t," in card and "toolRow" in card
    # the stages and tools are only inside the dropdown, and it opens only on a click
    assert card.index("turnDetails(t,") < card.index("toolRow")
    details = js[js.index("const turnDetails"):js.index("const toolCount")]
    assert re.search(r'TURN_OPEN\.has\(t\._turn\) \? " open" : ""', details)
