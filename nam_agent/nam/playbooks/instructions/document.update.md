# document.update: Upload an edited document

FRS: F39
Stage: S9, S10
Gate: nothing awaited

## Purpose

Bring back the documents edited outside NAM, measure how much each edit changed, and track the ones still awaited.

## Steps

1. The edited file is uploaded as a new version through the document endpoints.
2. Compare each newer version with the frozen one: accuracy 100% when unchanged, else the share kept.
3. List files added after the freeze, such as a corrigendum.
4. Track the documents the officer awaits (answer `expect`).

## Rules

- R32: an awaited document holds the handoff.
- R40: every version is kept for learning, under the store's retention.

## Tools

None.

## Outputs

- The updates with their accuracy, the added files, and what is awaited.
