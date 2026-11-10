"""The NAM knowledge skills (nam_agent/nam/skills/<name>/SKILL.md), as topics.

Two skills hold all of it: nam-documents (PQC, SCC, technical
specification, BOQ, approval note, and the extraction rules every document
follows) and nrl-procurement (NRL's goods, works and services manuals). Each
subject is a topic: a `## ` section whose first line is `terms: ...`, the
words that name it (nam_agent/memory/procedural/loader.py). A document topic is
written in four parts, `What:`, `Extract:`, `How:` and `Checks:`.

The NAM harness chat agent loads the same files (nam_agent.memory.bundled_skill_dirs) and
reads only the topics a message names, so a PQC question costs the PQC part,
not the whole skill. One copy: the chat and the harness cannot disagree about
what a PQC is. evals/deterministic/test_skill_triggers.py checks each skill
loads first on its own messages and never on an everyday one.

Used by:
  case.ask           "what is a PQC", "what do we extract from an SCC": the
                     topic's own part, cited as skill:<skill>/<topic>
  docs.review        the model of the review task is given the checks of
                     the topics of the documents it reviews
  extract.py         implements each topic's "Extract" part in code
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from nam_agent.memory.procedural.loader import Topic, _parse

ROOT = Path(__file__).parent / "skills"

# what case.ask and the tests ask for -> the label the topic is written with
_PARTS = {"what": "What", "what to extract": "Extract", "how to extract": "How",
          "checks": "Checks"}


@dataclass(frozen=True)
class NamTopic:
    name: str            # "<skill>/<topic>", e.g. "nam-documents/pqc"
    skill: str
    title: str
    terms: tuple[str, ...]
    flows: tuple[str, ...]
    body: str

    @property
    def description(self) -> str:
        return self.title

    def section(self, heading: str) -> str:
        """One part of a document topic ("What", "What to extract", "How to
        extract", "Checks"); "" when the topic is not written in parts."""
        label = _PARTS.get(heading.lower())
        return Topic(self.title, self.terms, self.body).part(label) if label else ""


def _flows(path: Path) -> tuple[str, ...]:
    front = path.read_text(encoding="utf-8").split("---", 2)[1]
    m = re.search(r"^flows:\s*(.+)$", front, re.MULTILINE)
    return tuple(x.strip().lower() for x in m.group(1).split(",") if x.strip()) if m else ()


@cache
def skills() -> dict[str, NamTopic]:
    """Every topic of every NAM skill, by name."""
    out = {}
    for path in sorted(ROOT.glob("*/SKILL.md")):
        parsed = _parse(path)
        if parsed is None:
            continue
        flows = _flows(path)
        for topic in parsed.topics():
            name = f"{parsed.name}/{topic.key}"
            out[name] = NamTopic(name, parsed.name, topic.title, topic.terms, flows, topic.text)
    return out


def for_flow(flow_id: str) -> list[NamTopic]:
    return [s for s in skills().values() if flow_id in s.flows]


def for_text(text: str) -> list[NamTopic]:
    """The topics the text names, most specific first (the extraction rules,
    which every document shares, last)."""
    low = (text or "").lower()
    found = [s for s in skills().values()
             if any(re.search(rf"\b{re.escape(t)}\b", low) for t in s.terms)]
    return sorted(found, key=lambda s: s.name == "nam-documents/rules")


DOCUMENT_SKILL = {"pqc": "nam-documents/pqc", "scc": "nam-documents/scc",
                  "specification": "nam-documents/spec", "boq": "nam-documents/boq",
                  "estimate": "nam-documents/boq", "aan": "nam-documents/aan"}


def checks_for(kinds) -> str:
    """The "Checks" parts of the topics of these document kinds, for a model
    that reviews them."""
    names = dict.fromkeys(DOCUMENT_SKILL[k] for k in kinds if k in DOCUMENT_SKILL)
    return "\n\n".join(f"{n}:\n{skills()[n].section('Checks')}" for n in names if n in skills())
