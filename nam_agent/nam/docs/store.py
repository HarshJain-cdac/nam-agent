"""The documents of a case: every version kept, the bytes on disk, the parsed
text in SQLite.

    nam_documents          one row per document: its case (and so its
                           tenant), kind, current version, and whether it is
                           deleted
    nam_document_versions  one row per version: who uploaded it, why, the
                           file's name, type, size and SHA-256, and its
                           parsed pages (docs/parse.py)
    <home>/nam/documents/<tenant>/<sha256>
                           the bytes, stored once per tenant by content and
                           checked against their hash on every read

upload() opens a document on a case, new_version() adds a version (a file
edited outside NAM, F39), delete() hides it from flows and listings but keeps
every version and the bytes for audit. A document belongs to the case's
tenant: another tenant gets forbidden_tenant, as for the case itself.

Writes are optimistic like the Need Record: new_version() and delete() name
the version they were made against and get stale_version if it moved. They
are safe to retry: an upload with the same Idempotency-Key, or a new version
that repeats the one just stored, returns what is there instead of storing
it twice. Every operation writes one audit row (kind "document").

Who may do what is the "documents" section of policy/permissions.json. A
closed case (handed off to NIT) takes no more writes; a frozen package keeps
its documents (no delete).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
from pathlib import Path

from nam_agent.nam import audit, permissions, schema, store
from nam_agent.nam.docs import parse
from nam_agent.nam.errors import VERSION, NamError
from nam_agent.nam.identity import Caller

DOC_ID = re.compile(r"^DOC-[0-9a-f]{12}$")
_KIND = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
MAX_DOC_BYTES = 25 * 1024 * 1024

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS nam_documents (
    doc_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL DEFAULT '',
    deleted_by TEXT NOT NULL DEFAULT '',
    delete_reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT ({store._NOW}),
    updated_at TEXT NOT NULL DEFAULT ({store._NOW})
);
CREATE INDEX IF NOT EXISTS nam_documents_by_case ON nam_documents (case_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS nam_documents_by_key ON nam_documents (tenant, idempotency_key)
    WHERE idempotency_key != '';
CREATE TABLE IF NOT EXISTS nam_document_versions (
    doc_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    at TEXT NOT NULL DEFAULT ({store._NOW}),
    tenant TEXT NOT NULL,
    user TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    pages INTEGER NOT NULL,
    parsed_json TEXT NOT NULL,
    PRIMARY KEY (doc_id, version)
);
"""


def ensure(conn: sqlite3.Connection) -> None:
    """Create or migrate these tables, once per database file per process
    (nam_agent.db.once): each call re-running the schema took the write lock."""
    from nam_agent import db

    db.once(conn, "nam.docs", _setup, table="nam_documents")


def _setup(conn: sqlite3.Connection) -> None:
    store.ensure(conn)
    conn.executescript(_SCHEMA)


# -- the bytes ------------------------------------------------------------------

def _blob(home: Path, tenant: str, sha: str) -> Path:
    if not _SHA.match(sha) or "/" in tenant or tenant in ("", ".", ".."):
        raise NamError("internal", "a document blob was addressed wrongly")
    return Path(home) / "nam" / "documents" / tenant / sha


def put_blob(home: Path, tenant: str, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    path = _blob(home, tenant, sha)
    if path.exists():
        return sha
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".up-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)       # atomic: a reader sees all of it or none
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return sha


def get_blob(home: Path, tenant: str, sha: str) -> bytes:
    path = _blob(home, tenant, sha)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise NamError("internal", "a stored document's file is missing") from None
    if hashlib.sha256(data).hexdigest() != sha:
        raise NamError("internal", "a stored document's file does not match its hash: it "
                       "was changed on disk")
    return data


# -- checks ---------------------------------------------------------------------

def _may(caller: Caller, op: str) -> None:
    permissions.require_document(caller, op)


def _writable(conn: sqlite3.Connection, case_id: str, op: str) -> None:
    from nam_agent.nam import orchestrator

    row = store.case_row(conn, case_id)
    wf = orchestrator.workflow_of(row["record"].get("workflow") or {}) if row else {}
    if wf.get("closed"):
        raise NamError("stage_gate", f"{case_id} is handed off to NIT and closed: its "
                       "documents can no longer change", case_id=case_id)
    if op == "delete" and wf.get("frozen"):
        raise NamError("stage_gate", f"{case_id}'s package is frozen: its documents are "
                       "kept; upload a new version instead", case_id=case_id)


def _filename(name: str) -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '"<>|*?:')
    name = name.strip(" .")[:200]
    if not name or "." not in name:
        raise NamError("bad_request", "X-Filename must name the file, with its extension")
    return name


