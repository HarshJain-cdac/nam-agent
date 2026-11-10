"""'Have we bought X before' (nam_agent/nam/kg_tools.py, KG /api/kg/items).

The case behind it: asked about a bimetallic temperature gauge with a 320 mm
stem, the agent called kg_precedent_search, got WEAK PRECEDENT and said no,
while the graph held eight exact rows written "TG Bimetallic U320mm ..." and
"TG BiMet ... U320T50". Replies below are trimmed real /api/kg/items output.
"""
import urllib.error

from nam_agent.nam import kg_mcp, kg_tools

URL = "http://kg.test"
ASK = "bimetallic temperature gauge with a 320 mm stem"

ITEMS_YES = {
    "query": ASK,
    "concepts": [{"concept": "temperature gauge", "forms": ["temperature gauge", "tg"]},
                 {"concept": "bimetallic", "forms": ["bimet", "bimetallic"]}],
    "sizes": [{"value": "320", "unit": "mm"}],
    "answer": "yes", "counts": {"exact": 2, "similar": 1}, "items_searched": 12480,
    "exact": [
        {"case_key": "PUR-INSTMNT-1003188", "item": "TG Bimetallic U320mm 6mm dia 0-80Cel",
         "material_code": "I-TG-ARC-G080-04",
         "rates": [{"case_key": "PUR-INSTMNT-1003188", "source": "BOQ", "price_level": "bid",
                    "qty": "1.0", "uom": "each", "unit_price": "2010.0"},
                   {"case_key": "PUR-INSTMNT-1003188", "source": "ComparativeStatement",
                    "observed_on": "01-Aug-2022", "price_level": "bid", "qty": "1.00",
                    "uom": "each", "total_price": "1060.00", "bid_rank": "L1"}]},
        {"case_key": "PUR-INSTMNT-1002364", "item": "TG BiMet 0-300 DegC, D150,OD6mm, U320T50",
         "material_code": "I-TG-G300-320-02",
         "rates": [{"case_key": "PUR-INSTMNT-1002364", "source": "ComparativeStatement",
                    "observed_on": "13-May-2022", "price_level": "bid", "qty": "50.00",
                    "uom": "each", "total_price": "63000.00", "bid_rank": "L1"}]},
    ],
    "similar": [{"case_key": "PUR-INSTMNT-1002364",
                 "item": "TG BiMet 0-150 DegC, D150, OD6mm, U240T50",
                 "material_code": "I-TG-G150-240-01", "rates": []}],
    "tenders": {"PUR-INSTMNT-1003188": {
        "tender_no": "OC03000186/BHA", "job_name": "Rate contract for Supply of PG, DPG, TG and AFR",
        "department": "Maintenance Instrumentation", "bid_opening_date": "11.06.2022",
        "bidders": ["Micro Process Controls", "Precision Mass Products Pvt. Ltd."],
        "awarded_to": []}},
}

WEAK = {"requirement": ASK, "precedent_found": False,
        "precedent_gap": "Nothing in the corpus is semantically close to this requirement.",
        "tenders": [{"pur_case_id": "PUR-MECHMNT-1003226",
                     "job_name": "Procurement of Screws, circlip", "matched_items": ["Circlip"]}]}


def _stub(monkeypatch, replies: dict):
    calls = []

    def fake(base_url, path, params):
        calls.append(path)
        reply = replies.get(path)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(kg_tools, "_get", fake)
    return calls


def _tool(name):
    return next(t for t in kg_tools.make_tools(URL) if t.name == name)


def test_item_history_is_registered_first():
    assert kg_tools.make_tools(URL)[0].name == "kg_item_history"


def test_yes_lists_the_exact_rows_as_written_with_l1_first(monkeypatch):
    _stub(monkeypatch, {"/api/kg/items": ITEMS_YES})
    out = _tool("kg_item_history").fn(item=ASK)
    assert out.startswith("ITEM CHECK — YES: 2 line item(s)")
    assert "TG Bimetallic U320mm 6mm dia 0-80Cel" in out
    assert "TG BiMet 0-300 DegC, D150,OD6mm, U320T50" in out
    assert "I-TG-G300-320-02" in out
    row = next(line for line in out.splitlines() if "0-80Cel" in line)
    # the L1 figure leads, with the rate it implies; the BOQ bid follows
    assert row.index("L1") < row.index("2,010.00")
    assert "Rs 63,000.00 for 50 each (= Rs 1,260.00/each) L1" in out
    assert "12,480 line items" in out and "tg" in out
    assert "award not on file" in out and "Micro Process Controls" in out


