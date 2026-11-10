"""The NAM playbooks: every flow of plan section 3, as data.

A Flow says what the response envelope reports (flow id, FRS numbers,
stage), which case stages it belongs to, the phrasings that route a short
system prompt to it (nam_agent/nam/router.py), the tools it may use, the schema
its `result` must pass, and the handler that does the work. Its instructions
for the model are playbooks/instructions/<flow id>.md, one file per flow, with
a section per category where Goods, Services and Works differ. They replace
SOUL.md for NAM calls.

Every flow is listed here, so every FRS function routes somewhere. A flow
whose handler is None is routed and permission-checked like any other, then
answered with flow_not_built (501): it is built in plan Step 15.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

INSTRUCTIONS = Path(__file__).parent / "instructions"
ANY_STAGE = "*"


@dataclass(frozen=True)
class Outcome:
    """What a flow hands back to run(); run() adds the envelope around it."""

    case_id: str | None
    status: str                      # ok | needs_input | needs_approval | refused
    result: dict
    questions: tuple = ()
    citations: tuple = ()
    warnings: tuple = ()
    next: tuple = ()
    # what the flow produced for the stage machine, by artifact name
    # (orchestrator.ARTIFACTS says which flow may write which). A flow never
    # proposes an approval or an irreversible action: only code does.
    artifacts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Flow:
    id: str
    title: str                       # what a question offering this flow shows
    frs: tuple[str, ...]
    stages: tuple[str, ...]          # case stages it belongs to; ANY_STAGE for all
    # (regex, weight) over the lowercased system prompt; the router adds the
    # weights of every pattern that matches, and the highest total wins
    intents: tuple[tuple[str, int], ...]
    tools: tuple[str, ...] = ()      # the contracts it may call (plan Step 10)
    result_schema: str = ""          # a file in nam_agent/nam/schemas/, without .json
    handler: Callable | None = None  # (request, ctx) -> Outcome; None: not built yet
    needs_case: bool = True          # False: it may open a new case
    category: str = ""               # goods | services | works, for per-category flows

    @property
    def stage(self) -> str:
        """The stage the envelope reports: the first one it belongs to."""
        return "S1" if self.stages == (ANY_STAGE,) else self.stages[0]

    def at(self, stage: str) -> bool:
        return ANY_STAGE in self.stages or stage in self.stages

    def instructions(self, category: str = "") -> str:
        """The flow's instructions, with only the given category's section."""
        return instructions(self.id, category)


# The tools a flow may name: the contracts of nam_agent/nam/contracts/base.py
# and the OCR tool (Step 11).
TOOLS = frozenset({"boilerplate", "resolve_reference", "enrichment", "price_history", "quantity_history",
                   "clauses", "documents", "suppliers", "tenders", "tender_record",
                   "ocr_document"})


def registry() -> dict[str, Flow]:
    from nam_agent.nam.playbooks import catalog

    return {f.id: f for f in catalog.FLOWS}


def intent_score(flow: Flow, text: str) -> int:
    """The sum of the weights of the flow's patterns that match `text`
    (re caches the compiled patterns)."""
    return sum(w for pattern, w in flow.intents if re.search(pattern, text))


_SECTION = re.compile(r"^## (.+)$", re.MULTILINE)
CATEGORY_HEADING = {"goods": "Goods", "services": "Services", "works": "Works"}


def instructions(flow_id: str, category: str = "") -> str:
    """playbooks/instructions/<flow id>.md. Sections headed "## Goods",
    "## Services" or "## Works" are kept only for that category."""
    text = (INSTRUCTIONS / f"{flow_id}.md").read_text(encoding="utf-8")
    parts = _SECTION.split(text)
    keep = [parts[0]]
    for heading, body in zip(parts[1::2], parts[2::2], strict=True):
        per_category = heading.strip() in CATEGORY_HEADING.values()
        if per_category and heading.strip() != CATEGORY_HEADING.get(category):
            continue
        keep.append(f"## {heading}{body}")
    return "".join(keep).strip() + "\n"
