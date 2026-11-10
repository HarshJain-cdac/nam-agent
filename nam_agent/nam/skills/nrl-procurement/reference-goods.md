<!-- The full text this skill condenses, kept as written (line endings normalised). Not loaded into a prompt: the condensed SKILL.md beside it is. -->

---
name: procurement-goods
description: >
  NRL (Numaligarh Refinery Limited) rules for procuring goods — modes of
  tendering (OTE/GTE/LTE/STE), direct/departmental/spot purchase thresholds,
  GeM, evaluation and award, and contract/goods-receiving basics. Trigger
  whenever the user asks how to buy or tender for goods/equipment/materials
  at NRL, which tendering route to use, what a purchase threshold is, how L1
  ties or MSE preference work, or how to draft/approve a purchase requisition
  for goods. Source: "Manual for Procurement of Goods" (Rev1, 16.03.2023).
---

# NRL — Procurement of Goods

Read this when the task is about buying goods, equipment, spares, or
materials for NRL — choosing a tendering route, checking a threshold,
handling an L1 tie, or moving a purchase requisition (PR) through Aarohan.
For consultancy/services engagements use `procurement/consultancy-services`;
for civil/mechanical/electrical works contracts use `procurement/works`. For
terms not defined here, check `procurement/glossary.md`.

This is procedural knowledge, not legal advice — always confirm current
thresholds against the live Delegation of Authority (DOA) and any
subsequent amendment before acting; this manual is current only to
October 2022 (Rev1 16.03.2023).

## The Five R's (what "right" procurement means at NRL)

Every procurement decision should balance: **Right Quality** (clear
technical specification, no more/less than needed), **Right Quantity**
(avoid both micro-buys and oversized lots), **Right Price** (not cheapest —
right for quality/quantity, and mind life-cycle cost, not just acquisition
price), **Right Time and Place**, **Right Source** (financially and
technically capable, proven past performance).

## Modes of procurement — pick the route, then check the threshold

**Open Tender Enquiry (OTE) is the default.** Widest competition, best
value for money, but slower. Use anything else only when justified and
approved by the competent authority (CA) under the DOA.

| Mode | When to use | Key conditions |
|---|---|---|
| **OTE** | Default mode for anything not otherwise justified | NIT published on NRL website + tender portal (CPPP/GeM); documents free to download |
| **GTE** (Global Tender) | Goods not available domestically at required spec/quality, no local branch of a global OEM, need for international standards, or suspected domestic cartel | **No GTE below Rs. 200 crore** without a domestic OTE first (issued after 15.05.2020) and CA approval per DPIIT norms; min. 4 weeks to bid opening; INCOTERMS must be specified |
| **LTE** (Limited Tender) | Special/urgent requirement, restricted field of eligible vendors | Min. **3** vendors invited (fewer only with CA approval + recorded reasons); simplified bid document; unsolicited bids from outside the list are ignored |
| **STE** (Single Tender) | Proprietary/OEM item, one specialized source, genuine urgency | Requires PAC/OEM declaration where applicable — see 4.6 |
| **Rate Contract / Framework Contract drawal** | Recurring item already under an RC/FC | Draw directly against the standing contract |
| **Direct procurement without quotation** (cash/imprest) | Off-the-shelf, simple-spec, small-value, urgent and not in the procurement plan | Only up to the DOA threshold; imprest typically sized for ~2 months' estimated spend; HOD must periodically review to catch demand-splitting |
| **Departmental purchase (Purchase Committee)** | Urgent, small-value, with justification | **Up to Rs. 1,00,000 (Rs. 2,00,000 for Mega Projects)** or the current DOA limit; min. 3 quotations; PR via Aarohan to Commercial for SAP PO creation |
| **Spot purchase** | Common/locally available items or services, urgent | 3-officer committee (min. 'A' grade, one each from Commercial/Indenting/Finance); **up to Rs. 10 lakh per visit** or current DOA limit; AMCs and regular services excluded |
| **GeM** | Common-use goods/services on the mandatory GeM catalogue | Direct order from a listed seller, L1 among ≥3 sellers, or online bidding/reverse auction on GeM — governed by GeM's own rules |

**Watch for demand-splitting** — breaking one requirement into smaller buys
to dodge a higher-authority approval or to avoid LTE/OTE is explicitly
flagged for periodic review; don't recommend or normalize it.

## L1 ties and MSE price-band preference

- **Tie among L1 bidders:** split the quantity proportionally (if divisible/
  economical) or resolve by lottery — the method must be stated up front in
  the tender/RFQ. If the winner refuses or fails to perform, the remaining
  quantity goes to the next eligible agency (refusal: within original/
  extended validity; failure: within 1 year of original award).
- **MSE price-band preference:** an MSE quoting within **L1+15%** may match
  L1 and supply a portion of the requirement, when L1 itself is not an MSE.
- Certain categories (Khadi/handloom, ~358 MSE-reserved item categories) are
  reserved for specific sources — check current notifications before
  recommending an open tender for these.

## Preparing, evaluating, and awarding

1. **Need assessment → technical specification → sanctions/approvals →
   procurement plan → initiate procurement** (Chapter 2 flow).
2. **Bid preparation, publication, receipt, opening** — use the relevant
   Standard Bidding Document (SBD); maintain custody/opening procedures.
3. **Evaluation** — comparative statement, technical & commercial
   examination, then decision on award; deliberations recorded.
4. **Award of Contract**, followed by **Contract Management**: goods
   receiving, quality assurance/inspection, storage/issue, accounting and
   payment, handling of any project-surplus material.

## Governance guardrails to flag proactively

- **Code of Integrity for Public Procurement (CIPP)** and, where applicable,
  an **Integrity Pact (IP)** apply to the transaction.
- **Nomination/single-tender awards above Rs. 5 lakh** get Internal Audit
  scrutiny (≥10% check) and quarterly reporting to the Board via Company
  Secretary — flag this when the user is heading toward a nomination-basis
  award of that size.
- Any deviation from a "shall" clause needs the appropriate competent
  authority's approval per the DOA — never assume it's a formality.

## Cross-references

- Delegation of Authority (DOA) monetary limits by post/level: **Annexure 1**
  of this manual — the exact figures are in a table, not prose; verify
  against the current DOA circular before quoting a number to a user.
- Proprietary/OEM declaration procedure: section 4.6 and Annexure 4.
- Abbreviations (OTE, GTE, LTE, STE, PAC, PBG, etc.) and defined terms
  (Bid, Aarohan, etc.): `skills/procurement/glossary.md`.
