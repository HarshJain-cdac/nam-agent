# case.plan: Make a to-do list and complete it step by step

FRS: F5
Stage: any
Gate: each step's own gates and actions, held by the stage machine

## Purpose

Turn the user's query into a to-do list of NAM steps and complete it one step at a time, each step verified on the case before the next.

## Steps

1. Read the goal from the query: the estimate, the documents, the validation, the PR, the approval, the upload, or the NIT handoff (the default).
2. List the steps from the case's stage to the goal in stage order (S1-S10), and store the list on the case (`todo`).
3. Run the next step that is not done, as its own call would run it, and verify it on the case.
4. Go on while each step verifies; stop at a question, an approval, a step for another role, or a refusal, and say which and why.
5. On "continue" (with the answer, if a question was asked), re-read the list against the case and go on from there.

## Rules

- A step is done only when the case shows it done: verified, its own check holding on the stored case, or the case past its stage.
- The plan decides nothing itself: every approval is still a user's, in an allowed role, through the stage machine.
- A rework that takes the case back makes the steps from there pending again.
- When no precedent fits, the guided form is added to the list.
- At most 12 steps run in one call.

## Tools

None.

## Outputs

- The to-do list with each step's status and why, the steps run in this call with their verification, and where it stopped.
