<!-- The full text this skill condenses, kept as written (line endings normalised). Not loaded into a prompt: the condensed SKILL.md beside it is. -->

---
name: procurement-consultancy-services
description: >
  NRL (Numaligarh Refinery Limited) rules for procuring Consultancy and
  Other (non-consultancy) Services — selection systems (LCS, QCBS, SSS,
  FBS), shortlisting via EoI, Terms of Reference, RfP evaluation, and
  single-source/nomination rules. Trigger whenever the user asks how to
  engage a consultant, service provider, or specialized agency at NRL,
  which selection method to use, how QCBS weighting works, or when
  single-source selection is justified. Source: "Manual for Procurement of
  Consultancy & Other Services" (Rev1, 16.03.2023).
---

# NRL — Procurement of Consultancy & Other Services

Read this when the task is about engaging a consultant, service provider,
NGO, specialized agency, or individual expert for NRL. For buying goods use
`procurement/goods`; for construction/EPC contracts use `procurement/works`.
Undefined terms: check `procurement/glossary.md`.

Procedural knowledge only — confirm current thresholds against the live DOA
before acting; content current to October 2022 (Rev1 16.03.2023).

## Consultancy services vs. "other services" — different tracks

- **Consultancy services** (quality/scope not tangibly measurable up front)
  go through the two-stage process below: shortlist → technical + financial
  proposal → selection system.
- **Other (non-consultancy) services** are normally procured like Goods/
  Works — lowest price (L1) among technically responsive offers. QCBS is
  only used for these if the work is genuinely technical/complex (e.g.
  seismic surveys, airborne data acquisition) — in which case treat it as a
  consultancy contract instead.

## The two-stage consultancy process

1. **Shortlist (EoI)**: advertise, collect Expressions of Interest, shortlist
   **3–8** qualified firms against relevant qualification/experience criteria.
2. **RfP**: shortlisted firms submit **separate sealed** technical and
   financial proposals. Technical proposals are evaluated **before** any
   financial envelope is opened.
3. **Select** using one of the systems below, negotiate, and award.

## Selection systems — pick based on how much quality vs. price matters

| System | Mechanics | Use when |
|---|---|---|
| **LCS** (Least Cost Selection) | Technical proposal must clear a pass/fail or minimum-marks benchmark (commonly ~75/100); among technically qualified bids, **L1 wins on price alone** — no weighting of technical score | **Default** for standard/routine assignments (e.g. non-complex engineering design); justification required to use anything else instead |
| **QCBS** (Quality & Cost Based Selection) | Minimum qualifying technical score (typically **70–80/100**); technically qualified bids get a cost score (100 for lowest, pro-rated for others); total = weighted technical + cost score | Complex/critical assignments justifying a quality premium. Suggested weightings: **80/20** (technical/cost) for highly complex/specialised work, **75–65 / 35–25** for moderate complexity, **60–50 / 40–50** for standard/routine (where LCS is usually more appropriate anyway) |
| **SSS / STE** (Single Source Selection, nomination) | Direct engagement, justified in the file and approved by the CA | Emergency/disaster situations; proprietary/OEM-only expertise; only one consultant has the requisite expertise; using another PSU/Government body's in-house expertise. **Contracts above Rs. 5 lakh get Internal Audit review (≥10%) and quarterly Board reporting** (two lists: up to Rs. 5 lakh, and above) |
| **FBS** (Fixed Budget Selection) | Cost is fixed in the tender document itself; select either by highest technical score within budget, or by empanelment for repetitive assignments | Simple/repetitive, well-estimable scope where the budget itself can be reasonably fixed |

**Single-offer situations against an open tender are still valid** (no need
to force a re-bid) provided: the tender was properly publicized with
adequate response time, qualification criteria weren't unduly restrictive,
and the price is reasonable versus market rates.

## Contract types for consultancy

- **Lump Sum (Firm Fixed Price)** — well-defined scope.
- **Time-Based (Retainer-ship)** — pay for time/input, not fixed output.
- **Percentage Contract** — fee as a % of project cost.
- **Indefinite Delivery Contract (Price Agreement)** — standing arrangement
  for services drawn down over time.

## Governance guardrails to flag proactively

- CIPP and Integrity Pact obligations apply, same as Goods/Works.
- **Nomination-basis awards above Rs. 5 lakh** trigger Internal Audit ≥10%
  review and quarterly Board reporting via Company Secretary — flag this
  whenever a user is heading toward single-source selection at that scale.
- Don't let a genuinely technical/complex "other service" get procured on
  pure L1 if quality risk is real — point the user toward treating it as a
  consultancy contract with QCBS instead.

## Cross-references

- Percentage-rate / manpower-oriented wage-display rules: section 9.4.6 of
  this manual, cross-referenced from `procurement/works` §3.2.3.
- Proprietary/OEM declaration: see `procurement/goods` §4.6 and Annexure 4.
- Abbreviations and defined terms: `skills/procurement/glossary.md`.
