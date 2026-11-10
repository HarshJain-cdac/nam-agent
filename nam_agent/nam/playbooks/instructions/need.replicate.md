# need.replicate: Copy a past tender, PR or PO

FRS: F2
Stage: S1
Gate: goes to Edit/Review (case.edit)

## Purpose

Open a case as an exact copy of a named past tender, PR or PO (create by copy).

## Steps

1. Resolve the reference number the officer gave to one record.
2. Copy scope, items, BOQ, specifications, quantities, rates, SCC and PQC exactly as recorded.
3. Mark the source tender as selected precedent: naming it is the officer's explicit choice.
4. Hand the case to Edit/Review for the officer's changes.

## Rules

- Copy exactly; never merge in a second record.
- A reference that resolves to nothing or to several records is asked about, never guessed.
- A PR or PO number needs the SAP connector (not connected yet): ask for the tender instead.
- Everything is read before the case opens, so a failed read leaves no half-made case.
- Rates copied from the old record are shown as historical; the estimate step proposes the latest rate (R7).

## Tools

- `resolve_reference`
- `tender_record`
- `documents`

## Outputs

- A new Need Record with the copied fields, each marked with its source record, and the source as selected precedent.
- The `replica` artifact: fields with their source, the BOQ with its rates marked historical, the clauses verbatim, SOR references, awardees and files, and whether the copy is complete.
