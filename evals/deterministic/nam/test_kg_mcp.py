"""DETERMINISTIC EVAL — the KG over MCP answers the old reads (nam_agent/nam/kg_mcp.py).

The KG moved to an MCP server (search_tenders, search_items, case_details,
...). The first adapter passed its rows on as they came: kg_find_tenders
failed on 'matching_item_count', need capture asked search_tenders for
top_k=100 and the server (max 50) refused it, so every procurement found
"Knowledge graph unreachable". Replies below are trimmed real envelopes from
the server, served by a fake JSON-RPC transport.
"""

from __future__ import annotations

import pytest

from nam_agent.nam import kg_mcp, kg_tools

URL = "http://kg.test/mcp"

TOOLS = [{"name": n, "inputSchema": {"properties": {
    "query": {"type": "string"}, "top_k": {"type": "integer", "minimum": 1, "maximum": 50}}}}
    for n in ("search_tenders", "search_items", "similar_items")] + [
    {"name": "case_details", "inputSchema": {"properties": {"case_key": {"type": "string"}}}}]

TENDERS = [
    {"case_key": "PUR-INSTMNT-1002364", "tender_no": "OC03000183/SNJ",
     "job_name": "Supply of Temperature Gauge, Thermocouple and Thermowell spares",
     "department": "Maintenance Instrumentation"},
    {"case_key": "PUR-INSTMNT-1000150", "tender_no": "OC03000171/SKD",
     "job_name": "Annual Rate Contract (ARC) for Supply of Transmitters to NRL",
     "department": "Instrumentation Maintenance"}]
ITEMS = [
    {"item_description": "Gauge Pressure Transmitters (PT), and Universal Input Temperature "
                         "Transmitters (TT)", "pur_case_id": "PUR-INSTMNT-1000150",
     "tender_no": "OC03000171/SKD", "job_name": TENDERS[1]["job_name"], "uom": None,
     "estimated_rate": None, "unit_price": None, "stated_by": "PO"},
    {"item_description": "TG BiMet temperature gauge 0-200 DegC, U320", "pur_case_id":
     "PUR-INSTMNT-1003188", "tender_no": "OC03000186", "job_name": "ARC for gauges",
     "material_code": "I-TG-ARC-01", "uom": "each", "estimated_rate": "4330.00",
     "unit_price": "", "stated_by": "BOQ", "order_qty": "1.0"},
    {"item_description": "Temperature Gauges 0-400 DegC", "pur_case_id": "PUR-INSTMNT-1002364",
     "tender_no": "OC03000183/SNJ", "job_name": TENDERS[0]["job_name"], "uom": "each",
     "estimated_rate": "0.0", "unit_price": "1240", "stated_by": "BOQ"}]
CASE = {"tender": {"tender_no": "OC01000435 / PRV", "pr_no": "10081673",
                   "job_name": "Annual rate contract for Gaskets",
                   "estimated_value": "INR 3731590.38", "bid_opening_date": "2024-10-15"},
        "related": {
            "IN_DEPARTMENT": [{"name": "Maintenance Mechanical"}],
            "PROCURES_ITEM": [{"name": "Gasket for 14-VV-04",
                               "properties": {"material_code": "M-VESL-146-35"}}],
            "HAS_PQC": [{"name": "ISO 9001 certified"}], "HAS_SCC": [{"name": "ARC terms"}],
            "BID": [{"name": "ASIAN SEALING PRODUCTS PVT LTD"}],
            "HAS_CONTRACT": [{"name": "Contract::6000007994",
                              "properties": {"work_order_number": "6000007994"}}],
            "SUPPORTED_BY": [{"properties": {"document_type": "AdminApprovalNote",
                                             "original_filename": "PUR-MECHMNT-1008357-8.docx",
                                             "original_path": "/data2/secret/x.docx",
                                             "terminal_state": "ACCEPTED"}}]}}


