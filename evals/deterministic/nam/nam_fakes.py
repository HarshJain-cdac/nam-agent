"""A scripted KG for the NAM harness evals: answers like /api/kg/* on :8077,
with no network. Six gasket tenders, so need capture has something to narrow."""

from __future__ import annotations

import sqlite3
import urllib.error
from pathlib import Path

import nam_agent.nam as harness

TENDERS = [
    ("PUR-1", "Supply of Spiral Wound Gaskets", ["spiral wound gasket 4in"]),
    ("PUR-2", "Supply of Spiral Wound Gaskets for CDU", ["spiral wound gasket 6in"]),
    ("PUR-3", "Spiral Wound Gaskets, SS316", ["spiral wound gasket 2in"]),
    ("PUR-4", "Supply of Ring Joint Gaskets", ["ring joint gasket R24"]),
    ("PUR-5", "Ring Joint Gaskets for HGU", ["ring joint gasket R31"]),
    ("PUR-6", "Supply of Rubber Gaskets", ["rubber gasket sheet"]),
]


# two tenders that share a base number across years (an ambiguous core match)
SHARED_CORE = {"PUR-4": "OC01000435/RJ", "PUR-5": "OC01000435/HGU"}


def tender_no(key: str) -> str:
    return SHARED_CORE.get(key, f"T-{key}")


def case_record(key: str) -> dict:
    """/api/kg/case for one of TENDERS: fields, BOQ with rates, clauses."""
    subject, items = next((s, i) for k, s, i in TENDERS if k == key)
    return {"case_key": key, "tender_no": tender_no(key), "job_name": subject,
            "estimated_value": 450000, "emd_amount": "9,000", "bid_type": "Two bid",
            "pr_no": "1000012345", "department": "Materials", "work_category": "Goods",
            "pqc": [{"text": "The bidder shall have supplied similar gaskets in the last 7 "
                             "years.", "summary": "experience"}],
            "scc": [{"text": "Delivery within 8 weeks of the purchase order.",
                     "summary": "delivery"},
                    {"text": "Rates shall be firm.", "summary": "price"}],
            "tech_spec": [{"text": "ASME B16.20, SS316 windings.", "summary": "standard"}],
            "boq": [{"item": item, "material_code": f"MC-{key}-{n}", "uom": "Nos", "qty": 20,
                     "unit_price": "450.00", "price_level": "awarded"}
                    for n, item in enumerate(items, 1)],
            "sor_references": [], "awarded_to": ["Acme Gaskets"],
            "counts": {"pqc": 1, "scc": 2, "tech_spec": 1, "boq": len(items)}}


def _resolve(ref: str) -> dict:
    wanted = ref.strip().upper()
    for tier, key_of in (("case_key", lambda k: k), ("tender_no", tender_no)):
        hits = [k for k, _, _ in TENDERS if key_of(k).upper() == wanted]
        if hits:
            break
    else:
        tier = "core"
        hits = [k for k in SHARED_CORE if SHARED_CORE[k].split("/")[0] == wanted]
    matches = [{"case_key": k, "tender_no": tender_no(k), "job_name": s, "match": tier}
               for k, s, _ in TENDERS if k in hits]
    return {"ref": ref, "tier": tier if matches else None, "ambiguous": len(matches) > 1,
            "matches": matches}


def _stats(value: str, n: int = 3) -> dict:
    return {"n": n, "min": value, "median": value, "max": value}


def prices(requirement: str) -> dict:
    """/api/kg/prices: gaskets have an NRL estimate (450) and an awarded rate
    (400, 2022); civil items an estimate only; anything else nothing."""
    text = requirement.lower()
    if "gasket" in text:
        rows = [{"item": "ring joint gasket", "uom": "Nos", "price_level": "estimate",
                 "unit_price": _stats("450.00"), "estimated_rate": None, "totals": None,
                 "observations": 3, "matched": True, "years": [2021, 2023],
                 "spans_years": True, "sources": ["PUR-4", "PUR-5"],
                 "typical_qty": _stats("20")},
                {"item": "ring joint gasket", "uom": "Nos", "price_level": "award",
                 "unit_price": _stats("400.00", 2), "estimated_rate": None, "totals": None,
                 "observations": 2, "matched": True, "years": [2022], "spans_years": False,
                 "sources": ["PUR-4"], "typical_qty": _stats("20")}]
    elif "plaster" in text or "excavation" in text:
        rows = [{"item": "cement plaster 12mm", "uom": "Sqm", "price_level": "estimate",
                 "unit_price": _stats("310.50"), "estimated_rate": None, "totals": None,
                 "observations": 3, "matched": True, "years": [2023], "spans_years": False,
                 "sources": ["PUR-W1"], "typical_qty": _stats("120")}]
    else:
        return {"requirement": requirement, "precedent_found": False, "rows": []}
    return {"requirement": requirement, "precedent_found": True, "rows": rows}


def clauses(requirement: str, kind: str) -> dict:
    """/api/kg/clauses: two verbatim clauses per kind from PUR-4 and PUR-6."""
    texts = {"pqc": [("The bidder shall have executed one similar order of 80% value in the "
                     "last 7 years."), ("The bidder shall have an average annual turnover of "
                     "30% of the estimated value.")],
             "scc": ["Delivery within 8 weeks of the purchase order.",
                     "Liquidated damages at 0.5% per week, up to 10%."],
             "tech_spec": ["Gaskets to ASME B16.20.", "Material SS316."]}[kind]
    return {"requirement": requirement, "kind": kind, "count": 2, "clauses": [
        {"kind": kind, "text": t, "source": {"pur_case_id": key, "tender_no": tender_no(key),
                                             "job_name": "gaskets"}}
        for t, key in zip(texts, ("PUR-4", "PUR-6"), strict=True)]}


