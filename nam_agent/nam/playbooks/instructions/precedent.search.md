# precedent.search: Find similar past tenders

FRS: F6
Stage: S2
Gate: the officer validates the relevant precedent

## Purpose

List past tenders and PRs similar to the need, for the officer to choose from (F6).

## Steps

1. Search with the need's search words, plus any words the officer adds ("try another with SS316").
2. Look up any PUR case id or tender number the officer gives.
3. When the word search finds nothing, ask the knowledge graph for the closest tenders by meaning, and say when that match is weak.
4. Rank by match strength and show why each one matched.
5. Offer the list as a question: pick one or more with precedent.select, or none (need.guided_form).

## Rules

- R4: a similar case is close, not exact; show the match basis, never claim more.
- R2: present and rank only; the officer chooses. Nothing is selected here.
- Every tender shown is recorded on the shortlist: only a tender shown can be selected.

## Tools

- `tenders`
- `resolve_reference`

## Outputs

- A ranked list of similar tenders with the match basis for each, and no selection.
- The `shortlist` artifact: every tender shown for the case.
