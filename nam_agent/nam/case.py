"""A NAM case: one need, from the officer's words to the PR, kept per chat.

One row per case in nam_flow_case (its data as JSON, a version that goes up on
every write) and every version kept in nam_flow_case_versions (names of their
own: a state.db from the earlier NAM backend still holds its nam_case and
nam_case_versions tables, in another shape, and they are left as they are), so what the case
held at any point can be read back. A case belongs to the chat that opened
it (session_id) and to the tenant of the caller that opened it: a caller of
another tenant can neither read nor change it.

The data:
  need, category           what the officer asked for: the job description (1a, 1b)
  items[]                  one per material the need names (1c), each with
    query, searched        what was searched in SAP
    candidates[], keys[]   every record found, and the keys still in
    selected               True when the officer takes it forward (1d), False
                           when not, absent before the choice
    pending                the open question and its options (1f)
    answers[], details{}   what the officer answered; details carry
                           default=true when NRL's standard value was used (1g)
    picked                 the one material in the cart, with quantity (1h)
    pr                     the item's own PR: fields, status (stage 3)
  choosing                 the open "which items go forward" question (1d)
  matched                  the PURs ranked by how many materials each holds (1j)
  estimate                 the cart's estimate when the officer asks (stage 2)

Every write names its act (_act, and what it did in _did). undo() goes back
over the officer's acts one at a time, by the versions kept: an answer, a
choice, an item put in the cart, a PR field. A confirmed PR is never taken
back.

summary() is the case as the agent reads it every turn: the chat's memory of
where the need stands, so "it" and "them" always mean this case's records.

Who acts: the identity context (identity.py) set for a turn with acting();
without one, the dashboard's own officer (LOCAL).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime

LOCAL_TENANT = "local"


@dataclass(frozen=True)
class Actor:
    tenant: str = LOCAL_TENANT
    user: str = "local"
    roles: tuple[str, ...] = ("U1",)
    run_id: str = ""
    flow: str = ""
    case_id: str = ""


_LOCAL = Actor()
_ACTOR: ContextVar[Actor | None] = ContextVar("nam_actor", default=None)


def current() -> Actor:
    return _ACTOR.get() or _LOCAL


@contextmanager
def acting(actor: Actor) -> Iterator[None]:
    """Every read and write inside is `actor`'s."""
    token = _ACTOR.set(actor)
    try:
        yield
    finally:
        _ACTOR.reset(token)


# -- tables -------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nam_flow_case (
    case_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nam_flow_case_by_session ON nam_flow_case (session_id, updated_at);
CREATE TABLE IF NOT EXISTS nam_flow_case_versions (
    case_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    user TEXT NOT NULL,
    data_json TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (case_id, version)
);
"""


def ensure(conn: sqlite3.Connection) -> None:
    from nam_agent import db

    db.once(conn, "nam.case", lambda c: (c.executescript(_SCHEMA), c.commit()),
            table="nam_flow_case")


@contextmanager
def _write(conn: sqlite3.Connection) -> Iterator[None]:
    """One IMMEDIATE transaction: a read-check-write inside cannot interleave."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class NotYours(Exception):
    """The case belongs to another tenant."""


def _next_id(conn: sqlite3.Connection) -> str:
    year = datetime.now(UTC).year
    rows = conn.execute("SELECT case_id FROM nam_flow_case WHERE case_id LIKE ?",
                        (f"NAM-{year}-%",)).fetchall()
    n = max((int(r[0].rsplit("-", 1)[1]) for r in rows if r[0].rsplit("-", 1)[1].isdigit()),
            default=0)
    return f"NAM-{year}-{n + 1:04d}"


def open_case(conn: sqlite3.Connection, session_id: str, need: str, category: str = "") -> dict:
    """A new case for the chat, as the acting caller's tenant."""
    ensure(conn)
    actor = current()
    with _write(conn):
        case_id = _next_id(conn)
        data = {"case_id": case_id, "need": need, "category": category, "items": [],
                "estimate": {}, "opened_by": actor.user, "_act": "need",
                "_did": "opened the case", "_base": 1}
        now = _now()
        conn.execute("INSERT INTO nam_flow_case (case_id, tenant, session_id, version, data_json, "
                     "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
                     (case_id, actor.tenant, session_id or "", json.dumps(data), now, now))
        conn.execute("INSERT INTO nam_flow_case_versions (case_id, version, user, data_json, at) "
                     "VALUES (?, 1, ?, ?, ?)", (case_id, actor.user, json.dumps(data), now))
    data["version"] = 1
    return data


def load(conn: sqlite3.Connection, case_id: str) -> dict | None:
    """The case, or None; NotYours when it is another tenant's."""
    ensure(conn)
    row = conn.execute("SELECT tenant, version, data_json FROM nam_flow_case WHERE case_id = ?",
                       (case_id or "",)).fetchone()
    if row is None:
        return None
    if row[0] != current().tenant:
        raise NotYours(case_id)
    return {**json.loads(row[2]), "version": row[1]}