def _doc(conn: sqlite3.Connection, caller: Caller, doc_id: str) -> dict:
    ensure(conn)
    if not DOC_ID.match(doc_id or ""):
        raise NamError("not_found", "no such document")
    row = conn.execute("SELECT doc_id, tenant, case_id, kind, version, deleted_at, deleted_by, "
                       "delete_reason, created_by, created_at, updated_at, idempotency_key "
                       "FROM nam_documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if row is None:
        raise NamError("not_found", f"no document {doc_id}")
    keys = ("doc_id", "tenant", "case_id", "kind", "version", "deleted_at", "deleted_by",
            "delete_reason", "created_by", "created_at", "updated_at", "idempotency_key")
    doc = dict(zip(keys, row, strict=True))
    if doc["tenant"] != caller.tenant:
        raise NamError("forbidden_tenant", f"document {doc_id} belongs to another tenant")
    return doc


def _version_row(conn, doc_id: str, version: int) -> dict | None:
    row = conn.execute("SELECT version, at, user, run_id, reason, filename, mime, bytes, sha256, "
                       "pages, parsed_json FROM nam_document_versions WHERE doc_id = ? AND "
                       "version = ?", (doc_id, version)).fetchone()
    if row is None:
        return None
    keys = ("version", "at", "user", "run_id", "reason", "filename", "mime", "bytes", "sha256",
            "pages", "parsed")
    out = dict(zip(keys, row, strict=True))
    out["parsed"] = json.loads(out["parsed"])
    return out


def _acting(caller: Caller, case_id: str, run_id: str):
    return store.acting(store.Actor(tenant=caller.tenant, user=caller.user,
                                    flow="nam.documents", run_id=run_id, case_id=case_id,
                                    roles=caller.roles))


def _audited(conn, op: str, doc_id: str, case_id: str, fn, **detail):
    try:
        out = fn()
    except NamError as exc:
        audit.record(conn, kind="document", name=op, status="failed", error_code=exc.code,
                     case_id=case_id, detail={"doc_id": doc_id, **detail,
                                              "message": exc.message[:200]})
        raise
    audit.record(conn, kind="document", name=op, status="ok", case_id=case_id,
                 detail={"doc_id": out.get("document", {}).get("doc_id", doc_id), **detail})
    return out


# -- parsing, with OCR through the tool layer ---------------------------------

def _parse(ctx, data: bytes, kind: str, sha: str) -> parse.Parsed:
    from nam_agent.nam import tools

    def ocr(pages: list[int]) -> dict:
        return tools.call(ctx, "ocr_document", {"sha256": sha, "mime": parse.MIME[kind],
                                                "pages": pages})

    has_ocr = getattr(ctx, "ocr", None) is not None
    if not has_ocr:
        from nam_agent.nam import ocr as ocr_pkg

        try:
            has_ocr = ocr_pkg.configured() is not None
        except ocr_pkg.OcrUnavailable:
            has_ocr = True          # named but broken: the tool reports it
    return parse.extract(data, kind, ocr=ocr if has_ocr else None,
                         min_chars=getattr(ctx, "ocr_min_chars", parse.MIN_USABLE_CHARS),
                         max_pages=parse.MAX_PAGES)


def _checked_body(ctx, data: bytes, filename: str, declared: str,
                  same_as: str = "") -> tuple[str, str, parse.Parsed]:
    """Size, format, the bytes stored, then the parse. `same_as`: the mime a
    new version must keep."""
    limit = getattr(ctx, "max_doc_bytes", MAX_DOC_BYTES)
    if not data:
        raise NamError("bad_request", "the document is empty")
    if len(data) > limit:
        raise NamError("too_large", f"the document is over {limit} bytes")
    kind = parse.sniff(data, filename, declared)
    if same_as and parse.MIME[kind] != same_as:
        raise NamError("unsupported_type", f"the document is {same_as}; a new version must "
                       f"be the same format, not {parse.MIME[kind]}")
    outside = parse.active_content(data, kind)
    sha = put_blob(ctx.home, store.current().tenant, data)
    parsed = _parse(ctx, data, kind, sha)
    parsed.flags = outside + parsed.flags
    if outside:
        parsed.warnings.append("; ".join(sorted({f["excerpt"] for f in outside})))
    if parsed.flags:
        audit.record(ctx.conn, kind="guard", name="document_flagged", status="flagged",
                     detail={"sha256": sha, "kinds": sorted({f["kind"] for f in parsed.flags}),
                             "pages": sorted({f["page"] for f in parsed.flags})[:50]})
    return kind, sha, parsed


def _insert_version(conn, doc_id: str, version: int, filename: str, data: bytes, sha: str,
                    parsed: parse.Parsed, reason: str) -> None:
    actor = store.current()
    conn.execute("INSERT INTO nam_document_versions (doc_id, version, tenant, user, run_id, "
                 "reason, filename, mime, bytes, sha256, pages, parsed_json) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (doc_id, version, actor.tenant, actor.user, actor.run_id, reason[:500],
                  filename, parsed.mime, len(data), sha, len(parsed.pages),
                  json.dumps(parsed.as_dict())))


