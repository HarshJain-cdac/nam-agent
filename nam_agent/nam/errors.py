"""The NAM API's error codes, and the one error body every failure uses.

A flow that cannot go on raises NamError with a code from CODES. The HTTP
server turns the code into its status; `nam_agent.nam.run()` turns it into the same
body for a caller in Python. Messages are written for the backend developer and
never carry a stack trace, a file path or a secret.
"""

from __future__ import annotations

VERSION = "nam.v1"

# code -> (HTTP status, retryable)
CODES = {
    "bad_request": (400, False),
    "schema": (400, False),
    "identity_missing": (401, False),
    "forbidden_role": (403, False),
    "forbidden_tenant": (403, False),
    "forbidden_action": (403, False),
    "not_found": (404, False),
    "method_not_allowed": (405, False),
    "stage_gate": (409, False),
    "stale_version": (409, True),
    # a write with this idempotency key is still running (nam_agent/nam/tools.py)
    "in_progress": (409, True),
    "too_large": (413, False),
    # more calls than NAM_RATE allows per user per minute (nam_agent/nam/api.py)
    "rate_limited": (429, True),
    # a flow's output, or an action, broke a rule nam_agent/nam/guardrails.py enforces
    "guardrail": (422, False),
    "unsupported_type": (415, False),
    # routed and permitted, but the flow is not built yet (plan Step 15)
    "flow_not_built": (501, False),
    "kg_unavailable": (502, True),
    "model_unavailable": (502, True),
    "ocr_unavailable": (502, True),
    "internal": (500, False),
    "internal_schema": (500, False),
}


class NamError(Exception):
    def __init__(self, code: str, message: str, *, details: list[str] | None = None,
                 case_id: str | None = None):
        if code not in CODES:
            raise ValueError(f"unknown NAM error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = list(details or [])
        self.case_id = case_id

    @property
    def status(self) -> int:
        return CODES[self.code][0]

    def body(self, run_id: str | None = None) -> dict:
        err = {"code": self.code, "message": self.message[:500],
               "retryable": CODES[self.code][1]}
        if self.details:
            err["details"] = [d[:300] for d in self.details[:20]]
        out = {"version": VERSION, "error": err}
        if run_id:
            out["run_id"] = run_id
        if self.case_id:
            # a flow that failed after it saved the case says which one, so a
            # retry continues it instead of starting a second case
            out["case_id"] = self.case_id
        return out


def status_of(body: dict) -> int:
    """The HTTP status for a response body: 200 unless it is an error body."""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict) and err.get("code") in CODES:
        return CODES[err["code"]][0]
    return 200
