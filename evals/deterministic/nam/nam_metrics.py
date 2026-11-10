"""The NAM harness's measured reliability (plan Step 18), offline and
deterministic: the same numbers every run. test_nam_metrics.py holds each
to its threshold; scripts/nam_eval_report.py prints them as a report.

  routing        every catalogued phrasing to its flow, the out-of-NAM
                 prompts refused, the vague ones asked about
  injection      hostile texts found, real tender wording left alone
  consistency    seeded cross-document inconsistencies the checker fails
  clauses        precedent clauses missing from the generated documents
  schema         responses of the end-to-end cases that passed their schemas
  end_to_end     a Goods, a Services and a Works case from need to NIT
  uncited        facts of those three cases without a source (Step 13)
  verified       replies of those cases that carry a verification (verify.py)
  false_done     replies that report a step done (ok, needs_approval) whose
                 verification failed: the feedback loop let a claim through
  flowless       words that name no step, under a generic prompt
                 (flowless_corpus.py): the step they need taken, a change in
                 words applied or said not to be, a question answered
  planned        a Goods case completed from one query by its to-do list
                 (case.plan), with only "continue", answers and approvals
  retrieval      recall@5 of past tenders over a labelled set: only against a
                 live KG named by NAM_EVAL_KG_URL (opt in), else not
                 measured (Step 13)
"""

from __future__ import annotations

import copy
import json
import tempfile
import time
from pathlib import Path

import nam_walk
from nam_fakes import case_record, ctx

from nam_agent.nam import guard, knowledge, need, orchestrator, router
from nam_agent.nam.playbooks import checker, review

THRESHOLDS = {"routing": 1.0, "refused": 1.0, "ambiguous": 1.0, "injection_found": 1.0,
              "injection_false_positives": 0.0, "consistency": 1.0, "clause_omission": 0.0,
              "schema": 1.0, "end_to_end": 1.0, "uncited": 0, "verified": 1.0,
              "false_done": 0, "planned": 1.0, "flowless": 1.0}

# a DRAFT until agreed: the share of labelled tenders the KG must return in
# its first five for their own subject (retrieval_labels.json)
RETRIEVAL_RECALL = 0.9
RETRIEVAL_K = 5


def _req(text: str) -> dict:
    return {"system": text, "messages": [{"role": "user", "content": ""}]}


def routing() -> dict:
    from test_nam_router import AMBIGUOUS, PHRASES, REFUSED

    pairs = [(flow, p) for flow, phrases in PHRASES.items() for p in phrases]
    hits = sum(router.route(_req(p), None).flow == flow for flow, p in pairs)
    refused = sum(router.route(_req(p), None).status == "refused" for p in REFUSED)
    vague = sum(router.route(_req(p), None).status == "needs_input" for p in AMBIGUOUS)
    return {"routing": hits / len(pairs), "routing_n": len(pairs),
            "refused": refused / len(REFUSED), "ambiguous": vague / len(AMBIGUOUS)}


def injection() -> dict:
    from injection_corpus import BENIGN, HOSTILE

    found = sum(any(x.kind == kind for x in guard.scan(text)) for _i, kind, text in HOSTILE)
    flagged = sum(bool([x for x in guard.scan(t) if x.kind != "secret"]) for t in BENIGN)
    return {"injection_found": found / len(HOSTILE), "injection_n": len(HOSTILE),
            "injection_false_positives": flagged / len(BENIGN), "benign_n": len(BENIGN)}


def _mutations():
    """(name, change(docs, est, qty, fields)) each a real cross-document slip."""
    def boq(docs):
        return next(d for d in docs["documents"] if d["kind"] == "boq")

    def scc(docs):
        return next(d for d in docs["documents"] if d["kind"] == "scc")

    return [
        ("boq quantity", lambda d, e, q: boq(d)["rows"][0].update(qty=99)),
        ("boq rate", lambda d, e, q: boq(d)["rows"][0].update(rate=1.0)),
        ("boq item", lambda d, e, q: boq(d)["rows"][0].update(item="another gasket")),
        ("boq unit", lambda d, e, q: boq(d)["rows"][0].update(uom="Sets")),
        ("boq total", lambda d, e, q: boq(d).update(total=1.0)),
        ("boq line missing", lambda d, e, q: boq(d)["rows"].clear()),
        ("quantity differs", lambda d, e, q: q["lines"][0].update(qty=5)),
        ("quantity unit differs", lambda d, e, q: q["lines"][0].update(uom="Kg")),
        ("estimate line not in quantity", lambda d, e, q: q["lines"].clear()),
        ("field contradicts clause", lambda d, e, q: d["fields"].update(
            delivery_period="12 weeks")),
        ("clauses contradict", lambda d, e, q: scc(d)["clauses"].append(
            {"text": "Delivery within 16 weeks.", "source": "officer", "class": "standard",
             "basis": "", "selected": True, "why": "x"})),
        ("other precedents", lambda d, e, q: d.update(precedents=["PUR-5"])),
        ("older estimate", lambda d, e, q: [x["metadata"].update(estimate_version=1)
                                            for x in d["documents"]]),
    ]


