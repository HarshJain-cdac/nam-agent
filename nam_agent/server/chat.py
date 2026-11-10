"""A chat turn, and the chats: who owns each, its history, the list of them.

A chat is a session id on chat_log rows. The caller keeps its id and sends it
with every message; a caller that sends none gets a new one, returned in the
reply. When the request carries the identity headers (X-NAM-Tenant,
X-NAM-User, X-NAM-Roles, set by the backend's auth component), the chat is
that user's: its first turn records the owner, and no other user can send to
it, read it or list it. A chat begun without identity stays open to callers
without identity only, so a user's chat is never readable by an anonymous
caller who learns its id.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from nam_agent.server.agent import agent_lock, get_agent

CASE_ID = re.compile(r"NAM-\d{4}-\d{4,}")
CHAT_ID = re.compile(r"[A-Za-z0-9._:-]{1,80}")


class ChatRefused(Exception):
    """The chat is not the caller's (HTTP 403), or its id is malformed (400)."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


# -- owners ------------------------------------------------------------------

def _owners(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_owner (
        session_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, user TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now')))""")


def owner_of(conn, session_id: str) -> tuple[str, str] | None:
    _owners(conn)
    row = conn.execute("SELECT tenant, user FROM chat_owner WHERE session_id=?",
                       (session_id,)).fetchone()
    return (row[0], row[1]) if row else None


def _who(actor) -> tuple[str, str] | None:
    return (actor.tenant, actor.user) if actor is not None else None


def check(conn, session_id: str, actor, *, claim: bool = False) -> None:
    """Refuse a chat that is not the caller's. With claim, a chat no one owns
    yet becomes the caller's (its first turn)."""
    if not CHAT_ID.fullmatch(session_id or ""):
        raise ChatRefused("session_id is malformed: letters, digits and ._:- only, "
                          "at most 80", 400)
    owner, who = owner_of(conn, session_id), _who(actor)
    if owner is None:
        if who is None:
            return
        has_rows = conn.execute("SELECT 1 FROM chat_log WHERE session_id=? LIMIT 1",
                                (session_id,)).fetchone()
        if has_rows and not claim:
            raise ChatRefused("this chat is not yours")
        if claim:
            conn.execute("INSERT OR IGNORE INTO chat_owner (session_id, tenant, user) "
                         "VALUES (?, ?, ?)", (session_id, *who))
            conn.commit()
            if owner_of(conn, session_id) != who:
                raise ChatRefused("this chat is not yours")
        return
    if owner != who:
        raise ChatRefused("this chat is not yours")


# -- a turn ------------------------------------------------------------------

def case_focus(payload: dict) -> dict | None:
    """The NAM case a screen has open beside the chat ({case_id, screen}):
    the agent is told which case "this" is. Only a real case id is taken."""
    case_id = str(payload.get("case_id") or "").strip()
    if not CASE_ID.fullmatch(case_id):
        return None
    return {"case_id": case_id, "screen": str(payload.get("screen") or "")[:60]}


def _focus_with_case(agent, focus: dict | None) -> dict | None:
    if not focus:
        return None
    from nam_agent.nam import journey

    return journey.focus(agent.conn, focus["case_id"], focus.get("screen") or "")


def tool_status(output: str) -> str:
    """ok / warn / error for a tool's result, from its own words."""
    low = (output or "").lower()
    if "failed" in low or "timed out" in low or low.startswith("error"):
        return "error"
    if "already exists" in low or "not synced" in low or "skipped" in low:
        return "warn"
    return "ok"


def chat_stream(message: str, emit, session_id: str | None = None,
                focus: dict | None = None, actor=None, source: str = "api") -> None:
    """Run one turn, calling emit(kind, event) for each event as it happens
    (text, tool, nam_cards, ask_options, ...), and last a "done" event with
    the result and the chat's session_id. Raises ChatRefused before the turn
    when the chat is not the caller's."""
    with agent_lock:
        agent = get_agent()
        if not session_id:
            from nam_agent.nam import chatstate

            session_id = chatstate.new_chat_id()
        check(agent.conn, session_id, actor, claim=True)
        if agent.session.session_id != session_id:
            agent.session.switch(session_id)
        start = datetime.now(UTC)
        agent.session.focus = _focus_with_case(agent, focus)
        try:
            result = agent.respond(message, observer=emit, source=source, stream=True,
                                   actor=actor)
        finally:
            agent.session.focus = None
        latency_ms = int((datetime.now(UTC) - start).total_seconds() * 1000)
        case_id = chat_case_id(agent.conn, session_id, actor)
    emit("done", {
        "session_id": session_id,
        "case_id": case_id,
        "reply": result.reply,
        "tools": [{"tool": c["tool"], "args": c["args"], "output": c["output"],
                   "status": tool_status(c["output"]),
                   "summary": (c["output"] or "").split(". ")[0][:120]}
                  for c in result.tool_calls],
        "iterations": result.iterations,
        "latency_ms": latency_ms,
        "model": agent.settings.model,
    })


def chat_case(conn, session_id: str, actor) -> dict | None:
    """The NAM case the chat opened (the one it touched last), as the caller's
    tenant sees it, or None."""
    from contextlib import nullcontext

    from nam_agent.nam import case as nam_case

    with nam_case.acting(actor) if actor is not None else nullcontext():
        try:
            return nam_case.for_session(conn, session_id)
        except nam_case.NotYours:
            return None


def chat_case_id(conn, session_id: str, actor) -> str | None:
    try:
        data = chat_case(conn, session_id, actor)
    except Exception:  # noqa: BLE001 - the reply stands without it
        return None
    return data["case_id"] if data else None


def chat(message: str, session_id: str | None = None, focus: dict | None = None,
         actor=None) -> dict:
    """One turn as one JSON result: the "done" event, with the turn's cards
    and its question (ask_options) gathered in, for a caller without SSE."""
    final: dict = {}

    def collect(kind: str, ev: dict) -> None:
        if kind == "nam_cards":
            final.setdefault("cards", []).append(ev)
        elif kind in ("ask_options", "ask_field", "flow_complete", "nit"):
            final[kind] = ev
        elif kind == "done":
            final.update(ev)

    chat_stream(message, collect, session_id=session_id, focus=focus, actor=actor)
    return final


# -- the chats ---------------------------------------------------------------

def _shown_meta(raw) -> dict | None:
    if not raw:
        return None
    meta = json.loads(raw)
    meta.pop("tools_note", None)
    return meta


def history(conn, session_id: str, actor) -> list[dict]:
    """A chat's messages, oldest first, each with its turn's meta (cards,
    question, tools), the model's tool note left out."""
    from nam_agent.runtime.session import strip_tools_note

    check(conn, session_id, actor)
    rows = conn.execute("SELECT role, content, meta FROM chat_log WHERE session_id=? "
                        "ORDER BY id", (session_id,)).fetchall()
    return [{"role": r["role"],
             "content": strip_tools_note(r["content"]) if r["role"] == "assistant"
             else r["content"],
             "meta": _shown_meta(r["meta"])} for r in rows]


def chats(conn, actor, ids: list[str] | None = None) -> list[dict]:
    """The caller's chats, newest first: with identity, the chats that user
    owns; without, only the ids it names that no one owns."""
    _owners(conn)
    who = _who(actor)
    if who is not None:
        found = [r[0] for r in conn.execute(
            "SELECT session_id FROM chat_owner WHERE tenant=? AND user=?", who).fetchall()]
        if ids is not None:
            found = [s for s in found if s in set(ids)]
    else:
        found = [s for s in (ids or []) if isinstance(s, str) and CHAT_ID.fullmatch(s)
                 and owner_of(conn, s) is None]
    out = []
    for sid in dict.fromkeys(found):
        g = conn.execute("SELECT COUNT(*) AS n, MAX(created_at) AS last_at FROM chat_log "
                         "WHERE session_id=?", (sid,)).fetchone()
        if not g["n"]:
            continue
        first = conn.execute("SELECT content FROM chat_log WHERE session_id=? AND role='user' "
                             "ORDER BY id LIMIT 1", (sid,)).fetchone()
        out.append({"id": sid, "title": first["content"][:60] if first else "(empty)",
                    "messages": g["n"], "last_at": g["last_at"]})
    out.sort(key=lambda s: s["last_at"] or "", reverse=True)
    return out


def session_action(payload: dict, actor=None) -> dict:
    """POST /api/session: {"action": "new"} a new chat id; {"action":
    "history", "id"} one chat's messages; {"action": "case", "id"} the chat's
    NAM case; {"action": "list", "ids"?} the caller's chats. Raises
    ChatRefused for a chat that is not the caller's."""
    from nam_agent.nam import chatstate

    action = payload.get("action")
    if action == "new":
        return {"ok": True, "session_id": chatstate.new_chat_id(), "history": []}
    with agent_lock:
        conn = get_agent().conn
        if action in ("history", "switch"):
            sid = str(payload.get("id") or "")
            return {"ok": True, "session_id": sid, "history": history(conn, sid, actor)}
        if action == "case":
            # the chat's NAM case with its cart node, or null before one is opened
            sid = str(payload.get("id") or "")
            check(conn, sid, actor)
            data = chat_case(conn, sid, actor)
            if data is not None:
                from nam_agent.nam import cart

                data = {**data, "node": cart.node_data(data)}
            return {"ok": True, "session_id": sid, "case": data}
        if action == "list":
            ids = payload.get("ids")
            return {"ok": True, "chats": chats(conn, actor,
                                               ids if isinstance(ids, list) else None)}
    return {"error": f"unknown action {action!r}: new, history, case or list"}
