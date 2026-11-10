# estimate.goods: Estimate: Goods (item rates)

FRS: F13, F16, F48
Stage: S4
Gate: the officer approves the estimate (gate estimate)

## Purpose

Give every line a rate with its basis and source, for the officer to approve.

## Steps

1. The officer's budgetary quotation for a line is its rate (answer `rates`, e.g. `2: rate=450`).
2. Else the knowledge graph's rates for the item, in the hierarchy's order: the NRL estimate (internal), then the awarded rate (po), then a bidder's quote.
3. Compare a quotation with the awarded rate: VARN, then CAGR over the years since, and the R10 flag.
4. A line with no rate is a New Item: the officer gives it.

## Rules

- F13: the header is given for the whole estimate: `all: start=YYYY-MM-DD, end=YYYY-MM-DD, cost_centre=..., location=...` (the location defaults to the case's).
- The estimate gate is proposed only when every line has a rate (a New Item is priced first) and the estimate is of the quantity now finalised.

- R5, R6: an NRL estimate before a PO rate; a PO rate or a quotation is the last resort.
- R7: a historical rate is said to be historical; the KG holds medians, not a single latest rate.
- R10, R38: the VARN/CAGR flag is the rule's, and only a flag; the PO date is known to the year only.
- No rate without a source: a tool result of this run, a number the officer typed, or a policy rule.

## Tools

- `price_history`

## Outputs

- The `estimate` artifact: each line's rate, basis, source and amount, the total, and the alerts.
