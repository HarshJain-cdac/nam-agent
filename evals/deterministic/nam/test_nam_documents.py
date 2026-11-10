"""Documents on a NAM case (plan Step 11): nam_agent/nam/docs/, the ocr_document
tool and the HTTP routes.

Every format (PDF, DOCX, XLSX, PNG, JPEG, TIFF) round-trips: upload, parse,
view, the original bytes back, a new version, the history, a soft delete.
OCR (a scripted engine) is called only for the pages normal extraction could
not read: a scanned PDF page, an image, garbled machine text. Never for a
text PDF, a DOCX or an XLSX. With no engine, or one that is down, the
document is still stored and its pages say "no usable text".
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import sqlite3
import threading
import zipfile
import zlib

import pytest
from nam_fakes import IDENTITY, OFFICER, FakeOcr, ask, fake_kg, run

import nam_agent.nam as harness
from nam_agent.nam import api, audit, ocr, schema, store
from nam_agent.nam.docs import parse
from nam_agent.nam.docs import store as docs
from nam_agent.nam.errors import NamError

docx = pytest.importorskip("docx", reason="python-docx ships in the [nit] extra")
openpyxl = pytest.importorskip("openpyxl", reason="openpyxl ships in the [nit] extra")
pypdf = pytest.importorskip("pypdf", reason="pypdf ships in the [nit] extra")

OTHER = harness.Caller(tenant="acme", user="x1", roles=("U1",))
READER = harness.Caller(tenant="nrl", user="u2", roles=("U2",))
DRAFTER = harness.Caller(tenant="nrl", user="u4", roles=("U4",))
APPROVER = harness.Caller(tenant="nrl", user="u7", roles=("U7",))


# -- files, built in memory ---------------------------------------------------

def make_pdf(pages: list[str | None]) -> bytes:
    """A PDF with one page per entry: its text in Helvetica, or no text at all
    (None: what a scanned page looks like to a text extractor)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        body = b""
        if text is not None:
            safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            body = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1")
        objs.append(f"<< /Length {len(body)} >>\nstream\n".encode() + body + b"\nendstream")
        content = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>")
        kids.append(len(objs))
    objs[1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
               f"/Count {len(kids)} >>")
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for n, obj in enumerate(objs, start=1):
        offsets.append(out.tell())
        data = obj if isinstance(obj, bytes) else obj.encode()
        out.write(f"{n} 0 obj\n".encode() + data + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n"
              "%%EOF\n".encode())
    return out.getvalue()


def make_docx(rows=(("File No.:", "PUR-INSTMNT-1000150"), ("Tender Type :", "Open Tender")),
              title="Administrative Approval Note") -> bytes:
    document = docx.Document()
    document.add_paragraph(title)
    table = document.add_table(rows=0, cols=2)
    for label, value in rows:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = label, value
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def make_xlsx(rows=(("Item", "Qty", "Rate"), ("Gate valve 4in", 20, 1500))) -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "BOQ"
    for row in rows:
        sheet.append(list(row))
    book.create_sheet("Empty")
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def make_png(seed: int = 0) -> bytes:
    def chunk(tag, data):
        return (len(data).to_bytes(4, "big") + tag + data
                + zlib.crc32(tag + data).to_bytes(4, "big"))

    ihdr = (1).to_bytes(4, "big") * 2 + bytes([8, 0, 0, 0, 0])
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes([0, seed % 256]))) + chunk(b"IEND", b""))


TEXT_A = "Supply of 20 gate valves, class 150, flanged ends, cast steel body."
TEXT_B = "Supply of 24 gate valves, class 300, flanged ends, forged steel body."
GARBLED = "(cid:3)(cid:4)(cid:5)(cid:6)(cid:7)(cid:8)(cid:9)(cid:10)(cid:11)(cid:12)" * 3

