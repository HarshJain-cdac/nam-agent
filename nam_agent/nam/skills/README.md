# NAM knowledge skills

What the NAM harness knows about a procurement case, as two skills an officer
can read and correct. Each folder holds one `SKILL.md` in the NAM harness's skill format
(`name`, `description`) plus the NAM field `flows`, the steps that use it.

| skill | topics |
|---|---|
| `nam-documents` | `rules` (every extraction), `pqc`, `scc`, `spec` (technical specification), `boq` (with SOR), `aan` (Administrative Approval Note) |
| `nrl-procurement` | `goods` (tender modes, purchase thresholds, L1 ties, MSE preference), `works` (contract types, sanction sequence, execution), `services` (selection systems, nomination) |

A topic is a `## ` section whose first line is `terms: ...`, the words that
name it. A document topic is written in four parts, `What:`, `Extract:`,
`How:` and `Checks:`. The text before the first topic is shared and loads
with any of them.

`nrl-procurement` condenses NRL's procurement manuals (Rev1, 16.03.2023);
the full text sits beside it in `reference-goods.md`, `reference-works.md`
and `reference-services.md`, which are never loaded into a prompt.

The NAM harness chat agent loads a skill when a message shares two words with its
name and description, and then only the topics the message names
(`nam_agent/memory/procedural/loader.py`): a PQC question loads about 400 tokens,
not the whole skill. `evals/deterministic/test_skill_budget.py` holds the
budgets (shared part 250, a topic 550, all one message loads 1,400 tokens);
`evals/deterministic/test_skill_triggers.py` checks each message loads the
right skill and topic, and that everyday messages load none.

`nam_agent/nam/skillset.py` serves the topics to the harness as
`<skill>/<topic>`: `case.ask` answers from them and cites
`skill:nam-documents/pqc`, `docs.review` gives the review model their checks,
and `nam_agent/nam/extract.py` does each document topic's "Extract" in code. A
change to an "Extract" part needs the matching change in `extract.py` and its
eval (`evals/deterministic/nam/test_nam_skills.py`).