def test_no_is_a_real_negative_not_an_outage(monkeypatch):
    _stub(monkeypatch, {"/api/kg/items": {**ITEMS_YES, "answer": "no", "exact": [],
                                          "similar": [], "tenders": {},
                                          "counts": {"exact": 0, "similar": 0}}})
    out = _tool("kg_item_history").fn(item="titanium gauge")
    assert out.startswith("ITEM CHECK — NO") and "not an outage" in out
    assert "UNAVAILABLE" not in out


def test_other_size_says_so(monkeypatch):
    _stub(monkeypatch, {"/api/kg/items": {**ITEMS_YES, "answer": "similar", "exact": [],
                                          "counts": {"exact": 0, "similar": 1}}})
    out = _tool("kg_item_history").fn(item=ASK)
    assert out.startswith("ITEM CHECK — NOT AT THIS SIZE") and "U240T50" in out


def test_outage_is_unavailable_never_no(monkeypatch):
    _stub(monkeypatch, {"/api/kg/items": urllib.error.URLError("refused")})
    out = _tool("kg_item_history").fn(item=ASK)
    assert out.startswith("KG PRECEDENT UNAVAILABLE") and "NO:" not in out


def test_weak_precedent_carries_the_item_check(monkeypatch):
    calls = _stub(monkeypatch, {"/api/kg/precedent": WEAK, "/api/kg/items": ITEMS_YES})
    out = _tool("kg_precedent_search").fn(requirement=ASK)
    assert calls == ["/api/kg/precedent", "/api/kg/items"]
    assert out.startswith("WEAK PRECEDENT") and "ITEM CHECK — YES" in out


def test_strong_precedent_skips_the_item_check(monkeypatch):
    strong = {**WEAK, "precedent_found": True, "precedent_gap": None, "best_similarity": 0.61}
    calls = _stub(monkeypatch, {"/api/kg/precedent": strong})
    out = _tool("kg_precedent_search").fn(requirement=ASK)
    assert calls == ["/api/kg/precedent"] and "ITEM CHECK" not in out


def test_old_kg_without_items_keeps_the_weak_answer(monkeypatch):
    _stub(monkeypatch, {"/api/kg/precedent": WEAK,
                        "/api/kg/items": urllib.error.HTTPError(URL, 404, "nf", {}, None)})
    out = _tool("kg_precedent_search").fn(requirement=ASK)
    assert out.startswith("WEAK PRECEDENT") and "ITEM CHECK" not in out


def test_find_tenders_names_the_exact_items(monkeypatch):
    _stub(monkeypatch, {"/api/kg/tenders": {
        "query": ASK, "terms": ["bimetallic", "gauge"], "count": 2,
        "tenders": [{"case_key": "PUR-INSTMNT-1003188", "job_name": "Rate contract for PG, DPG, TG",
                     "matching_item_count": 28, "item_match": "exact",
                     "exact_items": ["TG Bimetallic U320mm 6mm dia 0-80Cel"]},
                    {"case_key": "PUR-ESTATE-1000048", "job_name": "Doppler Radar",
                     "matching_item_count": 10, "item_match": None, "exact_items": []}]}})
    out = _tool("kg_find_tenders").fn(query=ASK)
    assert "Exact item on file" in out
    assert "- PUR-INSTMNT-1003188: TG Bimetallic U320mm 6mm dia 0-80Cel" in out
    assert "PUR-ESTATE-1000048:" not in out


# Two tenders, as /api/kg/items returns them since rows are kept per tender.
# The first reply that failed showed 12 rows of PUR-INSTMNT-1002364 only; the
# model reported one PUR and then said PUR-INSTMNT-1003188 "only provides an
# aggregated range", a reason no tool had given.
def _row(case, item, l1):
    return {"case_key": case, "item": item, "material_code": None, "tier": "exact",
            "rates": [{"case_key": case, "source": "ComparativeStatement", "price_level": "bid",
                       "qty": "1.00", "uom": "each", "total_price": l1, "bid_rank": "L1"}]}


def _side(case, contract, exact, low):
    return {"case_key": case, "tender_no": "OC1", "job_name": f"{contract} subject",
            "department": "Maintenance Instrumentation", "bid_opening_date": "2022",
            "contract_type": contract, "lines": {"exact": exact, "similar": 0},
            "specs": {"stem": ["320 mm"], "range": ["0-80 °C", "0-600 °C"]},
            "quantity": {"per_line_min": 10.0, "per_line_max": 50.0, "total": 355.0, "uom": "each"},
            "lowest_bid_per_unit": {"n": exact, "min": low, "median": low, "max": low},
            "lowest_bid_per_unit_exact": {"n": exact, "min": low, "median": low, "max": low},
            "highest_over_lowest_pct": {"n": exact, "min": 2.6, "median": 87.3, "max": 94.3},
            "bidders": ["A Ltd", "B Ltd"], "awarded_to": []}


