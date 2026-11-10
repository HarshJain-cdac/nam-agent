# docs.review: Review the drafts

FRS: F24
Stage: S5
Gate: conflicts fixed or accepted

## Purpose

Flag the clauses in the drafts that conflict or leave a term open, before the documents are approved.

## Steps

1. Read every selected clause and the contract-specific fields.
2. Flag a term stated with different figures, vague wording, and unclassified clauses.
3. Ask the review model for more, keeping only findings about clauses it was shown.
4. The drafter fixes a finding in docs.generate, or accepts it with a reason (answer `accept`).

## Rules

- A conflict not fixed or accepted keeps validation from passing.
- The model never rewrites a clause.
- The PQC, SCC and specification checks of the NAM skills (`nam_agent/nam/skills/`) run in code, as warnings: a PQC threshold off the norms (`policy/pqc_norms.json`, DRAFT) or a fixed rupee figure copied from a precedent, liquidated damages without a rate and a cap, a performance security without a percentage, a make without "or equivalent". The review model is given the same skills' checks.

## Tools

None.

## Outputs

- The numbered findings, what was accepted and why, and the open conflicts.
