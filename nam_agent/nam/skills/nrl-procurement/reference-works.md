<!-- The full text this skill condenses, kept as written (line endings normalised). Not loaded into a prompt: the condensed SKILL.md beside it is. -->

---
name: procurement-works
description: >
  NRL (Numaligarh Refinery Limited) rules for procuring civil/mechanical/
  electrical Works — contract types (lump sum, item rate, percentage rate,
  ARC, EPC, PPP), modes of tendering, bid evaluation and award, execution/
  monitoring, and dispute handling. Trigger whenever the user asks about a
  construction, EPC, or works contract at NRL — which contract type fits,
  which tendering route to use, GTE thresholds, or how execution/quality
  monitoring and closure work. Source: "Manual for Procurement of Works"
  (Rev1, 16.03.2023).
---

# NRL — Procurement of Works

Read this when the task involves a civil, mechanical, electrical, or
construction-type Works contract at NRL. For buying goods/equipment use
`procurement/goods`; for consultancy/engineering-services engagements use
`procurement/consultancy-services`. Undefined terms: check
`procurement/glossary.md`.

Procedural knowledge only — confirm current thresholds against the live DOA
before acting; content current to October 2022 (Rev1 16.03.2023).

## Choosing the contract type

The right contract type is a Value-for-Money decision tied to how well the
scope and site conditions are understood up front — the wrong choice causes
disputes, non-performance, or cost overrun.

| Contract type | Fits when | Key mechanics |
|---|---|---|
| **Lump sum (Fixed Price)** | Scope, design, and functional requirements are fully defined (standard/repetitive buildings, minor bridges, chimneys, tanks) | Single fixed price; no arithmetical correction during evaluation/execution; detailed measurement not required except for additions/omissions; still monitor progress monthly since payment isn't time-linked |
| **Item rate (Unit rate)** | Most major works (buildings, bridges, roads, sewer lines) — the default for civil works | Contractor quotes rate per BOQ item; quantity can reasonably vary (commonly **±15% per item**); lowest overall risk/uncertainty |
| **Percentage rate** | Small/routine original works or repair work where detailed design isn't needed (e.g. drainage, water supply, sewer lines) | Contractor quotes a single % above/below the estimated cost (built from an adopted SOR, e.g. CPWD/APWD, with applicable indices) |
| **Annual Rate Contract (ARC)** | Recurring/repetitive jobs needing more than one supplier for continuity | SOR derived from ~2-year usage trend; usually split 50:30:20 (or similar) across L1/L2/L3; validity ~2 years + 1-year renewal option |
| **EPC ("Design & Build")** | Large projects where design + construction risk should sit with the contractor for cost/time certainty | Output-spec based, lump-sum via competitive bidding; LD for delay = **0.5%/week, capped 5%** of contract price; scope-change ceiling **10%**; optional completion bonus up to ~5%; deviations from the "shall" clauses in this section need **Board approval** |
| **PPP** | Long-term public asset/service delivery with a private concessionaire | Concessionaire selected via transparent competitive bidding; performance-linked payments; refer to DEA/Ministry of Finance PPP instructions |

## Modes of tendering (same ladder as Goods, applied to Works)

| Mode | When | Key conditions |
|---|---|---|
| **OTE** | Default — widest competition | NIT on NRL website + CPPP/GeM; documents free to download |
| **GTE** | No suitable domestic capability, need for international standards, cartel concerns | **No GTE below Rs. 200 crore** without a prior domestic OTE (post-15.05.2020) and CA/DPIIT-aligned approval; min. 4 weeks to bid opening |
| **LTE** | Restricted field, special requirement | Preselected vendor list; simplified bid document |
| **STE** | Proprietary/specialized/urgent case | Justify and record before use |
| **Award through Quotations** (Departmental / Spot) | Small-value, urgent, local | Same Departmental/Spot-purchase thresholds and committee structure as `procurement/goods` |

## Bid process and evaluation

1. **Estimates first**: perspective planning → Preliminary/Rough Cost
   Estimate → in-principle approval → Detailed Project Report/Preliminary
   Estimate → Administrative Approval & Expenditure Sanction (A/A, E/S) →
   detailed design, detailed estimate, technical sanction → fund
   appropriation → procurement planning → initiate procurement (PR).
2. **Bid documents**: prepared, published, clarified/amended as needed;
   pre-bid conference if warranted; bids received and opened per procedure.
3. **Evaluation**: comparative statement + briefing note → preliminary
   examination → evaluation of responsive bids → Tender Evaluation
   Committee deliberations → award of work.
4. **Execution**: monitoring, quality assurance, design approvals, time and
   financial monitoring, commissioning/documentation, contract closure.
5. **Disputes**: conciliation → arbitration per the mechanism in the
   contract; breach/remedies/termination as specified.

## Governance guardrails to flag proactively

- Same CIPP / Integrity Pact obligations as Goods procurement.
- Contractor registration/enlistment and holiday-listing/debarment rules
  apply — check a contractor's standing before recommending direct award.
- EPC "shall" clauses (LD cap, scope-change ceiling, etc.) require **Board
  of Directors** approval to deviate from — don't suggest a workaround here
  without flagging that escalation need.

## Cross-references

- Delegation of Authority (DOA) limits by post/level: verify against the
  current DOA circular, not this manual's prose references.
- Manpower-oriented / percentage-rate wage-display rules: see section 9.4.6
  of the Consultancy & Other Services manual (`procurement/consultancy-services`).
- Abbreviations and defined terms: `skills/procurement/glossary.md`.
