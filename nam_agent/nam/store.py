"""The Need Record store: one versioned row per case, and every version kept.

    nam_cases          the current record of each case: its tenant, version,
                       stage and the need itself (record_json)
    nam_case_versions  one row per version: who wrote it, from which flow and
                       run, why, which fields changed, and the whole record as
                       it was then
    nam_counters       the case-number counter, one row per year

This replaces nam_needs as the one place a case lives. nam_agent/nam/need.py's
load() and save() read and write here, so the harness, the dashboard and the
chat tool all see the same record. nam_needs is kept as it was: its rows are
copied here the first time the store is opened, and nothing deletes it.

Writes are optimistic: a record carries the version it was read at, and a
save made against an older version is refused with stale_version (409,
retryable), never applied over the newer one.

Who writes is taken from the acting() context that nam_agent.nam.run() sets for
each call. Code outside a harness call (the dashboard's older routes, the
chat tool) writes as the tenant "local".
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from nam_agent.nam import guard
from nam_agent.nam.errors import NamError

LOCAL_TENANT = "local"
_NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS nam_cases (
    case_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    version INTEGER NOT NULL,
    stage TEXT NOT NULL DEFAULT 'S1',
    session_id TEXT NOT NULL DEFAULT '',
    record_json TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ({_NOW}),
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ({_NOW})
);
CREATE INDEX IF NOT EXISTS nam_cases_by_tenant ON nam_cases (tenant, updated_at);
CREATE TABLE IF NOT EXISTS nam_case_versions (
    case_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    at TEXT NOT NULL DEFAULT ({_NOW}),
    tenant TEXT NOT NULL,
    user TEXT NOT NULL,
    flow TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    changed_json TEXT NOT NULL DEFAULT '[]',
    record_json TEXT NOT NULL,
    PRIMARY KEY (case_id, version)
);
CREATE TABLE IF NOT EXISTS nam_counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS nam_action_results (
    action_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    result_json TEXT NOT NULL,
    at TEXT NOT NULL DEFAULT ({_NOW})
);
"""

# need.py's sub-stages -> the harness stage the case is in (plan section 3).
# Step 9's orchestrator owns stages; until then they follow need capture.
_STAGES = {"describe": "S1", "capture": "S1",
           "references": "S2", "matching": "S2", "selected": "S2"}


def stage_of(need_stage: str) -> str:
    return _STAGES.get(need_stage or "", "S1")


# -- who is writing -----------------------------------------------------------

@dataclass(frozen=True)
class Actor:
    tenant: str
    user: str
    flow: str = ""
    run_id: str = ""
    # The case version the caller last saw (request.case_version), for the
    # case it named: until this run writes to that case, reading any other
    # version of it is stale_version. It closes the gap between the check in
    # nam_agent.nam.run() and the flow's own read.
    case_id: str = ""
    seen: int | None = None
    # the caller's roles, for the tools that need one (nam_agent/nam/tools.py)
    roles: tuple[str, ...] = ()


# outside a harness call: the dashboard's older routes, the chat tool
_LOCAL = Actor(LOCAL_TENANT, "local")
_ACTOR: ContextVar[Actor | None] = ContextVar("nam_actor", default=None)


def current() -> Actor:
    return _ACTOR.get() or _LOCAL


@contextmanager
def acting(actor: Actor) -> Iterator[None]:
    """Every write inside is recorded as `actor`'s. Per thread and per call:
    concurrent harness calls do not see each other's actor."""
    token = _ACTOR.set(actor)
    try:
        yield
    finally:
        _ACTOR.reset(token)


# -- tables, migration, transactions ------------------------------------------

