# need.classify: Confirm the category, procurement and tender type

FRS: F4
Stage: S1
Gate: category, procurement type and tender type confirmed

## Purpose

Have the officer confirm the case's classification, one question at a time, and say what the category decides.

## Steps

1. Ask about the first field not confidently settled: category, then procurement type, then tender type.
2. Offer the whole closed vocabulary, with the prediction first and the words that suggested it.
3. Store each answer as the officer's, after checking it against its vocabulary.
4. Once the three are confirmed, offer the sub-type: it picks the guided form for Works (F8).
5. Report the route: the estimate flow, the mandatory documents and the guided form for the category.

## Rules

- R1: the category decides the estimate, the documents and the checks.
- R28: the sub-type is from the category's closed list.
- R3: a confident prediction is not asked about again.
- A tender type is never settled from words alone: the officer always answers it.
- An answer outside the vocabulary is refused, never mapped to the nearest value.
- The case cannot leave S1 until the three fields are confirmed.
- Run after S1 it is rework: the later stages are settled again and their approvals given again. After the freeze it is refused.

## Tools

None.

## Goods

- Sub-types: Codified, Uncodified. The estimate is estimate.goods.

## Services

- Sub-types: Repetitive (AMC, manpower), Consultancy, Non-consultancy. The estimate is estimate.wage.

## Works

- Sub-types: Repetitive, New, Capital / Revenue, Within or Outside impanelment. The estimate is estimate.works.
- New or Repetitive picks the guided form (New Works, Repetitive Works).

## Outputs

- The classification with the basis of each field, which gate fields are still open, and the route.