# format -> (filename, first version, second version, text in the first)
FORMATS = {
    "pdf": ("need.pdf", lambda: make_pdf([TEXT_A]), lambda: make_pdf([TEXT_B]), "20 gate valves"),
    "docx": ("aan.docx", make_docx, lambda: make_docx(title="AAN, revised"),
             "PUR-INSTMNT-1000150"),
    "xlsx": ("boq.xlsx", make_xlsx, lambda: make_xlsx((("Item", "Qty"), ("Gate valve", 24)),),
             "Gate valve 4in"),
    "png": ("scan.png", lambda: make_png(1), lambda: make_png(2), "scanned text on page 1"),
    "jpeg": ("scan.jpg", lambda: b"\xff\xd8\xff\xe0" + b"1" * 64,
             lambda: b"\xff\xd8\xff\xe0" + b"2" * 64, "scanned text on page 1"),
    "tiff": ("scan.tif", lambda: b"II*\x00" + b"1" * 64, lambda: b"II*\x00" + b"2" * 64,
             "scanned text on page 1"),
}


# -- fixtures -----------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_env_ocr(monkeypatch):
    monkeypatch.delenv("NAM_OCR_BACKEND", raising=False)
    ocr._load.cache_clear()
    from nam_agent.nam import tools
    monkeypatch.setattr(tools, "_sleep", lambda s: None)


def _ctx(tmp_path, engine=None, conn=None, **kw):
    c = harness.Context(conn=conn or sqlite3.connect(":memory:"), home=tmp_path,
                        kg_url="http://kg", fetch=fake_kg(), ocr=engine, **kw)
    return c


def _case(c, caller=OFFICER) -> str:
    body = run(ask("i need to procure gaskets"), c, caller)
    assert body.get("case_id"), body
    return body["case_id"]


def _up(c, case_id, fmt="pdf", caller=OFFICER, data=None, **kw):
    name, first, _, _ = FORMATS[fmt]
    return docs.upload(c, caller, case_id, filename=kw.pop("filename", name),
                       data=data if data is not None else first(), kind=kw.pop("kind", "other"),
                       **kw)


def _code(fn) -> str:
    with pytest.raises(NamError) as err:
        fn()
    return err.value.code


# -- the round trip, per format -----------------------------------------------

@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_each_format_round_trips_with_version_history(fmt, tmp_path):
    engine = FakeOcr()
    c = _ctx(tmp_path, engine)
    case_id = _case(c)
    name, first, second, needle = FORMATS[fmt]
    v1_bytes, v2_bytes = first(), second()

    up = docs.upload(c, OFFICER, case_id, filename=name, data=v1_bytes, kind="need",
                     reason="the officer's file")
    doc = up["document"]
    assert doc["mime"] == parse.MIME[fmt] and doc["doc_version"] == 1
    assert doc["sha256"] == hashlib.sha256(v1_bytes).hexdigest()
    assert up["content"]["untrusted"] is True
    assert needle in "\n".join(p["text"] for p in up["content"]["pages"])

    assert docs.content(c, OFFICER, doc["doc_id"]) == (v1_bytes, parse.MIME[fmt], name)
    listed = docs.listing(c, OFFICER, case_id)["documents"]
    assert [d["doc_id"] for d in listed] == [doc["doc_id"]]

    v2 = docs.new_version(c, OFFICER, doc["doc_id"], filename=name, data=v2_bytes, expected=1,
                          reason="edited outside NAM")
    assert v2["document"]["doc_version"] == 2
    assert docs.content(c, OFFICER, doc["doc_id"])[0] == v2_bytes
    assert docs.content(c, OFFICER, doc["doc_id"], 1)[0] == v1_bytes
    old = docs.view(c, OFFICER, doc["doc_id"], 1)
    assert old["document"]["doc_version"] == 1 and old["document"]["latest_version"] == 2

    hist = docs.versions(c, OFFICER, doc["doc_id"])
    assert [(v["doc_version"], v["reason"], v["uploaded_by"]) for v in hist["versions"]] == \
        [(1, "the officer's file", "u1"), (2, "edited outside NAM", "u1")]
    assert hist["deleted"] is None

    gone = docs.delete(c, OFFICER, doc["doc_id"], expected=2, reason="uploaded to wrong case")
    assert gone["document"]["deleted"]["reason"] == "uploaded to wrong case"
    assert docs.listing(c, OFFICER, case_id)["documents"] == []
    assert docs.versions(c, OFFICER, doc["doc_id"])["deleted"]["by"] == "u1"
    assert docs.content(c, OFFICER, doc["doc_id"], 1)[0] == v1_bytes    # kept for audit

    for body, name_ in ((up, "document"), (hist, "document.versions")):
        assert schema.validate(body, schema.load(name_)) == []


