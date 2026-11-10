"""What a chat is in the middle of, kept in state.db so it survives a restart.

One row per chat thread (session_id), with a few named slots:

  case       the NAM case this chat is working on: set when the chat opens a
             case, or when the officer says "resume NAM-2026-0012". It
             outranks "the case this chat touched last" (drive.session_case),
             so starting a second case and going back to the first is exact.
  awaiting   the question the harness asked last in this chat (fill.py), so
             the next message is read as its answer. It used to live in the
             process (NamAgent._nam_awaiting) and was lost on every restart.
  switch     a new need stated while a case is still going: the officer is
             asked whether to start a new case or carry on, and this holds
             the item until they answer (drive.turn).
  sap        need capture on SAP's lines (NAM_SOURCE=sap): the need, the
             materials still in and the open question (sap_need.py).

Nothing here is the case itself: the case lives in nam_cases and the harness
decides everything about it. This is only the chat's place in the flow.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, MutableMapping
from datetime import UTC, datetime

_TABLE = ("CREATE TABLE IF NOT EXISTS nam_chat_state (session_id TEXT NOT NULL, "
          "slot TEXT NOT NULL, value_json TEXT NOT NULL, updated_at TEXT NOT NULL, "
          "PRIMARY KEY (session_id, slot))")
SLOTS = ("case", "awaiting", "switch", "sap")


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE)


def get(conn: sqlite3.Connection, session_id: str, slot: str):
    """The slot's value, or None (no row, no table, or a bad row)."""
    if not session_id:
        return None
    try:
        _ensure(conn)
        row = conn.execute("SELECT value_json FROM nam_chat_state WHERE session_id = ? "
                           "AND slot = ?", (session_id, slot)).fetchone()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError):
        return None


def put(conn: sqlite3.Connection, session_id: str, slot: str, value) -> None:
    """Set the slot; None clears it."""
    if slot not in SLOTS:
        raise ValueError(f"no chat slot '{slot}'")
    if not session_id:
        return
    _ensure(conn)
    if value is None:
        conn.execute("DELETE FROM nam_chat_state WHERE session_id = ? AND slot = ?",
                     (session_id, slot))
    else:
        conn.execute("INSERT INTO nam_chat_state (session_id, slot, value_json, updated_at) "
                     "VALUES (?, ?, ?, ?) ON CONFLICT (session_id, slot) DO UPDATE SET "
                     "value_json = excluded.value_json, updated_at = excluded.updated_at",
                     (session_id, slot, json.dumps(value, default=str),
                      datetime.now(UTC).isoformat()))
    conn.commit()


class Awaiting(MutableMapping):
    """The `awaiting` slot of every chat as a {session_id: question} mapping,
    the shape drive.turn takes (tests pass a plain dict)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __getitem__(self, session_id: str):
        value = get(self.conn, session_id, "awaiting")
        if value is None:
            raise KeyError(session_id)
        return value

    def __setitem__(self, session_id: str, value) -> None:
        put(self.conn, session_id, "awaiting", value)

    def __delitem__(self, session_id: str) -> None:
        if get(self.conn, session_id, "awaiting") is None:
            raise KeyError(session_id)
        put(self.conn, session_id, "awaiting", None)

    def __iter__(self) -> Iterator[str]:
        try:
            _ensure(self.conn)
            rows = self.conn.execute("SELECT session_id FROM nam_chat_state WHERE "
                                     "slot = 'awaiting'").fetchall()
        except sqlite3.Error:
            rows = []
        return iter([r[0] for r in rows])

    def __len__(self) -> int:
        return sum(1 for _ in self)


# -- the chat a turn runs in, and what the PR Creation Flow asked in it ----------
#
# NAM harness sets these for each turn (NamAgent.respond: enter, then leave); the NAM
# tools read them: the chat whose case they work on (current), the question
# nam_ask or nam_choose showed this turn (asked), and whether the chat has a
# case still being worked (case_open).

from contextvars import ContextVar  # noqa: E402 - kept with what it serves

_CURRENT: ContextVar[str] = ContextVar("nam_chat_session", default="")
# the question nam_ask showed this turn, so a second ask in the same turn
# (the generic ask_options) does not replace its checked options
_ASKED: ContextVar[str] = ContextVar("nam_asked", default="")
# whether the chat has a NAM case still being worked (set per turn by NamAgent)
_OPEN: ContextVar[bool] = ContextVar("nam_case_open", default=False)


def current() -> str:
    """The chat the running turn belongs to, or ""."""
    return _CURRENT.get()


# the officer's message this turn, so a tool can name it back to the agent
_MESSAGE: ContextVar[str] = ContextVar("nam_message", default="")


def enter(session_id: str, message: str = ""):
    """Set the turn's chat (and the officer's message); returns the token for
    leave()."""
    return (_CURRENT.set(session_id or ""), _ASKED.set(""), _OPEN.set(False),
            _MESSAGE.set(message or ""), _CLICKED.set(""), _DONE.set(()))


def leave(token) -> None:
    current_token, asked_token, open_token, message_token, clicked_token, done_token = token
    _DONE.reset(done_token)
    _CLICKED.reset(clicked_token)
    _MESSAGE.reset(message_token)
    _OPEN.reset(open_token)
    _ASKED.reset(asked_token)
    _CURRENT.reset(current_token)


def set_case_open(value: bool) -> None:
    _OPEN.set(bool(value))


def case_open() -> bool:
    """True while the turn's chat has a NAM case not yet at its PR."""
    return _OPEN.get()


