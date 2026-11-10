"""Who is calling: the trusted identity context, read and checked for shape.

Authentication, user identity and role propagation belong to another
component, the backend's. That component authenticates the caller and
sets three headers on every call to the harness:

    X-NAM-Tenant   the organisation the caller acts for, e.g. "nrl"
    X-NAM-User     the user id, e.g. "u123"
    X-NAM-Roles    comma-separated FRS roles, e.g. "U1,U3" (or their aliases,
                   "officer,authorized_user"; see policy/permissions.json)

NAM harness trusts these as given. It never reads a credential (no Authorization
header, no key, no password) and keeps no user directory. What it does check
is that the context is present and well formed, because every permission
decision in nam_agent/nam/permissions.py is made from it.

Two readings of the headers:
  require_headers()  the harness API (api.py): all three, or 401
  from_headers()     the dashboard's chat and case routes: all three, or none
                     (None: this machine's own officer); partial is 401
A Caller acts in the PR Creation Flow too (case.acting): it carries the same
tenant, user and roles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nam_agent.nam.errors import NamError

_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
MAX_ROLES = 10


class BadIdentity(NamError):
    """The identity context is malformed or incomplete (HTTP 401)."""

    def __init__(self, message: str, code: str = "identity_missing"):
        super().__init__(code, message)


@dataclass(frozen=True)
class Caller:
    tenant: str
    user: str
    roles: tuple[str, ...]           # canonical FRS role ids, U1..U7


def parse(tenant: str | None, user: str | None, roles: str | None) -> Caller:
    """The Caller for three header values, or NamError('identity_missing')."""
    from nam_agent.nam import permissions

    tenant, user, roles = (tenant or "").strip(), (user or "").strip(), (roles or "").strip()
    missing = [name for name, value in (("X-NAM-Tenant", tenant), ("X-NAM-User", user),
                                        ("X-NAM-Roles", roles)) if not value]
    if missing:
        raise BadIdentity("the identity context is missing "
                       + ", ".join(missing) + " (set by the upstream auth component)")
    if not _TENANT.match(tenant):
        raise BadIdentity("X-NAM-Tenant is malformed")
    if tenant.lower() == permissions.LOCAL_TENANT:
        # the dashboard's own tenant, which holds the cases from before
        # tenants existed; no API caller may claim it
        raise BadIdentity(f"X-NAM-Tenant {tenant!r} is reserved", "forbidden_tenant")
    if not _USER.match(user):
        raise BadIdentity("X-NAM-User is malformed")
    names = [r.strip() for r in roles.split(",") if r.strip()]
    if not names or len(names) > MAX_ROLES:
        raise BadIdentity(f"X-NAM-Roles must list 1 to {MAX_ROLES} roles")
    canonical = []
    for name in names:
        role = permissions.canonical_role(name)
        if role is None:
            raise BadIdentity(f"X-NAM-Roles names an unknown role "
                           f"{name[:20]!r}; known: " + ", ".join(permissions.policy()["roles"]))
        if role not in canonical:
            canonical.append(role)
    return Caller(tenant=tenant, user=user, roles=tuple(canonical))


def require_headers(headers) -> Caller:
    """The context an HTTP request's headers carry (anything with .get);
    BadIdentity when any of the three is missing or malformed."""
    return parse(headers.get("X-NAM-Tenant"), headers.get("X-NAM-User"),
                 headers.get("X-NAM-Roles"))


def from_headers(headers) -> Caller | None:
    """The caller a request's headers name, or None when they name none (the
    dashboard's own officer). BadIdentity when they are partial or malformed."""
    values = [headers.get(h) for h in ("X-NAM-Tenant", "X-NAM-User", "X-NAM-Roles")]
    if not any(values):
        return None
    return parse(*values)