# -- OCR only where needed ----------------------------------------------------

def test_a_text_pdf_never_calls_ocr(tmp_path):
    engine = FakeOcr()
    c = _ctx(tmp_path, engine)
    body = _up(c, _case(c), data=make_pdf([TEXT_A, TEXT_B]))
    assert engine.calls == []
    assert [p["source"] for p in body["content"]["pages"]] == ["text", "text"]
    assert audit.events(c.conn, kind="tool") == []


@pytest.mark.parametrize("fmt", ["docx", "xlsx"])
def test_office_files_never_call_ocr(fmt, tmp_path):
    engine = FakeOcr()
    c = _ctx(tmp_path, engine)
    _up(c, _case(c), fmt)
    assert engine.calls == []


def test_only_the_scanned_pages_of_a_pdf_go_to_ocr(tmp_path):
    engine = FakeOcr()
    c = _ctx(tmp_path, engine)
    body = _up(c, _case(c), data=make_pdf([TEXT_A, None, TEXT_B, None]))
    assert engine.calls == [("application/pdf", [2, 4])]
    pages = body["content"]["pages"]
    assert [p["source"] for p in pages] == ["text", "ocr", "text", "ocr"]
    assert pages[1]["text"].startswith("scanned text on page 2") and pages[1]["usable"]
    assert pages[1]["regions"] == [{"text": "20 gate valves p2", "box": [0.1, 0.1, 0.9, 0.2],
                                    "confidence": 0.93}]
    assert pages[1]["confidence"] == 0.91
    assert body["content"]["ocr_engine"] == "fake-ocr 1.0"
    assert body["document"]["ocr_pages"] == 2 and body["document"]["text_pages"] == 4
    [row] = audit.events(c.conn, kind="tool")
    assert (row["name"], row["status"], row["flow"]) == ("ocr_document", "ok", "nam.documents")


def test_garbled_machine_text_goes_to_ocr(tmp_path):
    engine = FakeOcr()
    c = _ctx(tmp_path, engine)
    body = _up(c, _case(c), data=make_pdf([GARBLED]))
    assert engine.calls == [("application/pdf", [1])]
    assert body["content"]["pages"][0]["source"] == "ocr"


def test_an_image_goes_to_ocr(tmp_path):
    engine = FakeOcr()
    c = _ctx(tmp_path, engine)
    _up(c, _case(c), "png")
    assert engine.calls == [("image/png", [1])]


def test_no_ocr_backend_stores_the_document_marked_no_usable_text(tmp_path):
    c = _ctx(tmp_path)
    body = _up(c, _case(c), data=make_pdf([TEXT_A, None]))
    pages = body["content"]["pages"]
    assert [(p["source"], p["usable"]) for p in pages] == [("text", True), ("none", False)]
    assert any("no OCR backend" in w for w in body["content"]["warnings"])
    assert audit.events(c.conn, kind="tool") == []


def test_ocr_down_is_retried_then_the_document_is_still_stored(tmp_path):
    engine = FakeOcr(down=True)
    c = _ctx(tmp_path, engine)
    body = _up(c, _case(c), "png")
    assert len(engine.calls) == 2                      # one retry, then ocr_unavailable
    assert body["content"]["pages"][0]["source"] == "none"
    assert any("ocr_unavailable" in w for w in body["content"]["warnings"])
    [row] = audit.events(c.conn, kind="tool")
    assert (row["status"], row["error_code"], row["attempts"]) == \
        ("unavailable", "ocr_unavailable", 2)


