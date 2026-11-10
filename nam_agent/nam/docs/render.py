"""A generated document as a .docx, with the standard library only.

The smallest WordprocessingML package Word and LibreOffice open: headings,
paragraphs and tables, nothing else. docs/parse.py reads it back, so a
generated document is parsed and stored like an uploaded one (docs/store.py).
The bytes are the same for the same content (fixed zip timestamps), so a
regeneration that changes nothing stores no new version.
"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
          'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.'
          'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
         'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_EPOCH = (2020, 1, 1, 0, 0, 0)


def _text(value) -> str:
    # XML 1.0 has no place for most control characters
    clean = "".join(ch for ch in str(value) if ch in "\t\n" or ord(ch) >= 32)
    return escape(clean)


def _para(text, *, bold: bool = False, size: int = 0) -> str:
    props = ("<w:rPr>" + ("<w:b/>" if bold else "") + (f'<w:sz w:val="{size}"/>' if size
                                                       else "") + "</w:rPr>") \
        if bold or size else ""
    runs = []
    for i, part in enumerate(str(text).split("\n")):
        if i:
            runs.append(f"<w:r>{props}<w:br/></w:r>")
        runs.append(f'<w:r>{props}<w:t xml:space="preserve">{_text(part)}</w:t></w:r>')
    return f"<w:p>{''.join(runs)}</w:p>"


def _table(rows: list[list]) -> str:
    border = ('<w:tblBorders>' + "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
        + "</w:tblBorders>")
    out = [f"<w:tbl><w:tblPr>{border}</w:tblPr>"]
    for r, row in enumerate(rows):
        cells = "".join(f"<w:tc>{_para('' if v is None else v, bold=r == 0)}</w:tc>"
                        for v in row)
        out.append(f"<w:tr>{cells}</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def docx(title: str, blocks: list[tuple]) -> bytes:
    """blocks: ("h", text), ("p", text) or ("table", [header, *rows])."""
    body = [_para(title, bold=True, size=32)]
    for kind, value in blocks:
        if kind == "h":
            body.append(_para(value, bold=True, size=26))
        elif kind == "p":
            body.append(_para(value))
        elif kind == "table":
            body.append(_table(value))
        else:
            raise ValueError(f"unknown block {kind!r}")
    xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_NS}>'
           f"<w:body>{''.join(body)}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in (("[Content_Types].xml", _TYPES), ("_rels/.rels", _RELS),
                           ("word/document.xml", xml)):
            info = zipfile.ZipInfo(name, date_time=_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return buf.getvalue()
