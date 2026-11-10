# package.freeze: Freeze the package

FRS: F35, F36
Stage: S6
Gate: freeze_package confirmed by a functional reviewer

## Purpose

Assemble the Need Assessment package from what validation passed, for the freeze.

## Steps

1. Refuse unless the last validation passed after the documents last changed.
2. Collect the generated documents at the versions validated, the compliance list, the validation report and every attachment.
3. The stage machine puts the freeze to a functional reviewer.

## Rules

- R24: the category's mandatory documents are in the package.
- After the freeze nothing goes back.

## Tools

None.

## Outputs

- The package's files and whether it is frozen.