# -- operations -----------------------------------------------------------------

def upload(ctx, caller: Caller, case_id: str, *, filename: str, data: bytes, kind: str,
           declared_type: str = "", reason: str = "", idempotency_key: str = "",
           run_id: str = "", generated: bool = False) -> dict:
    """`generated`: a document a flow made (playbooks/documents.store_docx),
    stored under the flow's own permission rather than the upload one."""
    conn = ctx.conn
    ensure(conn)
    permissions.require_case(conn, caller, case_id)
    with _acting(caller, case_id, run_id):
        def op():
            if not generated:
                _may(caller, "upload")
            if not _KIND.match(kind or ""):
                raise NamError("bad_request", "X-Doc-Kind must be a short lowercase word, "
                               "e.g. aan, boq, specification, scc, pqc, quotation, other")
            if idempotency_key and not _KEY.match(idempotency_key):
                raise NamError("bad_request", "Idempotency-Key must be 8 to 128 characters")
            _writable(conn, case_id, "upload")
            name = _filename(filename)
            sha_now = hashlib.sha256(data).hexdigest()
            if idempotency_key:
                replay = _replay(conn, caller, idempotency_key, sha_now, case_id)
                if replay:
                    return replay
            _, sha, parsed = _checked_body(ctx, data, name, declared_type)
            doc_id = f"DOC-{secrets.token_hex(6)}"
            try:
                with store._write(conn):
                    conn.execute("INSERT INTO nam_documents (doc_id, tenant, case_id, kind, "
                                 "version, idempotency_key, created_by) VALUES (?, ?, ?, ?, 1, "
                                 "?, ?)", (doc_id, caller.tenant, case_id, kind,
                                           idempotency_key, caller.user))
                    _insert_version(conn, doc_id, 1, name, data, sha, parsed, reason)
            except sqlite3.IntegrityError:
                # the same key, uploaded at the same moment by another request
                replay = _replay(conn, caller, idempotency_key, sha_now, case_id)
                if replay:
                    return replay
                raise
            return view(ctx, caller, doc_id)
        return _audited(conn, "upload", "", case_id, op, filename=filename[:200], kind=kind)


def _replay(conn, caller: Caller, key: str, sha: str, case_id: str) -> dict | None:
    row = conn.execute("SELECT d.doc_id, d.case_id, v.sha256 FROM nam_documents d JOIN "
                       "nam_document_versions v ON v.doc_id = d.doc_id AND v.version = 1 "
                       "WHERE d.tenant = ? AND d.idempotency_key = ?",
                       (caller.tenant, key)).fetchone()
    if row is None:
        return None
    if row[2] != sha or row[1] != case_id:
        raise NamError("bad_request", f"Idempotency-Key {key!r} was used for another upload")
    out = _view(conn, caller, row[0], None)
    out["replayed"] = True
    return out


