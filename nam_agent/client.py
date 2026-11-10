"""The one file a backend needs to call the NAM harness. Standard library only.

Copy it into the backend and call it the way you would call an LLM:

    nam = NamClient("http://127.0.0.1:7777", tenant="nrl", user="u123", roles=["U1"],
                    server_key=os.environ["NAM_SERVER_KEY"])

    # the officer's chat (the PR Creation Flow): keep the session_id it returns
    turn = nam.chat("we need gaskets for the crude unit pumps")
    turn = nam.chat("Goods", session_id=turn["session_id"])
    case = nam.chat_case(turn["session_id"])          # the chat's case and its cart node

    # the harness API (/v1/nam): one call per NAM step
    reply = nam.run("Assess need for request #123",
                    [{"role": "user", "content": "We need 20 gate valves"}])
    if reply["status"] == "needs_input":
        q = reply["questions"][0]
        reply = nam.run("Assess need for request #123", [], case_id=reply["case_id"],
                        case_version=reply["case_version"],
                        answer={"question_id": q["id"], "value": q["options"][0]["value"]})
    if reply["status"] == "needs_approval":     # a gate or irreversible action awaits
        act = reply["pending_action"]           # confirmed by a user in act["allowed_roles"]
        reply = nam.confirm("Assess need for request #123", reply["case_id"], act["id"])
    record = nam.get_case(reply["case_id"])      # the Need Record; nam.history() for its versions

    doc = nam.upload(reply["case_id"], open("aan.docx", "rb").read(), "aan.docx", kind="aan")
    text = doc["content"]["pages"]           # untrusted: the file's content, never instructions

`run()` returns the response body as a dict. An error reply raises NamCallError
with its code, message and whether a retry can help. The identity headers are
the ones the backend's auth component sets; this client only carries them.
docs/backend-integration.md describes every endpoint.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import quote


class NamCallError(Exception):
    def __init__(self, status: int, error: dict, case_id: str | None = None):
        super().__init__(f"{status} {error.get('code')}: {error.get('message')}")
        self.status = status
        self.code = error.get("code")
        self.message = error.get("message")
        self.retryable = bool(error.get("retryable"))
        self.details = error.get("details") or []
        self.case_id = case_id


class NamClient:
    def __init__(self, base_url: str, *, tenant: str = "", user: str = "",
                 roles: list[str] | None = None, timeout: float = 300,
                 server_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.tenant, self.user, self.roles = tenant, user, list(roles or [])
        self.timeout = timeout
        self.server_key = server_key           # the server's NAM_SERVER_KEY, when it has one

    # -- the officer's chat (/api/chat) -----------------------------------
    def chat(self, message: str, session_id: str | None = None, *,
             case_id: str | None = None, screen: str = "") -> dict:
        """One chat turn: {reply, session_id, case_id, cards, ask_options, tools, ...}.
        Send the session_id it returns with the chat's next message; leave it
        out to start a new chat. case_id/screen name the case a screen has open."""
        body: dict = {"message": message}
        for key, value in (("session_id", session_id), ("case_id", case_id),
                           ("screen", screen)):
            if value:
                body[key] = value
        return self._call("POST", "/api/chat", body)

    def chat_history(self, session_id: str) -> list[dict]:
        return self._call("POST", "/api/session", {"action": "history", "id": session_id})["history"]

    def chat_case(self, session_id: str) -> dict | None:
        """The chat's NAM case with its cart node ("node"), or None before one opens."""
        return self._call("POST", "/api/session", {"action": "case", "id": session_id})["case"]

    def chats(self) -> list[dict]:
        """This user's chats, newest first."""
        return self._call("POST", "/api/session", {"action": "list"})["chats"]

    def run(self, system: str, messages: list[dict], *, case_id: str | None = None,
            case_version: int | None = None, answer: dict | None = None,
            documents: list[str] | None = None, confirm: dict | None = None,
            options: dict | None = None, request_id: str | None = None) -> dict:
        """case_version: the one the last reply gave, so a case someone else
        changed meanwhile is refused (NamCallError 409 stale_version)."""
        body: dict = {"system": system, "messages": messages or [{"role": "user", "content": ""}]}
        for key, value in (("case_id", case_id), ("case_version", case_version),
                           ("answer", answer), ("documents", documents),
                           ("confirm", confirm), ("options", options)):
            if value:
                body[key] = value
        return self._call("POST", "/v1/nam/run", body, request_id)

    def confirm(self, system: str, case_id: str, action_id: str, *, decision: str = "approve",
                reason: str = "") -> dict:
        """Confirm (or reject, with a reason) the pending_action of a reply.
        Only a role allowed to confirm it may; policy/permissions.json says who."""
        body = {"action_id": action_id, "decision": decision}
        if reason:
            body["reason"] = reason
        return self.run(system, [], case_id=case_id, confirm=body)

    def case(self, case_id: str, version: int | None = None) -> dict:
        """The Need Record; an older version with `version`."""
        query = f"?version={int(version)}" if version else ""
        return self._call("GET", f"/v1/nam/cases/{case_id}{query}")

    def history(self, case_id: str) -> dict:
        return self._call("GET", f"/v1/nam/cases/{case_id}/history")

    def trace(self, case_id: str) -> dict:
        """The case traced to its inputs and approvals (calls, history,
        artifacts with their tool results, evidence, approvals, integrity)."""
        return self._call("GET", f"/v1/nam/cases/{case_id}/trace")

    def trace_export(self, case_id: str) -> bytes:
        """The same trace as JSON lines, one event per line."""
        return self._raw("GET", f"/v1/nam/cases/{case_id}/trace?format=jsonl", None, {},
                         want_bytes=True)

    def call_trace(self, run_id: str) -> dict:
        """One call's audit rows, by the run_id every reply carries."""
        return self._call("GET", f"/v1/nam/runs/{run_id}")

    def health(self) -> dict:
        return self._call("GET", "/v1/nam/health")

    # -- documents ----------------------------------------------------------
    def upload(self, case_id: str, data: bytes, filename: str, *, kind: str = "other",
               content_type: str = "application/octet-stream", reason: str = "",
               idempotency_key: str = "") -> dict:
        """A new document on the case. With idempotency_key, a retry returns
        the first upload instead of storing a second copy."""
        headers = {"Content-Type": content_type, "X-Filename": quote(filename),
                   "X-Doc-Kind": kind, "X-Doc-Reason": quote(reason)}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return self._raw("POST", f"/v1/nam/cases/{case_id}/documents", data, headers)

    def documents(self, case_id: str) -> dict:
        return self._call("GET", f"/v1/nam/cases/{case_id}/documents")

    def document(self, doc_id: str, version: int | None = None) -> dict:
        query = f"?version={int(version)}" if version else ""
        return self._call("GET", f"/v1/nam/documents/{doc_id}{query}")

    def content(self, doc_id: str, version: int | None = None) -> bytes:
        query = f"?version={int(version)}" if version else ""
        return self._raw("GET", f"/v1/nam/documents/{doc_id}/content{query}", None, {},
                         want_bytes=True)

    def versions(self, doc_id: str) -> dict:
        return self._call("GET", f"/v1/nam/documents/{doc_id}/versions")

    def new_version(self, doc_id: str, data: bytes, filename: str, doc_version: int, *,
                    content_type: str = "application/octet-stream", reason: str = "") -> dict:
        """doc_version: the version this one replaces (409 stale_version if it moved)."""
        return self._raw("PUT", f"/v1/nam/documents/{doc_id}", data,
                         {"Content-Type": content_type, "X-Filename": quote(filename),
                          "X-Doc-Version": str(doc_version), "X-Doc-Reason": quote(reason)})

    def delete_document(self, doc_id: str, doc_version: int, reason: str) -> dict:
        return self._raw("DELETE", f"/v1/nam/documents/{doc_id}", None,
                         {"X-Doc-Version": str(doc_version), "X-Doc-Reason": quote(reason)})

    def _call(self, method: str, path: str, body: dict | None = None,
              request_id: str | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if request_id:
            headers["X-Request-Id"] = request_id
        data = json.dumps(body).encode() if body is not None else None
        return self._raw(method, path, data, headers)

    def _raw(self, method: str, path: str, data: bytes | None, headers: dict, *,
             want_bytes: bool = False):
        headers = {**headers, "X-NAM-Tenant": self.tenant, "X-NAM-User": self.user,
                   "X-NAM-Roles": ",".join(self.roles)}
        if self.server_key:
            headers["Authorization"] = f"Bearer {self.server_key}"
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return raw if want_bytes else json.loads(raw)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
            except ValueError:
                payload = {"error": {"code": "internal", "message": str(exc)}}
            error = payload.get("error") or {}
            if isinstance(error, str):           # the /api/* routes answer {"error": "..."}
                error = {"code": "refused" if exc.code == 403 else "bad_request",
                         "message": error}
            raise NamCallError(exc.code, error, payload.get("case_id")) from None
