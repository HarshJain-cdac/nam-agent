"""The KG data in an answer does not depend on the model.

Asked the same question, two models gave answers of different size and
quality: one reproduced the comparison table and every tender, another kept
one tender and rounded the rates. The words may differ between models; the
data must not. So after each turn the harness checks the reply against what
the KG tools returned in that turn, in code:

  kg_find_tenders   every PUR id of its tender table
  kg_item_history   every PUR id it found, and each tender's lowest bid per unit
  kg_price_reference every precedent PUR id of the matched rows
  kg_tender_record  the tender's PUR id and its PR number

One claim is checked too: when kg_item_history found no line item at the
size asked, a reply sentence that says that size was bought or matched gets
the KG's own correction added under it.

If the reply leaves any of these out, the tool's own table is added under
"Data from the KG", exactly as the graph returned it. A reply that already
carries the data is left as the model wrote it.

The PR Creation Flow's tables are kept the same way, under "From the case":

  nam_estimate      each line's rate and amount, and the total

The PR and the PUR cases are shown as cards (cards.py), so they are not
repeated in the reply as tables.
"""

from __future__ import annotations

import re

from nam_agent.nam import guard

HEADING = "**Data from the KG** (as the graph returned it):"
CASE_HEADING = "**From the case** (as NAM holds it):"
_PUR = re.compile(r"\bPUR-[A-Z]+-\d+\b")
_ROWS_SHOWN = 15


def _table_after(text: str, marker: str | None = None) -> list[str]:
    """The first markdown table in text (after marker, when given), as lines."""
    if marker is not None:
        at = text.find(marker)
        if at < 0:
            return []
        text = text[at:]
    lines, rows = text.splitlines(), []
    for line in lines:
        if line.lstrip().startswith("|"):
            rows.append(line.rstrip())
        elif rows:
            break
    return rows


def _numbers(text: str) -> set[float]:
    out = set()
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        try:
            out.add(round(float(raw.replace(",", "")), 2))
        except ValueError:
            continue
    return out


def _first_rs(cell: str) -> float | None:
    m = re.search(r"Rs\.?\s*([\d,]+(?:\.\d+)?)", cell)
    return round(float(m.group(1).replace(",", "")), 2) if m else None


def _trim(rows: list[str]) -> list[str]:
    if len(rows) <= _ROWS_SHOWN + 2:
        return rows
    return rows[:_ROWS_SHOWN + 2] + [f"| ... {len(rows) - _ROWS_SHOWN - 2} more row(s) on file |"]


def key_data(tool: str, output: str) -> tuple[set[str], set[float], list[str]] | None:
    """(PUR ids, figures, the block to show) one KG tool's output obliges the
    reply to carry, or None when it returned no data."""
    if not output or "UNAVAILABLE" in output[:200]:
        return None
    if tool == "kg_find_tenders":
        table = _table_after(output)
        ids = set(_PUR.findall("\n".join(table)))
        return (ids, set(), table) if ids else None
    if tool == "kg_item_history":
        table = _table_after(output, "Comparison (")
        figures = set()
        if table:
            row = next((r for r in table if "Lowest bid per unit" in r), "")
            cells = [c for c in row.split("|")[2:] if c.strip()]
            figures = {f for f in map(_first_rs, cells) if f is not None}
        else:
            table = _trim(_table_after(output, " items:"))
        ids = set(_PUR.findall(output))
        return (ids, figures, table) if ids and table else None
    if tool == "kg_tender_record" and output.startswith("TENDER RECORD"):
        table = _table_after(output)
        pr = next((r for r in table if r.startswith("| PR no.")), "")
        figures = {f for f in _numbers(pr) if f >= 1000} if "not on file" not in pr else set()
        return set(_PUR.findall(output.splitlines()[0])), figures, table
    if tool == "sap_search" and output.startswith("SAP — ") and "NO LINE" not in output[:20]:
        table = _trim(_table_after(output))
        nums = set()
        for row in table[2:]:
            cells = [c.strip() for c in row.split("|")]
            nums |= {float(c) for c in cells[-4:-2] if c.isdigit()}     # PR and PO numbers
        return set(_PUR.findall("\n".join(table))), nums, table
    if tool == "kg_price_reference":
        lines = [ln for ln in output.splitlines() if ln.startswith("* ")][:12]
        ids = set(_PUR.findall("\n".join(lines)))
        return (ids, set(), lines) if ids else None
    return None


