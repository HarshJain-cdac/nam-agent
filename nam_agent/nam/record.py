"""The Need Record as the API shows it: GET /v1/nam/cases/{id} and /history.

One record per case, apart from the chat that produced it: whichever call,
dashboard route or chat tool touched the case, this is what it left. The
record is nam_agent/nam/store.py's current row (or, with `version`, the row as it
was then); the history is one entry per version, saying who changed it, from
which flow and run, why, and which fields.

The record's shape is schemas/record.json, the history's schemas/history.json.
Fields later flows add (estimates, documents, the pending action) arrive with
those flows.
"""

from __future__ import annotations

import sqlite3

from nam_agent.nam import schema, store
from nam_agent.nam.errors import VERSION, NamError


def _open_question(record: dict) -> dict | None:
    pending = (record.get("followups") or {}).get("pending")
    if not pending:
        return None
    return {"id": str(pending.get("id") or ""), "text": str(pending.get("text") or "")}


def progress(record: dict) -> dict:
    """The stage machine's state of a record, as the API shows it."""
    from nam_agent.nam import need, orchestrator

    rec = need.NeedRecord(**{k: v for k, v in record.items() if k != "version"})
    wf = orchestrator.workflow(rec)
    return {"pending_action": orchestrator.public(wf["pending"]),
            "gates": sorted(wf["gates"]), "actions": sorted(wf["actions"]),
            "missing": [m["says"] for m in orchestrator.missing(rec, wf)]
            if not wf.get("closed") else []}


def view(conn: sqlite3.Connection, case_id: str, version: int | None = None) -> dict:
    row = store.case_row(conn, case_id)
    if row is None:
        raise NamError("not_found", f"no case {case_id}")
    body = {
        "version": VERSION,
        "case_id": case_id,
        "tenant": row["tenant"],
        "case_version": row["version"],
        "latest_version": row["version"],
        "stage": row["stage"],
        "need": row["record"],
        "open_question": _open_question(row["record"]),
        **progress(row["record"]),
        "created": {"at": row["created_at"], "by": row["created_by"]},
        "updated": {"at": row["updated_at"], "by": row["updated_by"]},
    }
    if version is not None and version != row["version"]:
        old = store.snapshot(conn, case_id, version)
        if old is None:
            raise NamError("not_found", f"case {case_id} has no version {version}")
        body.update(case_version=version, need=old["record"],
                    stage=(old["record"].get("workflow") or {}).get("stage")
                    or store.stage_of(old["record"].get("stage", "")),
                    open_question=_open_question(old["record"]), **progress(old["record"]),
                    updated={"at": old["at"], "by": old["user"]})
    return _checked(body, "record")


def history(conn: sqlite3.Connection, case_id: str) -> dict:
    if store.case_row(conn, case_id) is None:
        raise NamError("not_found", f"no case {case_id}")
    return _checked({"version": VERSION, "case_id": case_id,
                     "entries": store.versions(conn, case_id)}, "history")


def _checked(body: dict, name: str) -> dict:
    problems = schema.validate(body, schema.load(name))
    if problems:
        raise NamError("internal_schema", f"the case {name} failed its own schema",
                       details=problems)
    return body
