"""The Need Record (plan Step 7): one versioned record per case.

Every call on a case reads and writes the same record, whoever makes it; a
write against an older version is a 409 stale_version and changes nothing;
the history names who changed what, from which flow and run, and why; the
cases in nam_needs are copied over without loss; and case numbers are drawn
atomically, so concurrent calls never share one."""

import http.client
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime

import pytest
from nam_fakes import IDENTITY, OFFICER, ask, classified, ctx, fake_kg, run

import nam_agent.nam as harness
from nam_agent.nam import api, need, schema, store
from nam_agent.nam.errors import NamError

COLLEAGUE = harness.Caller(tenant="nrl", user="u2", roles=("U1",))
OTHER = harness.Caller(tenant="acme", user="x1", roles=("U1",))
VARIANT = {"question_id": "variant", "value": "ring joint"}


def _open(c):
    body = run(ask("i need to procure gaskets"), c)
    assert body["status"] == "needs_input", body
    return body


# -- one record ---------------------------------------------------------------

def test_calls_on_one_case_see_one_record(tmp_path):
    c = ctx(tmp_path)
    first = _open(c)
    second = run(ask("", case_id=first["case_id"], answer=VARIANT), c, COLLEAGUE)
    assert second["status"] == "ok"
    assert second["case_version"] > first["case_version"]
    # the harness, the dashboard's store calls and the record view agree
    record = harness.get_case(c, OFFICER, first["case_id"])
    assert record["case_version"] == second["case_version"]
    assert record["need"]["followups"]["answers"] == {"variant": "ring joint"}
    assert need.load(c.conn, first["case_id"]).followups["answers"] == {"variant": "ring joint"}
    assert schema.validate(record, schema.load("record")) == []


