# handoff.nit: Hand off to the NIT module

FRS: F41, F43, F44
Stage: S10
Gate: handoff_nit confirmed by the Commercial Department

## Purpose

Assemble the package and the case's structured data for the NIT module, so nothing is typed in again.

## Steps

1. Collect the frozen package at its latest versions, with the files added since.
2. Collect the structured data: category, types, items, estimate, fields, compliance, precedents, PR, approval and Aarohan references.
3. The stage machine puts the handoff to the Commercial Department; after it the case is closed.

## Rules

- R32: nothing goes to NIT while an approval, a validation or a mandatory document is missing, or a document is awaited.
- R33: NAM ends at the handoff.

## Tools

None.

## Outputs

- The handoff package and its structured data.
