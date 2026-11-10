# validate: Validate the PR and documents

FRS: F30, F31, F32, F33, F34, F46
Stage: S5, S6
Gate: discrepancies corrected and revalidated

## Purpose

Check the documents and the PR for format, completeness and consistency, their integrity, and how they changed, in one consolidated report.

## Steps

1. Check each document is stored, reads back and is not empty (format).
2. Check the mandatory documents, fields, quantities and PR fields (completeness).
3. Check the BOQ and SOR against the estimate, the estimate against the quantity, and each contract term stated alike (consistency).
4. Check each file's hash and the audit log's chain (integrity).
5. Compare with the last validation and with the precedents (versions).
6. Store the report as a .docx.

## Rules

- R22: format, completeness, consistency, integrity and versions are each checked; the manual and circulars are not connected, and the report says so.
- R23: a failure is corrected in its flow and validated again.

## Tools

None.

## Outputs

- One consolidated report with its status (pass or fail).
