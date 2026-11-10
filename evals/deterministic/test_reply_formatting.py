"""DETERMINISTIC EVAL — replies are presented, not dumped.

A real price answer came back as one run-on line of pipe characters in the NiT
UI. Two causes, one on each side, and both are pinned here:

  * the renderer had no table support at all, and joined a block's lines with
    "\\n" into a <p>, which HTML collapses to spaces
  * nothing told the agent to put a set of comparable numbers in a table, or to
    keep each row on its own line

Offline: reads source files, no browser, no model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MARKDOWN = ROOT / "nit-forge-ui" / "src" / "components" / "nit" / "Markdown.tsx"
SESSION = ROOT / "nam_agent" / "runtime" / "session.py"


@pytest.mark.skipif(not MARKDOWN.exists(), reason="frontend not checked out")
def test_the_renderer_handles_tables():
    source = MARKDOWN.read_text(encoding="utf-8")
    for needed in ("<table", "<thead", "<tbody", "splitRow", "isDividerRow"):
        assert needed in source, f"the reply renderer needs {needed} to draw a table"


@pytest.mark.skipif(not MARKDOWN.exists(), reason="frontend not checked out")
def test_line_breaks_survive_a_paragraph():
    """paragraph.join("\\n") inside a plain <p> is what flattened the tables:
    HTML collapses newlines unless the element says otherwise."""
    source = MARKDOWN.read_text(encoding="utf-8")
    assert "whitespace-pre-line" in source


@pytest.mark.skipif(not MARKDOWN.exists(), reason="frontend not checked out")
def test_ordered_lists_stay_ordered():
    """Steps rendered as bullets lose their sequence."""
    source = MARKDOWN.read_text(encoding="utf-8")
    assert "<ol" in source and "list-decimal" in source


@pytest.mark.skipif(not MARKDOWN.exists(), reason="frontend not checked out")
def test_a_table_that_lost_its_newlines_is_recovered():
    """Models sometimes emit a whole table on one line. The giveaway is a
    |---|---| divider sitting mid-line."""
    source = MARKDOWN.read_text(encoding="utf-8")
    assert "unflatten" in source


def test_the_persona_says_how_to_present_numbers():
    source = SESSION.read_text(encoding="utf-8").lower()
    assert "how to present an answer" in source
    for rule in ("table", "own line", "price level", "cite"):
        assert rule in source, f"the persona must say something about {rule}"


# --- fabrication ------------------------------------------------------------
# Asked "how many tenders do we have for gaskets", the agent answered with a
# table containing PUR-MECHMNT-1004892 — an id that does not exist in the graph
# — gave PUR-MECHMNT-1008357 a subject it does not have, invented item counts
# (12 and 20 against a real 1240) and asserted "Limited Tender mode (2-3
# bidders)", which no tool ever returned.

def test_the_persona_forbids_facts_no_tool_returned():
    source = SESSION.read_text(encoding="utf-8")
    assert "Never state a fact a tool did not give you" in source
    lowered = source.lower()
    for thing in ("tender ids", "job names", "item counts", "bidder"):
        assert thing in lowered, f"the rule must name {thing} explicitly"


def test_a_returned_table_must_be_reproduced_not_summarised():
    """The list tool returns a Markdown table. Summarising it into prose is how
    the counts and subjects drifted from what the graph actually holds."""
    source = SESSION.read_text(encoding="utf-8")
    assert "reproduce that table" in source.lower()

    from nam_agent.nam import kg_tools as kg_precedent

    tool = next(t for t in kg_precedent.make_tools("http://localhost:1")
                if t.name == "kg_find_tenders")
    assert "table" in tool.description.lower()


def test_the_list_tool_says_when_it_truncated():
    """'10 of 47, showing 10' is honest; '10' alone invites the model to treat
    a capped list as the whole answer."""
    import inspect

    from nam_agent.nam import kg_tools as kg_precedent

    source = inspect.getsource(kg_precedent.make_find_tenders_tool)
    assert "showing" in source
    assert "REPRODUCE THE TABLE" in source