def for_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    """The chat's case: the one it touched last, or None."""
    if not session_id:
        return None
    ensure(conn)
    row = conn.execute("SELECT case_id FROM nam_flow_case WHERE session_id = ? AND tenant = ? "
                       "ORDER BY updated_at DESC LIMIT 1",
                       (session_id, current().tenant)).fetchone()
    return load(conn, row[0]) if row else None


class Stale(Exception):
    """The case changed since it was read."""


def save(conn: sqlite3.Connection, data: dict, session_id: str = "", act: str = "",
         did: str = "") -> dict:
    """Write the case as a new version; Stale when another write came first.
    act names the step that wrote it (undo goes back over the officer's), did
    says what it did, in a few words."""
    ensure(conn)
    actor = current()
    body = {k: v for k, v in data.items() if k not in ("version", "_act", "_did", "_base")}
    if act:
        body["_act"], body["_did"] = act, did[:200]
    with _write(conn):
        row = conn.execute("SELECT tenant, version FROM nam_flow_case WHERE case_id = ?",
                           (data["case_id"],)).fetchone()
        if row is None:
            raise KeyError(data["case_id"])
        if row[0] != actor.tenant:
            raise NotYours(data["case_id"])
        if data.get("version") not in (None, row[1]):
            raise Stale(data["case_id"])
        version, now = row[1] + 1, _now()
        # the version whose state this is: itself, or the one an undo restored
        body["_base"] = data.get("_base") if act == "undo" else version
        text = json.dumps(body, default=str)
        conn.execute("UPDATE nam_flow_case SET version = ?, data_json = ?, updated_at = ?"
                     + (", session_id = ?" if session_id else "") + " WHERE case_id = ?",
                     (version, text, now, *([session_id] if session_id else []),
                      data["case_id"]))
        conn.execute("INSERT INTO nam_flow_case_versions (case_id, version, user, data_json, at) "
                     "VALUES (?, ?, ?, ?, ?)", (data["case_id"], version, actor.user, text, now))
    return {**body, "version": version}


def versions(conn: sqlite3.Connection, case_id: str) -> list[dict]:
    load(conn, case_id)                       # the tenant check
    rows = conn.execute("SELECT version, user, at FROM nam_flow_case_versions WHERE case_id = ? "
                        "ORDER BY version", (case_id,)).fetchall()
    return [{"version": v, "user": u, "at": a} for v, u, a in rows]


# the acts undo takes back: the officer's (an ask, a search or a read is the
# agent's, and is gone over with the act it led to)
OFFICER_ACTS = ("need", "choose", "answer", "detail", "pick", "unpick", "other", "estimate",
                "pr")


def _version(conn: sqlite3.Connection, case_id: str, v: int) -> dict | None:
    row = conn.execute("SELECT data_json FROM nam_flow_case_versions WHERE case_id = ? AND "
                       "version = ?", (case_id, v)).fetchone()
    return json.loads(row[0]) if row else None


def confirmed(data: dict) -> set[str]:
    """The items whose PR is confirmed (ready for SAP, or sent)."""
    return {it["name"] for it in data.get("items") or []
            if (it.get("pr") or {}).get("status") in ("ready_for_sap", "sent")}


def undo(conn: sqlite3.Connection, data: dict, session_id: str = "",
         steps: int = 1) -> tuple[dict, list[str]]:
    """(the case as it was before the officer's last `steps` acts, what was
    taken back). Each undo goes further back: the state an undo restored is
    where the next one starts. ValueError when nothing is left to take back,
    or when it would take back a confirmed PR."""
    load(conn, data["case_id"])                 # the tenant check
    at, taken = data.get("_base") or data["version"], []
    target = None
    for _ in range(max(1, int(steps or 1))):
        cur = at
        while cur >= 1:
            v = _version(conn, data["case_id"], cur)
            if v is None:
                break
            if v.get("_act") == "undo":
                cur = v.get("_base") or cur - 1
                continue
            if v.get("_act") in OFFICER_ACTS and cur > 1:
                taken.append(v.get("_did") or v["_act"])
                at = cur - 1
                target = _version(conn, data["case_id"], at)
                break
            cur -= 1
        else:
            break
        if target is None or cur < 1:
            break
    if target is None:
        raise ValueError("there is nothing to take back")
    lost = confirmed(data) - confirmed(target)
    if lost:
        raise ValueError(f"that would take back the confirmed PR of {', '.join(sorted(lost))}: "
                         "a confirmed PR stays")
    # the state restored is version `at`'s, or the one it had itself restored
    restored = {**target, "case_id": data["case_id"], "version": data["version"],
                "_base": (target.get("_base") or at) if target.get("_act") == "undo" else at}
    return save(conn, restored, session_id, act="undo",
                did="took back: " + "; ".join(taken)), taken


def with_actor(**changes) -> Actor:
    return replace(current(), **changes)


# -- the case as the agent reads it -------------------------------------------------

def item(data: dict, name: str) -> dict | None:
    """The item named (case-insensitive), or the only item when name is ''."""
    items = data.get("items") or []
    if not name and len(items) == 1:
        return items[0]
    low = " ".join((name or "").lower().split())
    return next((it for it in items if it["name"].lower() == low), None) or next(
        (it for it in items if low and low in it["name"].lower()), None)