def test_an_engine_answering_outside_the_contract_is_not_trusted(tmp_path):
    engine = FakeOcr(box=(10, 10, 900, 200))           # pixels, not fractions
    c = _ctx(tmp_path, engine)
    body = _up(c, _case(c), "png")
    assert body["content"]["pages"][0]["source"] == "none"
    assert any("internal_schema" in w for w in body["content"]["warnings"])


def test_usable_text_check():
    assert parse.usable(TEXT_A)
    for bad in ("", "   \n ", "p. 1", GARBLED, "�" * 40, "" * 30 + "abc"):
        assert not parse.usable(bad), bad


# -- the OCR backend loader ---------------------------------------------------

def test_ocr_backend_is_loaded_from_config():
    assert ocr.configured("") is None
    assert isinstance(ocr.configured("nam_fakes:FakeOcr"), FakeOcr)
    for spec in ("nam_fakes", "no_such_module:X", "nam_fakes:NoSuchClass", "json:JSONDecoder"):
        with pytest.raises(ocr.OcrUnavailable):
            ocr.configured(spec)


def test_ocr_backend_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NAM_OCR_BACKEND", "nam_fakes:FakeOcr")
    c = _ctx(tmp_path)                                  # no engine on the context
    body = _up(c, _case(c), "png")
    assert body["content"]["pages"][0]["source"] == "ocr"


def test_a_broken_ocr_config_is_reported_not_taken_for_no_ocr(tmp_path, monkeypatch):
    monkeypatch.setenv("NAM_OCR_BACKEND", "no_such_module:X")
    c = _ctx(tmp_path)
    body = _up(c, _case(c), "png")
    assert any("ocr_unavailable" in w for w in body["content"]["warnings"])


def test_the_ocr_tool_reads_only_the_callers_tenant(tmp_path):
    from nam_agent.nam import tools

    engine = FakeOcr()
    c = _ctx(tmp_path, engine)
    body = _up(c, _case(c), data=make_pdf([TEXT_A]))
    sha = body["document"]["sha256"]
    with store.acting(store.Actor("acme", "x1")), pytest.raises(NamError) as err:
        tools.call(c, "ocr_document", {"sha256": sha, "mime": "application/pdf",
                                       "pages": [1]})
    assert err.value.code == "internal" and engine.calls == []
    assert _code(lambda: tools.call(c, "ocr_document", {"sha256": "abc",
                                                        "mime": "application/pdf",
                                                        "pages": [1]})) == "schema"


# -- what is refused ----------------------------------------------------------

