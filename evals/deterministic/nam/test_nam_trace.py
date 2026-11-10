"""The audit trail (plan Step 17): every call is recorded with who asked, the
log is hash-chained, and a case's trace leads from each output to the tool
results, officers and rules it came from and to every approval. Queryable
over HTTP and exportable as JSON lines."""

import http.client
import json
import sqlite3
import threading

import pytest
from nam_fakes import IDENTITY, OFFICER, ask, ctx, fake_kg, run
from nam_walk import call, confirm, goods_approved

import nam_agent.nam as harness
from nam_agent.nam import api, audit, trace


def _closed(c):
    case_id = goods_approved(c)
    from nam_walk import COMMERCIAL
    confirm(c, case_id, call(c, case_id, "Hand off to the NIT module", caller=COMMERCIAL),
            COMMERCIAL)
    return case_id


def test_every_call_is_recorded_with_who_asked(tmp_path):
    c = ctx(tmp_path)
    body = run(ask("i need to procure gaskets"), c)
    row = audit.events(c.conn, run_id=body["run_id"], kind="call")[0]
    assert (row["user"], row["name"], row["status"]) == ("u1", "need.capture", body["status"])
    assert row["detail"]["roles"] == ["U1"] and row["detail"]["route"]["by"] == "intent"
    assert "gaskets" not in json.dumps(row["detail"])          # the message is not stored
    refused = harness.run(ask("x"), c, None)
    err = audit.events(c.conn, run_id=refused["run_id"], kind="call")[0]
    assert (err["status"], err["error_code"], err["user"]) == ("error", "identity_missing", "")


def test_the_log_is_chained_and_an_edit_or_a_deletion_breaks_it(tmp_path):
    c = ctx(tmp_path, conn=sqlite3.connect(tmp_path / "s.db"))
    for _ in range(3):
        run(ask("i need to procure gaskets"), c)
    assert audit.verify(c.conn)["ok"]
    c.conn.execute("DROP TRIGGER nam_events_no_delete")
    c.conn.execute("DELETE FROM nam_events WHERE id = 3")
    c.conn.commit()
    broken = audit.verify(c.conn)
    assert not broken["ok"] and broken["broken_at"] == 4


def test_rows_from_before_the_chain_are_unchained_not_broken(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.executescript("""CREATE TABLE nam_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL DEFAULT '', tenant TEXT NOT NULL, user TEXT NOT NULL,
        run_id TEXT NOT NULL DEFAULT '', flow TEXT NOT NULL DEFAULT '',
        case_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, name TEXT NOT NULL,
        effect TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
        error_code TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 1,
        ms INTEGER NOT NULL DEFAULT 0, detail_json TEXT NOT NULL DEFAULT '{}');
        INSERT INTO nam_events (tenant, user, kind, name, status) VALUES ('t', 'u', 'tool', 'x', 'ok');""")
    audit.record(conn, kind="tool", name="y", status="ok")
    out = audit.verify(conn)
    assert out == {"ok": True, "rows": 2, "unchained": 1, "broken_at": None,
                   "says": "1 chained row(s) verified"}


def test_the_trace_leads_each_output_to_its_inputs_and_approvals(tmp_path):
    c = ctx(tmp_path)
    case_id = _closed(c)
    t = trace.build(c.conn, case_id)
    assert t["closed"] and t["integrity"]["ok"]
    est = t["artifacts"]["estimate"]
    assert est["flow"] == "estimate.goods" and est["user"] == OFFICER.user
    assert any(e["kind"] == "tool" and e["name"] == "price_history" for e in est["inputs"])
    line = t["evidence"]["estimate"][0]
    # the last PO price, cited, brought up to date (PR flow 2(a)-(b))
    assert line["basis"] == "po" and line["source"].startswith("price_history ev_")
    assert "a year for" in line["source"] and line["authority"] == "precedent"
    assert {x["source"] for x in t["evidence"]["clauses"]} == {"PUR-4"}
    assert t["evidence"]["precedents"]["selected"] == ["PUR-4"]
    assert t["evidence"]["precedents"]["by"][0]["user"] == OFFICER.user
    kinds = {a["kind"]: a["user"] for a in t["approvals"]}
    assert kinds == {"gate:quantity": "u1", "gate:estimate": "u1", "gate:clauses": "d1",
                     "gate:documents": "d1", "freeze_package": "v5", "submit_pr": "b1",
                     "approve": "a7", "handoff_nit": "c6"}
    # the call that opened the case, before it had an id, is in it too
    first = t["calls"][0]
    assert first["name"] == "need.capture"
    assert any(e["run_id"] == first["run_id"] and e["kind"] in ("tool", "kg_fetch")
               for e in t["events"])


@pytest.fixture()
def server(tmp_path):
    db = tmp_path / "state.db"

    def make_ctx():
        conn = sqlite3.connect(db)
        return harness.Context(conn=conn, home=tmp_path, kg_url="http://kg",
                               fetch=fake_kg()), conn.close

    srv = api.serve("127.0.0.1", 0, make_ctx, max_body=1 << 20, model_configured=False,
                    kg_url="")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], make_ctx
    srv.shutdown()
    srv.server_close()


def _get(port, path, headers=IDENTITY):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.getheader("Content-Type"), resp.read()


def test_the_trace_and_a_call_over_http_and_as_json_lines(server):
    port, make_ctx = server
    c, close = make_ctx()
    try:
        body = run(ask("i need to procure gaskets"), c)
    finally:
        close()
    case_id = body["case_id"]
    status, _, data = _get(port, f"/v1/nam/cases/{case_id}/trace")
    t = json.loads(data)
    assert status == 200 and t["case_id"] == case_id and t["calls"]
    status, mime, data = _get(port, f"/v1/nam/cases/{case_id}/trace?format=jsonl")
    rows = [json.loads(x) for x in data.decode().splitlines()]
    assert status == 200 and mime == "application/x-ndjson"
    assert {r["type"] for r in rows} >= {"call", "version", "integrity"}
    other = {**IDENTITY, "X-NAM-Tenant": "acme"}
    assert _get(port, f"/v1/nam/cases/{case_id}/trace", other)[0] == 403
    status, _, data = _get(port, f"/v1/nam/runs/{body['run_id']}")
    assert status == 200 and json.loads(data)["events"][-1]["kind"] == "call"
    assert _get(port, f"/v1/nam/runs/{body['run_id']}", other)[0] == 404
