# case.ask: Answer a question without a step

FRS: F5
Stage: any
Gate: -

## Purpose

Answer the words that need no step: a question about the case, past tenders, past prices or a NAM term, a greeting, or "approve it", without changing anything.

## Steps

1. Read what the message asks: a term, past tenders, past prices, the case's estimate, quantity, documents, precedent, or where it stands.
2. Put the answer together in code from the glossary, the KG (`tenders`, `price_history`) or the Need Record and its history.
3. Give each fact its source and authority, and suggest the step that would act on it.

## Rules

- Writes nothing: no record, no artifact, no approval.
- An approval is never given in words: "approve it" is answered with the pending action and the roles that may confirm it.
- Past prices are medians of past tenders, never a current rate (R7); selecting a precedent stays the officer's (R2).
- A knowledge graph that cannot be reached is said, not hidden.

## Tools

- `tenders`
- `price_history`

## Outputs

- The answer, its facts with source and authority, and the suggested steps.
