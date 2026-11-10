# estimate.wage: Estimate: Manpower (wage rates)

FRS: F14, F16
Stage: S4
Gate: the officer approves the estimate (gate estimate)

## Purpose

Build the rate of every manday line from the basic wage, and the reimbursements, for the officer to approve.

## Steps

1. Take the officer's basic wage and non-statutory percentage per line (answer `rates`, e.g. `1: basic=650, non_statutory=35`).
2. Per manday line: basic + statutory 13.13% + non-statutory 35-40.6% = gross; Sunday and holiday lines x2, then + 15% CP; + any allowance.
3. Append the reimbursements as LS lines: PF/EDLI 13% of basic, ESI 4.75% or WCI 2.79% over the lines that carry each (per line `1: insurance=wci`, or for every line `all: insurance=esi`), PPE 5% and Tools and Tackles 5% of the wages, each rounded up to the nearest Rs 50,000.
4. A reimbursable item at the officer's rate (`3: rate=25000`).

## Rules

- F13: the header is given for the whole estimate: `all: start=YYYY-MM-DD, end=YYYY-MM-DD, cost_centre=..., location=...` (the location defaults to the case's).
- The estimate gate is proposed only when every line has a rate (a New Item is priced first) and the estimate is of the quantity now finalised.

- R12, R39: the percentages are policy, never the model's.
- R13: the reimbursements are computed over the manday lines and rounded up.
- R14: manpower within Works is priced the same way inside the works estimate (estimate.works).
- No basic wage is assumed: it is asked for.

## Tools

None.

## Outputs

- The `estimate` artifact: each line with its components and their sources, the LS lines, and the total.
