# need.enrich: Enrich the case from the enterprise record

FRS: F9
Stage: S2
Gate: none

## Purpose

Read what the enterprise record holds about the need before the PR. SAP and Aarohan are not connected, so the record is the knowledge graph's: the fields of the selected precedents, or of the closest related tenders.

## Steps

1. Read the enrichment fields of the selected precedents, or of up to 5 related tenders while none is selected.
2. Read a tender the officer names (answer `ref`), after resolving it.
3. List the SAP fields the record cannot give, for pr.build to ask.

## Rules

- R2: a tender read here is shown, never selected.
- R7: the EMD and the estimated value are history, never the case's own.

## Tools

- `enrichment`
- `resolve_reference`

## Outputs

- The tender records read, with their fields and awardees; the SAP fields still missing.