TWO = {**ITEMS_YES, "query": "bimet", "sizes": [], "counts": {"exact": 36, "similar": 0},
       "exact": [_row("PUR-INSTMNT-1002364", f"TG BiMet U{n}T50", "1240.00") for n in (240, 320)]
       + [_row("PUR-INSTMNT-1003188", f"TG Bimetallic U{n}mm", "1060.00") for n in (100, 320)],
       "similar": [],
       "comparison": [_side("PUR-INSTMNT-1002364", "one-time purchase", 12, 1240.0),
                      _side("PUR-INSTMNT-1003188", "rate contract", 24, 1060.0)]}


def test_every_tender_keeps_its_rows_and_says_what_is_not_shown(monkeypatch):
    _stub(monkeypatch, {"/api/kg/items": TWO})
    out = _tool("kg_item_history").fn(item="bimet")
    assert "36 line item(s) in 2 tender(s)" in out
    assert "PUR-INSTMNT-1002364 items:" in out and "PUR-INSTMNT-1003188 items:" in out
    assert "TG Bimetallic U100mm" in out
    assert "(+22 more row(s) of PUR-INSTMNT-1003188" in out
    assert "tender=PUR-INSTMNT-1003188" in out
    assert "never say a tender lacks item rates" in out


def test_two_tenders_come_with_a_comparison_and_how_to_answer_it(monkeypatch):
    _stub(monkeypatch, {"/api/kg/items": TWO})
    out = _tool("kg_item_history").fn(item="bimet")
    assert "|  | PUR-INSTMNT-1002364 | PUR-INSTMNT-1003188 |" in out
    for label in ("Contract type (from the subject)", "Spec: stem", "Spec: range",
                  "Quantity (planned or bid)", "Lowest bid per unit (min / median / max)",
                  "Highest bid above the lowest", "Bidders", "Award"):
        assert f"| {label} |" in out, label
    assert "| one-time purchase | rate contract |" in out
    assert "Rs 1,240 / Rs 1,240 / Rs 1,240 (12 lines)" in out
    assert "TO ANSWER A COMPARISON" in out and "'Pick:'" in out


def test_a_size_adds_the_exact_size_price_row(monkeypatch):
    _stub(monkeypatch, {"/api/kg/items": {**TWO, "sizes": [{"value": "320", "unit": "mm"}]}})
    out = _tool("kg_item_history").fn(item="bimetallic gauge 320 mm")
    assert "| Lowest bid per unit, 320 mm lines only |" in out


def test_one_tender_can_be_read_in_full(monkeypatch):
    seen = []

    def fake(base_url, path, params):
        seen.append(params)
        return TWO

    monkeypatch.setattr(kg_tools, "_get", fake)
    _tool("kg_item_history").fn(item="bimet", tender=" PUR-INSTMNT-1003188 ")
    assert seen[0]["tender"] == "PUR-INSTMNT-1003188" and seen[0]["per_tender"] == 100


def test_other_size_forbids_calling_any_size_the_one_asked_or_closest(monkeypatch):
    # a "30 mm x 6 mm" gasket came back NOT AT THIS SIZE, and the reply still
    # called a 500 NB row "the closest precedent, the 30 mm x 6 mm gasket"
    _stub(monkeypatch, {"/api/kg/items": {**ITEMS_YES, "answer": "similar", "exact": [],
                                          "counts": {"exact": 0, "similar": 1}}})
    out = _tool("kg_item_history").fn(item=ASK)
    assert "No row below is 320 mm" in out and "'closest'" in out


# -- the record of one tender: PR no., award, files ------------------------------------

CASE = {"case_key": "PUR-MECHMNT-1008357", "tender_no": "OC01000435 / PRV",
        "job_name": "Annual rate contract for Gaskets", "department": "Maintenance Mechanical",
        "pr_no": "10081673", "estimated_value": "INR 3731590.38",
        "bid_opening_date": "2024-10-15", "awarded_to": []}
DOCS = {"documents": [
    {"document_type": "NIT", "filename": "ARC_NIT_CPP.pdf", "path": "/kg/secret/ARC_NIT_CPP.pdf"},
    {"document_type": "PO", "filename": "PQC.pdf", "path": "/kg/a/PQC.pdf"},
    {"document_type": "PO", "filename": "PQC.pdf", "path": "/kg/b/PQC.pdf"},
    {"document_type": "AwardNote", "filename": "award.pdf", "path": "/kg/award.pdf"}]}


