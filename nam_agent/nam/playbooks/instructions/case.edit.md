# case.edit: Edit the case

FRS: F12, F42
Stage: any, before the freeze
Gate: every change has its reason

## Purpose

Change the case's own fields (location, term, quantity, sub-type), each with the reason, and show the history of every change.

## Steps

1. Read the edit (answer `edit`, 'all: location=..., why=...').
2. Refuse a field another flow owns, naming that flow.
3. Save the change with its old value, new value and reason.

## Rules

- F42: every change needs why=..., kept in the case history.
- After the freeze nothing changes.

## Tools

None.

## Outputs

- The fields, the changes made, and the case history.
