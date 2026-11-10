"""DETERMINISTIC EVAL — skills stay small, because every token of a matched
skill is read by the model on that turn: a lean skill is a faster, cheaper
and more focused answer.

The harness ships two skills, nam-documents and nrl-procurement
(nam_agent/nam/skills/), which are split into
topics so a message loads only the topics it names (loader.Skill.focus).
Budgets, at ~4 characters a token, on what is actually loaded:

  a skill without topics                 <= 400 tokens, loaded whole
  the shared part of a topic skill       <= 250 tokens, loaded with any topic
  one topic                              <= 550 tokens: tables over prose; the
                                            long source text stays in
                                            reference-*.md, never loaded
  everything one message loads           <= 1400 tokens (at most two skills)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nam_agent.memory.procedural.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[2]
DIRS = [ROOT / "nam_agent" / "nam" / "skills"]
LOADER = SkillLoader(DIRS)
PATHS = sorted(p for d in DIRS for p in d.rglob("SKILL.md"))


def tokens(text: str) -> int:
    return len(text) // 4


def test_nam_agent_ships_exactly_two_skills():
    assert sorted(s.name for s in LOADER.skills) == ["nam-documents", "nrl-procurement"]


@pytest.mark.parametrize("skill", LOADER.skills, ids=lambda s: s.name)
def test_each_skill_and_topic_is_within_budget(skill):
    topics = skill.topics()
    if not topics:
        assert tokens(skill.body) <= 400, tokens(skill.body)
        return
    shared = skill.body.split("\n## ", 1)[0]
    assert tokens(shared) <= 250, tokens(shared)
    for topic in topics:
        assert tokens(topic.render()) <= 550, (topic.title, tokens(topic.render()))


def test_what_one_message_loads_stays_under_1400_tokens():
    from test_skill_triggers import EVERYDAY, MESSAGES

    worst = max((sum(tokens(body) for _, body in LOADER.load(m)), m)
                for m in [*EVERYDAY, *(x for ms in MESSAGES.values() for x in ms)])
    assert worst[0] <= 1400, worst


def test_skills_are_plain_lf_text():
    """Windows line endings or a folded `description: >` make the loader drop
    a skill silently (it happened to three skills here)."""
    for path in PATHS:
        raw = path.read_bytes()
        assert b"\r\n" not in raw, path
        assert b"\ndescription: >" not in raw, path
