"""The HTTP server: every endpoint on one port, 7777 by default.

    GET  /health                   liveness, for a load balancer (no identity needed)
    POST /api/chat                 one chat turn, one JSON reply
    POST /api/chat/stream          one chat turn as server-sent events
    POST /api/session              a new chat id, a chat's history, the caller's chats
    GET  /api/nam/case/{id}        the case of a chat's PR Creation Flow (and its .xlsx)
         /api/nam/*, /api/nit/*, /api/tender/*   the NAM screens' routes (nam_routes.py)
         /v1/nam/*                 the NAM harness API (nam_agent/nam/api.py)
    GET  /                         the officer's chat page (static/)

docs/backend-integration.md has each endpoint's request and reply.

Identity is the backend's: its auth component sets X-NAM-Tenant, X-NAM-User
and X-NAM-Roles on every call (nam_agent/nam/identity.py); the server never
authenticates anyone. /v1/nam/* requires them. On the chat routes they are
optional, and when present the chat is that user's alone (chat.py).

NAM_SERVER_KEY, when set, is a shared secret every /api/* and /v1/* call must
carry as "Authorization: Bearer <key>": only the backend can then reach the
server. Bind beyond loopback (NAM_HOST=0.0.0.0) only with it set, or behind a
network that lets only the backend in.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from http.server import ThreadingHTTPServer
from pathlib import Path

from nam_agent.nam import api as nam_api
from nam_agent.nam.errors import VERSION
from nam_agent.server import chat as chats
from nam_agent.server import nam_routes
from nam_agent.server.agent import agent_lock, get_agent

PORT = 7777
STATIC = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml",
                ".html": "text/html; charset=utf-8", ".woff2": "font/woff2",
                ".png": "image/png", ".txt": "text/plain; charset=utf-8"}
ALLOW_HEADERS = ("Content-Type, Authorization, X-NAM-Tenant, X-NAM-User, X-NAM-Roles, "
                 "X-Request-Id, X-Filename, X-Doc-Kind, X-Doc-Reason, X-Doc-Version, "
                 "Idempotency-Key")
MAX_CHAT_BODY = 1024 * 1024

log = logging.getLogger("nam_agent.server")


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def server_key() -> str:
    return _env("NAM_SERVER_KEY", "NAM_DASHBOARD_KEY")


def cors_origin() -> str:
    return _env("NAM_CORS_ORIGIN", default="*")


def make_handler(api_handler):
    """The server's handler: the NAM harness API's handler (api_handler, from
    nam_api.make_handler) with the chat, the NAM screens' routes and the page."""

    key, origin = server_key(), cors_origin()

    class Handler(api_handler):
        server_version = "nam-agent"

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", ALLOW_HEADERS)
            super().end_headers()

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        # -- helpers ----------------------------------------------------
        def _send(self, body: bytes, ctype: str, *, no_cache: bool = False,
                  status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if no_cache:
                self.send_header("Cache-Control", "no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, body: dict, status: int = 200) -> None:
            self._send(json.dumps(body, default=str).encode(), "application/json",
                       no_cache=True, status=status)

        def _authorized(self) -> bool:
            if not key:
                return True
            given = self.headers.get("Authorization", "").encode()
            return hmac.compare_digest(given, f"Bearer {key}".encode())

        def _guarded(self) -> bool:
            """True when the request may go on; answers 401 otherwise."""
            path = self.path.split("?", 1)[0]
            if not path.startswith(("/api/", "/v1/")) or self._authorized():
                return True
            self.close_connection = True
            self._json({"error": "unauthorized: send Authorization: Bearer <NAM_SERVER_KEY>"},
                       401)
            return False

        def _actor(self):
            """(the caller the identity headers name, or None; the error when
            they are partial or malformed)."""
            from nam_agent.nam import identity

            try:
                return identity.from_headers(self.headers), ""
            except identity.BadIdentity as exc:
                return None, str(exc)

        def _length(self) -> int:
            raw = self.headers.get("Content-Length", "0")
            return int(raw) if raw.isdigit() else 0

        def _payload(self) -> dict | None:
            length = self._length()
            if length > MAX_CHAT_BODY:
                self.close_connection = True
                self._json({"error": f"the body is over {MAX_CHAT_BODY} bytes"}, 413)
                return None
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json({"error": "the body is not valid JSON"}, 400)
                return None
            if not isinstance(body, dict):
                self._json({"error": "the body must be a JSON object"}, 400)
                return None
            return body

        # -- routes -----------------------------------------------------
        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if not self._guarded():
                return
            if path.startswith("/v1/"):
                return super().do_GET()
            if path == "/health":
                return self._json({"ok": True, "version": VERSION})
            if path.startswith("/api/"):
                if nam_routes.handle_get(self):
                    return
                return self._json({"error": f"no such endpoint: GET {path}"}, 404)
            if path.startswith("/static/"):
                return self._static(path)
            if path in ("/", "/index.html"):
                return self._send((STATIC / "index.html").read_bytes(),
                                  "text/html; charset=utf-8", no_cache=True)
            self._json({"error": f"no such page: {path}"}, 404)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not self._guarded():
                return
            if path.startswith("/v1/"):
                return super().do_POST()
            if path in ("/api/chat", "/api/chat/stream", "/api/session"):
                return self._chat(path)
            if nam_routes.handle_post(self, self._length()):
                return
            self.close_connection = True
            self._json({"error": f"no such endpoint: POST {path}"}, 404)

        def do_PUT(self):
            if not self._guarded():
                return
            if self.path.startswith("/v1/"):
                return super().do_PUT()
            self.close_connection = True
            self._json({"error": "PUT is only on /v1/nam/documents/{id}"}, 405)

        def do_DELETE(self):
            if not self._guarded():
                return
            if self.path.startswith("/v1/"):
                return super().do_DELETE()
            self._json({"error": "DELETE is only on /v1/nam/documents/{id}"}, 405)

        def _static(self, path: str) -> None:
            target = (STATIC / path[len("/static/"):]).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                return self._json({"error": "not found"}, 404)
            self._send(target.read_bytes(),
                       STATIC_TYPES.get(target.suffix, "application/octet-stream"),
                       no_cache=True)

        def _chat(self, path: str) -> None:
            payload = self._payload()
            if payload is None:
                return
            actor, bad = self._actor()
            if bad:
                return self._json({"error": bad}, 401)
            try:
                if path == "/api/session":
                    out = chats.session_action(payload, actor)
                    return self._json(out, 400 if "error" in out else 200)
                message = str(payload.get("message") or "").strip()
                session_id = str(payload.get("session_id") or "").strip() or None
                if not message:
                    return self._json({"error": "message is required"}, 400)
                if session_id:
                    with agent_lock:
                        chats.check(get_agent().conn, session_id, actor, claim=True)
                focus = chats.case_focus(payload)
                source = "dashboard" if payload.get("source") == "page" else "api"
                if path == "/api/chat":
                    return self._json(chats.chat(message, session_id, focus, actor))
            except chats.ChatRefused as exc:
                return self._json({"error": str(exc)}, exc.status)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")       # nginx: do not buffer the stream
            self.end_headers()

            def emit(kind, ev):
                try:
                    self.wfile.write(f"data: {json.dumps({'kind': kind, **ev}, default=str)}"
                                     "\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass                                   # the caller went away

            try:
                chats.chat_stream(message, emit, session_id, focus, actor, source=source)
            except chats.ChatRefused as exc:
                emit("done", {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - a terminal event, never a dropped stream
                log.exception("chat turn failed")
                emit("done", {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def build(host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    """The server, bound and ready to serve_forever(), from the settings."""
    from nam_agent.nam import llm

    settings, make_ctx, models = nam_api._settings_ctx()
    shown = llm.configured(models)
    api_handler = nam_api.make_handler(
        make_ctx, max_body=settings.nam_max_body, model_configured=shown["configured"],
        kg_url=settings.kg_url, max_doc=settings.nam_max_doc, model_tasks=shown["tasks"],
        rate_per_minute=settings.nam_rate)
    host = host or settings.nam_host
    port = settings.nam_port if port is None else port
    try:
        server = ThreadingHTTPServer((host, port), make_handler(api_handler))
    except OSError as exc:
        raise SystemExit(f"port {port} on {host} is not available ({exc}). Free it "
                         f"(ss -ltnp | grep {port}) or set NAM_PORT.") from exc
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = list(argv or [])
    host = port = None
    for flag in ("--host", "--port"):
        if flag in args and args.index(flag) + 1 < len(args):
            value = args[args.index(flag) + 1]
            if flag == "--host":
                host = value
            else:
                port = int(value)
    server = build(host, port)
    bound_host, bound_port = server.server_address[:2]
    print(f"NAM harness on http://{bound_host}:{bound_port}  (chat page: /, API: /api/*, /v1/nam/*)")
    if bound_host not in ("127.0.0.1", "localhost", "::1") and not server_key():
        print("  warning: listening beyond loopback with no NAM_SERVER_KEY: anything that can "
              "reach this port can use it. Set NAM_SERVER_KEY or keep the port private.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