def new_version(ctx, caller: Caller, doc_id: str, *, filename: str, data: bytes,
                expected: int, declared_type: str = "", reason: str = "",
                run_id: str = "", generated: bool = False) -> dict:
    conn = ctx.conn
    doc = _doc(conn, caller, doc_id)
    with _acting(caller, doc["case_id"], run_id):
        def op():
            if not generated:
                _may(caller, "update")
            if doc["deleted_at"]:
                raise NamError("not_found", f"document {doc_id} is deleted")
            _writable(conn, doc["case_id"], "update")
            name = _filename(filename)
            sha_now = hashlib.sha256(data).hexdigest()
            current = _version_row(conn, doc_id, doc["version"])
            if current["sha256"] == sha_now and expected in (doc["version"],
                                                             doc["version"] - 1):
                # the same file again: this version, or a retry of the call
                # that made it. Nothing new is stored.
                out = _view(conn, caller, doc_id, None)
                out["replayed"] = True
                return out
            if expected != doc["version"]:
                raise NamError("stale_version", f"document {doc_id} is at version "
                               f"{doc['version']}, not {expected}: read it again and retry",
                               details=[f"current_version: {doc['version']}"])
            _, sha, parsed = _checked_body(ctx, data, name, declared_type,
                                           same_as=current["mime"])
            with store._write(conn):
                moved = conn.execute("UPDATE nam_documents SET version = version + 1, "
                                     f"updated_at = {store._NOW} WHERE doc_id = ? AND "
                                     "version = ?", (doc_id, expected)).rowcount
                if not moved:
                    raise NamError("stale_version", f"document {doc_id} changed while this "
                                   "version was uploaded: read it again and retry")
                _insert_version(conn, doc_id, expected + 1, name, data, sha, parsed, reason)
            return view(ctx, caller, doc_id)
        return _audited(conn, "update", doc_id, doc["case_id"], op, expected=expected)


def delete(ctx, caller: Caller, doc_id: str, *, expected: int, reason: str,
           run_id: str = "") -> dict:
    conn = ctx.conn
    doc = _doc(conn, caller, doc_id)
    with _acting(caller, doc["case_id"], run_id):
        def op():
            _may(caller, "delete")
            if not (reason or "").strip():
                raise NamError("bad_request", "a delete needs a reason (X-Doc-Reason)")
            if doc["deleted_at"]:
                out = _view(conn, caller, doc_id, None)       # deleting twice is a no-op
                out["replayed"] = True
                return out
            _writable(conn, doc["case_id"], "delete")
            if expected != doc["version"]:
                raise NamError("stale_version", f"document {doc_id} is at version "
                               f"{doc['version']}, not {expected}: read it again and retry")
            with store._write(conn):
                conn.execute(f"UPDATE nam_documents SET deleted_at = {store._NOW}, "
                             "deleted_by = ?, delete_reason = ?, "
                             f"updated_at = {store._NOW} WHERE doc_id = ?",
                             (caller.user, reason.strip()[:500], doc_id))
            return _view(conn, caller, doc_id, None)
        return _audited(conn, "delete", doc_id, doc["case_id"], op, reason=reason[:200])


# -- reads ----------------------------------------------------------------------

def _summary(doc: dict, ver: dict) -> dict:
    parsed = ver["parsed"]
    pages = parsed.get("pages") or []
    return {
        "doc_id": doc["doc_id"], "case_id": doc["case_id"], "kind": doc["kind"],
        "doc_version": ver["version"], "latest_version": doc["version"],
        "filename": ver["filename"], "mime": ver["mime"], "bytes": ver["bytes"],
        "sha256": ver["sha256"], "pages": ver["pages"],
        "text_pages": sum(1 for p in pages if p.get("usable")),
        "ocr_pages": sum(1 for p in pages if p.get("source") == "ocr"),
        "uploaded_by": ver["user"], "uploaded_at": ver["at"],
        "deleted": {"at": doc["deleted_at"], "by": doc["deleted_by"],
                    "reason": doc["delete_reason"]} if doc["deleted_at"] else None,
    }


