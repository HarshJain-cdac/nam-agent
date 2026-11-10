"""The NAM tool layer: every contract call a flow or a model makes goes
through call(), which, in this order:

  1. finds the tool; an unknown name is bad_request
  2. checks who may call it: a flow only the tools its catalog entry names
     (playbooks/catalog.py), an irreversible tool only the orchestrator's
     confirm path, and the tool's roles if it has any
  3. validates the arguments against the tool's input schema (schema, 400)
  4. runs it:
       read           with a timeout, retried with backoff while the backend
                      is Unavailable, then kg_unavailable (502, retryable)
       write,         never retried, and only with an idempotency key: the
       irreversible   first call with a key claims it and stores its result;
                      a repeat with the same key and arguments gets that
                      result back without running again, a repeat with other
                      arguments is bad_request, and one that arrives while
                      the first is still running is in_progress (409,
                      retryable). A write whose outcome is unknown (it
                      failed midway) is never run again under that key.
  5. validates the result against the tool's output schema; a backend that
     answers outside its contract is internal_schema, never passed on
  6. writes one audit row (nam_agent/nam/audit.py), whatever the outcome

"No data" and "could not ask" stay apart: a backend that was asked and
holds nothing returns found: False; one that could not be asked is an error
(contracts/base.py).

model_tools() exposes the read tools to a model as core nam_agent Tools. Write
and irreversible tools are never given to a model: only code calls them.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass

from nam_agent.nam import audit, guardrails, schema, store
from nam_agent.nam.contracts.base import CONTRACTS, EFFECTS, IRREVERSIBLE, READ, Rejected, Unavailable
from nam_agent.nam.errors import NamError

ORCHESTRATOR = "nam.orchestrator"       # the actor flow of the confirm path
BACKOFF_BASE_S = 0.25                   # 0.25s, 0.5s, 1s, ... between read attempts
BACKOFF_CAP_S = 4.0
CLAIM_TIMEOUT_S = 300                   # a write claim older than this is abandoned
KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$"

_sleep = time.sleep                     # evals replace it, so backoff costs no time
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="nam-tool")


@dataclass(frozen=True)
class NamTool:
    name: str
    description: str
    effect: str                          # read | write | irreversible
    input_schema: dict
    output_schema: dict
    # read: fn(**args) -> result; write and irreversible: fn(**args,
    # idempotency_key=key), and the backend must honour the key itself
    fn: Callable[..., dict]
    roles: tuple[str, ...] = ()          # empty: whoever may run the flow
    timeout_s: float = 150.0             # above the KG client's own 120s
    retries: int = 2                     # extra attempts for a read; writes get none
    unavailable_code: str = "kg_unavailable"

    def __post_init__(self):
        if self.effect not in EFFECTS:
            raise ValueError(f"{self.name}: effect must be one of {EFFECTS}")
        schema.check_schema(self.input_schema, f"{self.name}.input")
        schema.check_schema(self.output_schema, f"{self.name}.output")


def registry(ctx) -> dict[str, NamTool]:
    """The contracts of contracts/base.py, answered by the KG backend, and
    the ocr_document tool (nam_agent/nam/ocr/tool.py)."""
    from nam_agent.nam.contracts.kg import KgBackend

    fetch = getattr(ctx.fetch, "inner", ctx.fetch)     # a FetchLog is not audited twice
    backend = KgBackend(fetch, ctx.kg_url)
    from nam_agent.nam.ocr import tool as ocr_tool

    table = {c.name: NamTool(c.name, c.description, c.effect, c.input_schema, c.output_schema,
                             getattr(backend, c.name))
             for c in CONTRACTS.values()}
    table["ocr_document"] = ocr_tool.make(ctx)
    return table


# -- the call -----------------------------------------------------------------

def _hash(args: dict) -> str:
    text = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def call(ctx, name: str, args: dict, *, idempotency_key: str = "",
         tools: dict[str, NamTool] | None = None) -> dict:
    """One audited tool call; the result, or NamError. `tools` replaces the
    registry (evals, and later connectors)."""
    started = time.monotonic()
    args = dict(args or {})
    detail = {"args_hash": _hash(args),
              "args": json.dumps(args, sort_keys=True, default=str)[:600]}
    if idempotency_key:
        detail["idempotency_key"] = idempotency_key[:128]
    table = tools if tools is not None else registry(ctx)
    tool = table.get(name)
    state = {"attempts": 0, "effect": tool.effect if tool else ""}

    def log(status: str, code: str = "", **more) -> None:
        audit.record(ctx.conn, kind="tool", name=name[:80], effect=state["effect"],
                     status=status, error_code=code, attempts=state["attempts"],
                     ms=int((time.monotonic() - started) * 1000), detail={**detail, **more})

    try:
        if tool is None:
            raise NamError("bad_request", f"there is no NAM tool {name!r}")
        _permit(tool, store.current())
        problems = schema.validate(args, tool.input_schema)
        if problems:
            raise NamError("schema", f"the arguments to {name} do not match its input schema",
                           details=problems)
        if tool.effect == READ:
            result = _read(tool, args, state)
            _check_output(tool, result)
            guardrails.note(name, result, args)  # the evidence a rate or selection may cite
            log("ok", found=bool(result.get("found")))
            return result
        result, replayed = _write(ctx.conn, tool, args, idempotency_key, detail["args_hash"],
                                  state)
        log("replayed" if replayed else "ok", found=bool(result.get("found")))
        return result
    except NamError as exc:
        log(_STATUS.get(exc.code, "failed"), exc.code, message=exc.message[:300])
        raise
    except Exception as exc:  # noqa: BLE001 - a tool bug is an internal error, and audited
        log("failed", "internal", message=f"{type(exc).__name__}: {str(exc)[:200]}")
        raise NamError("internal", f"the tool {name} failed; see the server log") from exc


_STATUS = {"schema": "rejected", "bad_request": "rejected", "forbidden_action": "forbidden",
           "forbidden_role": "forbidden", "kg_unavailable": "unavailable",
           "ocr_unavailable": "unavailable", "model_unavailable": "unavailable",
           "in_progress": "in_progress", "internal_schema": "bad_output"}


def _permit(tool: NamTool, actor: store.Actor) -> None:
    from nam_agent.nam import playbooks

    if tool.effect == IRREVERSIBLE and actor.flow != ORCHESTRATOR:
        raise NamError("forbidden_action", f"{tool.name} is irreversible: it runs only when "
                       "a person confirms the pending action")
    flow = playbooks.registry().get(actor.flow)
    if flow is not None and tool.name not in flow.tools:
        raise NamError("forbidden_action", f"{flow.id} may not call {tool.name}",
                       details=[f"allowed: {', '.join(flow.tools) or 'none'}"])
    if tool.roles and actor.tenant != store.LOCAL_TENANT \
            and not set(actor.roles) & set(tool.roles):
        raise NamError("forbidden_role", f"{tool.name} needs one of the roles "
                       f"{', '.join(tool.roles)}")


def _check_output(tool: NamTool, result) -> None:
    problems = schema.validate(result, tool.output_schema)
    if problems:
        raise NamError("internal_schema", f"{tool.name} answered outside its contract",
                       details=problems)


def _attempt(tool: NamTool, args: dict):
    future = _POOL.submit(contextvars.copy_context().run, tool.fn, **args)
    try:
        return future.result(timeout=tool.timeout_s)
    except FutureTimeout:
        future.cancel()
        raise Unavailable(f"{tool.name} did not answer within {tool.timeout_s:g}s") from None


def _read(tool: NamTool, args: dict, state: dict) -> dict:
    last = None
    for attempt in range(1, tool.retries + 2):
        state["attempts"] = attempt
        try:
            return _attempt(tool, args)
        except Unavailable as exc:
            last = exc
            if attempt <= tool.retries:
                _sleep(min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (attempt - 1)))
        except Rejected as exc:
            raise NamError("bad_request", f"{tool.name}: {exc}") from None
    raise NamError(tool.unavailable_code, f"{tool.name} could not be answered after "
                   f"{state['attempts']} attempt(s): {last}. This is not the same as "
                   "'no data': the source was not consulted.")


# -- writes: idempotency ------------------------------------------------------

_RESULTS = f"""
CREATE TABLE IF NOT EXISTS nam_tool_results (
    tenant TEXT NOT NULL,
    tool TEXT NOT NULL,
    key TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    state TEXT NOT NULL,                -- running | done | unknown
    result_json TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL DEFAULT ({store._NOW}),
    PRIMARY KEY (tenant, tool, key)
);
"""


def _claim(conn: sqlite3.Connection, tool: NamTool, tenant: str, key: str,
           args_hash: str) -> dict | None:
    """The stored result for a repeat, None when this call now holds the key."""
    conn.executescript(_RESULTS)
    with store._write(conn):
        row = conn.execute(
            "SELECT args_hash, state, result_json, (julianday('now') - julianday(at)) * 86400 "
            "FROM nam_tool_results WHERE tenant = ? AND tool = ? AND key = ?",
            (tenant, tool.name, key)).fetchone()
        if row is None:
            conn.execute("INSERT INTO nam_tool_results (tenant, tool, key, args_hash, state, "
                         "run_id) VALUES (?, ?, ?, ?, 'running', ?)",
                         (tenant, tool.name, key, args_hash, store.current().run_id))
            return None
    stored_hash, state, result, age = row
    if stored_hash != args_hash:
        raise NamError("bad_request", f"idempotency key {key!r} was used for {tool.name} with "
                       "other arguments; a new write needs a new key")
    if state == "done":
        return json.loads(result)
    if state == "running" and age < CLAIM_TIMEOUT_S:
        raise NamError("in_progress", f"{tool.name} with key {key!r} is still running; retry "
                       "to get its result")
    raise NamError("internal", f"{tool.name} with key {key!r} stopped midway, so whether it "
                   "took effect is unknown. It is not run again under this key: check the "
                   "target system.")


def _settle(conn, tool: NamTool, tenant: str, key: str, state: str, result=None) -> None:
    with store._write(conn):
        if state == "released":
            conn.execute("DELETE FROM nam_tool_results WHERE tenant = ? AND tool = ? AND key = ?",
                         (tenant, tool.name, key))
        else:
            conn.execute("UPDATE nam_tool_results SET state = ?, result_json = ? "
                         "WHERE tenant = ? AND tool = ? AND key = ?",
                         (state, json.dumps(result) if result is not None else "", tenant,
                          tool.name, key))


def _write(conn, tool: NamTool, args: dict, key: str, args_hash: str,
           state: dict) -> tuple[dict, bool]:
    if not key or not re.match(KEY_PATTERN, key):
        raise NamError("bad_request", f"{tool.name} changes state: it needs an idempotency "
                       "key of 8 to 128 characters (letters, digits, _ . : -)")
    tenant = store.current().tenant
    stored = _claim(conn, tool, tenant, key, args_hash)
    if stored is not None:
        return stored, True
    state["attempts"] = 1
    try:
        result = tool.fn(**args, idempotency_key=key)
    except (Unavailable, Rejected) as exc:
        # the backend says it did not act: the key is free for a retry
        _settle(conn, tool, tenant, key, "released")
        if isinstance(exc, Rejected):
            raise NamError("bad_request", f"{tool.name}: {exc}") from None
        raise NamError(tool.unavailable_code, f"{tool.name} could not be run: {exc}. It was "
                       "not retried; the same key may be sent again.") from None
    except BaseException:
        _settle(conn, tool, tenant, key, "unknown")
        raise
    problems = schema.validate(result, tool.output_schema)
    if problems:
        _settle(conn, tool, tenant, key, "unknown")
        raise NamError("internal_schema", f"{tool.name} answered outside its contract",
                       details=problems)
    _settle(conn, tool, tenant, key, "done", result)
    return result, False


# -- raw KG fetches made by a flow outside the tool layer ---------------------

class FetchLog:
    """ctx.fetch for a flow that calls the KG itself (need capture's scan):
    the same fetch, with each call noted and written to the audit log by
    flush() once the flow returns. It is called from the flow's worker
    threads, so it only collects; the rows are written on the caller's
    connection, in its thread."""

    def __init__(self, inner):
        self.inner = inner
        self._rows: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, kg_url: str, path: str, params: dict) -> dict:
        started = time.monotonic()
        status, code = "ok", ""
        try:
            return self.inner(kg_url, path, params)
        except Exception as exc:
            status, code = "unavailable", type(exc).__name__[:60]
            raise
        finally:
            with self._lock:
                self._rows.append({"name": path[:80], "status": status, "code": code,
                                   "ms": int((time.monotonic() - started) * 1000),
                                   "args_hash": _hash(params),
                                   "args": json.dumps(params, sort_keys=True,
                                                      default=str)[:600]})

    def flush(self, conn: sqlite3.Connection, case_id: str = "") -> None:
        with self._lock:
            rows, self._rows = self._rows, []
        for r in rows:
            audit.record(conn, kind="kg_fetch", name=r["name"], effect=READ, status=r["status"],
                         error_code=r["code"], ms=r["ms"], case_id=case_id,
                         detail={"args_hash": r["args_hash"], "args": r["args"]})


# -- for a model --------------------------------------------------------------

def model_tools(ctx, names) -> list:
    """Core nam_agent Tools for the named read tools. The model reads each result
    (or error) as JSON; a write or irreversible tool is refused here."""
    from nam_agent.tools.registry import Tool

    table = registry(ctx)
    out = []
    for name in names:
        tool = table[name]
        if tool.effect != READ:
            raise ValueError(f"{name} changes state and is not given to a model")
        out.append(Tool(name=f"nam_{name}", description=tool.description,
                        input_schema=tool.input_schema, fn=_for_model(ctx, name)))
    return out


def _for_model(ctx, name: str):
    from nam_agent.nam import guard

    def fn(**args) -> str:
        try:
            # what the KG holds is data for the model, never instructions
            return guard.wrap_json(call(ctx, name, args), f"tool:{name}")
        except NamError as exc:
            return json.dumps({"error": exc.body()["error"]}, ensure_ascii=False)

    return fn
