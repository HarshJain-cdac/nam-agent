"""Rate limiting (task requirement 4): a caller over NAM_RATE calls a
minute is refused with 429 rate_limited, retryable, before the body is read;
other callers are not affected, and the window slides."""

import http.client
import json
import sqlite3
import threading

import pytest
from nam_fakes import IDENTITY, fake_kg

import nam_agent.nam as harness
from nam_agent.nam import api
from nam_agent.nam.errors import NamError


def test_the_window_slides_per_caller():
    now = [0.0]
    limit = api.RateLimit(2, clock=lambda: now[0])
    a = harness.Caller(tenant="nrl", user="a", roles=("U1",))
    b = harness.Caller(tenant="nrl", user="b", roles=("U1",))
    limit.check(a)
    limit.check(a)
    with pytest.raises(NamError) as exc:
        limit.check(a)
    assert exc.value.code == "rate_limited" and "retry in 61s" in exc.value.message
    limit.check(b)                                  # another caller is not affected
    now[0] = 61.0
    limit.check(a)                                  # the window slid
    api.RateLimit(0).check(a)                       # 0: off


def test_a_caller_over_the_limit_gets_429_over_http(tmp_path):
    db = tmp_path / "state.db"

    def make_ctx():
        conn = sqlite3.connect(db)
        return harness.Context(conn=conn, home=tmp_path, kg_url="http://kg",
                               fetch=fake_kg()), conn.close

    srv = api.serve("127.0.0.1", 0, make_ctx, max_body=1 << 20, model_configured=False,
                    kg_url="", rate_per_minute=2)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        statuses = []
        for _ in range(3):
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            body = json.dumps({"system": "Status of the case", "messages": [
                {"role": "user", "content": ""}]})
            conn.request("POST", "/v1/nam/run", body=body,
                         headers={"Content-Type": "application/json", **IDENTITY})
            resp = conn.getresponse()
            statuses.append((resp.status, json.loads(resp.read())))
    finally:
        srv.shutdown()
        srv.server_close()
    assert [s for s, _ in statuses][:2] != [429, 429] and statuses[2][0] == 429
    error = statuses[2][1]["error"]
    assert error["code"] == "rate_limited" and error["retryable"] is True
