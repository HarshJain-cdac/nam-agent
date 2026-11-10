"""DETERMINISTIC EVAL — skills load on the right messages, and only those.

A skill is chosen by keyword overlap (nam_agent/memory/procedural/loader.py): it
loads when a message shares 2+ words of 3+ letters with its name +
description, at most two per turn, highest overlap first. Review of two skill
PRs (#149, #159) found the two ways that goes wrong:

  1. Precision. A description built on question words (why, what, should,
     for) matches almost any message. "why did Alex move our meeting" loaded
     a metric-anomaly skill, and 28 lines of Shapley decomposition rode along.
  2. Hijacking. A new skill ties an existing one on shared words, and the tie
     goes to folder order, not relevance — so "prep me for tomorrow's standup"
     loaded interview-prep ahead of meeting-prep.

Both checks run over EVERY skill in the repo, so a skill PR is covered without
its author writing any Python. MESSAGES is kept by reviewers: when a skill
lands, add a few messages it should answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nam_agent.memory.procedural.loader import SkillLoader

ROOT = Path(__file__).resolve().parents[2]
# the repo's skills and the NAM knowledge skills the chat loads with them
LOADER = SkillLoader([ROOT / "nam_agent" / "nam" / "skills"])

HINT = ("Anchor the description on words specific to the skill's domain "
        "(DAU, interview, standup) — question words like why / what / should / "
        "for appear in almost every message.")

# Messages that should reach the plain loop with no skill at all. Keep this to
# things no reasonable skill would claim; anything borderline belongs in a
# per-skill decision, not here.
EVERYDAY = [
    "my weight dropped this month, why",
    "why did my package get delayed",
    "what caused the change to my flight",
    "what should I revise in this email",
    "what should I revise in my essay draft",
    "help me revise the pitch deck",
    "did I save the note about Alex",
    "remind me why I booked this",
    "what's the weather tomorrow",
    "tell me a joke",
    "thanks, that's all",
    "translate this sentence into French",
    "what is 17 times 23",
    "summarize this article for me",
    "who won the game last night",
]

# Each skill must be the FIRST one loaded for its own messages. With every
# skill loaded together, this is also what catches a new skill taking over an
# existing skill's messages.
MESSAGES = {
    "nrl-procurement": [
        "which tender mode for goods worth 30 lakh, OTE or LTE",
        "spot purchase threshold at NRL",
        "how is an L1 tie resolved, and the MSE preference",
        "which works contract type: item rate or lump sum",
        "EPC contract LD cap and scope change",
        "QCBS or LCS selection system for a consultancy",
        "when is single source nomination of a consultant allowed",
    ],
    "nam-documents": [
        "explain the PQC criteria for this tender",
        "what turnover and experience should the pre-qualification criteria ask",
        "extract the PQC clauses from this document",
        "what does the SCC say about liquidated damages",
        "explain the special conditions of contract",
        "extract the SCC clauses: warranty and delivery period",
        "check the technical specification of the gasket",
        "which ASME standards and materials should the specification name",
        "extract the BOQ line items with quantity and rate",
        "explain the bill of quantities for this tender",
        "draft the administrative approval note",
        "what goes in an AAN approval note",
        "OCR the scanned pages and keep units",
        "what are the extraction rules for scanned tender pages",
    ],
}


def _loaded(message: str) -> list[str]:
    return [skill.name for skill in LOADER.match(message)]


@pytest.mark.parametrize("message", EVERYDAY)
def test_no_skill_loads_on_an_everyday_message(message):
    loaded = _loaded(message)
    assert loaded == [], f"{message!r} loaded {loaded}. {HINT}"


@pytest.mark.parametrize("skill,message", [(s, m) for s, ms in MESSAGES.items() for m in ms])
def test_each_skill_is_first_for_its_own_messages(skill, message):
    loaded = _loaded(message)
    assert loaded[:1] == [skill], (
        f"{message!r} should load {skill} first, got {loaded}. "
        "If another skill is first, one of the two descriptions is claiming "
        f"words that belong to the other. {HINT}"
    )


def test_every_skill_named_here_still_exists():
    """A renamed or deleted skill must not leave a table row passing vacuously."""
    real = {skill.name for skill in LOADER.skills}
    stale = sorted(set(MESSAGES) - real)
    assert not stale, f"MESSAGES names skills that no longer exist: {stale}"


# A skill split into topics loads only the topics a message names
# (loader.Skill.focus): a PQC question must not carry the BOQ and SCC parts.
TOPICS = {
    "explain the PQC criteria for this tender": ["PQC"],
    "what does the SCC say about liquidated damages": ["SCC"],
    "check the technical specification of the gasket": ["Spec"],
    "explain the bill of quantities for this tender": ["BOQ"],
    "what goes in an AAN approval note": ["AAN"],
    "extract the PQC clauses from this document": ["Rules", "PQC"],
    "which tender mode for goods worth 30 lakh, OTE or LTE": ["Goods"],
    "QCBS or LCS selection system for a consultancy": ["Services"],
}


@pytest.mark.parametrize("message", sorted(TOPICS))
def test_a_topic_skill_loads_only_the_topics_named(message):
    skill = LOADER.match(message)[0]
    loaded = [line[3:].split()[0] for line in skill.focus(message).splitlines()
              if line.startswith("## ")]
    assert loaded == TOPICS[message], (message, loaded)
