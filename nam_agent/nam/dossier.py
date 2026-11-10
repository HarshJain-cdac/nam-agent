"""The NAM dossier — one runtime Markdown file per need, filled step by step.

A need travels through several NAM steps (Need capture, then References, and
the ones after). Each step learns something from the knowledge graph, and the
officer and the agent both need to see all of it in one place, in the order it
was learned. The dossier is that place: `.nam/nam/<NEED-ID>.md`.

It is deliberately compact. A step writes what it found in a few lines and a
table, never a raw API response: the file is read by a person reviewing the
case and fed back to the model on later steps, and both pay for every line.

HOW THE FILE IS BUILT
---------------------
A header (need id, requirement, stage, times) followed by one block per step,
each between markers:

    <!-- nam:step=capture -->
    ## 1. Need capture
    ...
    <!-- /nam:step=capture -->

`write_step` replaces a step's block when it exists and appends it when it does
not, so running a step again (the officer refined the requirement) rewrites
that step's section and leaves the other steps alone. The header is rebuilt on
every write, so the stage it shows is always the current one.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

# The steps in the order NAM runs them. Only a step listed here can be written,
# so a typo in a step name fails loudly instead of creating a stray section.
STEPS = {
    "capture": "1. Need capture",
    "references": "2. References",
}

_BLOCK = re.compile(r"<!-- nam:step=(\w+) -->\n.*?<!-- /nam:step=\1 -->\n?", re.DOTALL)
# need ids are NAM-YYYY-NNNN; anything else never becomes a path
_NEED_ID = re.compile(r"^NAM-\d{4}-\d{4,}$")


def path_for(home: Path, need_id: str) -> Path:
    if not _NEED_ID.match(need_id or ""):
        raise ValueError(f"not a NAM need id: {need_id!r}")
    return Path(home) / "nam" / f"{need_id}.md"


def read(home: Path, need_id: str) -> str:
    """The dossier's text, or "" when no step has written it yet."""
    try:
        return path_for(home, need_id).read_text(encoding="utf-8")
    except (FileNotFoundError, ValueError):
        return ""


def _header(need_id: str, requirement: str, stage: str) -> str:
    now = datetime.now().isoformat(timespec="seconds")
    return (f"# {need_id}\n\n"
            f"**Requirement:** {requirement.strip()}\n\n"
            f"**Stage:** {stage} · **Updated:** {now}\n\n")


def write_step(home: Path, need_id: str, step: str, body: str, *,
               requirement: str, stage: str) -> Path:
    """Write one step's section, replacing that step's previous section."""
    if step not in STEPS:
        raise ValueError(f"unknown NAM step {step!r}; one of {', '.join(STEPS)}")
    path = path_for(home, need_id)
    blocks = {m.group(1): m.group(0) for m in _BLOCK.finditer(read(home, need_id))}
    blocks[step] = (f"<!-- nam:step={step} -->\n## {STEPS[step]}\n\n"
                    f"{body.strip()}\n<!-- /nam:step={step} -->\n")
    ordered = [blocks[s] for s in STEPS if s in blocks]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_header(need_id, requirement, stage) + "\n".join(ordered),
                    encoding="utf-8")
    return path
