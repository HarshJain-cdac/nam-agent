# quantity.finalise: Finalise the quantity and scope

FRS: F10
Stage: S3
Gate: the officer approves the quantity (gate quantity)

## Purpose

Settle the case's lines, each with its quantity and unit, for the officer to approve.

## Steps

1. Seed the lines once: every BOQ line of the selected precedents, with the quantity that tender ordered, or the line the guided form described.
2. Apply the officer's edits: change, add or remove lines (answer `lines`, e.g. `2: qty=40; add: item=..., qty=5, uom=Nos`).
3. For a line with no quantity, show what NRL ordered before (quantity_history). Never apply it.
4. Once every line has a positive quantity and a unit, the lines are complete and the quantity gate is put to the officer.

## Rules

- A quantity taken from a precedent is historical: the officer confirms or changes it before approving.
- An edit that cannot be read changes nothing and says why.
- A Codified item needs its material code.

## Tools

- `tender_record`
- `quantity_history`

## Outputs

- The `quantity` artifact: the lines, each with its source, and whether they are complete.