def test_the_record_lives_in_one_table(tmp_path):
    c = ctx(tmp_path)
    case_id = _open(c)["case_id"]
    tables = {r[0] for r in c.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "nam_needs" not in tables and "nam_case_access" not in tables
    assert store.tenant_of(c.conn, case_id) == "nrl"


# -- stale versions -----------------------------------------------------------

def test_a_stale_case_version_is_refused_and_changes_nothing(tmp_path):
    c = ctx(tmp_path)
    first = _open(c)
    run(ask("", case_id=first["case_id"], answer=VARIANT), c, COLLEAGUE)
    before = harness.get_case(c, OFFICER, first["case_id"])
    body = run(ask("", case_id=first["case_id"], case_version=first["case_version"],
                   options={"skip_questions": True}), c)
    assert body["error"]["code"] == "stale_version" and body["error"]["retryable"]
    assert harness.status_of(body) == 409 and body["case_id"] == first["case_id"]
    assert harness.get_case(c, OFFICER, first["case_id"]) == before


def test_the_current_case_version_is_accepted(tmp_path):
    c = ctx(tmp_path)
    first = _open(c)
    body = run(ask("", case_id=first["case_id"], case_version=first["case_version"],
                   answer=VARIANT), c)
    assert body["status"] == "ok", body


def test_two_writers_from_one_version_the_second_is_refused(tmp_path):
    c = ctx(tmp_path)
    case_id = _open(c)["case_id"]
    mine, theirs = need.load(c.conn, case_id), need.load(c.conn, case_id)
    theirs.location = "Plant 2"
    need.save(c.conn, theirs)
    mine.location = "Plant 9"
    with pytest.raises(NamError) as exc:
        need.save(c.conn, mine)
    assert exc.value.code == "stale_version"
    assert need.load(c.conn, case_id).location == "Plant 2"


def test_the_seen_version_holds_until_the_run_itself_writes(tmp_path):
    # a write landing between run()'s check and the flow's own read
    c = ctx(tmp_path)
    case_id = _open(c)["case_id"]
    seen = store.version_of(c.conn, case_id)
    other = need.load(c.conn, case_id)
    other.term = "12 months"
    need.save(c.conn, other)
    actor = store.Actor("nrl", "u1", "need.capture", "run_0000000000000001", case_id, seen)
    with store.acting(actor), pytest.raises(NamError) as exc:
        need.load(c.conn, case_id)
    assert exc.value.code == "stale_version"


def test_a_save_that_changes_nothing_writes_no_version(tmp_path):
    c = ctx(tmp_path)
    case_id = _open(c)["case_id"]
    record = need.load(c.conn, case_id)
    before = store.version_of(c.conn, case_id)
    need.save(c.conn, record)
    assert store.version_of(c.conn, case_id) == before


# -- history ------------------------------------------------------------------

def test_history_names_who_changed_what_and_why(tmp_path):
    c = ctx(tmp_path)
    first = _open(c)
    second = run(ask("", case_id=first["case_id"], answer=VARIANT), c, COLLEAGUE)
    body = harness.history(c, OFFICER, first["case_id"])
    assert schema.validate(body, schema.load("history")) == []
    entries = body["entries"]
    assert [e["case_version"] for e in entries] == list(range(1, second["case_version"] + 1))
    opened = entries[0]
    assert opened["by"] == {"tenant": "nrl", "user": "u1"}
    assert opened["reason"] == "opened from the stated need"
    assert opened["flow"] == "need.capture" and opened["run_id"] == first["run_id"]
    answered = next(e for e in entries if e["run_id"] == second["run_id"]
                    and e["flow"] == "need.capture")
    assert answered["by"]["user"] == "u2" and "followups" in answered["changed"]
    # the officer's classification, then the stage machine's own write, in the
    # same run and in the caller's name
    third = classified(c, first["case_id"], COLLEAGUE)
    entries = harness.history(c, OFFICER, first["case_id"])["entries"]
    set_by, moved = entries[-2], entries[-1]
    assert set_by["flow"] == "need.classify" and set_by["by"]["user"] == "u2"
    assert set_by["reason"] == "classification: tender_type set to Open"
    assert moved["flow"] == "nam.orchestrator" and moved["run_id"] == third["run_id"]
    assert moved["reason"] == "orchestrator: S1 -> S2" and moved["changed"] == ["workflow"]
    assert all(e["reason"] and e["changed"] for e in entries)


def test_an_older_version_can_be_read_back(tmp_path):
    c = ctx(tmp_path)
    first = _open(c)
    run(ask("", case_id=first["case_id"], answer=VARIANT), c)
    old = harness.get_case(c, OFFICER, first["case_id"], version=first["case_version"])
    assert old["case_version"] == first["case_version"] < old["latest_version"]
    assert old["need"]["followups"]["answers"] == {}
    missing = harness.get_case(c, OFFICER, first["case_id"], version=99)
    assert missing["error"]["code"] == "not_found"


def test_reads_are_scoped_like_runs(tmp_path):
    c = ctx(tmp_path)
    case_id = _open(c)["case_id"]
    assert harness.get_case(c, OTHER, case_id)["error"]["code"] == "forbidden_tenant"
    assert harness.history(c, OTHER, case_id)["error"]["code"] == "forbidden_tenant"
    assert harness.get_case(c, None, case_id)["error"]["code"] == "identity_missing"
    assert harness.get_case(c, OFFICER, "NAM-2020-9999")["error"]["code"] == "not_found"
    # the dashboard's draft list is its own tenant's
    assert all(r["need_id"] != case_id for r in need.recent(c.conn))


# -- migration from nam_needs -------------------------------------------------

def _legacy_db(path):
    year = datetime.now().year
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE nam_needs (need_id TEXT PRIMARY KEY, session_id TEXT NOT NULL "
                 "DEFAULT '', record_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL "
                 "DEFAULT (datetime('now')))")
    conn.execute("CREATE TABLE nam_case_access (case_id TEXT PRIMARY KEY, tenant TEXT NOT NULL, "
                 "created_by TEXT NOT NULL DEFAULT '', created_at TEXT)")
    full = need.NeedRecord(need_id=f"NAM-{year}-0003", created_at="2026-01-02T03:04:05",
                           raw_text="supply of gaskets", selected_cases=["PUR-4"],
                           stage="selected", followups={"answers": {"variant": "ring joint"}})
    record = asdict(full)
    record.pop("version")
    # a record from before readiness and followups existed
    old = {"need_id": f"NAM-{year}-0007", "created_at": "2025-12-01T00:00:00",
           "raw_text": "AMC for housekeeping", "stage": "describe"}
    rows = [(f"NAM-{year}-0003", "s1", json.dumps(record), "2026-01-02 03:04:05"),
            (f"NAM-{year}-0007", "", json.dumps(old), "2026-01-03 00:00:00")]
    conn.executemany("INSERT INTO nam_needs VALUES (?, ?, ?, ?)", rows)
    conn.execute("INSERT INTO nam_case_access (case_id, tenant, created_by) VALUES (?, ?, ?)",
                 (f"NAM-{year}-0007", "nrl", "u1"))
    conn.commit()
    return conn, rows, year


def test_migration_copies_nam_needs_without_loss(tmp_path):
    conn, rows, year = _legacy_db(tmp_path / "state.db")
    store.ensure(conn)
    store.ensure(conn)                      # idempotent
    assert conn.execute("SELECT count(*) FROM nam_cases").fetchone()[0] == 2
    for need_id, _session, record_json, _updated in rows:
        copied = store.case_row(conn, need_id)
        assert copied["record"] == json.loads(record_json)
        assert copied["version"] == 1
        loaded = asdict(need.load(conn, need_id))
        assert {k: loaded[k] for k in json.loads(record_json)} == json.loads(record_json)
        entry, = store.versions(conn, need_id)
        assert entry["by"]["user"] == "migration" and entry["reason"] == "copied from nam_needs"
    assert store.tenant_of(conn, f"NAM-{year}-0003") == "local"
    assert store.tenant_of(conn, f"NAM-{year}-0007") == "nrl"      # Step 6's binding kept
    assert store.case_row(conn, f"NAM-{year}-0003")["stage"] == "S2"
    # nam_needs is left exactly as it was
    assert conn.execute("SELECT * FROM nam_needs ORDER BY need_id").fetchall() == rows
    # numbering goes on after the highest number already used
    assert store.next_case_id(conn) == f"NAM-{year}-0008"


def test_a_migrated_case_continues_like_any_other(tmp_path):
    conn, _rows, year = _legacy_db(tmp_path / "state.db")
    c = ctx(tmp_path, conn=conn)
    body = run(ask("rubber, 3 mm", case_id=f"NAM-{year}-0007"), c)
    assert body["case_id"] == f"NAM-{year}-0007" and body["case_version"] > 1, body


# -- atomic numbering ---------------------------------------------------------

def test_case_numbers_are_unique_under_concurrency(tmp_path):
    db = tmp_path / "state.db"
    store.ensure(sqlite3.connect(db))

    def draw(_):
        conn = sqlite3.connect(db, timeout=30)
        try:
            return [store.next_case_id(conn) for _ in range(5)]
        finally:
            conn.close()

    with ThreadPoolExecutor(8) as pool:
        drawn = [cid for batch in pool.map(draw, range(8)) for cid in batch]
    assert len(drawn) == len(set(drawn)) == 40


@pytest.fixture()
def server(tmp_path):
    db = tmp_path / "state.db"

    def make_ctx():
        conn = sqlite3.connect(db, timeout=30)
        return harness.Context(conn=conn, home=tmp_path, kg_url="http://kg",
                               fetch=fake_kg()), conn.close

    srv = api.serve("127.0.0.1", 0, make_ctx, max_body=1 << 20, model_configured=False,
                    kg_url="")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data,
                 headers={"Content-Type": "application/json", **IDENTITY, **(headers or {})})
    resp = conn.getresponse()
    payload = json.loads(resp.read())
    conn.close()
    return resp.status, payload


