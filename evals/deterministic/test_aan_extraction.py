"""DETERMINISTIC EVAL — an uploaded AAN answers the wizard's questions.

Four bugs made an AAN upload extract nothing, so the wizard asked all 37
questions by hand including the ones in the file the user had just uploaded.
Each has a case here that fails without its fix.

Offline: the AAN is built in-memory with python-docx, and extraction is
deterministic label matching, so nothing here needs a model or a network.

The text is read by nam_agent/nam/docs/parse.py, the one parser for every
uploaded document. (The dashboard's own _extract_text and _EXTRACT_FIELDS
were removed in plan Step 5, and the NIT wizard's question lists
(nam_agent/tools/nit_flows.py) before that; the cases that checked answers
against those lists went with them.)
"""

from __future__ import annotations

import io

import pytest

docx = pytest.importorskip("docx", reason="python-docx ships in the [nit] extra")

from nam_agent.nam.docs import parse  # noqa: E402
from nam_agent.tools import aan_reader  # noqa: E402


def _extract_text(filename: str, raw: bytes) -> str:
    return parse.extract(raw, parse.sniff(raw, filename)).text

# The rows of a real AAN (PUR-INSTMNT-1000150), including the label spellings
# as typed on the form — note "Tender Type :" with a space before the colon.
AAN_ROWS = [
    ("File No.:", "PUR-INSTMNT-1000150"),
    ("PR No.:", "10072565"),
    ("Value (Rs):", "4,12,74,555.90"),
    ("Category:", "Supply"),
    ("Nature of contract:", "AMC /Rate Contract"),
    ("Tender Type :", "Open Tender"),
    ("Name of proposal:", "ARC for Supply of Transmitters"),
    ("Justification/Reason for Procurement:", "NRL intends to go for an ARC."),
    ("Urgency:", "No"),
    ("Type of budget:", "Capital"),
    ("Payment Terms:", "100% within 30 days."),
    ("Engineer-In-Charge:", "CM(Instrumentation)"),
    ("Approving authority:", "N.P. Sharma DGM(Instrumentation)"),
    ("Department:", "Maintenance Instrumentation"),
    ("Pre-bid meet required:", "No"),
]


def _aan_bytes() -> bytes:
    """An AAN the way NRL issues one: a title paragraph, everything else in a
    table. The shape is the whole point — see test_tables_are_read."""
    document = docx.Document()
    document.add_paragraph("Numaligarh Refinery Limited")
    document.add_paragraph("Administrative Approval Note")
    table = document.add_table(rows=0, cols=2)
    for label, value in AAN_ROWS:
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = value
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def test_tables_are_read_not_just_paragraphs():
    """The original reader took .paragraphs only. On a real AAN that is the
    62-character title and none of the 3,733 characters that matter."""
    text = _extract_text("aan.docx", _aan_bytes())
    assert "PUR-INSTMNT-1000150" in text
    assert "Open Tender" in text
    assert "Maintenance Instrumentation" in text


def test_fields_are_extracted_without_any_model():
    """A form is label-and-value. Reading it must not depend on an LLM being
    reachable — client=None and model="" here, and it still works."""
    raw = _aan_bytes()
    fields = aan_reader.extract_fields("aan.docx", raw, _extract_text("aan.docx", raw))
    assert fields["aan_file_number"] == "PUR-INSTMNT-1000150"
    assert fields["aan_pr_number"] == "10072565"
    assert fields["aan_q_value"] == "4,12,74,555.90"
    assert fields["aan_name_of_proposal"] == "ARC for Supply of Transmitters"


def test_a_blank_row_is_not_treated_as_an_answer():
    """A form row left empty must still be asked, not silently recorded."""
    document = docx.Document()
    table = document.add_table(rows=0, cols=2)
    for label, value in [("File No.:", "PUR-X-1"), ("Cost Center:", ""), ("WBS:", "-")]:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = label, value
    buf = io.BytesIO()
    document.save(buf)
    fields = aan_reader.extract_fields("aan.docx", buf.getvalue(), "")
    assert fields["aan_q_file_no"] == "PUR-X-1"
    assert "aan_q_cost_center" not in fields
    assert "aan_q_wbs" not in fields