BOILERPLATE = {
    "mse": [(("Purchase preference shall be given to Micro and Small Enterprises (MSEs) as "
              "per the Public Procurement Policy for MSEs Order, 2012."), "PUR-4"),
            ("Delivery within 8 weeks of the purchase order.", "PUR-4"),
            (("MSEs are exempted from payment of the EMD on submission of a valid "
              "registration."), "PUR-6")],
    "ppl_c": [(("Purchase preference to Class-I local suppliers as per the Public "
                "Procurement (Preference to Make in India) Order 2017."), "PUR-6")],
    "ptr_relaxation": [],
}


def boilerplate(section: str) -> dict:
    rows = BOILERPLATE.get(section, [])
    return {"section": section, "count": len(rows), "retrieval_error": None, "clauses": [
        {"text": t, "source": {"pur_case_id": key, "tender_no": tender_no(key),
                               "job_name": "gaskets"}} for t, key in rows]}


def fake_kg(*, down: bool = False, material: bool = True):
    calls = []

    def fetch(kg_url, path, params):
        calls.append((path, dict(params)))
        if down:
            raise OSError("connection refused")
        if path == "/api/kg/tenders":
            if "gasket" not in params["query"]:
                return {"count": 0, "terms": [params["query"]], "tenders": []}
            return {"count": len(TENDERS), "terms": ["gasket", "gaskets"], "tenders": [
                {"case_key": k, "tender_no": tender_no(k), "job_name": s, "matched_by_name": True,
                 "matching_item_count": len(items), "matching_items": items}
                for k, s, items in TENDERS]}
        if path == "/api/kg/precedent":
            return {"best_similarity": 0.7, "tenders": []}
        if path == "/api/kg/items":
            # the item check finds no line that is the whole item: the tender
            # search's own rows stand (capture.item_tenders)
            return {"answer": "no", "counts": {"exact": 0, "similar": 0}, "exact": [],
                    "similar": [], "tenders": {}}
        if path == "/api/kg/resolve":
            return _resolve(params["ref"])
        if path == "/api/kg/case":
            if params["case_key"] not in {k for k, _, _ in TENDERS}:
                raise urllib.error.HTTPError(kg_url + path, 404, "not found", {}, None)
            return case_record(params["case_key"])
        if path == "/api/kg/documents":
            return {"case_key": params["case_key"], "count": 1, "documents": [
                {"document_type": "NIT", "filename": "nit.pdf", "byte_size": 1200,
                 "extension": "pdf", "readable": True, "path": "/srv/nit.pdf"}],
                "best_nit": {"filename": "nit.pdf"}}
        if path == "/api/kg/prices":
            return prices(params["requirement"])
        if path == "/api/kg/clauses":
            return clauses(params["requirement"], params["kind"])
        if path == "/api/kg/boilerplate":
            return boilerplate(params["section"])
        if path == "/health":
            return {"tenders": 203}
        if path == "/api/kg/case_summary":
            counts = ({"boq_lines": 4, "pqc_clauses": 3, "scc_clauses": 2, "spec_clauses": 1}
                      if material else {})
            return {"cases": [{"case_key": k, "tender_no": tender_no(k), **counts}
                              for k in params["case_keys"].split(",")]}
        raise AssertionError(f"unexpected KG path {path}")

    fetch.calls = calls
    return fetch


def ctx(tmp_path: Path, fetch=None, conn=None) -> harness.Context:
    return harness.Context(conn=conn or sqlite3.connect(":memory:"), home=tmp_path,
                           kg_url="http://kg", fetch=fetch or fake_kg())


def ask(text: str, **extra) -> dict:
    return {"system": "Assess need for request #123",
            "messages": [{"role": "user", "content": text}], **extra}


OFFICER = harness.Caller(tenant="nrl", user="u1", roles=("U1",))
IDENTITY = {"X-NAM-Tenant": "nrl", "X-NAM-User": "u1", "X-NAM-Roles": "U1"}


def run(request, context, caller=OFFICER) -> dict:
    """harness.run() as the NRL officer, unless a test says otherwise."""
    return harness.run(request, context, caller)


# the classification the officer confirms in the tests that move a case on
CLASSIFIED = {"category": "goods", "procurement_type": "OPEX", "tender_type": "Open"}


def classified(context, case_id: str, caller=OFFICER, values=CLASSIFIED) -> dict:
    """need.classify, answering each question it asks from `values`, until
    it asks none of them. Returns the last response."""
    def call(**extra):
        return harness.run(ask("", system="Confirm the classification", case_id=case_id,
                               **extra), context, caller)

    body = call()
    while body.get("questions") and body["questions"][0]["id"] in values \
            and body["status"] == "needs_input":
        q = body["questions"][0]["id"]
        body = call(answer={"question_id": q, "value": values[q]})
    return body


class FakeOcr:
    """A scripted OCR engine: 'scanned text on page N' for every page asked,
    with one region each. `calls` records (mime, pages); `down` makes it
    raise OcrUnavailable. Loadable as NAM_OCR_BACKEND=nam_fakes:FakeOcr."""

    name = "fake-ocr 1.0"

    def __init__(self, down: bool = False, box=(0.1, 0.1, 0.9, 0.2)):
        self.calls, self.down, self.box = [], down, box

    def recognise(self, data, mime, pages):
        from nam_agent.nam.ocr import OcrPage, OcrResult, OcrUnavailable, Region

        self.calls.append((mime, list(pages)))
        if self.down:
            raise OcrUnavailable("engine offline")
        return OcrResult(engine=self.name, pages=tuple(
            OcrPage(n, f"scanned text on page {n}: 20 gate valves, class 150, flanged",
                    (Region(f"20 gate valves p{n}", self.box, 0.93),), 0.91) for n in pages))
