"""DETERMINISTIC EVAL — what the Procurix screens read beside the flows.

The Need Assessment register shows each case's stage as the harness stored
it (never a guess), sums only estimates a flow produced, keeps the officer's
unsent edits per screen section, and writes an edited document as a real
.docx (nam_agent/nam/register.py).
"""

from __future__ import annotations

import io
import sqlite3
import zipfile
from pathlib import Path

import pytest

from nam_agent.nam import need as nam
from nam_agent.nam import register

ROOT = Path(__file__).resolve().parents[3]


def _case(conn, text, stage=None, artifacts=None):
    need = nam.create(conn, text)
    if stage:
        need.workflow = {"stage": stage, "artifacts": {
            k: {"value": v, "flow": "x", "at_version": 1} for k, v in (artifacts or {}).items()}}
        nam.save(conn, need)
    return need


def test_rows_carry_the_harness_stage_and_its_badge():
    conn = sqlite3.connect(":memory:")
    _case(conn, "Supply of API 610 pump spares")
    _case(conn, "Annual maintenance of DCS", "S6",
          {"validation": {"findings": [{"says": "a"}, {"says": "b"}, {"says": "c"}]},
           "estimate": {"total": 19600000.0}})
    _case(conn, "Catalyst for hydrotreater", "S7",
          {"pr_reference": {"pr_number": "1000487002"}, "estimate": {"total": 1.0}})
    _case(conn, "Contract manpower", "S10")
    out = register.register(conn)
    labels = {r["requirement"]: r["stage_label"] for r in out["rows"]}
    assert labels["Supply of API 610 pump spares"] == "Need Capture"
    assert labels["Annual maintenance of DCS"] == "Validation - 3 findings"
    assert labels["Catalyst for hydrotreater"] == "PR created - 1000487002"
    assert labels["Contract manpower"] == "Routed to NIT"
    k = out["kpis"]
    assert k["open_needs"] == 4 and k["awaiting_validation"] == 1 and k["routed_to_nit"] == 1
    assert k["estimated_value"] == 19600001.0 and k["priced"] == 2


def test_no_estimate_is_none_not_zero():
    conn = sqlite3.connect(":memory:")
    _case(conn, "Supply of gaskets")
    out = register.register(conn)
    assert out["rows"][0]["estimate"] is None and out["kpis"]["estimated_value"] is None


def test_drafts_are_kept_per_case_and_section():
    conn = sqlite3.connect(":memory:")
    need = _case(conn, "Supply of gaskets")
    assert register.save_draft(conn, need.need_id, "pqc", {"basis": [50, 40, 30, 20]}) is None
    assert register.save_draft(conn, need.need_id, "pqc", {"basis": [45, 40, 30, 20]}) is None
    assert register.drafts(conn, need.need_id)["pqc"]["data"] == {"basis": [45, 40, 30, 20]}
    assert "unknown section" in register.save_draft(conn, need.need_id, "nope", {})
    assert "no case" in register.save_draft(conn, "NAM-2099-9999", "pqc", {})


def test_an_edited_document_becomes_a_docx():
    data = register.docx("Scope of Work", [["h", "1. Painting Works"], ["p", "Execute painting."],
                                           ["table", [["Item", "Qty"], ["Paint", 10]]]])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode()
    assert "Painting Works" in xml and "Execute painting." in xml and "Paint" in xml
    with pytest.raises(ValueError):
        register.docx("x", [["script", "alert(1)"]])


def test_the_dashboard_serves_them():
    src = "".join((ROOT / "nam_agent/server" / n).read_text(encoding="utf-8") for n in ("nam_routes.py", "chat.py", "http.py"))
    for route in ('"/api/nam/register"', '"/api/nam/drafts"', 'route == "drafts"',
                  'route == "docx"'):
        assert route in src, route
