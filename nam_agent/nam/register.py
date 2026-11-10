"""What the Procurix screens read and write beside the harness flows.

The Need Assessment register (the landing table and its KPI cards), the
officer's edits made on the wizard screens before they are sent to the
harness, and a .docx of an edited document. Every value comes from the case
as the harness stored it (nam_agent/nam/store.py, the flows' artifacts); a value
no flow has produced yet is None, and the screen shows it as empty.

  register(conn)            one row per case, newest first, plus the KPIs
  drafts(conn, case_id)     the officer's unsent edits, per screen section
  save_draft(...)           keep one section's edits
  docx(title, blocks)       an edited document as .docx (docs/render.py)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from nam_agent.nam import store
from nam_agent.nam.docs import render

# The stage badge the register shows for each harness stage (orchestrator.py).
# The tone is a colour name the screen maps to its own palette.
STAGE_BADGE = {
    "S1": ("Need Capture", "amber"),
    "S2": ("Precedents", "slate"),
    "S3": ("Quantity", "slate"),
    "S4": ("Estimate", "sky"),
    "S5": ("Documents", "violet"),
    "S6": ("Validation", "rose"),
    "S7": ("PR creation", "green"),
    "S8": ("Awaiting DoA approval", "gray"),
    "S9": ("User upload", "gray"),
    "S10": ("Routed to NIT", "blue"),
}
SECTIONS = {"estimate", "review", "boq", "pqc", "wage", "scope", "spec", "scc", "compliance",
            "capture", "sheet"}
MAX_DRAFT_BYTES = 2_000_000


def _artifact(record: dict, name: str):
    return (((record.get("workflow") or {}).get("artifacts") or {}).get(name) or {}).get("value")


def _value(pred) -> str | None:
    return (pred or {}).get("value") if isinstance(pred, dict) else None


def row(record: dict, updated_at: str) -> dict:
    """One register row from a stored case."""
    wf = record.get("workflow") or {}
    stage = wf.get("stage") or store.stage_of(record.get("stage") or "")
    label, tone = STAGE_BADGE.get(stage, (stage, "gray"))
    validation = _artifact(record, "validation") or {}
    findings = len(validation.get("findings") or [])
    pr_ref = _artifact(record, "pr_reference") or {}
    if stage == "S6" and findings:
        label = f"Validation - {findings} finding{'' if findings == 1 else 's'}"
    if pr_ref.get("pr_number") and stage in ("S7", "S8", "S9"):
        label, tone = f"PR created - {pr_ref['pr_number']}", "green"
    estimate = _artifact(record, "estimate") or {}
    return {
        "need_id": record.get("need_id"),
        "requirement": record.get("raw_text") or "",
        "category": _value(record.get("category")),
        "fund": _value(record.get("procurement_type")),
        "tender_preference": _value(record.get("tender_type")),
        "estimate": estimate.get("total"),
        "stage": stage,
        "stage_label": label,
        "stage_tone": tone,
        "closed": bool(wf.get("closed")),
        "related": len(record.get("related_cases") or []),
        "findings": findings,
        "pr_number": pr_ref.get("pr_number"),
        "pending": bool(wf.get("pending")),
        "updated_at": updated_at,
    }


def register(conn: sqlite3.Connection, tenant: str = "local", limit: int = 500) -> dict:
    """{"rows": [...], "kpis": {...}}: the register and its four cards."""
    rows = [row(rec, at) for rec, at in store.recent(conn, tenant, limit)]
    open_rows = [r for r in rows if not r["closed"]]
    values = [r["estimate"] for r in open_rows if isinstance(r["estimate"], (int, float))]
    return {
        "rows": rows,
        "kpis": {
            "open_needs": len(open_rows),
            "awaiting_validation": sum(1 for r in open_rows if r["stage"] == "S6"),
            "routed_to_nit": sum(1 for r in rows if r["stage"] == "S10"),
            # None, not 0, when no case has an estimate yet: nothing was priced
            "estimated_value": sum(values) if values else None,
            "priced": len(values),
        },
    }


# -- the officer's unsent edits ------------------------------------------------------

def ensure(conn: sqlite3.Connection) -> None:
    from nam_agent import db

    db.once(conn, "nam.ui_drafts", _setup, table="nam_ui_drafts")


def _setup(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS nam_ui_drafts (case_id TEXT NOT NULL, "
                 "section TEXT NOT NULL, data_json TEXT NOT NULL, updated_at TEXT NOT NULL, "
                 "PRIMARY KEY (case_id, section))")
    conn.commit()


def drafts(conn: sqlite3.Connection, case_id: str) -> dict:
    ensure(conn)
    out = {}
    for section, data, at in conn.execute(
            "SELECT section, data_json, updated_at FROM nam_ui_drafts WHERE case_id = ?",
            (case_id,)):
        out[section] = {"data": json.loads(data), "updated_at": at}
    return out


def save_draft(conn: sqlite3.Connection, case_id: str, section: str, data) -> str | None:
    """Keep one section's edits; returns an error message, or None."""
    if section not in SECTIONS:
        return f"unknown section {section!r}; one of {', '.join(sorted(SECTIONS))}"
    if store.load(conn, case_id) is None:
        return f"no case {case_id}"
    text = json.dumps(data)
    if len(text) > MAX_DRAFT_BYTES:
        return "the draft is too large"
    ensure(conn)
    conn.execute("INSERT INTO nam_ui_drafts (case_id, section, data_json, updated_at) "
                 "VALUES (?, ?, ?, ?) ON CONFLICT (case_id, section) DO UPDATE SET "
                 "data_json = excluded.data_json, updated_at = excluded.updated_at",
                 (case_id, section, text, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return None


# -- an edited document as .docx -------------------------------------------------------

def docx(title: str, blocks: list) -> bytes:
    """blocks as the editor sends them: [["h"|"p", text] | ["table", rows]]."""
    clean = []
    for block in blocks or []:
        if not isinstance(block, (list, tuple)) or len(block) != 2:
            raise ValueError("each block is [kind, value]")
        kind, value = block
        if kind in ("h", "p"):
            clean.append((kind, str(value)))
        elif kind == "table" and isinstance(value, list):
            clean.append(("table", [[str(c) for c in r] for r in value if isinstance(r, list)]))
        else:
            raise ValueError(f"unknown block {kind!r}")
    return render.docx(str(title or "Document")[:200], clean)