@pytest.fixture
def server(monkeypatch):
    calls = []
    kg_mcp._schemas.clear()

    def rpc(base_url, method, params, timeout):
        calls.append((method, params.get("name"), dict(params.get("arguments") or {})))
        if method == "tools/list":
            return {"tools": TOOLS}
        args = params["arguments"]
        top = (TOOLS[0]["inputSchema"]["properties"]["top_k"]["maximum"])
        assert args.get("top_k", 1) <= top, "the server refuses top_k over its maximum"
        data = {"search_tenders": TENDERS, "search_items": ITEMS, "similar_items": [],
                "case_details": CASE if args.get("case_key") == "PUR-MECHMNT-1008357" else {}
                }[params["name"]]
        return {"structuredContent": {"status": "ok", "data": data}}

    monkeypatch.setattr(kg_mcp, "_rpc", rpc)
    return calls


def test_a_limit_over_the_servers_maximum_is_brought_within_it(server):
    kg_mcp.legacy_get(URL, "/api/kg/tenders", {"query": "temperature gauge", "limit": 100})
    sent = [a for m, n, a in server if n == "search_tenders"]
    assert sent[0]["top_k"] == 50                  # read from the server's own schema


def test_tender_rows_carry_their_lines_and_whether_they_are_the_item(server):
    out = kg_mcp.legacy_get(URL, "/api/kg/tenders", {"query": "temperature gauge", "limit": 100})
    rows = {r["case_key"]: r for r in out["tenders"]}
    assert set(rows) == {"PUR-INSTMNT-1002364", "PUR-INSTMNT-1003188", "PUR-INSTMNT-1000150"}
    assert rows["PUR-INSTMNT-1002364"]["matched_by_name"] is True
    assert rows["PUR-INSTMNT-1003188"]["exact_items"] == ["TG BiMet temperature gauge 0-200 "
                                                         "DegC, U320"]
    # "Gauge Pressure Transmitters ... Temperature Transmitters": both words, not the item
    assert rows["PUR-INSTMNT-1000150"]["exact_items"] == []
    assert all("matching_item_count" in r for r in out["tenders"])
    assert out["item_match"]["exact"] == 2


def test_the_item_check_says_yes_at_the_size_and_similar_at_another(server):
    yes = kg_mcp.legacy_get(URL, "/api/kg/items", {"query": "temperature gauge 0-200"})
    assert yes["answer"] == "yes" and [r["case_key"] for r in yes["exact"]] == [
        "PUR-INSTMNT-1003188"]
    assert yes["exact"][0]["rates"][0]["price_level"] == "estimate"
    other = kg_mcp.legacy_get(URL, "/api/kg/items", {"query": "temperature gauge 0-900"})
    assert other["answer"] == "similar" and not other["exact"]
    none = kg_mcp.legacy_get(URL, "/api/kg/items", {"query": "titanium flange"})
    assert none["answer"] == "no"


def test_a_tender_record_gives_pr_po_bidders_and_file_names_never_paths(server):
    case = kg_mcp.legacy_get(URL, "/api/kg/case", {"case_key": "PUR-MECHMNT-1008357"})
    assert case["pr_no"] == "10081673" and case["purchase_orders"] == []
    assert case["work_orders"] == ["6000007994"]           # a document's number, not SAP's
    assert case["bidders"] == ["ASIAN SEALING PRODUCTS PVT LTD"] and case["boq_lines"] == 1
    assert case["department"] == "Maintenance Mechanical"
    docs = kg_mcp.legacy_get(URL, "/api/kg/documents", {"case_key": "PUR-MECHMNT-1008357"})
    assert docs["documents"][0]["filename"] == "PUR-MECHMNT-1008357-8.docx"
    assert "/data2/secret" not in str(docs)
    out = next(t for t in kg_tools.make_tools(URL) if t.name == "kg_tender_record").fn(
        tender="PUR-MECHMNT-1008357")
    assert "| PR no. | 10081673 |" in out
    assert "| PO no. (SAP) | not recorded in the knowledge graph |" in out
    assert "| Contract / work order no. (from its documents) | 6000007994 |" in out
    assert kg_mcp.legacy_get(URL, "/api/kg/case", {"case_key": "PUR-X-1"}).get("error")