def _drafted():
    c = ctx(Path(tempfile.mkdtemp()))
    case_id = nam_walk.goods_drafted(c)
    rec = need.load(c.conn, case_id)
    return c, rec, orchestrator.workflow(rec)


def consistency() -> dict:
    _c, rec, wf = _drafted()
    base = (orchestrator._artifact(wf, "documents"), orchestrator._artifact(wf, "estimate"),
            orchestrator._artifact(wf, "quantity"))

    def fails(docs, est, qty):
        r = checker.Report()
        checker._consistency(r, rec, wf, docs, est, qty, {})
        return [x for x in r.items if x["severity"] == "fail"]

    assert fails(*base) == [], fails(*base)
    missed = []
    cases = _mutations()
    for name, change in cases:
        docs, est, qty = (copy.deepcopy(x) for x in base)
        change(docs, est, qty)
        if not fails(docs, est, qty):
            missed.append(name)
    return {"consistency": (len(cases) - len(missed)) / len(cases),
            "consistency_n": len(cases), "consistency_missed": missed}


def clauses() -> dict:
    _c, _rec, wf = _drafted()
    docs = orchestrator._artifact(wf, "documents")["documents"]
    record = case_record("PUR-4")
    wanted = [(kind, c["text"]) for kind, key in (("scc", "scc"), ("pqc", "pqc"),
                                                  ("specification", "tech_spec"))
              for c in record[key]]
    have = {(d["kind"], c["text"]) for d in docs for c in d.get("clauses") or []
            if c["selected"]}
    missing = [w for w in wanted if w not in have]
    conflicts = [f for f in review.code_findings(docs, {}) if f["kind"] == "conflict"]
    return {"clause_omission": len(missing) / len(wanted), "clauses_n": len(wanted),
            "clause_conflicts_in_clean_case": len(conflicts)}


def end_to_end() -> dict:
    from test_nam_estimate import INPUTS, LINES, MANPOWER

    nam_walk.VALIDATED.clear()
    nam_walk.VERIFIED.clear()
    out, times, uncited, facts = {}, {}, [], 0
    for category in ("goods", "services", "works"):
        started = time.monotonic()
        c = ctx(Path(tempfile.mkdtemp()))
        try:
            if category == "goods":
                case_id = nam_walk.goods_at_s5(c)
                docs = ()
                fields = nam_walk.FIELDS
            elif category == "services":
                case_id = nam_walk.services_at_s4(c, MANPOWER)
                nam_walk.confirm(c, case_id, nam_walk.call(c, case_id, "Wage estimate",
                                                           answer="rates", value=INPUTS))
                fields, docs = "all: contract_period=12 months, location=Plant 1", ("pqc", "scc")
            else:
                case_id = nam_walk.works_at_s4(c, LINES)
                nam_walk.confirm(c, case_id, nam_walk.call(
                    c, case_id, "Works estimate", answer="rates",
                    value="2: rate=245, schedule=nrl_sor, ref=2.1.4; 3: rate=7800, "
                          "schedule=dsr; 4: rate=6100, basis=similar, score=0.97, ref=4.1.2; "
                          "5: c=frame:9000|shutter:14000|fixing:1500"))
                fields, docs = ("all: completion_period=6 months, location=Township",
                                ("specification", "pqc", "scc"))
            nam_walk.to_nit(c, case_id, fields, docs)
            wf = orchestrator.workflow(need.load(c.conn, case_id))
            closed = wf.get("closed")
            arts = {n: orchestrator._artifact(wf, n) for n in
                    ("estimate", "documents", "compliance", "materials")}
            facts += len(knowledge.facts(arts))
            uncited += [f"{category}: {x}" for x in knowledge.uncited(arts)]
            out[category] = bool(closed)
        except AssertionError as exc:
            out[category] = False
            out[f"{category}_why"] = str(exc)[:300]
        times[category] = round(time.monotonic() - started, 2)
    passed = sum(out[k] for k in ("goods", "services", "works"))
    return {"end_to_end": passed / 3, "end_to_end_cases": out, "end_to_end_s": times,
            "schema": 1.0 if passed == 3 else 0.0, "schema_n": len(nam_walk.VALIDATED),
            "uncited": len(uncited), "facts_n": facts, "uncited_facts": uncited[:20],
            **_verified(passed == 3)}