@contextmanager
def _write(conn: sqlite3.Connection) -> Iterator[None]:
    """One IMMEDIATE transaction: the write lock is taken before the first
    read, so a read-check-write inside cannot interleave with another."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def ensure(conn: sqlite3.Connection) -> None:
    """Create or migrate these tables, once per database file per process
    (nam_agent.db.once): each call re-running the schema took the write lock."""
    from nam_agent import db

    db.once(conn, "nam.store", _setup, table="nam_cases")


def _setup(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _migrate(conn)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def _migrate(conn: sqlite3.Connection) -> None:
    """Copy every nam_needs row not yet here, as version 1 of its case.

    record_json is copied as it is, so nothing in it is lost. The tenant is
    the one Step 6 bound the case to (nam_case_access), else "local". Both old
    tables are left untouched. Idempotent: a copied case is never copied again.
    """
    if not _has_table(conn, "nam_needs"):
        return
    access = _has_table(conn, "nam_case_access")
    join = ("LEFT JOIN nam_case_access a ON a.case_id = n.need_id" if access else "")
    owner = "a.tenant, a.created_by" if access else "NULL, NULL"
    rows = conn.execute(
        f"SELECT n.need_id, n.session_id, n.record_json, n.updated_at, {owner} "
        f"FROM nam_needs n {join} "
        "WHERE NOT EXISTS (SELECT 1 FROM nam_cases c WHERE c.case_id = n.need_id)").fetchall()
    if not rows:
        return
    with _write(conn):
        for need_id, session_id, record_json, updated_at, tenant, created_by in rows:
            try:
                record = json.loads(record_json)
            except ValueError:
                record = {}
            created = str(record.get("created_at") or updated_at)
            tenant = tenant or LOCAL_TENANT
            conn.execute(
                "INSERT OR IGNORE INTO nam_cases (case_id, tenant, version, stage, session_id, "
                "record_json, created_by, created_at, updated_by, updated_at) "
                "VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'migration', ?)",
                (need_id, tenant, stage_of(record.get("stage", "")), session_id or "",
                 record_json, created_by or "", created, updated_at))
            conn.execute(
                "INSERT OR IGNORE INTO nam_case_versions (case_id, version, at, tenant, user, "
                "reason, changed_json, record_json) VALUES (?, 1, ?, ?, 'migration', ?, ?, ?)",
                (need_id, updated_at, tenant, "copied from nam_needs",
                 json.dumps(sorted(record)), record_json))


# -- case numbers -------------------------------------------------------------

_NUMBER = re.compile(r"^NAM-\d{4}-(\d+)$")


def _highest(conn: sqlite3.Connection, year: int) -> int:
    ids = [r[0] for r in conn.execute("SELECT case_id FROM nam_cases WHERE case_id LIKE ?",
                                      (f"NAM-{year}-%",))]
    if _has_table(conn, "nam_needs"):
        ids += [r[0] for r in conn.execute("SELECT need_id FROM nam_needs WHERE need_id LIKE ?",
                                           (f"NAM-{year}-%",))]
    numbers = [int(m.group(1)) for m in map(_NUMBER.match, ids) if m]
    return max(numbers, default=0)


def _draw(conn: sqlite3.Connection, name: str, start: int) -> int:
    """The next value of counter `name` (the first is start + 1), inside one
    write transaction, so two callers at once never draw the same value."""
    ensure(conn)
    with _write(conn):
        conn.execute("INSERT OR IGNORE INTO nam_counters (name, value) VALUES (?, ?)",
                     (name, start))
        conn.execute("UPDATE nam_counters SET value = value + 1 WHERE name = ?", (name,))
        return conn.execute("SELECT value FROM nam_counters WHERE name = ?",
                            (name,)).fetchone()[0]


def next_case_id(conn: sqlite3.Connection, year: int | None = None) -> str:
    """NAM-YYYY-NNNN, drawn atomically. The counter starts after the highest
    number already used that year."""
    year = year or datetime.now().year
    ensure(conn)
    return f"NAM-{year}-{_draw(conn, f'case:{year}', _highest(conn, year)):04d}"


def next_pr_number(conn: sqlite3.Connection, year: int | None = None) -> str:
    """PRX-YYYY-NNNN: the PR's own file number in Procurix.ai (FRS F27), used
    until a SAP connector returns SAP's number."""
    year = year or datetime.now().year
    return f"PRX-{year}-{_draw(conn, f'pr:{year}', 0):04d}"


def once(conn: sqlite3.Connection, action_id: str, case_id: str, kind: str, effect) -> dict:
    """The result of an irreversible action's effect, run at most once per
    action id: a retry after a crash, or a second confirmation racing the
    first, gets the result the first run stored. `effect()` itself is run
    only when no result is stored yet; a connector it calls gets action_id
    as its idempotency key."""
    ensure(conn)
    row = conn.execute("SELECT result_json FROM nam_action_results WHERE action_id = ?",
                       (action_id,)).fetchone()
    if row:
        return json.loads(row[0])
    result = effect()
    with _write(conn):
        conn.execute("INSERT OR IGNORE INTO nam_action_results (action_id, case_id, kind, "
                     "result_json) VALUES (?, ?, ?, ?)",
                     (action_id, case_id, kind, json.dumps(result)))
        stored = conn.execute("SELECT result_json FROM nam_action_results WHERE action_id = ?",
                              (action_id,)).fetchone()[0]
    return json.loads(stored)


# -- the record ---------------------------------------------------------------

def stale(case_id: str, expected: int, current: int) -> NamError:
    return NamError("stale_version", f"case {case_id} is at version {current}, not "
                    f"{expected}: it changed since it was read. Read it again and retry.",
                    details=[f"current_version: {current}"], case_id=case_id)


def load(conn: sqlite3.Connection, case_id: str) -> tuple[dict, int] | None:
    """(record, version) of a case, or None."""
    ensure(conn)
    case_id = guard.plain(case_id or "").strip()       # "NAM‑2026‑0012" typed with U+2011
    row = conn.execute("SELECT record_json, version FROM nam_cases WHERE case_id = ?",
                       (case_id,)).fetchone()
    if row is None:
        return None
    actor = current()
    if (actor.seen is not None and actor.case_id == case_id and int(row[1]) != actor.seen
            and not _written_by_run(conn, case_id, actor.run_id)):
        raise stale(case_id, actor.seen, int(row[1]))
    return json.loads(row[0]), int(row[1])


