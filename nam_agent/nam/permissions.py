"""What a caller may do: NAM workflow permissions, enforced in code.

The identity context (nam_agent/nam/identity.py) says who is calling. This module
decides, from policy/permissions.json, whether that caller may:

  run a flow       flows:   flow id  -> roles allowed to run it
  pass a gate      gates:   gate     -> roles allowed to approve it (orchestrator.py)
  confirm an act   actions: kind     -> roles allowed to confirm it (orchestrator.py)
  touch documents  documents: read | upload | update | delete -> roles (docs/store.py)
  touch a case     a case belongs to the tenant that opened it; nobody else
                   may read or change it

The PR Creation Flow's tools (stage_tools.py) check their acts with
require(roles, action) against the same "actions" section: need,
estimate.build, estimate.lock, pr.fill, pr.confirm, case.read.

The matrix is a draft for review (its "status" field says so). Changing who
may do what is an edit to that file, not to code.

A case's tenant is stored with it (nam_agent/nam/store.py), set when the case is
opened. Cases opened outside a harness call, and nam_needs rows from before
tenants existed, belong to the tenant "local", the one the dashboard runs as.
An API caller from any other tenant cannot reach them.
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from pathlib import Path

from nam_agent.nam import store
from nam_agent.nam.errors import NamError
from nam_agent.nam.identity import Caller

POLICY = Path(__file__).parent / "policy" / "permissions.json"
LOCAL_TENANT = store.LOCAL_TENANT


@lru_cache(maxsize=1)
def policy() -> dict:
    data = json.loads(POLICY.read_text(encoding="utf-8"))
    roles = set(data["roles"])
    for section in ("flows", "gates", "actions", "documents"):
        for name, allowed in data[section].items():
            unknown = set(allowed) - roles
            if unknown:
                raise ValueError(f"permissions.json {section}.{name}: unknown roles {unknown}")
    return data


def canonical_role(name: str) -> str | None:
    data = policy()
    if name.upper() in data["roles"]:
        return name.upper()
    return data["aliases"].get(name.lower())


def _allowed(section: str, name: str) -> list[str]:
    try:
        return list(policy()[section][name])
    except KeyError:
        # a flow, gate or action with no entry is allowed to nobody
        return []


def _require(caller: Caller, section: str, name: str, code: str, what: str) -> None:
    allowed = _allowed(section, name)
    if not set(caller.roles) & set(allowed):
        raise NamError(code, f"{what} {name} needs one of the roles "
                       f"{', '.join(allowed) or '(none)'}; the caller has "
                       f"{', '.join(caller.roles)}")


def require_flow(caller: Caller, flow_id: str) -> None:
    _require(caller, "flows", flow_id, "forbidden_role", "the flow")


def require_gate(caller: Caller, gate: str) -> None:
    _require(caller, "gates", gate, "forbidden_role", "the gate")


def require_document(caller: Caller, op: str) -> None:
    """read | upload | update | delete, on a case's documents (nam_agent/nam/docs/)."""
    _require(caller, "documents", op, "forbidden_role", "the document operation")


def require_action(caller: Caller, kind: str) -> None:
    _require(caller, "actions", kind, "forbidden_action", "confirming")


# -- tenant scoping of cases --------------------------------------------------

def case_tenant(conn: sqlite3.Connection, case_id: str) -> str | None:
    """The tenant that owns a case, or None when there is no such case."""
    return store.tenant_of(conn, case_id)


def require_case(conn: sqlite3.Connection, caller: Caller, case_id: str) -> None:
    owner = case_tenant(conn, case_id)
    if owner is None:
        raise NamError("not_found", f"no case {case_id}")
    if owner != caller.tenant:
        raise NamError("forbidden_tenant", f"case {case_id} belongs to another tenant")


# -- the PR Creation Flow's acts ------------------------------------------------

class Forbidden(Exception):
    """The caller's roles do not allow the act."""


def require(roles: tuple[str, ...], action: str) -> None:
    """Forbidden unless one of `roles` may do `action`; an action with no
    entry is allowed to nobody."""
    allowed = _allowed("actions", action)
    if not set(roles) & set(allowed):
        raise Forbidden(f"'{action}' needs one of the roles {', '.join(allowed) or '(none)'}; "
                        f"the caller has {', '.join(roles) or 'none'}")
