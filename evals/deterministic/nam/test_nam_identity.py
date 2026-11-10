"""Step 6: the trusted identity context and the NAM permissions.

NAM harness authenticates nobody (the backend's auth component). It reads tenant, user and
roles from the upstream auth component and enforces, in code: which roles may
run a flow, pass a gate or confirm an action, and that a case is reachable
only by the tenant that opened it."""

import http.client
import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest
from nam_fakes import OFFICER, ask, ctx, fake_kg, run

import nam_agent.nam as harness
from nam_agent.nam import api, identity, need, permissions, playbooks
from nam_agent.nam.errors import NamError

OTHER_TENANT = harness.Caller(tenant="acme", user="u9", roles=("U1",))
DRAFTER = harness.Caller(tenant="nrl", user="u2", roles=("U4",))


def _code(body: dict) -> str:
    return body["error"]["code"]


# -- reading the context ------------------------------------------------------

@pytest.mark.parametrize("tenant, user, roles, code", [
    (None, "u1", "U1", "identity_missing"),
    ("nrl", "", "U1", "identity_missing"),
    ("nrl", "u1", "  ", "identity_missing"),
    ("nrl corp", "u1", "U1", "identity_missing"),        # malformed tenant
    ("nrl", "u1\nX-Admin: 1", "U1", "identity_missing"),  # header smuggling
    ("nrl", "u1", "U1,ROOT", "identity_missing"),          # unknown role
    ("nrl", "u1", ",".join(["U1"] * 11), "identity_missing"),
    ("local", "u1", "U1", "forbidden_tenant"),             # the dashboard's, reserved
])
def test_bad_identity_context_is_refused(tenant, user, roles, code):
    with pytest.raises(NamError) as err:
        identity.parse(tenant, user, roles)
    assert err.value.code == code


def test_roles_are_canonical_ids_and_aliases_map_to_them():
    caller = identity.parse("nrl", "u1@nrl.in", "officer, U3,u3")
    assert caller == harness.Caller(tenant="nrl", user="u1@nrl.in", roles=("U1", "U3"))


def test_a_credential_is_not_an_identity():
    # the harness never reads Authorization: a bearer token alone is no context
    with pytest.raises(NamError) as err:
        identity.require_headers({"Authorization": "Bearer secret-token"})
    assert err.value.code == "identity_missing"
    # the dashboard's chat reads no context there either: its own officer
    assert identity.from_headers({"Authorization": "Bearer secret-token"}) is None
    # and no harness code looks the header up (docstrings may name it)
    lookup = re.compile(r"""['"]Authorization['"]""", re.IGNORECASE)
    for path in Path(harness.__file__).parent.rglob("*.py"):
        assert not lookup.search(path.read_text()), path


def test_run_without_a_caller_is_refused(tmp_path):
    body = harness.run(ask("i need to procure gaskets"), ctx(tmp_path), None)
    assert _code(body) == "identity_missing" and harness.status_of(body) == 401


# -- flows, gates, actions ----------------------------------------------------

def test_a_role_not_allowed_the_flow_is_refused(tmp_path):
    c = ctx(tmp_path)
    body = run(ask("i need to procure gaskets"), c, DRAFTER)
    assert _code(body) == "forbidden_role" and harness.status_of(body) == 403
    # refused before any work: no case was opened
    assert need.recent(c.conn) == []


def test_actions_and_gates_follow_the_matrix():
    authorized = harness.Caller(tenant="nrl", user="u3", roles=("U3",))
    permissions.require_action(authorized, "submit_pr")
    with pytest.raises(NamError) as err:
        permissions.require_action(OFFICER, "submit_pr")
    assert err.value.code == "forbidden_action"
    with pytest.raises(NamError) as err:
        permissions.require_gate(OFFICER, "documents")
    assert err.value.code == "forbidden_role"
    # anything the matrix does not name is allowed to nobody
    with pytest.raises(NamError):
        permissions.require_action(authorized, "delete_everything")