def test_concurrent_captures_get_distinct_cases(server):
    with ThreadPoolExecutor(6) as pool:
        replies = list(pool.map(lambda _: _call(server, "POST", "/v1/nam/run",
                                                ask("i need to procure gaskets")), range(6)))
    assert all(status == 200 for status, _ in replies), replies
    assert len({body["case_id"] for _, body in replies}) == 6


def test_cases_and_history_over_http(server):
    _, first = _call(server, "POST", "/v1/nam/run", ask("i need to procure gaskets"))
    case_id = first["case_id"]
    status, _ = _call(server, "POST", "/v1/nam/run",
                      ask("", case_id=case_id, case_version=first["case_version"],
                          answer=VARIANT))
    assert status == 200
    status, body = _call(server, "POST", "/v1/nam/run",
                         ask("", case_id=case_id, case_version=first["case_version"],
                             answer=VARIANT))
    assert status == 409 and body["error"]["code"] == "stale_version"

    status, record = _call(server, "GET", f"/v1/nam/cases/{case_id}")
    assert status == 200 and schema.validate(record, schema.load("record")) == []
    status, old = _call(server, "GET", f"/v1/nam/cases/{case_id}?version=1")
    assert status == 200 and old["case_version"] == 1
    status, hist = _call(server, "GET", f"/v1/nam/cases/{case_id}/history")
    assert status == 200 and len(hist["entries"]) == record["case_version"]


@pytest.mark.parametrize("method, path, headers, status, code", [
    ("GET", "/v1/nam/cases/NAM-2020-9999", {}, 404, "not_found"),
    ("GET", "/v1/nam/cases/..%2Fetc", {}, 404, "not_found"),
    ("GET", "/v1/nam/cases/NAM-2020-9999?version=x", {}, 400, "bad_request"),
    ("GET", "/v1/nam/cases/NAM-2020-9999", {"X-NAM-Roles": ""}, 401, "identity_missing"),
    ("GET", "/v1/nam/cases/NAM-2020-9999", {"X-NAM-Tenant": "acme"}, 404, "not_found"),
    ("POST", "/v1/nam/cases/NAM-2020-9999", {}, 405, "method_not_allowed"),
], ids=["unknown", "bad-id", "bad-version", "no-identity", "unknown-other-tenant", "post"])
def test_case_route_errors(server, method, path, headers, status, code):
    got, body = _call(server, method, path, body={} if method == "POST" else None,
                      headers=headers)
    assert got == status and body["error"]["code"] == code


def test_another_tenants_case_is_403_over_http(server):
    _, first = _call(server, "POST", "/v1/nam/run", ask("i need to procure gaskets"))
    for path in (f"/v1/nam/cases/{first['case_id']}", f"/v1/nam/cases/{first['case_id']}/history"):
        status, body = _call(server, "GET", path, headers={"X-NAM-Tenant": "acme"})
        assert status == 403 and body["error"]["code"] == "forbidden_tenant"