def test_the_tender_record_gives_the_pr_number_and_files_without_paths(monkeypatch):
    # "get me the PR and PO from the PUR" was answered "I don't have a tool"
    calls = _stub(monkeypatch, {"/api/kg/case": CASE, "/api/kg/documents": DOCS})
    out = _tool("kg_tender_record").fn(tender="PUR-MECHMNT-1008357")
    assert out.startswith("TENDER RECORD — PUR-MECHMNT-1008357")
    assert "| PR no. | 10081673 |" in out
    assert "| PO no. (SAP) | not recorded in the knowledge graph |" in out
    assert "| Awarded to | not on file |" in out
    rows = [ln for ln in out.splitlines() if ln.startswith("| ") and ".pdf" in ln]
    assert rows[0].startswith("| PO | PQC.pdf |") and rows[1].startswith("| AwardNote")
    assert "/kg/" not in out                       # paths on the KG host are not passed on
    assert calls == ["/api/kg/case", "/api/kg/documents"]


def test_the_tender_record_takes_a_tender_number(monkeypatch):
    calls = _stub(monkeypatch, {"/api/kg/resolve": {"matches": [
        {"case_key": "PUR-MECHMNT-1008357"}]}, "/api/kg/case": CASE, "/api/kg/documents": DOCS})
    assert _tool("kg_tender_record").fn(tender="OC01000435").startswith("TENDER RECORD")
    assert calls[0] == "/api/kg/resolve"
    _stub(monkeypatch, {"/api/kg/resolve": {"matches": [{"case_key": "PUR-A-1"},
                                                        {"case_key": "PUR-B-2"}]}})
    assert "names 2 tenders" in _tool("kg_tender_record").fn(tender="OC0100")
    _stub(monkeypatch, {"/api/kg/resolve": {"matches": []}})
    assert "No past NRL tender" in _tool("kg_tender_record").fn(tender="XYZ")


def test_the_tender_record_outage_is_unavailable(monkeypatch):
    _stub(monkeypatch, {"/api/kg/case": urllib.error.URLError("refused")})
    assert "UNAVAILABLE" in _tool("kg_tender_record").fn(tender="PUR-MECHMNT-1008357")


def test_a_dropped_kg_call_is_tried_once_more(monkeypatch):
    # "capillary" came back UNAVAILABLE once; the same call answered in a second
    tries = []
    body = (b'{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": '
            b'"{\\"data\\": {\\"nodes\\": []}}"}]}}')

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    def flaky(req, timeout):
        tries.append(req.full_url)
        if len(tries) == 1:
            raise urllib.error.URLError("connection reset")
        return Reply()

    monkeypatch.setattr(kg_tools.urllib.request, "urlopen", flaky)
    kg_mcp._schemas.clear()
    assert kg_tools._get(URL.rstrip("/") + "/mcp", "/api/kg/resolve", {"ref": "capillary"})["matches"] == []
    assert len(tries) >= 2                      # the dropped call went again, and answered

    def missing(req, timeout):
        tries.append(1)
        raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)

    tries.clear()
    monkeypatch.setattr(kg_tools.urllib.request, "urlopen", missing)
    try:
        kg_tools._get(URL.rstrip("/") + "/mcp", "/api/kg/case", {"case_key": "PUR-X-1"})
    except urllib.error.HTTPError:
        pass
    assert len(tries) == 1                      # a 404 is the answer, not retried


def test_the_kg_tools_read_the_kg_api_and_sap_stays_on_mcp(monkeypatch):
    """The tender knowledge graph is its own source again (NAM_KG_API_URL):
    the kg_* tools read its REST API; sap_search stays on the MCP server."""
    seen = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"matches": []}'

    def urlopen(req, timeout):
        seen.append(req if isinstance(req, str) else req.full_url)
        return Reply()
    monkeypatch.setattr(kg_tools.urllib.request, "urlopen", urlopen)
    assert kg_tools._get("http://127.0.0.1:8077", "/api/kg/resolve",
                         {"ref": "PUR‑X‑1"}) == {"matches": []}
    assert seen == ["http://127.0.0.1:8077/api/kg/resolve?ref=PUR-X-1"]
    tools = kg_tools.make_all_tools("http://h:8091/mcp", "http://127.0.0.1:8077")
    sap = next(t for t in tools if t.name == "sap_search")
    sent = []
    monkeypatch.setattr(kg_mcp, "call", lambda base, tool, **a: sent.append((base, tool)) or {})
    sap.fn(query="thermowell")
    assert sent == [("http://h:8091/mcp", "sap_search")]
    assert kg_tools.kg_api("mcp", "http://h:8091/mcp") == "http://h:8091/mcp"
    assert kg_tools.kg_api("http://127.0.0.1:8077", "x") == "http://127.0.0.1:8077"