def test_every_flow_the_harness_runs_is_in_the_matrix():
    policy = permissions.policy()
    assert set(playbooks.registry()) <= set(policy["flows"])
    assert set(policy["roles"]) == {f"U{i}" for i in range(1, 8)}


# -- tenant scoping -----------------------------------------------------------

def test_a_case_belongs_to_the_tenant_that_opened_it(tmp_path):
    c = ctx(tmp_path)
    first = run(ask("i need to procure gaskets"), c)
    follow = ask("", case_id=first["case_id"],
                 answer={"question_id": "variant", "value": "ring joint"})
    other = run(follow, c, OTHER_TENANT)
    assert _code(other) == "forbidden_tenant" and harness.status_of(other) == 403
    # another user of the same tenant may carry the case on
    colleague = harness.Caller(tenant="nrl", user="u7", roles=("U1",))
    assert run(follow, c, colleague)["status"] == "ok"


def test_a_case_saved_before_a_failure_still_belongs_to_its_tenant(tmp_path):
    conn = sqlite3.connect(":memory:")
    down = run(ask("i need to procure gaskets"), ctx(tmp_path, fake_kg(down=True), conn))
    assert _code(down) == "kg_unavailable"
    retry = ask("i need to procure gaskets", case_id=down["case_id"])
    assert _code(run(retry, ctx(tmp_path, conn=conn), OTHER_TENANT)) == "forbidden_tenant"
    assert run(retry, ctx(tmp_path, conn=conn))["case_id"] == down["case_id"]


def test_cases_from_before_tenants_stay_with_the_dashboard(tmp_path):
    c = ctx(tmp_path)
    legacy = need.create(c.conn, "i need to procure gaskets")
    body = run(ask("gaskets", case_id=legacy.need_id), c)
    assert _code(body) == "forbidden_tenant"
    local = harness.Caller(tenant="local", user="dashboard", roles=("U1",))
    assert run(ask("gaskets", case_id=legacy.need_id), c, local)["case_id"] == legacy.need_id


def test_an_unknown_case_is_not_found(tmp_path):
    assert _code(run(ask("x", case_id="NAM-2026-0404"), ctx(tmp_path))) == "not_found"


# -- over HTTP ----------------------------------------------------------------

@pytest.fixture()
def port(tmp_path):
    db = tmp_path / "state.db"

    def make_ctx():
        conn = sqlite3.connect(db)
        return harness.Context(conn=conn, home=tmp_path, kg_url="http://kg",
                               fetch=fake_kg()), conn.close

    srv = api.serve("127.0.0.1", 0, make_ctx, max_body=4096, model_configured=False, kg_url="")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _post(port, headers, body=None, length=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body or ask("i need to procure gaskets")).encode()
    conn.putrequest("POST", "/v1/nam/run")
    conn.putheader("Content-Type", "application/json")
    for name, value in headers.items():
        conn.putheader(name, value)
    conn.putheader("Content-Length", str(length or len(data)))
    conn.endheaders()
    if length is None:
        conn.send(data)
    resp = conn.getresponse()
    out = resp.status, json.loads(resp.read())
    conn.close()
    return out


NRL = {"X-NAM-Tenant": "nrl", "X-NAM-User": "u1", "X-NAM-Roles": "U1"}


def test_http_identity_is_checked_before_the_body_is_read(port):
    # no context, and a body that never arrives: the 401 comes at once
    status, body = _post(port, {"Authorization": "Bearer x"}, length=1 << 30)
    assert status == 401 and _code(body) == "identity_missing"


def test_http_roles_and_tenants(port):
    status, first = _post(port, NRL)
    assert status == 200
    status, body = _post(port, {**NRL, "X-NAM-Roles": "drafter"})
    assert status == 403 and _code(body) == "forbidden_role"
    status, body = _post(port, {**NRL, "X-NAM-Tenant": "acme"},
                         ask("gaskets", case_id=first["case_id"]))
    assert status == 403 and _code(body) == "forbidden_tenant"