def _written_by_run(conn: sqlite3.Connection, case_id: str, run_id: str) -> bool:
    return bool(run_id) and conn.execute(
        "SELECT 1 FROM nam_case_versions WHERE case_id = ? AND run_id = ?",
        (case_id, run_id)).fetchone() is not None


def write(conn: sqlite3.Connection, case_id: str, record: dict, *, expected: int,
          stage: str, session_id: str = "", reason: str = "") -> int:
    """Save `record` as the next version of `case_id` and return that version.

    expected 0 opens a new case (owned by the acting tenant); otherwise it is
    the version the record was read at, and a newer one on disk is a
    stale_version error. A save that changes nothing writes nothing."""
    ensure(conn)
    actor = current()
    data = json.dumps(record)
    with _write(conn):
        row = conn.execute("SELECT version, record_json FROM nam_cases WHERE case_id = ?",
                           (case_id,)).fetchone()
        if expected == 0:
            if row is not None:
                raise stale(case_id, 0, int(row[0]))
            before: dict = {}
            conn.execute(
                "INSERT INTO nam_cases (case_id, tenant, version, stage, session_id, record_json, "
                "created_by, updated_by) VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                (case_id, actor.tenant, stage, session_id, data, actor.user, actor.user))
        else:
            if row is None:
                raise NamError("not_found", f"no case {case_id}")
            if int(row[0]) != expected:
                raise stale(case_id, expected, int(row[0]))
            before = json.loads(row[1])
            if before == record:
                return expected
            conn.execute(
                f"UPDATE nam_cases SET version = ?, stage = ?, record_json = ?, updated_by = ?, "
                f"updated_at = {_NOW} WHERE case_id = ? AND version = ?",
                (expected + 1, stage, data, actor.user, case_id, expected))
        version = expected + 1
        changed = sorted(k for k in set(before) | set(record) if before.get(k) != record.get(k))
        conn.execute(
            "INSERT INTO nam_case_versions (case_id, version, tenant, user, flow, run_id, reason, "
            "changed_json, record_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (case_id, version, actor.tenant, actor.user, actor.flow, actor.run_id, reason,
             json.dumps(changed), data))
    return version


def tenant_of(conn: sqlite3.Connection, case_id: str) -> str | None:
    ensure(conn)
    row = conn.execute("SELECT tenant FROM nam_cases WHERE case_id = ?", (case_id,)).fetchone()
    return row[0] if row else None


def last_flow(conn: sqlite3.Connection, case_id: str) -> str:
    """The flow that last wrote the case ("" when none did)."""
    ensure(conn)
    row = conn.execute("SELECT flow FROM nam_case_versions WHERE case_id = ? AND flow != '' "
                       "ORDER BY version DESC LIMIT 1", (case_id,)).fetchone()
    return row[0] if row else ""


def version_of(conn: sqlite3.Connection, case_id: str) -> int | None:
    ensure(conn)
    row = conn.execute("SELECT version FROM nam_cases WHERE case_id = ?", (case_id,)).fetchone()
    return int(row[0]) if row else None


def case_row(conn: sqlite3.Connection, case_id: str) -> dict | None:
    ensure(conn)
    row = conn.execute(
        "SELECT case_id, tenant, version, stage, record_json, created_by, created_at, "
        "updated_by, updated_at FROM nam_cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        return None
    keys = ("case_id", "tenant", "version", "stage", "record", "created_by", "created_at",
            "updated_by", "updated_at")
    out = dict(zip(keys, row, strict=True))
    out["record"] = json.loads(out["record"])
    return out


def versions(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    """Every version of a case, oldest first, without the records."""
    ensure(conn)
    rows = conn.execute(
        "SELECT version, at, tenant, user, flow, run_id, reason, changed_json "
        "FROM nam_case_versions WHERE case_id = ? ORDER BY version", (case_id,)).fetchall()
    return [{"case_version": v, "at": at, "by": {"tenant": t, "user": u}, "flow": f,
             "run_id": r, "reason": why, "changed": json.loads(ch)}
            for v, at, t, u, f, r, why, ch in rows]


def snapshot(conn: sqlite3.Connection, case_id: str, version: int) -> dict | None:
    """One version as it was: its row in nam_case_versions, with the record."""
    ensure(conn)
    row = conn.execute(
        "SELECT at, tenant, user, record_json FROM nam_case_versions "
        "WHERE case_id = ? AND version = ?", (case_id, version)).fetchone()
    if row is None:
        return None
    return {"at": row[0], "tenant": row[1], "user": row[2], "record": json.loads(row[3])}


def recent(conn: sqlite3.Connection, tenant: str, limit: int = 10) -> list[tuple[dict, str]]:
    """The tenant's latest cases, newest first: (record, updated_at)."""
    ensure(conn)
    rows = conn.execute(
        "SELECT record_json, updated_at FROM nam_cases WHERE tenant = ? "
        "ORDER BY updated_at DESC LIMIT ?", (tenant, limit)).fetchall()
    return [(json.loads(r[0]), r[1]) for r in rows]