def _verified(all_ran: bool) -> dict:
    replies = nam_walk.VERIFIED
    done = [r for r in replies if r[1] in ("ok", "needs_approval")]
    false = [f"{flow}: {status}" for flow, status, ok in done if not ok]
    return {"verified": (len(replies) / len(nam_walk.VALIDATED)
                         if all_ran and nam_walk.VALIDATED else 0.0),
            "verified_n": len(replies),
            "verification_failed_n": sum(1 for r in replies if not r[2]),
            "false_done": len(false), "false_done_steps": false[:20]}


LABELS = Path(__file__).with_name("retrieval_labels.json")


def recall(labels: list[dict], search, k: int = RETRIEVAL_K) -> dict:
    """Of the labelled queries, the share whose tender the search returns in
    its first k. `search(query) -> [case_key, ...]` in rank order."""
    missed = []
    for row in labels:
        got = search(row["query"])[:k]
        if row["case_key"] not in got:
            missed.append({"query": row["query"], "want": row["case_key"], "got": got})
    n = len(labels)
    return {"recall": (n - len(missed)) / n if n else None, "n": n, "missed": missed}


def retrieval(kg_url: str | None = None, fetch=None) -> dict:
    """Recall@k of the KG's tender search over retrieval_labels.json. Runs
    only against the KG named by NAM_EVAL_KG_URL: a deterministic suite
    makes no network call unless asked to."""
    import os

    from nam_agent.nam.contracts.base import Unavailable
    from nam_agent.nam.contracts.kg import KgBackend

    kg_url = kg_url if kg_url is not None else os.environ.get("NAM_EVAL_KG_URL", "")
    if not kg_url:
        return {"retrieval": None, "retrieval_note": "not measured: set NAM_EVAL_KG_URL "
                "to a live KG to measure recall@5 over the labelled set (Step 13)"}
    data = json.loads(LABELS.read_text(encoding="utf-8"))
    if not data["labels"]:
        return {"retrieval": None, "retrieval_note": "the labelled set is empty: build it "
                "with scripts/nam_retrieval_labels.py while the KG is reachable"}
    if fetch is None:
        from nam_agent.nam.need import _get as fetch
    backend = KgBackend(fetch, kg_url)
    try:
        out = recall(data["labels"], lambda q: [t["cite"]["case_key"] for t in
                                                backend.tenders(q, limit=RETRIEVAL_K)["tenders"]])
    except Unavailable as exc:
        return {"retrieval": None, "retrieval_note": f"not measured: {exc}"}
    return {"retrieval": out["recall"], "retrieval_n": out["n"],
            "retrieval_missed": out["missed"][:20],
            "retrieval_note": f"recall@{RETRIEVAL_K} over {out['n']} labelled queries "
                              f"(threshold {RETRIEVAL_RECALL}, a DRAFT)"}


def planned() -> dict:
    from test_nam_plan import drive

    c = ctx(Path(tempfile.mkdtemp()))
    body = nam_walk.call(c, None, "Complete the whole procurement step by step",
                         "i need to procure gaskets")
    try:
        body, calls = drive(c, body)
    except AssertionError as exc:
        return {"planned": 0.0, "planned_why": str(exc)[:300]}
    # NAM ends with the PR: the case is done once the PR is submitted
    done = "submit_pr" in orchestrator.workflow(need.load(c.conn, body["case_id"]))["actions"]
    return {"planned": 1.0 if body["result"]["complete"] and done else 0.0,
            "planned_calls": calls}


def flowless() -> dict:
    from flowless_corpus import CORPUS, check

    missed = [f"{e[0]} '{e[1]}': {why}" for e in CORPUS if (why := check(e))]
    return {"flowless": (len(CORPUS) - len(missed)) / len(CORPUS), "flowless_n": len(CORPUS),
            "flowless_missed": missed[:20]}


def all_metrics() -> dict:
    out = {}
    for part in (routing, injection, consistency, clauses, end_to_end, planned, flowless,
                 retrieval):
        out.update(part())
    return out


def failures(metrics: dict) -> list[str]:
    bad = []
    for key, want in THRESHOLDS.items():
        have = metrics.get(key)
        ok = have is not None and (have <= want if want == 0.0 else have >= want)
        if not ok:
            bad.append(f"{key}: {have} (threshold {want})")
    have = metrics.get("retrieval")
    if have is not None and have < RETRIEVAL_RECALL:     # measured only when asked
        bad.append(f"retrieval: {have} (threshold {RETRIEVAL_RECALL})")
    return bad
