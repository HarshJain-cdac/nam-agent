# approval: Route for approval (Aarohan / e-office)

FRS: F37, F38, F45
Stage: S8
Gate: approve confirmed by an approver

## Purpose

Prepare the approval file for the submitted PR, name its approving authority, and generate the ANN.

## Steps

1. Open the Procurix.ai e-office file (no Aarohan connector); link an Aarohan file made by hand (answer `aarohan_file`).
2. Find the approving authority from the DoA (policy/doa.json); while it has no levels, the approver names it (answer `authority`).
3. Generate the Administrative Approval Note as a .docx.

## Rules

- R4: the estimated value, and the DoA level it picks, are provisional.
- The approval itself is an irreversible action, confirmed by an approver.

## Tools

None.

## Outputs

- The file, the approving authority and its basis, and the ANN.
