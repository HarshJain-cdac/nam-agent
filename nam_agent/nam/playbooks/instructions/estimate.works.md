# estimate.works: Estimate: Works (SOR, DSR, rate analysis)

FRS: F15, F16, F47
Stage: S4
Gate: the officer approves the estimate (gate estimate)

## Purpose

Give every line a rate from a schedule, a match, a rate analysis or past NRL estimates, for the officer to approve.

## Steps

1. The officer's schedule rate with its item reference (`2: rate=245, schedule=nrl_sor, ref=2.1.4`): NRL SOR, DSR, DSR E&M, DAR, APWD SOR or SOR RR.
2. A Similar rate with its match score (`score=0.97`), at or above the configured threshold.
3. A Derived or First Principle rate from its components (`c=labour:120|material:300`).
4. Else the knowledge graph's NRL estimate for the item (internal); else a New Item.
5. Within an empanelment, check every line is NRL SOR (F47).

## Rules

- F13: the header is given for the whole estimate: `all: start=YYYY-MM-DD, end=YYYY-MM-DD, cost_centre=..., location=...` (the location defaults to the case's).
- The estimate gate is proposed only when every line has a rate (a New Item is priced first) and the estimate is of the quantity now finalised.

- R5: Same, then Similar, then internal, then Derived.
- R8, R9, R36: a DSR or DAR rate carries its alert and is not an NRL-approved rate.
- R34: a match below the threshold is not Similar; give the components instead.
- R14: a manday line is priced as wages (basic, non_statutory, insurance, as estimate.wage), and the reimbursements are added as LS lines.

## Tools

- `price_history`

## Outputs

- The `estimate` artifact: each line's rate, basis, schedule, source and amount, the total, and the alerts.
