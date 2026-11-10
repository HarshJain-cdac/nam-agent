"""DETERMINISTIC EVAL — SAP alone (NAM_KG_TOOLS=sap_search).

The officers want to test SAP's purchasing records on their own: of the KG
server's tools, only sap_search; NAM's need capture, which reads the
tenders, steps aside. The reply below is a trimmed real sap_search envelope.
"""

from __future__ import annotations

from evals.helpers import make_agent
from nam_agent.nam import answer_data, kg_mcp, kg_tools

URL = "http://127.0.0.1:9/mcp"     # nothing listens there
SAP = {"status": "ok", "results": [{"description": "TW for DCU coke chamb. _03TE1311", "lines": [
    {"kind": "PO", "doc_no": "4500024964", "item_no": "00150", "date": "2022-07-13",
     "material": "I-TW-03TE1311-00", "material_name": "TW for DCU coke chamb. _03TE1311",
     "quantity": 1.0, "unit": "NOS", "rate_inr": 18835.35, "pur_case_id": "PUR-INSTMNT-1002364",
     "pr_no": "10073969", "po_no": "4500024964"},
    {"kind": "PR", "doc_no": "10073969", "item_no": "2090", "date": "2021-09-28",
     "material": "I-TW-03TE1311-00", "quantity": 1.0, "unit": "NOS", "rate_inr": 15000.0,
     "pur_case_id": "PUR-INSTMNT-1002364", "pr_no": "10073969", "po_no": "4500024964"}]}],
    "kg_cases": {}}


def _sap(monkeypatch, data):
    sent = []

    def call(base, tool, **args):
        sent.append((tool, args))
        return data
    monkeypatch.setattr(kg_mcp, "call", call)
    return sent


def test_sap_search_gives_each_line_newest_first(monkeypatch):
    sent = _sap(monkeypatch, SAP)
    out = kg_tools.make_sap_tool(URL).fn(query="PUR‑INSTMNT‑1002364")
    assert sent == [("sap_search", {"query": "PUR-INSTMNT-1002364", "top_k": 10})]
    assert out.startswith("SAP — 2 line(s)") and "PO: 4500024964" in out
    rows = [ln for ln in out.splitlines() if ln.startswith("| PO") or ln.startswith("| PR")]
    assert rows[0].startswith("| PO | 4500024964 | 2022-07-13 | I-TW-03TE1311-00")
    assert "18835.35" in rows[0] and "PUR-INSTMNT-1002364" in rows[0]


def test_no_line_is_a_real_negative(monkeypatch):
    _sap(monkeypatch, {"status": "ok", "results": [], "kg_cases": {}})
    out = kg_tools.make_sap_tool(URL).fn(query="titanium flange")
    assert out.startswith("SAP — NO LINE") and "not an outage" in out


def test_an_outage_is_unavailable_never_no_line(monkeypatch):
    def down(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(kg_mcp, "call", down)
    assert "UNAVAILABLE" in kg_tools.make_sap_tool(URL).fn(query="capillary")


def test_only_the_tools_named_are_given_and_nam_steps_aside(tmp_path):
    nam_agent = make_agent(tmp_path, kg_url=URL, kg_tools="sap_search")
    names = {s["name"] for s in nam_agent.tools.schemas()}
    assert "sap_search" in names
    assert not names & {"kg_item_history", "kg_find_tenders", "kg_tender_record", "nam_need"}
    assert nam_agent._nam_guide() is None                 # no flow without its tools
    system = nam_agent.session.build_system("what did we pay for thermowell")
    assert "you have ONLY sap_search" in system
    every = make_agent(tmp_path / "all", kg_url=URL)
    assert {"kg_item_history", "sap_search", "nam_need", "nam_ask"} <= {
        s["name"] for s in every.tools.schemas()}
    assert kg_tools.allowed("") is None and kg_tools.allowed(" sap_search, ") == {"sap_search"}


def test_the_flow_on_sap_alone_reads_only_sap(tmp_path, monkeypatch):
    sent = _sap(monkeypatch, SAP)
    nam_agent = make_agent(tmp_path, kg_url=URL, kg_tools="sap_search,nam")
    from nam_agent.nam import chatstate

    token = chatstate.enter("s1")
    try:
        out = nam_agent.tools._tools["nam_need"].fn(category="goods", need="thermowell")    # 1c searches at once
    finally:
        chatstate.leave(token)
    assert [t for t, _ in sent] == ["sap_search"]
    assert "| 1 | thermowell | 1 material |" in out


def test_a_reply_without_the_sap_numbers_gets_the_table(monkeypatch):
    _sap(monkeypatch, SAP)
    out = kg_tools.make_sap_tool(URL).fn(query="thermowell")
    calls = [{"tool": "sap_search", "output": out}]
    thin = answer_data.complete("We bought it in 2022.", calls)
    assert answer_data.SAP_HEADING in thin and "4500024964" in thin
    full = ("PUR-INSTMNT-1002364: PR 10073969, PO 4500024964, Rs 18,835.35 per NOS.")
    assert answer_data.complete(full, calls) == full