@pytest.mark.parametrize("filename,data,declared", [
    ("scan.pdf", make_png(), ""),                                  # a PNG named .pdf
    ("need.pdf", make_pdf([TEXT_A]), "image/png"),                 # declared type lies
    ("aan.xlsx", make_docx(), ""),                                 # a DOCX named .xlsx
    ("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 100, ""),
    ("run.pdf", b"#!/bin/sh\nrm -rf /\n", ""),
    ("notes.zip", b"PK\x03\x04" + b"0" * 40, ""),                  # a damaged zip
])
def test_wrong_content_is_415(filename, data, declared, tmp_path):
    c = _ctx(tmp_path)
    case_id = _case(c)
    assert _code(lambda: _up(c, case_id, filename=filename, data=data,
                             declared_type=declared)) == "unsupported_type"
    assert not (tmp_path / "nam" / "documents").exists() or \
        not any((tmp_path / "nam" / "documents").rglob("*"))


def test_a_corrupt_or_locked_pdf_is_415_not_a_crash(tmp_path):
    c = _ctx(tmp_path)
    case_id = _case(c)
    assert _code(lambda: _up(c, case_id, data=b"%PDF-1.4\nnot really a pdf")) == \
        "unsupported_type"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(100, 100)
    writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    assert _code(lambda: _up(c, case_id, data=buf.getvalue())) == "unsupported_type"


def test_size_and_page_limits(tmp_path, monkeypatch):
    c = _ctx(tmp_path, max_doc_bytes=1000)
    case_id = _case(c)
    assert _code(lambda: _up(c, case_id, data=make_pdf([TEXT_A] * 20))) == "too_large"
    assert _code(lambda: _up(c, case_id, data=b"")) == "bad_request"
    c.max_doc_bytes = 10 * 1024 * 1024
    monkeypatch.setattr(parse, "MAX_PAGES", 3)
    assert _code(lambda: _up(c, case_id, data=make_pdf([TEXT_A] * 4))) == "too_large"


def test_a_zip_bomb_is_refused(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"0" * (20 * 1024 * 1024))
    c = _ctx(tmp_path)
    assert _code(lambda: _up(c, _case(c), "docx", data=buf.getvalue())) == "too_large"


def test_the_filename_is_cleaned_and_needs_an_extension(tmp_path):
    c = _ctx(tmp_path)
    case_id = _case(c)
    body = _up(c, case_id, filename="../../etc/need.pdf")
    assert body["document"]["filename"] == "need.pdf"
    assert _code(lambda: _up(c, case_id, filename="noextension")) == "bad_request"
    assert _code(lambda: _up(c, case_id, kind="Not A Kind!")) == "bad_request"


# -- tenants, roles, stages ---------------------------------------------------

def test_another_tenant_can_touch_nothing(tmp_path):
    c = _ctx(tmp_path)
    case_id = _case(c)
    doc_id = _up(c, case_id)["document"]["doc_id"]
    for fn in (lambda: _up(c, case_id, caller=OTHER),
               lambda: docs.listing(c, OTHER, case_id),
               lambda: docs.view(c, OTHER, doc_id),
               lambda: docs.content(c, OTHER, doc_id),
               lambda: docs.versions(c, OTHER, doc_id),
               lambda: docs.new_version(c, OTHER, doc_id, filename="n.pdf",
                                        data=make_pdf([TEXT_B]), expected=1),
               lambda: docs.delete(c, OTHER, doc_id, expected=1, reason="x")):
        assert _code(fn) == "forbidden_tenant"


def test_roles(tmp_path):
    c = _ctx(tmp_path)
    case_id = _case(c)
    doc_id = _up(c, case_id)["document"]["doc_id"]
    assert docs.view(c, READER, doc_id)["document"]["doc_id"] == doc_id
    assert _code(lambda: _up(c, case_id, caller=APPROVER)) == "forbidden_role"
    assert _up(c, case_id, caller=DRAFTER)["document"]["uploaded_by"] == "u4"
    assert _code(lambda: docs.delete(c, DRAFTER, doc_id, expected=1, reason="x")) == \
        "forbidden_role"


def _set_workflow(c, case_id, **flags):
    row = store.case_row(c.conn, case_id)
    record = {**row["record"], "workflow": {**(row["record"].get("workflow") or {}), **flags}}
    with store.acting(store.Actor("nrl", "u1")):
        store.write(c.conn, case_id, record, expected=store.version_of(c.conn, case_id),
                    stage=row["stage"], session_id="", reason="test")


def test_a_frozen_package_keeps_its_documents_and_a_closed_case_takes_none(tmp_path):
    c = _ctx(tmp_path)
    case_id = _case(c)
    doc_id = _up(c, case_id)["document"]["doc_id"]
    _set_workflow(c, case_id, frozen={"at_version": 3})
    assert _code(lambda: docs.delete(c, OFFICER, doc_id, expected=1, reason="x")) == \
        "stage_gate"
    assert docs.new_version(c, OFFICER, doc_id, filename="need.pdf", data=make_pdf([TEXT_B]),
                            expected=1)["document"]["doc_version"] == 2      # F39
    _set_workflow(c, case_id, closed=True)
    assert _code(lambda: _up(c, case_id)) == "stage_gate"
    assert _code(lambda: docs.new_version(c, OFFICER, doc_id, filename="need.pdf",
                                          data=make_pdf([TEXT_A]), expected=2)) == "stage_gate"


# -- versions: optimistic, retry-safe -----------------------------------------

def test_a_new_version_against_an_old_one_is_stale(tmp_path):
    c = _ctx(tmp_path)
    doc_id = _up(c, _case(c))["document"]["doc_id"]
    docs.new_version(c, OFFICER, doc_id, filename="need.pdf", data=make_pdf([TEXT_B]),
                     expected=1)
    err = _code(lambda: docs.new_version(c, OFFICER, doc_id, filename="need.pdf",
                                         data=make_pdf(["Third text, 30 gate valves here."]),
                                         expected=1))
    assert err == "stale_version"
    assert _code(lambda: docs.delete(c, OFFICER, doc_id, expected=1, reason="x")) == \
        "stale_version"


def test_repeating_a_new_version_stores_nothing_twice(tmp_path):
    c = _ctx(tmp_path)
    doc_id = _up(c, _case(c))["document"]["doc_id"]
    first = docs.new_version(c, OFFICER, doc_id, filename="need.pdf", data=make_pdf([TEXT_B]),
                             expected=1)
    retry = docs.new_version(c, OFFICER, doc_id, filename="need.pdf", data=make_pdf([TEXT_B]),
                             expected=1)
    assert retry["replayed"] and retry["document"]["doc_version"] == 2
    same = docs.new_version(c, OFFICER, doc_id, filename="need.pdf", data=make_pdf([TEXT_B]),
                            expected=2)
    assert same["replayed"] and first["document"]["sha256"] == same["document"]["sha256"]
    assert len(docs.versions(c, OFFICER, doc_id)["versions"]) == 2


def test_a_new_version_keeps_the_format(tmp_path):
    c = _ctx(tmp_path)
    doc_id = _up(c, _case(c))["document"]["doc_id"]
    assert _code(lambda: docs.new_version(c, OFFICER, doc_id, filename="need.docx",
                                          data=make_docx(), expected=1)) == "unsupported_type"


def test_an_upload_with_an_idempotency_key_is_stored_once(tmp_path):
    c = _ctx(tmp_path)
    case_id = _case(c)
    first = _up(c, case_id, idempotency_key="upload-0001")
    again = _up(c, case_id, idempotency_key="upload-0001")
    assert again["replayed"] and again["document"]["doc_id"] == first["document"]["doc_id"]
    assert _code(lambda: _up(c, case_id, data=make_pdf([TEXT_B]),
                             idempotency_key="upload-0001")) == "bad_request"
    _up(c, case_id)                                   # no key: a second document
    assert len(docs.listing(c, OFFICER, case_id)["documents"]) == 2


def test_a_deleted_document_takes_no_new_version_and_deleting_twice_is_a_no_op(tmp_path):
    c = _ctx(tmp_path)
    doc_id = _up(c, _case(c))["document"]["doc_id"]
    docs.delete(c, OFFICER, doc_id, expected=1, reason="duplicate")
    assert docs.delete(c, OFFICER, doc_id, expected=1, reason="duplicate")["replayed"]
    assert _code(lambda: docs.new_version(c, OFFICER, doc_id, filename="need.pdf",
                                          data=make_pdf([TEXT_B]), expected=1)) == "not_found"
    assert _code(lambda: docs.delete(c, OFFICER, doc_id, expected=1, reason=" ")) in \
        ("bad_request",)


def test_a_file_changed_on_disk_is_detected(tmp_path):
    c = _ctx(tmp_path)
    body = _up(c, _case(c))
    blob = tmp_path / "nam" / "documents" / "nrl" / body["document"]["sha256"]
    blob.write_bytes(b"%PDF-1.4 tampered")
    assert _code(lambda: docs.content(c, OFFICER, body["document"]["doc_id"])) == "internal"


def test_unknown_documents_are_not_found(tmp_path):
    c = _ctx(tmp_path)
    for doc_id in ("DOC-000000000000", "../etc", "doc-1"):
        assert _code(lambda d=doc_id: docs.view(c, OFFICER, d)) == "not_found"


def test_every_document_operation_is_audited(tmp_path):
    c = _ctx(tmp_path)
    case_id = _case(c)
    doc_id = _up(c, case_id)["document"]["doc_id"]
    docs.new_version(c, OFFICER, doc_id, filename="need.pdf", data=make_pdf([TEXT_B]),
                     expected=1)
    _code(lambda: docs.delete(c, OFFICER, doc_id, expected=1, reason="x"))
    docs.delete(c, OFFICER, doc_id, expected=2, reason="done")
    rows = audit.events(c.conn, kind="document")
    assert [(r["name"], r["status"]) for r in rows] == \
        [("upload", "ok"), ("update", "ok"), ("delete", "failed"), ("delete", "ok")]
    assert {r["case_id"] for r in rows} == {case_id}
    assert rows[2]["error_code"] == "stale_version"


# -- the AAN reader, through the parser (was test_aan_extraction.py) ----------

def test_an_aan_is_read_from_its_tables():
    """An AAN is a form: its fields live in tables. Reading only paragraphs
    gave the title and nothing that matters."""
    from nam_agent.tools import aan_reader

    raw = make_docx((("File No.:", "PUR-INSTMNT-1000150"), ("PR No.:", "10072565"),
                     ("Tender Type :", "Open Tender")))
    parsed = parse.extract(raw, parse.sniff(raw, "aan.docx"))
    assert "PUR-INSTMNT-1000150" in parsed.text and "Open Tender" in parsed.text
    assert ["PR No.:", "10072565"] in parsed.rows
    fields = aan_reader.extract_fields("aan.docx", raw, parsed.text)
    assert fields["aan_file_number"] == "PUR-INSTMNT-1000150"
    assert fields["aan_pr_number"] == "10072565"


# -- over HTTP ----------------------------------------------------------------

MAX_DOC = 200_000


@pytest.fixture()
def server(tmp_path):
    db = tmp_path / "state.db"
    engine = FakeOcr()

    def make_ctx():
        conn = sqlite3.connect(db)
        return harness.Context(conn=conn, home=tmp_path, kg_url="http://kg", fetch=fake_kg(),
                               ocr=engine), conn.close

    srv = api.serve("127.0.0.1", 0, make_ctx, max_body=4096, model_configured=False,
                    kg_url="", max_doc=MAX_DOC)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    conn = sqlite3.connect(db)
    case_id = _case(harness.Context(conn=conn, home=tmp_path, kg_url="http://kg",
                                    fetch=fake_kg()))
    conn.close()
    yield srv.server_address[1], case_id
    srv.shutdown()
    srv.server_close()


def _http(port, method, path, data=None, headers=None, identity=True):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {**(IDENTITY if identity else {}), **(headers or {})}
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    assert b"Traceback" not in payload
    return resp.status, payload, resp


def test_documents_over_http(server):
    port, case_id = server
    pdf = make_pdf([TEXT_A, None])
    status, raw, _ = _http(port, "POST", f"/v1/nam/cases/{case_id}/documents", pdf,
                           {"Content-Type": "application/pdf", "X-Filename": "need%20v1.pdf",
                            "X-Doc-Kind": "need", "Idempotency-Key": "http-up-0001"})
    assert status == 201, raw
    body = json.loads(raw)
    doc_id = body["document"]["doc_id"]
    assert body["document"]["filename"] == "need v1.pdf"
    assert [p["source"] for p in body["content"]["pages"]] == ["text", "ocr"]
    status, raw, _ = _http(port, "POST", f"/v1/nam/cases/{case_id}/documents", pdf,
                           {"Content-Type": "application/pdf", "X-Filename": "need v1.pdf",
                            "X-Doc-Kind": "need", "Idempotency-Key": "http-up-0001"})
    assert status == 200 and json.loads(raw)["replayed"]

    status, raw, _ = _http(port, "GET", f"/v1/nam/cases/{case_id}/documents")
    assert status == 200 and len(json.loads(raw)["documents"]) == 1
    status, raw, resp = _http(port, "GET", f"/v1/nam/documents/{doc_id}/content")
    assert status == 200 and raw == pdf
    assert resp.getheader("Content-Type") == "application/pdf"
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    assert "attachment" in resp.getheader("Content-Disposition")

    status, raw, _ = _http(port, "PUT", f"/v1/nam/documents/{doc_id}", make_pdf([TEXT_B]),
                           {"Content-Type": "application/pdf", "X-Filename": "need.pdf",
                            "X-Doc-Version": "1", "X-Doc-Reason": "revised"})
    assert status == 200 and json.loads(raw)["document"]["doc_version"] == 2
    status, raw, _ = _http(port, "GET", f"/v1/nam/documents/{doc_id}?version=1")
    assert json.loads(raw)["content"]["pages"][0]["text"].startswith("Supply of 20")
    status, raw, _ = _http(port, "GET", f"/v1/nam/documents/{doc_id}/versions")
    assert [v["reason"] for v in json.loads(raw)["versions"]] == ["", "revised"]

    status, raw, _ = _http(port, "DELETE", f"/v1/nam/documents/{doc_id}", None,
                           {"X-Doc-Version": "1", "X-Doc-Reason": "wrong"})
    assert status == 409 and json.loads(raw)["error"]["code"] == "stale_version"
    status, raw, _ = _http(port, "DELETE", f"/v1/nam/documents/{doc_id}", None,
                           {"X-Doc-Version": "2"})
    assert status == 400                                  # a delete needs a reason
    status, raw, _ = _http(port, "DELETE", f"/v1/nam/documents/{doc_id}", None,
                           {"X-Doc-Version": "2", "X-Doc-Reason": "wrong%20case"})
    assert status == 200 and json.loads(raw)["document"]["deleted"]["reason"] == "wrong case"


def test_http_refusals(server):
    port, case_id = server
    path = f"/v1/nam/cases/{case_id}/documents"
    hdrs = {"Content-Type": "application/pdf", "X-Filename": "a.pdf", "X-Doc-Kind": "need"}
    status, raw, _ = _http(port, "POST", path, make_png(), {**hdrs})
    assert status == 415
    status, raw, _ = _http(port, "POST", path, b"x" * (MAX_DOC + 1), hdrs)
    assert status == 413 and json.loads(raw)["error"]["code"] == "too_large"
    status, raw, _ = _http(port, "POST", path, make_pdf([TEXT_A]), hdrs, identity=False)
    assert status == 401
    status, raw, _ = _http(port, "POST", path, make_pdf([TEXT_A]), {"Content-Type":
                                                                    "application/pdf"})
    assert status == 400                                  # X-Filename is required
    status, raw, _ = _http(port, "GET", "/v1/nam/documents/DOC-000000000000")
    assert status == 404
    status, raw, _ = _http(port, "POST", "/v1/nam/documents/DOC-000000000000", b"x",
                           {"Content-Type": "application/pdf"})
    assert status == 405
    status, raw, _ = _http(port, "PUT", "/v1/nam/documents/DOC-000000000000/versions", b"x",
                           {"Content-Type": "application/pdf"})
    assert status == 405
    status, raw, _ = _http(port, "GET", "/v1/nam/documents/DOC-000000000000?version=0")
    assert status == 400
    for body in (raw,):
        assert schema.validate(json.loads(body), schema.load("error")) == []


def test_the_schema_endpoint_lists_the_document_schemas(server):
    port, _ = server
    _status, raw, _ = _http(port, "GET", "/v1/nam/schema")
    assert {"document", "document.list", "document.versions"} <= set(
        json.loads(raw)["schemas"])
