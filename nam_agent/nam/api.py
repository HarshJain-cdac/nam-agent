"""The NAM harness over HTTP: /v1/nam/*, served by `nam-agent serve` (nam_agent/server/).

    POST /v1/nam/run                  the LLM-like call (schemas/request.json -> response.json)
    GET  /v1/nam/cases/{id}           the Need Record (schemas/record.json); ?version=n for an
                                      older version
    GET  /v1/nam/cases/{id}/history   every version: who, which flow and run, why, what changed
    POST /v1/nam/cases/{id}/documents upload a document: the raw file as the body, with
                                      Content-Type, X-Filename, X-Doc-Kind, and optionally
                                      X-Doc-Reason and Idempotency-Key
    GET  /v1/nam/cases/{id}/documents the case's documents (schemas/document.list.json)
    GET  /v1/nam/documents/{doc}      metadata and parsed text, marked untrusted
                                      (schemas/document.json); ?version=n
    GET  /v1/nam/documents/{doc}/content   the original file; ?version=n
    GET  /v1/nam/documents/{doc}/versions  every version (schemas/document.versions.json)
    PUT  /v1/nam/documents/{doc}      a new version (a file edited outside NAM, F39): the raw
                                      file, with X-Doc-Version (the version it replaces)
    DELETE /v1/nam/documents/{doc}    soft delete, with X-Doc-Version and X-Doc-Reason: kept
                                      for audit, hidden from listings and flows
    GET  /v1/nam/cases/{id}/trace     the case traced to its inputs and approvals
                                      (?format=jsonl: one event per line, for export)
    GET  /v1/nam/runs/{run_id}        one call's audit rows (the caller's tenant only)
    GET  /v1/nam/health               liveness, KG reachability, the model per task,
                                      the knowledge sources and their authority
    GET  /v1/nam/schema               every request, response and result schema

make_handler builds the handler the server extends with the chat and the NAM
screens' routes (nam_agent/server/http.py). Identity and roles arrive from the
trusted component that authenticates callers, so only that component should
reach the port.
Those three headers (X-NAM-Tenant, X-NAM-User, X-NAM-Roles) are read by
nam_agent/nam/identity.py; a call without them is a 401 before its body is read.
A caller over NAM_RATE calls a minute (run, upload, new version) gets
429 rate_limited, retryable, before the body is read. Calls run concurrently: the Need Record store numbers cases atomically and
refuses a write made against an older version of a case (stale_version).

Every reply is JSON with a real HTTP status. The body is refused before it is
read when it is too large or not JSON, and a failure is an error body
(schemas/error.json), never a dropped connection or a stack trace.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nam_agent.nam import (
    Context,
    get_case,
    case_trace,
    guard,
    history,
    identity,
    kg_mcp,
    knowledge,
    new_run_id,
    run,
    run_trace,
    schema,
)
from nam_agent.nam.errors import VERSION, NamError, status_of

log = logging.getLogger("nam_agent.nam.api")

READ_TIMEOUT_S = 30          # a client that stops sending mid-body is dropped
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SCHEMA_NAMES = ("request", "response", "error", "record", "history", "document",
                "document.list", "document.versions", "flow.nam.route", "flow.need.capture",
                "flow.case.status")
_CASE_PATH = re.compile(r"^/v1/nam/cases/([^/]+)(/history|/trace)?$")
_RUN_PATH = re.compile(r"^/v1/nam/runs/(run_[0-9a-f]{16})$")
_CASE_DOCS = re.compile(r"^/v1/nam/cases/([^/]+)/documents$")
_DOC_PATH = re.compile(r"^/v1/nam/documents/([^/]+)(/content|/versions)?$")
_CASE_ID = re.compile(r"^NAM-\d{4}-\d{4,8}$")


def kg_reachable(kg_url: str) -> bool | None:
    """None when no KG is configured. A short timeout: health must answer fast."""
    if not kg_url:
        return None
    try:
        return kg_mcp.reachable(kg_url)
    except Exception:  # noqa: BLE001 - any failure means not reachable
        return False


class RateLimit:
    """At most `per_minute` calls per tenant and user in any 60 seconds (a
    sliding window, in memory, per server process). 0 turns it off. A call
    over it is 429 rate_limited, retryable, before its body is read."""

    def __init__(self, per_minute: int, clock=time.monotonic):
        self.per_minute, self.clock = per_minute, clock
        self._seen: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def check(self, caller) -> None:
        if self.per_minute <= 0:
            return
        now = self.clock()
        with self._lock:
            key = (caller.tenant, caller.user)
            recent = [t for t in self._seen.get(key, []) if now - t < 60]
            if len(recent) >= self.per_minute:
                self._seen[key] = recent
                wait = int(60 - (now - recent[0])) + 1
                raise NamError("rate_limited", f"more than {self.per_minute} calls a minute "
                               f"for {caller.user}: retry in {wait}s",
                               details=[f"retry_after_s: {wait}"])
            recent.append(now)
            self._seen[key] = recent
            if len(self._seen) > 10000:            # forget idle callers
                self._seen = {k: v for k, v in self._seen.items() if now - v[-1] < 60}


def make_handler(make_ctx, *, max_body: int, model_configured: bool, kg_url: str,
                 max_doc: int = 25 * 1024 * 1024, model_tasks: dict | None = None,
                 rate_per_minute: int = 0):
    """A handler class bound to one harness. `make_ctx()` returns a Context and
    a close() for it; it is called once per request that reads or writes a
    case, from that request's own thread."""

    limit = RateLimit(rate_per_minute)

    class Handler(BaseHTTPRequestHandler):
        server_version = "nam_agent-nam"
        sys_version = ""
        timeout = READ_TIMEOUT_S

        def log_message(self, fmt, *args):   # to the logger, not stderr noise
            log.info("%s %s", self.address_string(), fmt % args)

        # -- replies ----------------------------------------------------
        def _reply(self, body: dict, status: int | None = None) -> None:
            if guard.leaks(body):
                # the last check on every reply: the prompt canary and the
                # server's own secrets never leave (nam_agent/nam/guard.py)
                log.error("NAM reply withheld: it held %s", guard.leaks(body))
                body, status = NamError("internal", "the reply was withheld by the output "
                                        "check; see the server log").body(new_run_id()), None
            data = json.dumps(body, default=str).encode()
            self.send_response(status or status_of(body))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            rid = self.headers.get("X-Request-Id", "")
            if _REQUEST_ID.match(rid):
                self.send_header("X-Request-Id", rid)
            self.end_headers()
            self.wfile.write(data)

        def _error(self, code: str, message: str, *, close: bool = False) -> None:
            if close:
                # the body was not read: the connection cannot be reused
                self.close_connection = True
            self._reply(NamError(code, message).body(new_run_id()))

        # -- routes -----------------------------------------------------
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/v1/nam/health":
                self._reply({"version": VERSION, "ok": True,
                             "kg": {"configured": bool(kg_url),
                                    "reachable": kg_reachable(kg_url)},
                             "model": {"configured": model_configured,
                                       "tasks": model_tasks or {}},
                             "knowledge": knowledge.sources(bool(kg_url))})
            elif path == "/v1/nam/schema":
                self._reply({"version": VERSION,
                             "schemas": {n: schema.load(n) for n in SCHEMA_NAMES}})
            elif _CASE_PATH.match(path):
                self._case(path)
            elif _RUN_PATH.match(path):
                try:
                    caller = identity.require_headers(self.headers)
                except NamError as exc:
                    return self._reply(exc.body(new_run_id()))
                ctx, close = make_ctx()
                try:
                    body = run_trace(ctx, caller, _RUN_PATH.match(path).group(1))
                finally:
                    close()
                self._reply(body)
            elif _CASE_DOCS.match(path) or _DOC_PATH.match(path):
                self._documents("GET", path)
            elif path == "/v1/nam/run":
                self._error("method_not_allowed", "use POST for /v1/nam/run")
            else:
                self._error("not_found", "no such NAM endpoint")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if _CASE_DOCS.match(path):
                return self._documents("POST", path)
            if path != "/v1/nam/run":
                if path in ("/v1/nam/health", "/v1/nam/schema") or _CASE_PATH.match(path) \
                        or _DOC_PATH.match(path):
                    return self._error("method_not_allowed", f"use GET for {path}", close=True)
                return self._error("not_found", "no such NAM endpoint", close=True)
            # identity first, before a byte of the body is read (plan D4:
            # the upstream auth component sets these; NAM harness reads nothing else)
            try:
                caller = identity.require_headers(self.headers)
                limit.check(caller)
            except NamError as exc:
                self.close_connection = True
                return self._reply(exc.body(new_run_id()))
            request = self._read_json()
            if request is None:
                return
            ctx, close = make_ctx()
            try:
                body = run(request, ctx, caller)
            finally:
                close()
            self._reply(body)

        def _case(self, path: str) -> None:
            case_id, want_history = _CASE_PATH.match(path).groups()
            try:
                caller = identity.require_headers(self.headers)
                if not _CASE_ID.match(case_id):
                    raise NamError("not_found", "no such case")
                query = urllib.parse.parse_qs(self.path.partition("?")[2])
                version = None
                if "version" in query and not want_history:
                    raw = query["version"][0]
                    if not raw.isdigit() or int(raw) < 1:
                        raise NamError("bad_request", "version must be a positive integer")
                    version = int(raw)
            except NamError as exc:
                return self._reply(exc.body(new_run_id()))
            ctx, close = make_ctx()
            try:
                if want_history == "/trace":
                    body = case_trace(ctx, caller, case_id)
                elif want_history:
                    body = history(ctx, caller, case_id)
                else:
                    body = get_case(ctx, caller, case_id, version)
            finally:
                close()
            if want_history == "/trace" and query.get("format") == ["jsonl"] \
                    and "error" not in body and not guard.leaks(body):
                from nam_agent.nam import trace as trace_mod

                return self._file(trace_mod.jsonl(body).encode(), "application/x-ndjson",
                                  f"{case_id}-trace.jsonl")
            self._reply(body)

        def do_PUT(self):
            path = self.path.split("?", 1)[0]
            found = _DOC_PATH.match(path)
            if found and found.group(2) is None:
                return self._documents("PUT", path)
            self._error("method_not_allowed", "not supported on this endpoint", close=True)

        def do_DELETE(self):
            path = self.path.split("?", 1)[0]
            found = _DOC_PATH.match(path)
            if found and found.group(2) is None:
                return self._documents("DELETE", path)
            self._error("method_not_allowed", "not supported on this endpoint", close=True)

        def do_PATCH(self):
            self._error("method_not_allowed", "not supported on this endpoint", close=True)

        # -- documents --------------------------------------------------
        def _header(self, name: str, required: bool = False) -> str:
            value = urllib.parse.unquote(self.headers.get(name, "") or "").strip()
            if required and not value:
                raise NamError("bad_request", f"the {name} header is required")
            return value

        def _query_version(self) -> int | None:
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            if "version" not in query:
                return None
            raw = query["version"][0]
            if not raw.isdigit() or int(raw) < 1:
                raise NamError("bad_request", "version must be a positive integer")
            return int(raw)

        def _expected(self) -> int:
            raw = self._header("X-Doc-Version", required=True)
            if not raw.isdigit() or int(raw) < 1:
                raise NamError("bad_request", "X-Doc-Version must be a positive integer")
            return int(raw)

        def _read_file(self) -> bytes | None:
            raw_len = self.headers.get("Content-Length")
            if raw_len is None or not raw_len.isdigit():
                self._error("bad_request", "Content-Length is required", close=True)
                return None
            if int(raw_len) > max_doc:
                self._error("too_large", f"the document is over {max_doc} bytes", close=True)
                return None
            try:
                return self.rfile.read(int(raw_len))
            except OSError:
                self.close_connection = True
                return None

        def _documents(self, method: str, path: str) -> None:
            from nam_agent.nam.docs import store as docs

            run_id = new_run_id()
            try:
                caller = identity.require_headers(self.headers)
                if method in ("POST", "PUT"):
                    limit.check(caller)
            except NamError as exc:
                if method in ("POST", "PUT"):
                    self.close_connection = True      # the body is not read
                return self._reply(exc.body(run_id))
            data = b""
            if method in ("POST", "PUT"):
                data = self._read_file()
                if data is None:
                    return
            case_match, doc_match = _CASE_DOCS.match(path), _DOC_PATH.match(path)
            ctx, close = make_ctx()
            try:
                if case_match:
                    case_id = case_match.group(1)
                    if not _CASE_ID.match(case_id):
                        raise NamError("not_found", "no such case")
                    if method == "GET":
                        return self._reply(docs.listing(ctx, caller, case_id))
                    body = docs.upload(
                        ctx, caller, case_id, filename=self._header("X-Filename", True),
                        data=data, kind=self._header("X-Doc-Kind", True).lower(),
                        declared_type=self.headers.get("Content-Type", ""),
                        reason=self._header("X-Doc-Reason"),
                        idempotency_key=self._header("Idempotency-Key"), run_id=run_id)
                    return self._reply(body, 200 if body.get("replayed") else 201)
                doc_id, part = doc_match.groups()
                if method == "PUT":
                    return self._reply(docs.new_version(
                        ctx, caller, doc_id, filename=self._header("X-Filename", True),
                        data=data, expected=self._expected(),
                        declared_type=self.headers.get("Content-Type", ""),
                        reason=self._header("X-Doc-Reason"), run_id=run_id))
                if method == "DELETE":
                    return self._reply(docs.delete(ctx, caller, doc_id, expected=self._expected(),
                                                   reason=self._header("X-Doc-Reason", True),
                                                   run_id=run_id))
                if part == "/versions":
                    return self._reply(docs.versions(ctx, caller, doc_id))
                if part == "/content":
                    blob, mime, name = docs.content(ctx, caller, doc_id, self._query_version())
                    return self._file(blob, mime, name)
                return self._reply(docs.view(ctx, caller, doc_id, self._query_version()))
            except NamError as exc:
                return self._reply(exc.body(run_id))
            except Exception:  # noqa: BLE001 - a clean error for the caller, the trace for the log
                log.exception("NAM documents %s %s failed (%s)", method, path, run_id)
                return self._reply(NamError("internal", "the harness failed on this request; "
                                            f"see the server log for {run_id}").body(run_id))
            finally:
                close()

        def _file(self, data: bytes, mime: str, filename: str) -> None:
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in filename)[:120]
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"; '
                             f"filename*=UTF-8''{urllib.parse.quote(filename)}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self):
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._error("unsupported_type",
                                   "send the request as Content-Type: application/json",
                                   close=True)
            raw_len = self.headers.get("Content-Length")
            if raw_len is None or not raw_len.isdigit():
                return self._error("bad_request", "Content-Length is required", close=True)
            length = int(raw_len)
            if length > max_body:
                return self._error("too_large", f"the body is over {max_body} bytes",
                                   close=True)
            try:
                raw = self.rfile.read(length)
            except OSError:
                self.close_connection = True
                return None      # the client went away; nobody to answer
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return self._error("bad_request", "the body is not valid UTF-8 JSON")

    return Handler


