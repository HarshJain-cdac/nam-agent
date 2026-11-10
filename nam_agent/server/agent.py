"""The one agent the server's chat turns run on.

It is built on the first chat and reused, behind `agent_lock`, so turns run
one at a time: the SQLite connection is shared by the server's threads. Each
turn switches the agent to the caller's own chat (Session.switch) first, so
one chat's history never reaches another.
"""

from __future__ import annotations

import threading

from nam_agent.config import load_settings
from nam_agent.db import connect

_agent = None
agent_lock = threading.Lock()


def get_agent():
    """The shared agent, built on first use. Call it holding agent_lock."""
    global _agent
    if _agent is None:
        from nam_agent.app import NamAgent
        from nam_agent.nam import chatstate

        settings = load_settings()
        settings.ensure_home()
        conn = connect(settings.home, check_same_thread=False)
        _agent = NamAgent(settings=settings, conn=conn)
        # never a shared thread: a caller that names no chat gets a new one
        _agent.session.session_id = chatstate.new_chat_id()
    return _agent


def current():
    """The shared agent, or None when no chat has run yet."""
    return _agent
