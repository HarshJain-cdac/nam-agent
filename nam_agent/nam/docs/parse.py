"""Reading an uploaded document: what it is, and the text on each page.

    sniff()    the format from the bytes themselves (magic numbers, and for a
               zip the part that makes it a DOCX or an XLSX), checked against
               the file's name and declared type. A PNG named .pdf is refused.
    extract()  normal extraction first, per page: pypdf for a PDF, python-docx
               for a DOCX (tables included, nam_agent/tools/aan_reader.py), openpyxl
               for an XLSX, one page per sheet. Then usable() looks at each
               page. Only the pages without usable text (a scanned page, an
               image, or machine text that cannot be read) go to OCR, through
               the ocr_document tool (plan D8). No OCR backend, or one that is
               down, leaves those pages marked "no usable text": the document
               is still stored, never refused for it.

Every page says where its text came from (source: text | ocr | none). The
text is the document's content, not instructions. active_content() refuses
what could run (PDF JavaScript or launch actions, Office macros) and notes
what points elsewhere (embedded files, external links); extract() records
every instruction-like passage it finds (nam_agent/nam/guard.py scan) as a flag
on the document. A flagged document is still stored: its text reaches a
model only as an untrusted block, with those passages removed.

The readers are the [nit] extra (pypdf, python-docx, openpyxl), already the
dashboard's upload path; no OCR engine is a dependency of this file.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field

from nam_agent.nam.errors import NamError

MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "tiff": "image/tiff",
}
EXTENSIONS = {"pdf": ("pdf",), "docx": ("docx",), "xlsx": ("xlsx", "xlsm"), "png": ("png",),
              "jpeg": ("jpg", "jpeg"), "tiff": ("tif", "tiff")}
IMAGES = ("png", "jpeg", "tiff")
GENERIC_TYPES = ("", "application/octet-stream", "binary/octet-stream")

MAX_PAGES = 500
PAGE_TEXT_CAP = 100_000          # characters kept per page
ZIP_MAX_ENTRIES = 5_000          # a DOCX or XLSX with more is not a document
ZIP_MAX_UNPACKED = 200 * 1024 * 1024
ZIP_MAX_RATIO = 200              # unpacked / packed: a zip bomb is far above this
MIN_USABLE_CHARS = 20


def _unsupported(message: str, *details: str) -> NamError:
    return NamError("unsupported_type", message, details=list(details))


def _zip_kind(data: bytes) -> str:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        raise _unsupported("the file is a damaged zip archive, not a readable DOCX or "
                           "XLSX") from None
    unpacked = sum(i.file_size for i in infos)
    if len(infos) > ZIP_MAX_ENTRIES or unpacked > ZIP_MAX_UNPACKED \
            or unpacked > ZIP_MAX_RATIO * max(len(data), 1):
        raise NamError("too_large", "the archive unpacks to far more than a document would "
                       f"({len(infos)} parts, {unpacked} bytes)")
    names = {i.filename for i in infos}
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    raise _unsupported("the zip archive is neither a DOCX nor an XLSX")


def sniff(data: bytes, filename: str, declared: str = "") -> str:
    """The format of `data` (a key of MIME), or unsupported_type (415)."""
    head = data[:8]
    if head.startswith(b"%PDF-"):
        kind = "pdf"
    elif head.startswith(b"PK\x03\x04"):
        kind = _zip_kind(data)
    elif head.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    elif head.startswith(b"\xff\xd8\xff"):
        kind = "jpeg"
    elif head.startswith((b"II*\x00", b"MM\x00*")):
        kind = "tiff"
    elif head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        raise _unsupported("legacy .doc and .xls files are not read here: save the file as "
                           ".docx or .xlsx and upload it again")
    else:
        raise _unsupported("the content is not a PDF, DOCX, XLSX, PNG, JPEG or TIFF",
                           "accepted: pdf, docx, xlsx, png, jpg, tiff")
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in EXTENSIONS[kind]:
        raise _unsupported(f"the file is named .{ext or '(none)'} but its content is "
                           f"{kind.upper()}", f"expected extension: {', '.join(EXTENSIONS[kind])}")
    declared = (declared or "").split(";")[0].strip().lower()
    if declared not in GENERIC_TYPES and declared != MIME[kind]:
        raise _unsupported(f"Content-Type {declared} does not match the content "
                           f"({MIME[kind]})")
    return kind


_CID = re.compile(r"\(cid:\d+\)")


def usable(text: str, min_chars: int = MIN_USABLE_CHARS) -> bool:
    """Whether a page's text can be read: enough letters and digits, and not
    mostly the debris a PDF with a broken font map gives ('(cid:12)',
    replacement characters, private-use glyphs)."""
    cleaned = _CID.sub("", text or "")
    chars = [c for c in cleaned if not c.isspace()]
    if not chars:
        return False
    good = sum(c.isalnum() for c in chars)
    bad = sum(c == "�" or unicodedata.category(c) in ("Co", "Cn", "Cs", "Cc")
              for c in chars)
    cids = len(_CID.findall(text or ""))
    return (good >= min_chars and good / len(chars) >= 0.5
            and bad / len(chars) < 0.1 and cids < good / 4)


@dataclass
class Page:
    number: int
    text: str
    source: str                    # text | ocr | none
    usable: bool
    regions: list = field(default_factory=list)
    confidence: float | None = None

    def as_dict(self) -> dict:
        return {"number": self.number, "text": self.text, "source": self.source,
                "usable": self.usable, "regions": self.regions,
                "confidence": self.confidence}


@dataclass
class Parsed:
    kind: str
    mime: str
    pages: list[Page]
    rows: list[list[str]] = field(default_factory=list)    # DOCX table rows
    flags: list[dict] = field(default_factory=list)        # {page, kind, excerpt}
    ocr_engine: str | None = None
    ocr_pages: list[int] = field(default_factory=list)     # pages OCR was asked for
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "mime": self.mime,
                "pages": [p.as_dict() for p in self.pages], "rows": self.rows,
                "ocr_engine": self.ocr_engine, "ocr_pages": self.ocr_pages,
                "flags": self.flags[:50], "warnings": self.warnings[:20]}

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages if p.text)


def _needs_extra(exc: ImportError, kind: str) -> NamError:
    return NamError("internal", f"reading {kind.upper()} files needs the [nit] extra "
                    f"(pip install 'nam-agent[nit]'); missing {exc.name}")


def _pdf_pages(data: bytes, max_pages: int) -> list[str]:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise _needs_extra(exc, "pdf") from None
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise _unsupported("the PDF is password protected: upload it without the password")
        count = len(reader.pages)
    except NamError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise _unsupported(f"the PDF could not be read ({type(exc).__name__})") from None
    if count > max_pages:
        raise NamError("too_large", f"the PDF has {count} pages; the limit is {max_pages}")
    out = []
    for page in reader.pages:
        try:
            out.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one unreadable page goes to OCR, not the whole file
            out.append("")
    return out


def _docx(data: bytes) -> tuple[str, list[list[str]]]:
    try:
        import docx  # noqa: F401 - imported for the ImportError below
    except ImportError as exc:
        raise _needs_extra(exc, "docx") from None
    from nam_agent.tools import aan_reader

    try:
        return (aan_reader.document_text("upload.docx", data),
                [list(r) for r in aan_reader.document_rows("upload.docx", data)])
    except Exception as exc:  # noqa: BLE001 - python-docx raises many kinds on a bad file
        raise _unsupported(f"the DOCX could not be read ({type(exc).__name__})") from None


def _xlsx(data: bytes, max_pages: int) -> list[str]:
    try:
        import openpyxl
    except ImportError as exc:
        raise _needs_extra(exc, "xlsx") from None
    try:
        book = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        if len(book.worksheets) > max_pages:
            raise NamError("too_large", f"the workbook has {len(book.worksheets)} sheets; "
                           f"the limit is {max_pages}")
        sheets = []
        for sheet in book.worksheets:
            lines = [" | ".join(str(c) for c in row if c is not None)
                     for row in sheet.iter_rows(values_only=True)]
            sheets.append(f"Sheet: {sheet.title}\n" + "\n".join(x for x in lines if x))
        book.close()
        return sheets
    except NamError:
        raise
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many kinds on a bad file
        raise _unsupported(f"the XLSX could not be read ({type(exc).__name__})") from None


# ocr(pages) -> the ocr_document tool's result; raises NamError when OCR
# cannot run. None: no OCR backend is configured.
Ocr = Callable[[list[int]], dict] | None


def extract(data: bytes, kind: str, *, ocr: Ocr = None, min_chars: int = MIN_USABLE_CHARS,
            max_pages: int = MAX_PAGES) -> Parsed:
    parsed = Parsed(kind=kind, mime=MIME[kind], pages=[])
    if kind == "pdf":
        texts = _pdf_pages(data, max_pages)
    elif kind == "docx":
        text, parsed.rows = _docx(data)
        texts = [text]
    elif kind == "xlsx":
        texts = _xlsx(data, max_pages)
    else:
        texts = [""]                  # an image has no text layer
    for n, text in enumerate(texts, start=1):
        text = text[:PAGE_TEXT_CAP]
        ok = usable(text, min_chars)
        parsed.pages.append(Page(n, text, "text" if ok else "none", ok))

    # OCR only where it can help: a PDF page or an image without usable text.
    # A DOCX or XLSX has its text in the file; an empty sheet is just empty.
    wanted = [p.number for p in parsed.pages if not p.usable and kind in ("pdf", *IMAGES)]
    if wanted:
        _ocr(parsed, wanted, ocr, min_chars)
    unread = [p.number for p in parsed.pages if not p.usable]
    if unread and kind in ("pdf", *IMAGES):
        parsed.warnings.append(f"no usable text on page(s) {_ranges(unread)}")
    _flag(parsed)
    return parsed


def _flag(parsed: Parsed) -> None:
    from nam_agent.nam import guard

    for page in parsed.pages:
        for f in guard.scan(page.text):
            parsed.flags.append({"page": page.number, **f.as_dict()})
    for label, value in parsed.rows:
        for f in guard.scan(f"{label}: {value}"):
            parsed.flags.append({"page": 1, **f.as_dict()})
    pages = sorted({f["page"] for f in parsed.flags})
    if pages:
        parsed.warnings.append(f"page(s) {_ranges(pages)} contain text that reads like an "
                               "instruction to a model; it is kept as data and never followed")


_PDF_ACTIVE = re.compile(rb"/(JavaScript|JS|Launch|SubmitForm|ImportData)\b")
_PDF_EMBEDDED = re.compile(rb"/(EmbeddedFile|RichMedia)\b")


def active_content(data: bytes, kind: str) -> list[dict]:
    """unsupported_type for content that can run; flags for content that
    points elsewhere. NAM reads documents, it never needs either."""
    flags: list[dict] = []
    if kind == "pdf":
        found = _PDF_ACTIVE.search(data) or _pdf_catalog_active(data)
        if found:
            raise _unsupported("the PDF carries active content (JavaScript, launch or form "
                               "actions): print it to a plain PDF and upload that")
        if _PDF_EMBEDDED.search(data):
            flags.append({"page": 1, "kind": "embedded_file",
                          "excerpt": "the PDF carries embedded files; they are not read"})
    elif kind in ("docx", "xlsx"):
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = archive.namelist()
        if any(n.lower().endswith("vbaproject.bin") or n.lower().endswith(".bin")
               and "activex" in n.lower() for n in names):
            raise _unsupported("the file carries macros or ActiveX controls: save it as a "
                               "plain .docx or .xlsx and upload that")
        if any("/embeddings/" in n.lower() for n in names):
            flags.append({"page": 1, "kind": "embedded_file",
                          "excerpt": "the file embeds other files; they are not read"})
        for n in names:
            if n.endswith(".rels"):
                rels = archive.read(n)[:1_000_000]
                if b'TargetMode="External"' in rels:
                    flags.append({"page": 1, "kind": "external_link",
                                  "excerpt": "the file links to content outside it; it is "
                                             "not fetched"})
                    break
    return flags


def _pdf_catalog_active(data: bytes) -> bool:
    """Active content hidden in a compressed object stream, where the raw
    bytes do not show it: the catalog's OpenAction, names tree and page
    actions, read through pypdf."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        root = reader.trailer["/Root"]
        action = root.get("/OpenAction")
        if hasattr(action, "get_object"):
            action = action.get_object()
        if isinstance(action, dict) and action.get("/S") in ("/JavaScript", "/Launch"):
            return True
        names = root.get("/Names")
        if names is not None and "/JavaScript" in names.get_object():
            return True
        if "/AA" in root:
            return True
        return any("/AA" in page for page in reader.pages[:MAX_PAGES])
    except Exception:  # noqa: BLE001 - an unreadable PDF is refused by the reader itself
        return False


def _ocr(parsed: Parsed, wanted: list[int], ocr: Ocr, min_chars: int) -> None:
    if ocr is None:
        parsed.warnings.append(f"page(s) {_ranges(wanted)} have no usable text and no OCR "
                               "backend is configured (NAM_OCR_BACKEND)")
        return
    parsed.ocr_pages = list(wanted)
    try:
        result = ocr(wanted)
    except NamError as exc:
        parsed.warnings.append(f"OCR could not run ({exc.code}): page(s) {_ranges(wanted)} "
                               "are kept without text")
        return
    parsed.ocr_engine = result.get("engine")
    by_number = {p["number"]: p for p in result.get("pages") or []}
    for page in parsed.pages:
        got = by_number.get(page.number)
        if page.number not in wanted or got is None:
            continue
        page.text = (got.get("text") or "")[:PAGE_TEXT_CAP]
        page.source = "ocr"
        page.usable = usable(page.text, min_chars)
        page.regions = list(got.get("regions") or [])
        page.confidence = got.get("confidence")


def _ranges(numbers: list[int]) -> str:
    out, start, prev = [], None, None
    for n in sorted(numbers):
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if start != prev else str(start))
    return ", ".join(out)
