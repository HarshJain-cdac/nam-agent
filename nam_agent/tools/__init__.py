"""The agent's tools: the officer's question (ask_options), the SAP and
knowledge-graph readers, and the PR Creation Flow's steps.

The SAP / KG tools and the flow's steps are registered only when NAM_KG_URL
points at the MCP server: a tool the model can see but never reach would be
tried, fail, and be narrated to the officer mid-flow.
"""

from __future__ import annotations

import sqlite3

from nam_agent.config import Settings
from nam_agent.tools import clarify
from nam_agent.tools.registry import ToolRegistry


def build_registry(conn: sqlite3.Connection, settings: Settings, memory=None) -> ToolRegistry:
    registry = ToolRegistry()
    # ask a fixed-choice question instead of guessing
    registry.register(clarify.make_tool())

    if getattr(settings, "kg_url", ""):
        from nam_agent.nam import kg_tools

        only = kg_tools.allowed(getattr(settings, "kg_tools", ""))
        kg_api = kg_tools.kg_api(getattr(settings, "kg_api_url", ""), settings.kg_url)
        for t in kg_tools.make_tools(settings.kg_url, only, kg_api):
            registry.register(t)

        # the PR Creation Flow (nam_agent/nam/flow.py): one tool per step, each
        # gate in code (nam_agent/nam/stage_tools.py). NAM_KG_TOOLS without
        # "nam" leaves them out.
        if only is None or "nam" in only:
            from nam_agent.nam import stage_tools

            for t in stage_tools.make_tools(conn, settings.home, settings.kg_url,
                                            kg=kg_tools.kg_on(only)):
                registry.register(t)
        if getattr(settings, "nam_chat", "") == "driver" and (
                only is None or "nam_capture_need" in only):
            # the earlier need capture (nam_agent/nam/capture.py), for the chat
            # driver's clients: a stated need -> its related past tenders
            from nam_agent.nam import capture as nam_capture
            from nam_agent.nam import llm as nam_llm

            ask = nam_llm.client_asker(getattr(memory, "client", None), settings, conn,
                                       "followup")
            registry.register(nam_capture.make_tool(conn, settings.home, settings.kg_url, ask))
    return registry
