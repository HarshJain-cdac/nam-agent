"""DETERMINISTIC EVAL: docs/backend-integration.md names every endpoint.

The guide is what a backend team connects by, so a route added to the server
without a line in it is a route nobody can find, and a route the guide names
that the server lacks is a call that 404s. Both are read from the code.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUIDE = (ROOT / "docs" / "backend-integration.md").read_text(encoding="utf-8")
SERVER = ROOT / "nam_agent" / "server"
CODE = "".join((SERVER / n).read_text(encoding="utf-8") for n in ("http.py", "nam_routes.py"))
API = (ROOT / "nam_agent" / "nam" / "api.py").read_text(encoding="utf-8")


def _paths() -> set[str]:
    found = set(re.findall(r'"(/api/[a-z_/]+)"', CODE))
    found |= {f"/api/nam/{r}" for r in re.findall(r'route == "([a-z_]+)"', CODE)}
    return {p.rstrip("/") for p in found if p not in ("/api/", "/api/nam", "/api/nit",
                                                     "/api/tender")}


def test_every_server_route_is_in_the_guide():
    missing = sorted(p for p in _paths()
                     if p not in GUIDE and f"`{p.rsplit('/', 1)[1]}`" not in GUIDE
                     and f"/{p.rsplit('/', 1)[1]}`" not in GUIDE)
    assert not missing, f"docs/backend-integration.md does not name: {missing}"


def test_every_v1_route_is_in_the_guide():
    listed = re.findall(r"^\s+(GET|POST|PUT|DELETE)\s+(/v1/nam/\S+)", API, re.MULTILINE)
    assert listed
    for method, path in listed:
        assert f"{method} {path}" in GUIDE, f"{method} {path}"


def test_the_guide_says_7777_and_names_the_identity_headers():
    assert "7777" in GUIDE and "7788" not in GUIDE
    for header in ("X-NAM-Tenant", "X-NAM-User", "X-NAM-Roles", "NAM_SERVER_KEY"):
        assert header in GUIDE


def test_the_button_texts_in_the_guide_are_the_harness_own():
    from nam_agent.nam import stage_tools

    for label in (stage_tools.YES_LABEL, stage_tools.NO_LABEL, stage_tools.UNDO_LABEL):
        assert label in GUIDE, label
