"""DETERMINISTIC EVAL — the NIT from NRL's template, only on the final yes, as a PDF.

The chat could capture a need but never produce the notice. generate_nit now
fills the template ported from nit-forge-ui/src/lib/nit-template.ts from the
case, prints a gap as [TO BE FILLED] instead of inventing it, and is made only
after its preview was shown and the user chose "Yes, generate the NIT". The
chat card then offers View NIT and Download PDF.
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path

from nam_agent import db
from nam_agent.nam import need as nam
from nam_agent.nam import nit
from nam_agent.server import chat as chat_mod
from nam_agent.server import nam_routes as dashboard

ROOT = Path(__file__).resolve().parents[3]


def _case(tmp_path):
    conn = db.connect(tmp_path)
    need = nam.create(conn, "AMC for housekeeping at Plant-1 for 12 months")
    return conn, need


def _text(pdf: bytes) -> str:
    """Every string drawn on every page."""
    out = []
    for m in re.finditer(rb"stream\n(.*?)\nendstream", pdf, re.DOTALL):
        out += re.findall(r"\(((?:\\.|[^\\)])*)\) Tj", zlib.decompress(m.group(1)).decode("latin-1"))
    return " ".join(s.replace("\\(", "(").replace("\\)", ")") for s in out)


def test_the_template_sections_come_in_order_with_gaps_marked(tmp_path):
    conn, need = _case(tmp_path)
    doc = nit.build(nam.load(conn, need.need_id))
    assert [s["title"] for s in doc["sections"]] == [
        "Tender Schedule", "Tender Information Summary (TIS)",
        "Pre-Qualification Criteria (PQC)", "Commercial Terms"]
    header = {r["label"]: r["value"] for r in doc["header"]}
    assert header["Subject"].startswith("AMC for housekeeping")
    assert header["Tender No"] is None                       # never invented
    assert "Tender No" in doc["missing"] and doc["filled"] < doc["total"]
    assert not doc["handed_off"]
    # no estimate yet: the need is the one line; an item never carries a rate
    assert [set(x) for x in doc["items"]] == [{"item", "qty", "uom"}]


def test_the_pdf_is_valid_and_carries_every_section(tmp_path):
    conn, need = _case(tmp_path)
    data = nit.pdf(nit.build(nam.load(conn, need.need_id)))
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    text = _text(data)
    for part in ("NUMALIGARH REFINERY LIMITED", "Tender Schedule", "Commercial Terms",
                 "Pre-Qualification", "[TO BE FILLED]", "DRAFT", "Holiday Listing",
                 "Annexure - Schedule of items", "Page 1 of"):
        assert part in text, part
    # the xref offsets point at the objects
    xref = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[xref:xref + 4] == b"xref"


def test_only_the_final_yes_generates_it(tmp_path):
    conn, need = _case(tmp_path)
    events = []
    tool, _ = nit.make_tools(conn)
    notify = lambda kind, ev: events.append((kind, ev))          # noqa: E731
    # confirm=true without a preview is not a yes the user gave
    out = tool.fn(need_id=need.need_id, confirm=True, _notify=notify)
    assert "ask_options" in out and nit.YES in out and events == []
    out = tool.fn(need_id=need.need_id, confirm=True, _notify=notify)
    assert out.startswith("Generated the NIT") and events[0][0] == "nit"
    assert events[0][1]["case_id"] == need.need_id and events[0][1]["draft"] is True
    assert "No case" in tool.fn(need_id="NAM-2099-9999")


def test_text_is_pdf_safe_and_a_ready_nit_has_no_draft_banner():
    doc = {"case_id": "NAM-2026-0001", "title": nit.TITLE, "provenance": nit.PROVENANCE,
           "handed_off": True, "intro": "x", "header": [], "sections": [], "contacts": [],
           "items": [{"item": "Gauge — ₹ (bimetallic)", "qty": 8, "uom": "No"}],
           "filled": 0, "total": 0, "missing": [], "ready": True}
    text = _text(nit.pdf(doc))
    assert "Gauge - Rs. (bimetallic)" in text and "DRAFT" not in text


def test_the_nit_opens_beside_the_chat_65_35():
    static = ROOT / "nam_agent/server/static"
    js = (static / "js/render.js").read_text(encoding="utf-8")
    # generated -> shown at once beside the chat; the card's View NIT reopens it
    assert 'ev.kind === "nit"){ pending.nit = ev; openNit(ev.case_id); }' in js
    assert "${nitRow(t)}" in js and "openNit('${id}')" in js
    assert "?download=1" in js and "/^NAM-\\d{4}-\\d{4,}$/.test(id" in js
    html = (static / "index.html").read_text(encoding="utf-8")
    assert html.index('id="nitpane"') < html.index('id="dock"')      # NIT left, chat right
    assert 'id="nit-sheet"' in html and 'onclick="closeNit()"' in html
    css = (static / "style.css").read_text(encoding="utf-8")
    assert "body.nit-open #nitpane{display:flex;flex-direction:column;flex:0 0 65%" in css
    assert "body.nit-open #dock{flex:0 0 35%;width:35%" in css
    src = Path(dashboard.__file__).read_text(encoding="utf-8")
    assert 'self.path.startswith("/api/nit/")' in src and '"application/pdf"' in src
    assert '"flow_complete", "nit")' in Path(chat_mod.__file__).read_text(encoding="utf-8")


PAST = {"case_key": "PUR-MECHMNT-1003042", "tender_no": "OC01000375/YUV", "emd_amount": "0.00",
        "pre_bid_meeting_date": "Not Applicable",
        "pqc": [{"text": "I. The bidder shall be ISO 9001-2015 certified for manufacturing of gaskets."},
                {"text": "I. The bidder shall be ISO 9001-2015 certified for manufacturing of gaskets."},
                {"text": "Please refer Tender documents."}],
        "scc": [{"text": "PAYMENT TERMS:\n100% payment within 30 days from the date of receipt "
                         "and acceptance of consignment."},
                {"text": "B RATES: Rates should be quoted in line with clause 9 above C EVALUATION "
                         "OF TENDER: Bids shall be evaluated as per clause 10 above D SECURITY "
                         "DEPOSIT: Successful bidders shall furnish security deposit as per clause "
                         "11 above E DEFECT LIABILITY PERIOD: 12 months from completion."},
                {"text": "Consortium/JV bids not accepted."}]}


def _with_kg(monkeypatch, conn, need, reply):
    from nam_agent.nam import kg_tools

    nit._kg_cache.clear()
    need.selected_cases = ["PUR-MECHMNT-1003042"]
    monkeypatch.setattr(kg_tools, "_get", reply)
    return nit.build(need, "http://kg")


def test_empty_rows_are_suggested_from_past_tenders_with_their_source(tmp_path, monkeypatch):
    conn, need = _case(tmp_path)
    doc = _with_kg(monkeypatch, conn, nam.load(conn, need.need_id), lambda *a, **k: PAST)
    rows = {r["label"]: r["value"] for s in doc["sections"] for r in s["rows"]}
    pqc = rows["Pre-Qualification Criteria (PQC)"]
    assert pqc["source"] == "PUR-MECHMNT-1003042 (tender OC01000375/YUV)"
    assert pqc["text"].count("ISO 9001") == 1 and "refer Tender" not in pqc["text"]
    assert rows["Milestone Payment"]["text"].startswith("PAYMENT TERMS: 100% payment")
    assert rows["Security Deposit"]["text"] == \
        "...SECURITY DEPOSIT: Successful bidders shall furnish security deposit as per clause 11 above"
    assert rows["Consortium Agreement"]["text"] == "Consortium/JV bids not accepted."
    # a past EMD is a reference, never this tender's EMD
    assert rows["Earnest Money Deposit (EMD)"]["text"] == "Nil in the past tender; set this tender's EMD"
    assert rows["Pre-Bid Meeting Requirement"]["text"] == "Not Applicable"
    assert doc["suggested"] >= 6 and doc["sources"] == [pqc["source"]]
    text = _text(nit.pdf(doc))
    assert "Suggested from PUR-MECHMNT-1003042" in text and "0.11 0.31 0.85 rg" in \
        zlib.decompress(re.search(rb"stream\n(.*?)\nendstream", nit.pdf(doc), re.DOTALL)
                        .group(1)).decode("latin-1")


def test_the_kg_down_still_makes_the_nit_with_gaps(tmp_path, monkeypatch):
    conn, need = _case(tmp_path)

    def down(*a, **k):
        raise OSError("connection refused")

    doc = _with_kg(monkeypatch, conn, nam.load(conn, need.need_id), down)
    assert doc["suggested"] == 0 and "Pre-Qualification Criteria (PQC)" in doc["missing"]
    assert nit.pdf(doc).startswith(b"%PDF")


# -- the filled template beside the chat: asked, changed, ready ---------------------

def test_the_sheet_is_the_filled_template_not_a_pdf():
    static = ROOT / "nam_agent/server/static"
    js = (static / "js/render.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")
    assert "iframe" not in html.split('id="nitpane"')[1].split("</section>")[0]
    assert "fetch(`${nitUrl(id)}.json`)" in js and "function renderNit(d)" in js
    for part in ("Numaligarh Refinery Limited", "nit-sec", "Annexure &mdash; Schedule of items",
                 "Draft &mdash; not for issue", "[TO BE FILLED]"):
        assert part in js, part
    # a suggestion has Accept and Change; a gap has Fill; changes go through the chat
    assert 'uiButton("Accept"' in js and 'uiButton("Change"' in js and 'uiButton("Fill"' in js
    assert "input.value = `${verb} ${NIT_LABELS[rowId]} to `" in js
    # an accepted suggestion shows only its text, not "accepted from PUR-..."
    filled = js.split("const nitCell = r => {")[1].split("};")[0]
    assert "return esc(r.value);" in filled and "r.source" not in filled.split('"suggested")')[1]
    # Download PDF only once ready; a draft any time
    assert 'id="nit-download" onclick="downloadNit()" disabled' in html
    assert "dl.disabled = !d.ready" in js and "downloadNit(null, true)" in html


def test_missing_entries_are_asked_one_at_a_time_and_saved(tmp_path):
    conn, need = _case(tmp_path)
    gen, set_field = nit.make_tools(conn)
    events = []
    notify = lambda kind, ev: events.append(ev)                  # noqa: E731
    gen.fn(need_id=need.need_id)
    out = gen.fn(need_id=need.need_id, confirm=True, _notify=notify)
    assert "Next entry: E-File Ref" in out and "ask_options" in out and "Never invent" in out
    out = set_field.fn(need_id=need.need_id, field="aan_file_number", value="NRL/MM/2026/77",
                       _notify=notify)
    assert "Next entry: Tender No" in out and events[-1]["missing"] == events[0]["missing"] - 1
    # a change asked for by its label, and a skip
    set_field.fn(need_id=need.need_id, field="Earnest Money Deposit (EMD)", value="Nil")
    out = set_field.fn(need_id=need.need_id, field="tender_no", value="skip")
    assert "Next entry: Tender Publish Date" in out
    doc = nit.build(nam.load(conn, need.need_id), "", conn)
    rows = {r["id"]: r for r in nit.rows_of(doc)}
    assert rows["aan_file_number"]["value"] == "NRL/MM/2026/77"
    assert rows["emd"]["value"] == "Nil" and rows["emd"]["source"] == "you"
    assert rows["tender_no"]["kind"] == "missing" and not doc["ready"]
    assert "No entry" in set_field.fn(need_id=need.need_id, field="colour", value="x")


def test_suggestions_are_accepted_all_or_one_and_the_nit_becomes_ready(tmp_path, monkeypatch):
    conn, need = _case(tmp_path)
    case = nam.load(conn, need.need_id)
    doc = _with_kg(monkeypatch, conn, case, lambda *a, **k: PAST)
    from nam_agent.nam import kg_tools

    gen, set_field = nit.make_tools(conn, "http://kg")
    monkeypatch.setattr(kg_tools, "_get", lambda *a, **k: PAST)
    store_need = nam.load(conn, need.need_id)
    store_need.selected_cases = ["PUR-MECHMNT-1003042"]
    monkeypatch.setattr(nam, "load", lambda c, i: store_need if i == need.need_id else None)
    gen.fn(need_id=need.need_id)
    out = gen.fn(need_id=need.need_id, confirm=True)
    assert f"Accept the {doc['suggested']} suggestions" in out
    # "one by one" must not accept anything
    set_field.fn(need_id=need.need_id, field="all_suggestions", value=nit.ONE_BY_ONE)
    assert nit.build(store_need, "http://kg", conn)["suggested"] == doc["suggested"]
    out = set_field.fn(need_id=need.need_id, field="all_suggestions", value=nit.ACCEPT_ALL)
    after = nit.build(store_need, "http://kg", conn)
    assert after["suggested"] == 0 and after["filled"] == doc["suggested"] + doc["filled"]
    row = next(r for r in nit.rows_of(after) if r["id"] == "consortium_agreement")
    assert row["source"] == "accepted from PUR-MECHMNT-1003042 (tender OC01000375/YUV)"
    for r in nit.rows_of(after):                                 # the user fills the rest
        if r["kind"] == "missing":
            nit.save(conn, need.need_id, r["id"], "given", "you")
    final = nit.build(store_need, "http://kg", conn)
    assert final["ready"] and "DRAFT" not in _text(nit.pdf(final))
    assert "complete" in set_field.fn(need_id=need.need_id, field="emd", value="Nil")


def test_the_pdf_waits_for_ready_and_the_sheet_accepts_a_suggestion():
    src = Path(dashboard.__file__).read_text(encoding="utf-8")
    assert 'if not nit_doc["ready"] and not query.get("draft"):' in src and "status=409" in src
    assert 're.fullmatch(r"/api/nit/(NAM-\\d{4}-\\d{4,})/field", self.path)' in src
    assert 'row["kind"] != "suggested" or payload.get("value") != "accept"' in src