_NOT_AT_SIZE = re.compile(r"ITEM CHECK — NOT AT THIS SIZE:.*?no line item has (.+?)\. ")
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z\"#]*)", re.IGNORECASE)
_CLAIM = re.compile(r"\b(precedent|procured|bought|purchased|matche[sd]?|found|on file|supplied|"
                    r"awarded|used)\b", re.IGNORECASE)
_NEGATION = re.compile(r"\b(no|not|none|never|without|nothing)\b|n't", re.IGNORECASE)


def size_claims(reply: str, output: str) -> list[str]:
    """Sentences of the reply that say an item was bought at a size the KG
    found no line item at ("the closest precedent is the 30 mm x 6 mm gasket
    procured in ..." after NOT AT THIS SIZE for 30 mm, 6 mm)."""
    m = _NOT_AT_SIZE.search(output or "")
    if not m:
        return []
    sizes = [re.compile(rf"(?<![\d.]){re.escape(n)}\s*{re.escape(u)}", re.IGNORECASE)
             for n, u in _SIZE.findall(m.group(1))]
    if not sizes:
        return []
    text = "\n".join(ln for ln in reply.splitlines() if not ln.lstrip().startswith("|"))
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if (any(p.search(sentence) for p in sizes) and _CLAIM.search(sentence)
                and not _NEGATION.search(sentence)):
            out.append(sentence.strip())
    return out


def case_data(tool: str, output: str) -> tuple[set[str], set[float], list[str]] | None:
    """(PUR ids, figures, the block to show) a NAM step's output obliges the
    reply to carry, or None."""
    if not output or output.startswith("REFUSED"):
        return None
    table = _table_after(output)
    if not table:
        return None
    if tool == "nam_estimate":
        figures = set()
        for row in table[2:]:
            cells = [c.strip() for c in row.split("|")]
            figures |= {f for f in _numbers(" ".join(cells[7:9]))}       # rate, amount
        total = next((ln for ln in output.splitlines() if ln.startswith("Total excl. GST")), "")
        figures |= _numbers(total.split("Evaluation")[0])
        return set(), figures, table + ([total] if total else [])
    return None


SAP_HEADING = "**Data from SAP** (as SAP holds it):"


def complete(reply: str, tool_calls: list[dict], case_open: bool = False) -> str:
    """The reply, with the KG data it left out added under HEADING. Ids in
    it are written with a plain "-" (a model writes U+2011, and an officer
    who copies "PUR‑X‑1" back must reach the same tender)."""
    reply = guard.plain_ids(reply)
    if not reply:
        return reply
    for call in tool_calls or []:
        if call.get("tool") == "kg_item_history":
            output = str(call.get("output") or "")
            if size_claims(reply, output):
                sizes = _NOT_AT_SIZE.search(output).group(1)
                reply = (reply.rstrip() + f"\n\n**Correction from the KG:** no past NRL line "
                         f"item is {sizes}. NRL bought this item only at other sizes, and "
                         f"none of them is that size.")
                break
    latest: dict[str, str] = {}
    for call in tool_calls or []:
        latest[call.get("tool", "")] = str(call.get("output") or "")
    have_ids, have_numbers = set(_PUR.findall(reply)), _numbers(reply)
    # a turn of the PR Creation Flow narrows the records with a question: the
    # KG's raw tables stay out of it (the flow shows records at the pick)
    # (and so does a side question asked while a NAM case is open: the case is
    # the officer's screen, and its cards carry SAP's records)
    flow_turn = case_open or any(str(c.get("tool", "")).startswith("nam_")
                                 for c in tool_calls or [])
    for heading, read in ((CASE_HEADING, case_data), (HEADING, key_data)):
        if heading in reply or (flow_turn and heading == HEADING):
            continue
        blocks, tools = [], set()
        for tool, output in latest.items():
            need = read(tool, output)
            if need is None:
                continue
            ids, figures, block = need
            if ids <= have_ids and figures <= have_numbers:
                continue
            blocks.append("\n".join(block))
            tools.add(tool)
        if blocks:
            # SAP's lines are SAP's, not the knowledge graph's
            if heading == HEADING and tools == {"sap_search"}:
                heading = SAP_HEADING
            reply = reply.rstrip() + "\n\n" + heading + "\n\n" + "\n\n".join(blocks)
    return reply
