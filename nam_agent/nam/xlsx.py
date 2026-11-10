"""A small .xlsx writer, standard library only .

An .xlsx file is a zip of XML parts. NAM's output sheets need only rows of
text and numbers, a bold header row per sheet and readable column widths, so
this writes exactly that: no formulas, no merged cells, no dependency.

    xlsx([("Estimate", [["S.No", "Item"], [1, "ring joint gasket"]])]) -> bytes
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")      # not allowed in XML 1.0


def _col(n: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _cell(ref: str, value, bold: bool) -> str:
    style = ' s="1"' if bold else ""
    if isinstance(value, bool) or value is None:
        value = "" if value is None else ("yes" if value else "no")
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{style}><v>{value}</v></c>'
    text = escape(_BAD.sub("", str(value)))[:32000]
    return f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet(rows: list[list]) -> str:
    widths: dict[int, int] = {}
    body = []
    for r, row in enumerate(rows):
        cells = []
        for c, value in enumerate(row):
            cells.append(_cell(f"{_col(c)}{r + 1}", value, bold=(r == 0)))
            widths[c] = max(widths.get(c, 8), min(60, len(str(value if value is not None
                                                                 else "")) + 2))
        body.append(f'<row r="{r + 1}">{"".join(cells)}</row>')
    cols = "".join(f'<col min="{c + 1}" max="{c + 1}" width="{w}" customWidth="1"/>'
                   for c, w in sorted(widths.items()))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
            'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            + (f"<cols>{cols}</cols>" if cols else "")
            + f'<sheetData>{"".join(body)}</sheetData></worksheet>')


def xlsx(sheets: list[tuple[str, list[list]]]) -> bytes:
    """A workbook of (sheet name, rows); the first row of each is its header."""
    names = []
    for name, _ in sheets:
        clean = re.sub(r"[\[\]:*?/\\]", " ", str(name)).strip()[:31] or "Sheet"
        while clean in names:
            clean = (clean[:28] + f" {len(names)}")[:31]
        names.append(clean)
    ns = "http://schemas.openxmlformats.org"
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Types xmlns="{ns}/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.spreadsheetml.styles+xml"/>'
            + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType='
                      '"application/vnd.openxmlformats-officedocument.spreadsheetml.'
                      'worksheet+xml"/>' for i in range(len(sheets)))
            + "</Types>"),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{ns}/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{ns}/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{ns}/spreadsheetml/2006/main" '
            f'xmlns:r="{ns}/officeDocument/2006/relationships"><sheets>'
            + "".join(f'<sheet name="{escape(n)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                      for i, n in enumerate(names))
            + "</sheets></workbook>"),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{ns}/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i + 1}" Type="{ns}/officeDocument/2006/'
                      f'relationships/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
                      for i in range(len(sheets)))
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="{ns}/officeDocument/2006/'
              'relationships/styles" Target="styles.xml"/></Relationships>'),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<styleSheet xmlns="{ns}/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
            '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
            '<cellXfs count="2"><xf fontId="0"/><xf fontId="1" applyFont="1"/></cellXfs>'
            "</styleSheet>"),
    }
    for i, (_, rows) in enumerate(sheets):
        files[f"xl/worksheets/sheet{i + 1}.xml"] = _sheet(rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()