def test_kg_find_tenders_runs_on_the_mcp_rows(server):
    # live: "Error running kg_find_tenders: 'matching_item_count'"
    out = next(t for t in kg_tools.make_tools(URL) if t.name == "kg_find_tenders").fn(
        query="temperature gauge")
    assert "PUR-INSTMNT-1002364" in out and "Error" not in out


def test_a_tender_number_resolves_to_its_tender(server):
    found = kg_mcp.legacy_get(URL, "/api/kg/resolve", {"ref": "OC03000183/SNJ"})
    assert [m["case_key"] for m in found["matches"]] == ["PUR-INSTMNT-1002364"]
    assert kg_mcp.legacy_get(URL, "/api/kg/resolve", {"ref": "OC03000"})["ambiguous"] is True
    assert kg_mcp._stem("gauges") == "gauge" and kg_mcp._stem("glass") == "glass"


def test_a_failed_item_search_leaves_the_tenders_by_subject(server, monkeypatch):
    # live: search_items failed on the server ("graph: IOErrorException") and
    # kg_find_tenders reported the whole KG offline
    real = kg_mcp._search_items

    def broken(*a, **k):
        raise RuntimeError("Error executing tool search_items: unavailable: graph")

    monkeypatch.setattr(kg_mcp, "_search_items", broken)
    out = kg_mcp.legacy_get(URL, "/api/kg/tenders", {"query": "temperature gauge"})
    assert [r["case_key"] for r in out["tenders"]] == ["PUR-INSTMNT-1002364"]
    assert real is not broken


def test_an_empty_lookup_is_the_agents_to_fix_never_an_outage(monkeypatch):
    sent = []
    monkeypatch.setattr(kg_mcp, "call", lambda *a, **k: sent.append(a) or {})
    tools = {t.name: t for t in kg_tools.make_all_tools("http://127.0.0.1:9/mcp")}
    for name, args in (("kg_item_history", {"item": ""}), ("sap_search", {"query": " "}),
                       ("kg_find_tenders", {"query": ""}), ("kg_tender_record", {"tender": ""})):
        out = tools[name].fn(**args)
        assert out.startswith("NOTHING TO LOOK UP") and "UNAVAILABLE" not in out, name
    assert not sent


def test_a_tender_with_no_item_lists_its_line_items(monkeypatch):
    monkeypatch.setattr(kg_mcp, "legacy_get", lambda base, path, params: {
        "case_key": params["case_key"], "job_name": "ARC for gauges",
        "boq": [{"item": "TG BiMet 0-200 DegC U320T50", "material_code": "I-TG-G200-320-02"}]})
    tool = kg_tools.make_item_history_tool("http://127.0.0.1:9/mcp")
    out = tool.fn(item="", tender="PUR‑INSTMNT‑1003188")
    assert out.startswith("TENDER ITEMS — PUR-INSTMNT-1003188 · ARC for gauges: 1 line item(s).")
    assert "| 1 | TG BiMet 0-200 DegC U320T50 | I-TG-G200-320-02 |" in out


def test_a_tender_read_the_mcp_server_lacks_goes_to_the_kg_api(monkeypatch):
    # the MCP server serves SAP's records alone: the harness API's tender
    # reads go to the tender knowledge graph's own API instead
    from nam_agent.nam import kg_mcp, kg_tools, need

    def no_tool(*a, **k):
        raise RuntimeError("Unknown tool: search_tenders")

    seen = []
    monkeypatch.setattr(kg_mcp, "legacy_get", no_tool)
    monkeypatch.setattr(kg_tools, "_get", lambda base, path, params: seen.append(
        (base, path)) or {"tenders": []})
    monkeypatch.setenv("NAM_KG_API_URL", "http://kg-api:8077")
    assert need._get("http://sap/mcp", "/api/kg/tenders", {"query": "valve"}) == {"tenders": []}
    assert seen == [("http://kg-api:8077", "/api/kg/tenders")]
    monkeypatch.setenv("NAM_KG_API_URL", "mcp")            # no KG API: the error stands
    with pytest.raises(RuntimeError, match="Unknown tool"):
        need._get("http://sap/mcp", "/api/kg/tenders", {"query": "valve"})