def serve(host: str, port: int, make_ctx, *, max_body: int, model_configured: bool,
          kg_url: str, max_doc: int = 25 * 1024 * 1024,
          model_tasks: dict | None = None, rate_per_minute: int = 0) -> ThreadingHTTPServer:
    handler = make_handler(make_ctx, max_body=max_body, model_configured=model_configured,
                           kg_url=kg_url, max_doc=max_doc, model_tasks=model_tasks,
                           rate_per_minute=rate_per_minute)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def _settings_ctx():
    """The real harness: settings from the environment, nam_agent's own model."""
    from nam_agent import db
    from nam_agent.config import load_settings
    from nam_agent.nam import llm

    settings = load_settings()
    home = settings.ensure_home()
    # a bad NAM_MODELS stops the server here, before any call is routed
    try:
        llm.parse_chains(settings.nam_models, "", "")
    except ValueError as exc:
        raise SystemExit(f"NAM: {exc}") from None
    models = None
    try:
        from nam_agent.loop.models import get_client

        models = llm.Models.from_settings(get_client(settings), settings)
    except (SystemExit, Exception) as exc:  # noqa: BLE001 - no key: no model calls
        print(f"NAM: no model client ({str(exc).splitlines()[0][:120]}); routing asks the "
              "caller and follow-up questions come from the tenders.", file=sys.stderr)
    for task, chain in llm.configured(models)["tasks"].items():
        print(f"NAM: model for {task}: {', '.join(chain) or 'none'}", file=sys.stderr)

    # a named OCR engine that cannot load stops the server here, rather than
    # every scanned page quietly being stored as "no usable text"
    from nam_agent.nam import ocr

    try:
        engine = ocr.configured(settings.nam_ocr_backend)
    except ocr.OcrUnavailable as exc:
        raise SystemExit(f"NAM: {exc}") from None
    if engine is None:
        print("NAM: no OCR backend (NAM_OCR_BACKEND); pages without text are stored "
              "as 'no usable text'.", file=sys.stderr)

    def make_ctx():
        conn = db.connect(home)
        return Context(conn=conn, home=home, kg_url=settings.kg_url, models=models, ocr=engine,
                       max_doc_bytes=settings.nam_max_doc,
                       ocr_min_chars=settings.nam_ocr_min_chars), conn.close

    return settings, make_ctx, models
