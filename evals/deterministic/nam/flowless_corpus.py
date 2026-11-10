"""Flowless work: user words that name no NAM step, under one generic system
prompt, each with what the harness must do with it (router rule "words",
intent.py, case.ask). Used by test_nam_flowless.py and the metric
`flowless` (nam_metrics.py).

Each entry: (where, message, flow, check)
  where    "none" (no case), "S3", "S4" or "S7": the case it is sent on
  flow     the step that must run ("nam.route": none, the router answers)
  check    what must hold after it, or None:
             ("qty", n)       line 1 of the quantity is n
             ("rate", r)      line 1 of the estimate has rate r
             ("pr", k, v)     the PR header field k is v
             ("status", s)    the reply's status is s
             ("not_applied",) the reply says the words changed nothing
"""

GENERIC = "Procurix NAM assistant"

CORPUS = (
    # -- no case: a need opens one, a question is answered, the rest refused
    ("none", "We need 20 ring joint gaskets for the CDU shutdown", "need.capture", None),
    ("none", "gaskets 20 nos urgent", "need.capture", None),
    ("none", "I want to buy 10 laptops", "need.capture", None),
    ("none", "Required: 50 gate valves 4 inch", "need.capture", None),
    ("none", "procure housekeeping manpower for one year", "need.capture", None),
    ("none", "we need spiral wound gaskets for the hydrocracker", "need.capture", None),
    ("none", "What did we pay for gaskets last time?", "case.ask", None),
    ("none", "how much did gaskets cost before?", "case.ask", None),
    ("none", "Show me past tenders for gaskets", "case.ask", None),
    ("none", "List past tenders for gaskets", "case.ask", None),
    ("none", "find old tenders for gaskets", "case.ask", None),
    ("none", "What is a PQC?", "case.ask", None),
    ("none", "What does EMD mean?", "case.ask", None),
    ("none", "what is the difference between SCC and GCC?", "case.ask", None),
    ("none", "Explain the SOR", "case.ask", None),
    ("none", "hello", "case.ask", None),
    ("none", "good morning", "case.ask", None),
    ("none", "go ahead", "case.ask", None),
    ("none", "Write me a poem about the sea", "nam.route", ("status", "refused")),
    ("none", "What's the weather in Guwahati?", "nam.route", ("status", "refused")),
    # -- a case at S3, the quantity
    ("S3", "the quantity should be 35", "quantity.finalise", ("qty", 35)),
    ("S3", "make it 35 nos", "quantity.finalise", ("qty", 35)),
    ("S3", "35 nos", "quantity.finalise", ("qty", 35)),
    ("S3", "change quantity to 40", "quantity.finalise", ("qty", 40)),
    ("S3", "set the qty to 12", "quantity.finalise", ("qty", 12)),
    ("S3", "line 1 quantity should be 25", "quantity.finalise", ("qty", 25)),
    ("S3", "make line 1 50 nos", "quantity.finalise", ("qty", 50)),
    ("S3", "what is left to do?", "case.ask", None),
    ("S3", "where does this case stand?", "case.ask", None),
    ("S3", "what is the quantity?", "case.ask", None),
    ("S3", "why was PUR-4 chosen?", "case.ask", None),
    ("S3", "who selected the precedent?", "case.ask", None),
    ("S3", "use PUR-6 instead", "precedent.select", None),
    ("S3", "let's go with PUR-6", "precedent.select", None),
    ("S3", "go ahead", "quantity.finalise", None),
    ("S3", "next", "quantity.finalise", None),
    ("S3", "ok, proceed", "quantity.finalise", None),
    ("S3", "approve it", "case.ask", None),
    ("S3", "I approve", "case.ask", None),
    ("S3", "change the category to services", "need.classify", None),
    ("S3", "make it a lot more", "nam.route", ("status", "needs_input")),
    # -- a case at S4, the estimate
    ("S4", "the rate for line 1 is 520", "estimate.goods", ("rate", 520)),
    ("S4", "the rate is Rs 520", "estimate.goods", ("rate", 520)),
    ("S4", "line 1 rate 520", "estimate.goods", ("rate", 520)),
    ("S4", "set the rate of line 1 to 610", "estimate.goods", ("rate", 610)),
    ("S4", "line 1 rate 5x0", "estimate.goods", ("not_applied",)),
    ("S4", "price it", "estimate.goods", None),
    ("S4", "work out the cost", "estimate.goods", None),
    ("S4", "next", "estimate.goods", None),
    ("S4", "carry on", "estimate.goods", None),
    ("S4", "what does the estimate come to?", "case.ask", None),
    ("S4", "how many lines are there?", "case.ask", None),
    ("S4", "What did we pay for gaskets last time?", "case.ask", None),
    ("S4", "what is a BOQ?", "case.ask", None),
    ("S4", "hello", "case.ask", None),
    # -- a case at S7, the PR
    ("S7", "the cost centre is 4100, plant NR01 and purchasing group P12", "pr.build",
     ("pr", "cost_centre", "4100")),
    ("S7", "plant is NR02", "pr.build", ("pr", "plant", "NR02")),
    ("S7", "go ahead", "pr.build", None),
    ("S7", "approve it", "case.ask", None),
    ("S7", "what is left to do?", "case.ask", None),
)


def _setup(where: str):
    """A fresh context and case for one entry, and the caller to send as."""
    import tempfile
    from pathlib import Path

    import nam_walk
    from nam_fakes import OFFICER, ctx

    c = ctx(Path(tempfile.mkdtemp()))
    if where == "none":
        return c, None, OFFICER
    if where == "S3":
        return c, nam_walk.goods_at_s3(c), OFFICER
    if where == "S4":
        return c, nam_walk.goods_at_s4(c), OFFICER
    return c, nam_walk.goods_frozen(c), nam_walk.BUYER


def check(entry) -> str:
    """'' when the entry holds, else what went wrong."""
    import nam_agent.nam as harness
    from nam_agent.nam import need, orchestrator, schema

    where, message, flow, want = entry
    c, case_id, caller = _setup(where)
    req = {"system": GENERIC, "messages": [{"role": "user", "content": message}]}
    if case_id:
        req["case_id"] = case_id
    body = harness.run(req, c, caller)
    if "error" in body:
        return f"error {body['error']['code']}: {body['error']['message'][:120]}"
    if schema.validate(body, schema.load("response")):
        return "the reply is outside its schema"
    if body["flow"] != flow:
        return f"ran {body['flow']} ({body['route']['by']}: {body['route']['why'][:80]})"
    if want is None:
        return ""
    wf = orchestrator.workflow(need.load(c.conn, case_id)) if case_id else {}
    if want[0] == "status" and body["status"] != want[1]:
        return f"status {body['status']}, not {want[1]}"
    if want[0] == "qty":
        got = orchestrator._artifact(wf, "quantity")["lines"][0]["qty"]
        return "" if got == want[1] else f"quantity {got}, not {want[1]}"
    if want[0] == "rate":
        got = orchestrator._artifact(wf, "estimate")["lines"][0]["rate"]
        return "" if got == want[1] else f"rate {got}, not {want[1]}"
    if want[0] == "pr":
        got = (orchestrator._artifact(wf, "pr") or {}).get("header", {}).get(want[1])
        return "" if got == want[2] else f"PR {want[1]} {got}, not {want[2]}"
    if want[0] == "not_applied":
        failed = [x["name"] for x in body["verification"]["checks"] if not x["ok"]]
        if "message_applied" not in failed or body["status"] == "needs_approval":
            return f"the unread words were not flagged (status {body['status']})"
    return ""