def remaining(it: dict) -> list[dict]:
    keep = set(it.get("keys") or [])
    return [c for c in it.get("candidates") or [] if c["key"] in keep]


def chosen(data: dict) -> list[dict]:
    """The items the officer takes forward, in their order; every item while
    the choice is not made yet."""
    items = data.get("items") or []
    if not any("selected" in it for it in items):
        return items
    return [it for it in items if it.get("selected")]


def current_item(data: dict) -> dict | None:
    """The item being worked now: the first chosen one not yet in the cart."""
    return next((it for it in chosen(data) if not it.get("picked")), None)


def progress(data: dict, it: dict) -> dict | None:
    """Which of the chosen materials a question is about: {"n": 1, "of": 3,
    "item": "thermowell"} (1 of 1 for a need of one)."""
    chosen = [i["name"] for i in chosen_items(data)]
    if not chosen or it["name"] not in chosen:
        return None
    return {"n": chosen.index(it["name"]) + 1, "of": len(chosen), "item": it["name"]}


def chosen_items(data: dict) -> list[dict]:
    return chosen(data)


def summary(data: dict) -> str:
    """The case in a few lines: the need, each material and where it stands
    (chosen or not, its records, its question, the cart), the matching PURs
    and each PR. Values are the records' and the officer's."""
    from nam_agent.nam import guard, narrow

    out = [f"{data['case_id']}: need '{guard.sanitise(data.get('need') or '', 300)}'"
           + (f", category {data['category']}" if data.get("category") else "") + "."]
    items = data.get("items") or []
    decided = any("selected" in it for it in items)
    if data.get("asking_category"):
        out.append(f"Open question: {data['asking_category']['question']} Options: "
                   + " | ".join(data["asking_category"]["options"]))
    if data.get("choosing"):
        out.append(f"Open question: {data['choosing']['question']} Options: "
                   + " | ".join(data["choosing"]["options"]))
    cart = [it for it in chosen(data) if it.get("picked")]
    if decided:
        out.append(f"Cart: {len(cart)} of {len(chosen(data))} chosen item(s)"
                   + (": " + ", ".join(it["name"] for it in cart) if cart else "") + ".")
    now = current_item(data) if decided or len(items) == 1 else None
    for it in items:
        head = f"Item '{it['name']}'"
        if decided and not it.get("selected"):
            head += " (not taken forward)"
        elif now is not None and it is now:
            head += " (being worked now)"
        if it.get("picked"):
            p = it["picked"]
            pr = it.get("pr") or {}
            out.append(f"{head}: IN THE CART {p.get('code') or p['key']} "
                       f"{p.get('description', '')} (PUR {p.get('pur') or '-'}), quantity "
                       f"{p.get('quantity') or '?'} {p.get('unit') or ''}."
                       + (f" PR: {pr.get('status', 'draft')}"
                          + (f", missing {', '.join(pr['missing'])}" if pr.get("missing") else "")
                          + "." if pr else ""))
            continue
        if not it.get("searched"):
            out.append(f"{head}: not searched yet.")
            continue
        left = remaining(it)
        if decided and not it.get("selected"):
            out.append(f"{head}: SAP has {narrow.count(it.get('candidates') or []) or 'nothing'}.")
            continue
        rows = "; ".join(f"{c['key']} {c['description'][:60]}" for c in left[:12])
        more = f" (+{len(left) - 12} more)" if len(left) > 12 else ""
        out.append(f"{head}: {len(left)} of {len(it.get('candidates') or [])} record(s) still "
                   f"in: {guard.sanitise(rows, 1500)}{more}.")
        if it.get("answers"):
            out.append("  Answers: " + "; ".join(f"{a['question']} -> {a['answer']}"
                                                 for a in it["answers"]))
        if it.get("details"):
            out.append("  Details: " + "; ".join(
                f"{k} = {v['value']}" + (" (default)" if v.get("default") else "")
                for k, v in it["details"].items()))
        if it.get("pending"):
            q = it["pending"]
            out.append(f"  Open question: {q['question']} Options: "
                       + " | ".join(o["label"] for o in q["options"]))
    if data.get("matched"):
        top = data["matched"][:3]
        out.append("Matching PURs: " + "; ".join(f"{t['pur']} {t['match']}% ({t['covers']})"
                                                 for t in top) + ".")
    est = data.get("estimate") or {}
    if est.get("lines"):
        flagged = sum(1 for ln in est["lines"] if ln.get("flag") and not ln.get("decision"))
        out.append(f"Estimate: {len(est['lines'])} line(s), total INR {est.get('total', 0):,.2f}"
                   f"{', LOCKED' if est.get('locked') else ''}"
                   f"{f', {flagged} flagged line(s) awaiting a decision' if flagged else ''}.")
    if data.get("_did") and data.get("_act") == "undo":
        out.append(f"Last act: {data['_did']}.")
    return "\n".join(out)