def mark_asked(question: str) -> None:
    _ASKED.set(question)


def asked() -> str:
    """The question nam_ask showed in this turn, or ""."""
    return _ASKED.get()


def message() -> str:
    """The officer's message this turn, or ""."""
    return _MESSAGE.get()


# what the turn shows the officer beside the reply: cards (the records, the
# PUR cases, a PR) a tool built from the case, sent to the page as they come
_EMIT: ContextVar = ContextVar("nam_emit", default=None)


def set_emitter(fn):
    """The turn's event sink (NamAgent.respond); returns the token to reset."""
    return _EMIT.set(fn)


def reset_emitter(token) -> None:
    _EMIT.reset(token)


def emit(kind: str, event: dict) -> None:
    """Send one event to the page, when a turn is listening."""
    fn = _EMIT.get()
    if fn is not None:
        fn(kind, event)


# the officer's click the harness already read this turn (stage_tools.read_click),
# so the agent does not read the same answer a second time
_CLICKED: ContextVar[str] = ContextVar("nam_clicked", default="")


def mark_clicked(tool: str) -> None:
    _CLICKED.set(tool)


def clicked() -> str:
    """The tool that read the officer's click this turn, or ""."""
    return _CLICKED.get()


def shared_memory(settings) -> bool:
    """Whether chats feed the shared memory and turns recall from it
    (NAM_CHAT_MEMORY): off by default where NAM is set up, so one officer's
    chat never reaches another's."""
    value = getattr(settings, "nam_chat_memory", "") or ""
    if value in ("on", "1", "yes"):
        return True
    if value in ("off", "0", "no"):
        return False
    return not getattr(settings, "kg_url", "")


def new_chat_id() -> str:
    """A chat id for a caller that did not bring one: "nam-chat-<date>-<8 hex>"."""
    import secrets
    from datetime import datetime

    return f"nam-chat-{datetime.now():%Y%m%d}-{secrets.token_hex(4)}"


# what the NAM tools did in this turn ("answer", "need"), so a later tool in the
# same turn can tell whether the officer has seen its result yet
_DONE: ContextVar[tuple] = ContextVar("nam_done", default=())


def mark_done(act: str) -> None:
    _DONE.set(tuple(_DONE.get()) + (act,))


def done(act: str) -> bool:
    """True when a NAM tool did `act` in this turn."""
    return act in _DONE.get()


# the tool the harness itself is running for the officer's click (app
# _nam_click), so a tool can tell the officer's own choice from the agent's
_READING: ContextVar[str] = ContextVar("nam_reading", default="")


def reading_set(tool: str):
    return _READING.set(tool)


def reading_reset(token) -> None:
    _READING.reset(token)


def reading() -> str:
    """The tool the harness runs on the officer's click right now, or ""."""
    return _READING.get()
