"""DETERMINISTIC EVAL — NAM's outputs are the PR Creation Flow's, as Excel.

A Goods case walked through the real harness (nam_fakes, no model) gives
the three outputs the flow names: the requirement sheet (1(h)), the estimate
sheet (2(f)) and the filled PR (3(a)). Each is read back from the .xlsx and
checked against the case record; one asked for before its stage is refused
with the reason. NIT generation is not part of NAM.
"""

from __future__ import annotations

import io
import re
import zipfile

import pytest
from nam_fakes import ctx
from nam_walk import call, goods_at_s3, goods_at_s4, goods_submitted

from nam_agent.nam import need, orchestrator, outputs
from nam_agent.nam.docs.xlsx import xlsx


def _sheets(data: bytes) -> dict[str, list[str]]:
    """Each sheet's cell texts, in order (inline strings and numbers)."""
    z = zipfile.ZipFile(io.BytesIO(data))
    names = re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
    out = {}
    for i, name in enumerate(names, 1):
        xml = z.read(f"xl/worksheets/sheet{i}.xml").decode()
        out[name] = re.findall(r"<t[^>]*>([^<]*)</t>|<v>([^<]*)</v>", xml)
        out[name] = [a or b for a, b in out[name]]
    return out


def test_the_writer_makes_a_workbook_excel_can_open():
    data = xlsx([("Sheet: one", [["A", "B"], [1, "x & <y>"]]), ("Sheet: one", [["C"]])])
    z = zipfile.ZipFile(io.BytesIO(data))
    assert {"[Content_Types].xml", "xl/workbook.xml", "xl/styles.xml",
            "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"} <= set(z.namelist())
    sheets = _sheets(data)
    assert list(sheets) == ["Sheet  one", "Sheet  one 1"]          # names cleaned, unique
    assert sheets["Sheet  one"] == ["A", "B", "1", "x &amp; &lt;y&gt;"]


def test_the_requirement_sheet_carries_every_detail_and_where_it_came_from(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    name, data = outputs.build(need.load(c.conn, case_id), "requirement")
    assert name == f"{case_id}-requirement.xlsx"
    sheets = _sheets(data)
    req = sheets["Requirement"]
    assert req[:4] == ["Item", "Detail", "Value", "Taken from"]
    assert "Which kind of gaskets do you need?" in req and "ring joint" in req
    assert "Goods" in req and "confirmed by the officer" in req
    assert "PUR-4" in sheets["Matching tenders"]
    assert sheets["Precedent"] == ["Selected precedent", "PUR-4"]


def test_the_estimate_sheet_has_the_flows_columns_and_the_harness_numbers(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s4(c)
    with pytest.raises(outputs.OutputError, match="no estimate"):
        outputs.build(need.load(c.conn, case_id), "estimate")
    call(c, case_id, "Goods estimate")
    rec = need.load(c.conn, case_id)
    line = orchestrator._artifact(orchestrator.workflow(rec), "estimate")["lines"][0]
    sheet = _sheets(outputs.build(rec, "estimate")[1])["Estimate"]
    assert sheet[:9] == ["S.No", "Code", "Description", "Quantity", "Unit", "Unit price",
                         "Total", "Basis", "Source"]
    assert str(line["rate"]) in sheet and str(line["amount"]) in sheet
    assert "Total excl. GST" in sheet
    assert any(cell.startswith("price_history ev_") for cell in sheet)
    assert any(cell.startswith("+") and "a year" in cell for cell in sheet)


def test_the_pr_sheet_has_each_field_its_value_and_source(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_submitted(c)
    rec = need.load(c.conn, case_id)
    sheets = _sheets(outputs.build(rec, "pr")[1])
    header = sheets["PR header"]
    assert header[:4] == ["PR field", "Value", "Taken from", "Mandatory"]
    assert "the approved estimate" in header and "the officer" in header
    assert "PR items" in sheets and "Submitted" in sheets["Status"]


def test_what_is_ready_says_why_not(tmp_path):
    c = ctx(tmp_path)
    case_id = goods_at_s3(c)
    ready = {o["kind"]: o for o in outputs.available(need.load(c.conn, case_id))}
    assert ready["requirement"]["ready"] and not ready["estimate"]["ready"]
    assert "S4" in ready["estimate"]["why"] and "S7" in ready["pr"]["why"]
    assert set(ready) == set(outputs.KINDS)                      # requirement, estimate, pr
