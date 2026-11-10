# material.suggest: Suggest materials and specifications

FRS: F11
Stage: S3
Gate: officer satisfied

## Purpose

Suggest the materials or services, with their specification, that similar past tenders bought, for the officer to pick.

## Steps

1. Search the past tenders' line items for the need, with any words the officer added (answer `refine`).
2. Read the specification clauses of the closest tenders verbatim.
3. The officer picks (answer `pick`, '2: use=yes'); a picked item becomes a line in quantity.finalise.

## Rules

- Nothing is picked for the officer.
- A specification is quoted from a tender, never written.

## Tools

- `tenders`
- `tender_record`

## Outputs

- The suggestions with their source tender and specification, and which are picked.
