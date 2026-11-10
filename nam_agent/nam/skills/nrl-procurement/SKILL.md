---
name: nrl-procurement
description: NRL procurement manual: goods tender mode OTE GTE LTE STE GeM, spot purchase, departmental purchase threshold, L1 tie, MSE preference; works contract type lump sum, item rate, percentage rate, ARC, EPC cap, scope; consultancy selection system QCBS LCS SSS FBS, single source nomination, consultant.
flows: need.classify, estimate.works, estimate.wage, case.ask, approval
---

Source: NRL Manuals for Procurement of Goods, Works, Consultancy & Other Services (Rev1, 16.03.2023, current to Oct 2022); full text in reference-goods.md, reference-works.md, reference-services.md. Name the mode, type or system, its condition and the manual section; check every figure against the live DoA; never invent one.
All: CIPP and Integrity Pact; never split a demand to dodge an approval or a mode (flag it); a "shall" deviation needs DoA approval (EPC: the Board); nomination or single tender > Rs 5 lakh -> Internal Audit checks >= 10% and a quarterly Board report.

## Goods
terms: goods procurement, goods, tender mode, mode of tendering, ote, gte, lte, ste, gem, spot purchase, departmental purchase, l1 tie, tie, purchase threshold, demand splitting, mse, purchase preference
OTE is the default; any other mode needs CA approval under the DoA.

| mode | when | conditions |
|---|---|---|
| OTE | default | NIT on NRL site + CPPP/GeM; documents free |
| GTE | not available domestically, international standard, OEM without Indian branch, cartel | below Rs 200 crore only after a domestic OTE (post 15.05.2020) + CA/DPIIT approval; >= 4 weeks to opening; INCOTERMS stated |
| LTE | special or urgent, restricted field | >= 3 vendors (fewer: CA approval + reasons); unsolicited bids ignored |
| STE | proprietary/OEM, one source, genuine urgency | PAC/OEM declaration (s4.6, Annex 4) |
| RC/FC drawal | item under a rate or framework contract | draw against it |
| direct, no quotation | small, simple, urgent, unplanned | DoA limit; imprest ~2 months' spend; HOD reviews for splitting |
| departmental | urgent, small | <= Rs 1,00,000 (Rs 2,00,000 Mega Projects) or DoA; >= 3 quotations; PR via Aarohan to Commercial for the SAP PO |
| spot | common, local, urgent | 3 officers >= A grade (Commercial, Indenting, Finance); <= Rs 10 lakh a visit or DoA; no AMC or regular services |
| GeM | GeM catalogue item | direct order, L1 of >= 3 sellers, or bid/RA |

- Five R's: right quality, quantity, price (life-cycle cost), time and place, source.
- L1 tie: split the quantity (if divisible) or draw lots, as the tender stated; refusal within validity or failure within 1 year -> next eligible bidder.
- MSE within L1+15% may match L1 for a share when L1 is not an MSE; reserved items (Khadi, handloom, ~358 MSE items): check notifications.
- Flow: need -> specification -> sanction -> plan -> bid (SBD) -> evaluation (recorded comparative statement) -> award -> receipt, inspection, storage, payment.

## Works
terms: works contract, works, contract type, lump sum, item rate, percentage rate, annual rate contract, arc, epc, ppp, technical sanction, expenditure sanction
Fit the type to how well scope and site are known.

| type | fits | mechanics |
|---|---|---|
| lump sum | scope and design fully defined (standard buildings, tanks, chimneys) | one fixed price; no arithmetic correction; measure only additions/omissions; monitor monthly |
| item rate (civil default) | most major works | rate per BOQ item; quantity may vary, commonly +/-15% per item |
| percentage rate | small routine or repair | one % above/below an estimate from an adopted SOR (CPWD/APWD + indices) |
| ARC | recurring jobs, several suppliers | SOR from ~2 years' usage; ~50:30:20 across L1/L2/L3; ~2 years + 1-year renewal |
| EPC | large projects, design-build risk on the contractor | output spec, lump sum; LD 0.5%/week capped 5%; scope change <= 10%; bonus up to ~5% |
| PPP | long-term asset or service, concessionaire | competitive selection; performance-linked payment; DEA/MoF instructions |

- Modes as for goods; LTE from a preselected list; departmental/spot at the goods thresholds.
- Before the PR: rough estimate -> in-principle approval -> DPR -> A/A and E/S -> design, detailed estimate, technical sanction -> funds -> plan.
- Evaluation: comparative statement + briefing note -> responsiveness -> TEC -> award. Execution: monitoring, QA, design approval, time and cost, commissioning, closure. Disputes: conciliation -> arbitration per contract.
- Before any direct award: contractor enlistment, holiday listing, debarment.

## Services (consultancy and other services)
terms: consultancy, consultant, services, selection system, lcs, qcbs, sss, fbs, eoi, rfp, terms of reference, single source, nomination
- Consultancy (quality not measurable up front): EoI shortlist of 3-8 firms -> RfP, technical and financial proposals sealed separately -> technical evaluated before any financial opening -> selection -> award.
- Other services: L1 among responsive offers; a genuinely technical one (seismic survey, airborne data) is treated as consultancy (QCBS).

| system | mechanics | use |
|---|---|---|
| LCS (default) | technical pass mark (~75/100), then L1 on price | standard, routine |
| QCBS | technical minimum 70-80/100; lowest cost scores 100, others pro rata; weighted total | 80/20 highly complex, 75-65/35-25 moderate, 60-50/40-50 routine (LCS usually better) |
| SSS (nomination) | direct, justified in file, CA approval | emergency, proprietary/OEM expertise, sole expert, another PSU's in-house expertise |
| FBS | budget fixed in the tender; best technical within it, or empanelment | simple, repetitive, well-estimable |

- A single offer on an open tender stands if well publicised with time to respond, criteria not restrictive, price reasonable.
- Contracts: lump sum, time-based (retainer), percentage of project cost, indefinite delivery (price agreement).
- Never buy a quality-risky service on pure L1. Manpower wage display: s9.4.6.