def _view(conn, caller: Caller, doc_id: str, version: int | None) -> dict:
    doc = _doc(conn, caller, doc_id)
    ver = _version_row(conn, doc_id, version or doc["version"])
    if ver is None:
        raise NamError("not_found", f"document {doc_id} has no version {version}")
    parsed = ver["parsed"]
    body = {"version": VERSION, "document": _summary(doc, ver),
            "content": {
                # the text of an uploaded file is data, never instructions
                "untrusted": True,
                "pages": parsed.get("pages") or [], "rows": parsed.get("rows") or [],
                "ocr_engine": parsed.get("ocr_engine"),
                "flags": parsed.get("flags") or [],
                "warnings": parsed.get("warnings") or [],
                # what the document's skill says to extract (nam_agent/nam/skills/,
                # extract.py), read from its text in code; still untrusted data
                "extracted": _extracted(doc["kind"], parsed)}}
    return _valid(body, "document")


def _extracted(kind: str, parsed: dict) -> dict | None:
    from nam_agent.nam import extract

    text = "\n".join(p.get("text") or "" for p in parsed.get("pages") or [] if p.get("usable"))
    return extract.document(kind, text, parsed.get("rows") or [])


def _valid(body: dict, name: str) -> dict:
    problems = schema.validate(body, schema.load(name))
    if problems:
        raise NamError("internal_schema", f"the {name} view failed its schema",
                       details=problems)
    return body


def view(ctx, caller: Caller, doc_id: str, version: int | None = None) -> dict:
    """The document's metadata and parsed text (the latest version, or
    `version`). A deleted document is still readable here, marked deleted:
    it is hidden from listings and flows, not from its own audit trail."""
    _may(caller, "read")
    return _view(ctx.conn, caller, doc_id, version)


def content(ctx, caller: Caller, doc_id: str, version: int | None = None
            ) -> tuple[bytes, str, str]:
    """(bytes, mime, filename) of a version, verified against its hash."""
    _may(caller, "read")
    doc = _doc(ctx.conn, caller, doc_id)
    ver = _version_row(ctx.conn, doc_id, version or doc["version"])
    if ver is None:
        raise NamError("not_found", f"document {doc_id} has no version {version}")
    return get_blob(ctx.home, doc["tenant"], ver["sha256"]), ver["mime"], ver["filename"]


def versions(ctx, caller: Caller, doc_id: str) -> dict:
    _may(caller, "read")
    doc = _doc(ctx.conn, caller, doc_id)
    rows = ctx.conn.execute("SELECT version FROM nam_document_versions WHERE doc_id = ? "
                            "ORDER BY version", (doc_id,)).fetchall()
    out = []
    for (n,) in rows:
        ver = _version_row(ctx.conn, doc_id, n)
        s = _summary(doc, ver)
        out.append({k: s[k] for k in ("doc_version", "filename", "mime", "bytes", "sha256",
                                      "pages", "text_pages", "ocr_pages", "uploaded_by",
                                      "uploaded_at")} | {"reason": ver["reason"],
                                                         "run_id": ver["run_id"]})
    body = {"version": VERSION, "doc_id": doc_id, "case_id": doc["case_id"],
            "latest_version": doc["version"], "versions": out,
            "deleted": _summary(doc, _version_row(ctx.conn, doc_id, doc["version"]))["deleted"]}
    return _valid(body, "document.versions")


def listing(ctx, caller: Caller, case_id: str) -> dict:
    """The case's documents that are not deleted, oldest first."""
    ensure(ctx.conn)
    permissions.require_case(ctx.conn, caller, case_id)
    _may(caller, "read")
    ids = [r[0] for r in ctx.conn.execute(
        "SELECT doc_id FROM nam_documents WHERE case_id = ? AND tenant = ? AND deleted_at = '' "
        "ORDER BY created_at, doc_id", (case_id, caller.tenant))]
    docs = []
    for doc_id in ids:
        doc = _doc(ctx.conn, caller, doc_id)
        docs.append(_summary(doc, _version_row(ctx.conn, doc_id, doc["version"])))
    return _valid({"version": VERSION, "case_id": case_id, "documents": docs},
                  "document.list")


def blob_for(ctx, tenant: str, sha: str) -> bytes:
    """The bytes the ocr_document tool reads: only the acting tenant's."""
    return get_blob(ctx.home, tenant, sha)
