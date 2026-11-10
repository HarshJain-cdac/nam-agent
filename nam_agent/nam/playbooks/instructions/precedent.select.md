# precedent.select: Select the precedent tender

FRS: F7
Stage: S2
Gate: the officer validates the relevant precedent

## Purpose

Record which past tenders the officer selected as precedent (F7).

## Steps

1. Take the tenders the officer named: the answer to the `precedent` question, or the PUR case ids and tender numbers in the message.
2. Check that each was shown to the officer (the shortlist, or the related tenders need capture found).
3. Read each one's record and report what it carries: BOQ lines, PQC, SCC and specification clauses.
4. Record them as the case's selected precedent, replacing the previous selection. "none" clears it.

## Rules

- R2: generation downstream reads only the selected tenders. Nothing is selected by the model.
- A tender that was not in the list shown to the officer is refused, and nothing is selected.
- A tender number that matches several tenders is asked about, never picked.
- On a case past S2 a new selection is rework: the later stages and their approvals are done again.

## Tools

- `resolve_reference`
- `tender_record`

## Outputs

- The selected precedent on the Need Record and a summary of the material each selected tender carries.
