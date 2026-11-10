# docs.generate: Generate the documents (BOQ, SOR, SCC, PQC, specification)

FRS: F17, F18, F19, F20, F21, F22, F23, F25
Stage: S5
Gate: the drafter approves the clauses and the documents (gates clauses, documents)

## Purpose

Draft the case's mandatory documents from its approved estimate and its selected precedents.

## Steps

1. Take the category's mandatory documents (policy/guardrails.json).
2. Build the estimate, BOQ and SOR line by line from the approved estimate (R19).
3. Put every clause of the selected precedents into the SCC, PQC and specification, verbatim, an identical clause once (R16).
4. Offer the closest other tenders' clauses as suggestions; include one only when the drafter keeps it.
5. Class each clause standard or contract-specific by its words (F22, R17).
6. Collect the contract-specific fields from the case or the officer (F23).
7. Store each document as a .docx, a new version whenever it changes, with its metadata and clause-selection summary (F25).

## Rules

- Quote clauses; never compose an NRL clause. The drafter adds one with its reason.
- Dropping or adding a clause records why (R27).
- A New Item has no rate; its past awardees are names to ask for a quotation, never an approved vendor list (F21).
- No master SCC/PQC repository is connected: the drafter checks that no mandatory clause is omitted (R18).
- A consultancy evaluated by QCBS needs a qualification matrix in the PQC (R29).

## Tools

- `tender_record`
- `clauses`
- `suppliers`

## Outputs

- The `documents` artifact: each document's content, where its clauses came from, the stored .docx and its version.
- Complete once every contract-specific field is given and no required document is empty; until then a draft.

## Goods

- The specification holds the precedents' technical-specification clauses; delivery period and location are the contract fields.

## Services

- The scope of work takes the guided form's scope or the need as stated; contract period and location are the contract fields.

## Works

- The SOR lists each line's schedule and item reference; completion period and location are the contract fields.
