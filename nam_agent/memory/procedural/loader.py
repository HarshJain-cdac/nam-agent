"""Procedural memory — SKILL.md files: how to act, loaded only when relevant.

Official Anthropic Agent Skills format: YAML frontmatter with `name` and
`description` (the description doubles as the trigger — no custom `triggers:`
field, which launch-agent-skills used before the spec settled).

Progressive disclosure, the part that matters:
  1. frontmatter of every skill is always scanned (cheap)
  2. a skill's BODY is loaded into the prompt only when it matches the message
  3. a skill split into topics loads only the topics the message names
  4. files a skill references are only read if the model asks

Topics. A `## ` section whose first line is `terms: a, b, c` is a topic: it
loads only when the message names one of its terms (whole words). The text
before the first topic is the skill's shared part and always loads with it.
One skill can then hold several subjects (PQC, SCC, BOQ...) and a question
about one of them still costs only that one. A message that names no topic
gets the whole body, so nothing is ever hidden from a match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path

    def topics(self) -> list[Topic]:
        return topics(self.body)

    def focus(self, message: str, whole: bool = True) -> str:
        """The body to load for this message: the shared part plus the topics
        the message names. When it names none: the whole body, or "" with
        whole=False (a second match that adds nothing specific)."""
        found = self.topics()
        if not found:
            return self.body
        named = [t for t in found if t.named_in(message)]
        if not named and not whole:
            return ""
        named = named or found
        shared = self.body.split("\n## ", 1)[0].strip()
        return "\n\n".join([shared] * bool(shared) + [t.render() for t in named])


# The parts a document topic is written in: what it is, what to extract, how,
# and the checks. Labels at a line start, so a topic stays one compact block.
LABELS = ("What", "Extract", "How", "Checks")


@dataclass(frozen=True)
class Topic:
    """One `## ` section of a skill that starts with a `terms:` line."""

    title: str
    terms: tuple[str, ...]
    text: str

    @property
    def key(self) -> str:
        return re.split(r"[\s(]", self.title.strip(), maxsplit=1)[0].lower()

    def named_in(self, message: str) -> bool:
        low = (message or "").lower()
        return any(re.search(rf"\b{re.escape(t)}\b", low) for t in self.terms)

    def part(self, label: str) -> str:
        """The text after a `Label:` line start, up to the next label line."""
        found = re.search(rf"^{re.escape(label)}:\s*(.*?)(?=^(?:{'|'.join(LABELS)}):\s|\Z)",
                          self.text, re.MULTILINE | re.DOTALL)
        return found.group(1).strip() if found else ""

    def render(self) -> str:
        return f"## {self.title}\n{self.text}"


def topics(body: str) -> list[Topic]:
    out = []
    for section in re.split(r"^## ", body, flags=re.MULTILINE)[1:]:
        title, _, rest = section.partition("\n")
        first, _, text = rest.partition("\n")
        if first.lower().startswith("terms:"):
            terms = tuple(t.strip().lower() for t in first[6:].split(",") if t.strip())
            out.append(Topic(title.strip(), terms, text.strip()))
    return out


def _parse_text(text: str, path: Path) -> Skill | None:
    """Validate SKILL.md content (used by the loader AND the create_skill tool)."""
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        return None
    front, body = match.groups()
    fields = {
        k.strip(): v.strip().strip("'\"")
        for k, _, v in (line.partition(":") for line in front.splitlines() if ":" in line)
    }
    if "name" not in fields or "description" not in fields:
        return None
    return Skill(fields["name"], fields["description"], body.strip(), path)


def _parse(path: Path) -> Skill | None:
    return _parse_text(path.read_text(encoding="utf-8"), path)


class SkillLoader:
    """Scans skill directories: the repo's skills/ (built-in + community) and
    NAM_HOME/skills (installed or agent-authored). Re-scans automatically
    when any SKILL.md changes, so a skill created mid-session is live next turn."""

    def __init__(self, dirs: list[Path]):
        self.dirs = dirs
        self.skills: list[Skill] = []
        self._sig: tuple = ()
        self.refresh()

    def _scan_sig(self) -> tuple:
        sig = []
        for d in self.dirs:
            if d.is_dir():
                for f in sorted(d.rglob("SKILL.md")):
                    sig.append((str(f), f.stat().st_mtime))
        return tuple(sig)

    def refresh(self) -> None:
        self.skills = []
        for d in self.dirs:
            if not d.is_dir():
                continue
            for f in sorted(d.rglob("SKILL.md")):
                skill = _parse(f)
                if skill:
                    self.skills.append(skill)
        self._sig = self._scan_sig()

    def match(self, message: str, max_skills: int = 2) -> list[Skill]:
        """Transparent trigger: keyword overlap between the message and each
        skill's name+description. No embeddings, no magic — you can compute
        the score in your head."""
        if self._scan_sig() != self._sig:   # a skill was added/edited — reload
            self.refresh()
        msg_words = set(re.findall(r"[a-z0-9]{3,}", message.lower()))
        scored = []
        for skill in self.skills:
            skill_words = set(re.findall(r"[a-z0-9]{3,}", (skill.name + " " + skill.description).lower()))
            overlap = len(msg_words & skill_words)
            if overlap >= 2:
                scored.append((overlap, skill))
        scored.sort(key=lambda pair: -pair[0])
        return [skill for _, skill in scored[:max_skills]]

    def load(self, message: str) -> list[tuple[Skill, str]]:
        """The matched skills with the text each loads. The first match
        loads whole when it names no topic; a later one only for a topic it
        names: "which works contract: item rate" is about the procurement
        manual's Works topic, and the document skill's "rate" must not pull
        all of its topics in beside it."""
        out = []
        for i, skill in enumerate(self.match(message)):
            body = skill.focus(message, whole=(i == 0))
            if body:
                out.append((skill, body))
        return out
