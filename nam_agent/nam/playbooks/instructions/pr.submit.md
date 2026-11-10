# pr.submit: Submit the PR to SAP

FRS: F28, F29
Stage: S7, S8
Gate: the submission is confirmed by an allowed role (action submit_pr)

## Purpose

Show where the PR's submission stands and link its number to the case.

## Steps

1. Before the submission: show the pending action; the stage machine runs it once confirmed.
2. After it: show the PR number drawn (PRX-...), since there is no SAP connector.
3. Record the SAP PR number when the PR was also created in SAP by hand (`sap_pr`), then its SAP status (`sap_status`, e.g. Released).

## Rules

- The submission is an irreversible action: only the stage machine runs it, once, for an allowed role.
- A PR item already on another open PR is refused, with no override.
- A SAP PR number is 8 to 12 digits, and belongs to one case only.

## Tools

None.

## Outputs

- The PR number, and the `pr_reference` artifact with the SAP PR number once given.
