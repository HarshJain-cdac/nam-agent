# pr.build: Build the purchase requisition

FRS: F27
Stage: S7
Gate: no mandatory field missing; then the submission (action submit_pr)

## Purpose

Build the PR from what the case already holds, and say what is still missing.

## Steps

1. Fill the header from the case (category, procurement and tender type, date opened, proposal name), the approved estimate (estimated value) and the guided form (justification, delivery date).
2. Ask the officer for the rest: cost centre (the estimate's by default), plant, purchasing group and the optional SAP fields: PR type, account assignment, material group, GL account, item category, MRP controller, order, project, control code, budget head, financial category code (`all: cost_centre=4100, plant=NR01`).
3. Take every line of the approved estimate as an item, with its material code, rate and amount.
4. List every mandatory field still empty (R20); the submission is proposed only when none is.

## Rules

- R20: missing mandatory fields are shown and completed before the PR is created.
- A field the case decides (category, estimate value) is changed where it is decided, not in the PR.
- A Codified item needs its material code.
- The field list is a draft (policy/pr_fields.json) until the bulk-PR-upload Excel is in hand.

## Tools

None.

## Outputs

- The `pr` artifact: header, items, the officer's fields and what is missing.
