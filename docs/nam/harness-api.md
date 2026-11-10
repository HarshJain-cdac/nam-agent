
# NAM harness integration guide

This guide is for the Procurix.ai backend team. It covers how to run the Need
Assessment Module (NAM) harness, how to call it from many places the way you
would call an LLM, and what comes back.

The harness API is `/v1/nam/*` on the NAM harness server (`nam-agent serve`,
port 7777), next to the chat (`/api/chat`) that
[../backend-integration.md](../backend-integration.md) describes. The backend sends a short
system prompt ("Assess need for request #123") and the user's messages. The
harness works out the NAM step, runs it with its own FRS rules, gates and
checks, and returns one schema-valid JSON reply. It does not authenticate
anyone, and it does not hold the frontend.



## 1. Run it

```
nam-agent serve         # or: python -m nam_agent serve [--host H] [--port P]
```

It listens on `127.0.0.1:7777` by default. Settings come from the environment:

| Variable                                             | Default                                                                                                        | What it sets                                                                                                                                                                                                                                                                                                                                                                                          |
| ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `NAM_HOST`, `NAM_PORT`                           | `127.0.0.1`, `7777`                                                                                        | where it listens; keep it private (section 2)                                                                                                                                                                                                                                                                                                                                                         |
| `NAM_KG_URL`                                       | none                                                                                                           | the NRL knowledge graph's MCP server, e.g. `http://<mcp-host>:8091/mcp` (read through `nam_agent/nam/kg_mcp.py`)                                                                                                                                                                                                                                                                              |
| `NAM_KG_TOOLS`                                     | every KG tool                                                                                                  | the KG tools the agent is given, comma separated; `sap_search` tests SAP's PR, PO and service lines alone, and NAM's need capture (which reads the tenders) steps aside                                                                                                                                                                                                                             |
| `NAM_SOURCE`                                       | `tender`                                                                                                     | where need capture reads from: `tender` narrows the past tenders; `sap` reads SAP's PR, PO and service lines (`sap_search`), asks questions built from their material descriptions and codes, and ends at one material with its last PR, PO and PUR case (`nam_agent/nam/sap_need.py`). With `NAM_KG_TOOLS=sap_search` alone, need capture is off and the agent answers with `sap_search` |
| `NAM_PROVIDER`, `NAM_MODEL`, `NAM_SMALL_MODEL` | the provider's defaults                                                                                        | the model provider and its two models                                                                                                                                                                                                                                                                                                                                                                 |
| `NAM_MODELS`                                       | small model for `route`, `intent` and `domain`, main model for `followup`, `review` and `quantity` | the model per task, in the order tried: `route=m1,m2; followup=m3; review=m4`                                                                                                                                                                                                                                                                                                                       |
| `NAM_STOP_AT`                                      | `tender`                                                                                                     | how far a case is led: `tender` stops at the past tender that matches (need capture asks no detail question, and the chat asks no tender type, sub-type, quantity, estimate or PR question); `pr` runs the whole flow to the submitted PR. The later steps' screens and flows still run when called                                                                                               |
| `NAM_DOMAIN_ONLY`                                  | on where `NAM_KG_URL` is set                                                                                 | the chat answers only NRL procurement work (`nam_agent/nam/domain.py`): word rules first, then the small model's `domain` task (`work` or `other`); anything else gets one fixed reply. A slow or absent model lets a message through. `0` turns it off, `1` on anywhere                                                                                                                  |
| `NAM_RATE`                                         | `120`                                                                                                        | calls per tenant and user per minute (`0`: off)                                                                                                                                                                                                                                                                                                                                                     |
| `NAM_MAX_BODY`                                     | 1 MiB                                                                                                          | the largest `/run` body                                                                                                                                                                                                                                                                                                                                                                             |
| `NAM_MAX_DOC`                                      | 25 MiB                                                                                                         | the largest document upload                                                                                                                                                                                                                                                                                                                                                                           |
| `NAM_OCR_BACKEND`                                  | none                                                                                                           | the OCR engine, `module:Class` (`nam_agent/nam/ocr/base.py`)                                                                                                                                                                                                                                                                                                                                      |
| `NAM_OCR_MIN_CHARS`                                | `20`                                                                                                         | letters and digits a page needs to count as text                                                                                                                                                                                                                                                                                                                                                      |

At start it prints the model it will use for each task. `GET /v1/nam/health`
shows the same, with whether the KG answers, and the knowledge sources: each
with its authority (`precedent`, `nrl_schedule`, `reference`, `policy`,
`officer`) and whether it is connected (`nam_agent/nam/knowledge.py`).

With no model configured, the harness still works. Routing asks the caller
which step to run when the prompt is ambiguous, follow-up questions come from
the tenders, and the review keeps to its code checks. With no KG, the flows
that read precedent say so and stop with 502 `kg_unavailable` (retryable).

## 2. Identity and access

Authentication belongs to the backend's own auth component. That
component authenticates the user, then sets three headers on every call:

| Header           | Example                                  | Meaning                            |
| ---------------- | ---------------------------------------- | ---------------------------------- |
| `X-NAM-Tenant` | `nrl`                                  | the organisation the user acts for |
| `X-NAM-User`   | `u123`                                 | the user id                        |
| `X-NAM-Roles`  | `U1,U3` or `officer,authorized_user` | the user's FRS roles               |

The harness trusts these headers as given. It reads no password, token or key,
and keeps no user directory. A call without them is 401 `identity_missing`,
before its body is read. So **only the auth component may reach the port**:
bind it to loopback or a private network, never the public internet.

| Role | Who                         | What they do in NAM                                                                    |
| ---- | --------------------------- | -------------------------------------------------------------------------------------- |
| U1   | Officer (indenting officer) | capture, classify, precedents, quantity, estimate; the quantity and estimate approvals |
| U2   | User department             | the compliance requirements                                                            |
| U3   | Authorized user             | validation, the PR and its submission                                                  |
| U4   | Drafter                     | the documents, their review, the clauses and documents approvals                       |
| U5   | Functional reviewer         | validation, the package freeze                                                         |
| U6   | Commercial Department       | the Aarohan upload, the handoff to NIT                                                 |
| U7   | Approver                    | the approval file, the approval                                                        |

The full matrix (flow, gate, action and document permissions per role) is
`nam_agent/nam/policy/permissions.json`. Cases and documents belong to the tenant
that opened them. Another tenant's case is 403 `forbidden_tenant`, and another
tenant's document or call is 404.

## 3. The client file

Copy `nam_agent/client.py` into the backend. It is one file, standard
library only:

```python
from nam_client import NamClient, NamCallError

nam = NamClient("http://127.0.0.1:7777", tenant="nrl", user=current_user.id,
                roles=current_user.nam_roles)

reply = nam.run("Assess need for request #123",
                [{"role": "user", "content": "We need 20 ring joint gaskets R24"}])
```

`run()` returns the reply as a dict. An error reply raises `NamCallError` with
`status`, `code`, `message`, `retryable` and `details`. The client also has
`confirm`, `case`, `history`, `trace`, `trace_export`, `call_trace`, `upload`,
`documents`, `document`, `content`, `versions`, `new_version`,
`delete_document` and `health`.

## 4. One call

`POST /v1/nam/run`, schema `schemas/request.json`:

| Field            | Required | Meaning                                                                                                        |
| ---------------- | -------- | -------------------------------------------------------------------------------------------------------------- |
| `system`       | yes      | the call site's short instruction: "Assess need for request #123", "Validate the documents"                    |
| `messages`     | yes      | the conversation, `[{"role": "user", "content": "..."}]`; the last user message may carry the need           |
| `case_id`      | no       | the case to continue (`NAM-2026-0001`). Leave it out to open a new case. A case id in `system` counts too. |
| `case_version` | no       | the `case_version` of the last reply. If someone changed the case since, the call is 409 `stale_version`.  |
| `answer`       | no       | `{"question_id": "...", "value": "..."}`, the answer to an open question; `""` skips it                    |
| `confirm`      | no       | `{"action_id": "...", "decision": "approve" or "reject", "reason": "..."}`, for the pending action           |
| `documents`    | no       | ids of the case's documents the call is about, recorded with the call                                          |
| `options`      | no       | `{"skip_questions": true}` stops the follow-up questions                                                     |

The reply, schema `schemas/response.json`:

| Field                                    | Meaning                                                                                                                                                                          |
| ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `status`                               | `ok`; `needs_input` (answer `questions[0]`); `needs_approval` (confirm `pending_action`); `refused` (outside NAM)                                                    |
| `case_id`, `case_version`, `stage` | the case after the call, and its stage S1-S10                                                                                                                                    |
| `flow`, `frs`, `route`             | which NAM step ran, its FRS functions, and how it was chosen                                                                                                                     |
| `result`                               | the step's own output, valid against `schemas/flow.<flow>.json`                                                                                                                |
| `questions`                            | what the harness needs, each with `id`, `text`, `why` and `options`                                                                                                      |
| `pending_action`                       | the approval or irreversible step waiting, with `id`, `kind`, `summary`, `allowed_roles`                                                                                 |
| `citations`                            | the sources the result rests on (KG tenders, with their authority)                                                                                                               |
| `warnings`                             | what the user must know: provisional values, missing connectors, checks to do by hand                                                                                            |
| `next`                                 | the steps that can follow (empty while an approval is pending)                                                                                                                   |
| `verification`                         | what the harness checked after the step, against the case as stored: `passed`, `checks` (`name`, `ok`, `says`) and `stage_needs` (what the case's stage still needs) |
| `run_id`                               | this call, for its trace (`GET /v1/nam/runs/{run_id}`)                                                                                                                         |

Every step is verified before its reply goes out (`nam_agent/nam/verify.py`): its
artifacts are on the case, inside their schemas, cited, and any document they
name is stored with its file intact; a step that needs input says what; and a
step that says `ok` meets its own definition of done (an estimate with every
line priced and of the current quantity, a PR with no field missing, a
validation that passed on the documents as they are now, ...). A step that
fails a check is never reported done: its `ok` becomes `needs_input`, the
failed checks are in `warnings`, and the step is offered again in `next`.
Before the freeze and the NIT handoff the whole case is verified again (422
`guardrail`, rule `verification`, naming the step to run again). A model's
answer is used only after the harness's check accepts it; a refused answer
goes back to the model with the reason, at most twice, and each refusal is an
audit row.

Example:

```
curl -s localhost:7777/v1/nam/run -H 'Content-Type: application/json' \
  -H 'X-NAM-Tenant: nrl' -H 'X-NAM-User: u123' -H 'X-NAM-Roles: U1' \
  -d '{"system": "Assess need for request #123",
       "messages": [{"role": "user", "content": "We need 20 ring joint gaskets R24"}]}'
```

### What NAM knows: the skills

`nam_agent/nam/skills/` holds NAM's domain knowledge as two skills split into
topics: `nam-documents` (`pqc`, `scc`, `spec`, `boq`, `aan`, and the shared
extraction `rules`) and `nrl-procurement` (NRL's manuals condensed: `goods`,
`works`, `services` tender modes and thresholds, contract types, selection
systems; full text in `reference-*.md`). Each document topic says what the
document is, what to extract from it and how, and what to check. `case.ask` answers from them ("What is a
PQC?", "What do we extract from an SCC?"), `docs.review` checks with them,
and `nam_agent/nam/extract.py` does their extraction in code. The NAM harness chat agent
loads the same skills when a message names their subject ("explain the PQC
criteria", "what does the SCC say about liquidated damages"), and answers
from them with NRL's wording only from the KG tools.

### Words that name no step

A call site may send one generic system prompt ("Procurix NAM assistant") and
let the user's words say what they want. The harness takes a step only when
the words need one (router rule `words`, `nam_agent/nam/intent.py`):

| The words                                                                           | What happens                                                                                                                             |
| ----------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| a question, "show me ...", a greeting                                               | `case.ask` answers it, from the case, the KG (past tenders, past prices) or the glossary, each fact cited; nothing on the case changes |
| "approve it"                                                                        | `case.ask` says which approval is pending and which roles may confirm it: an approval is never given in words                          |
| "go ahead", "next", "carry on"                                                      | the to-do list continues, or the next step of the case's stage runs                                                                      |
| a change, "make it 35 nos", "the rate for line 1 is 520", "the cost centre is 4100" | the step of the case's stage that takes it runs, with the words read as its edit (`route.why` says how they were read)                 |
| a change the harness cannot read ("line 1 rate 5x0")                                | the step runs but says it applied nothing: verification check `message_applied` fails and the reply is `needs_input`                 |
| a tender named with "use" or "instead"                                              | `precedent.select`                                                                                                                     |
| a need, with no case                                                                | `need.capture` opens the case                                                                                                          |
| anything outside NAM                                                                | refused                                                                                                                                  |

### One query, a to-do list, step by step

A call site may hand the harness the whole job instead of one step:

```python
reply = nam.run("Complete the whole procurement step by step",
                [{"role": "user", "content": "We need 20 ring joint gaskets R24"}])
# then, as long as reply["result"]["complete"] is false:
reply = nam.run("continue", [], case_id=reply["case_id"],
                answer=...)        # only when reply["questions"] asks something
```

`case.plan` turns the query into a to-do list (`result.todo`): the steps from
the case's stage to the goal the query names (the estimate, the documents,
the validation, the PR, the approval, the upload, or the NIT handoff by
default), each with its stage, the roles that may run it and its status
(`pending`, `done`, `waiting_input`, `waiting_approval`, `blocked`). It runs
them in order, each exactly as its own call would run (role, stage,
guardrails, verification), and marks a step done only when the case shows it
done. It stops at the first question, approval, step for another role or
refusal, says why in `result.stopped`, and `continue` resumes it, by any user
whose role the next step needs. The list is stored on the case (`todo`), so
it follows reworks: steps the case went back on become pending again, and
when no precedent fits, the guided form is added to it.

### The three loops every call site needs

```python
reply = nam.run(prompt, messages, case_id=case_id, case_version=version)
while True:
    if reply["status"] == "needs_input":          # show questions[0] to the user
        q = reply["questions"][0]
        reply = nam.run(prompt, [], case_id=reply["case_id"],
                        case_version=reply["case_version"],
                        answer={"question_id": q["id"], "value": user_answer(q)})
    elif reply["status"] == "needs_approval":     # route to a user in allowed_roles
        act = reply["pending_action"]
        reply = approver_client.confirm(prompt, reply["case_id"], act["id"])
    else:
        break
```

- **Questions.** A question has `options` to pick from, or none for free text.
  Some answers take one line of edits, for example quantities as
  `"2: qty=40; add: item=valve, qty=5, uom=Nos"` or PR fields as
  `"all: cost_centre=4100"`. Each question's `text` shows its format.
- **Approvals.** A gate (quantity, estimate, clauses, documents) or an
  irreversible action (freeze the package, submit the PR, approve, hand off to
  NIT) never runs from the model or from a flow. It runs only when a user with
  one of `allowed_roles` confirms it, and only if the case has not changed
  since. A repeated confirmation returns the first result and runs nothing
  again. A rejection needs a `reason`.
- **Retries.** Retry only an error with `retryable: true`: `stale_version`
  (read the case again first), `in_progress`, `rate_limited` (wait for the time
  given in `details`), `kg_unavailable`, `model_unavailable`, `ocr_unavailable`.

## 5. Calling from many places

The backend can call from any screen with just a short prompt. The harness
routes it by the words, the case's stage and its category:

| Stage  | Step                                                                          | Example prompts                                                                                      | Who           |
| ------ | ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | ------------- |
| S1     | `need.capture`, `need.replicate`, `need.guided_form`, `need.classify` | "Assess need for request #123", "Copy tender PUR-12", "Fill the guided form", "Confirm the category" | U1            |
| S2     | `precedent.search`, `precedent.select`, `need.enrich`                   | "Find similar cases", "Select the precedent PUR-4", "Enrich the case from SAP"                       | U1            |
| S3     | `quantity.finalise`, `material.suggest`                                   | "Finalise the quantity", "Suggest materials"                                                         | U1            |
| S4     | `estimate.goods`, `estimate.wage`, `estimate.works`                     | "Prepare the estimate" (the category picks which)                                                    | U1            |
| S5     | `docs.generate`, `docs.review`, `compliance.build`, `validate`        | "Generate the BOQ and SCC", "Review the drafts", "Build the compliance requirements"                 | U4, U2, U3/U5 |
| S6     | `validate`, `package.freeze`                                              | "Validate the documents", "Freeze the package"                                                       | U3, U5        |
| S7     | `pr.build`, `pr.submit`                                                   | "Prepare the PR", "Submit the PR", "Record the SAP PR number"                                        | U3            |
| S8     | `approval`, `package.publish`                                             | "Route for approval", "Push the documents to Aarohan"                                                | U7, U6        |
| S9-S10 | `document.update`, `handoff.nit`                                          | "Upload the edited SCC", "Hand off to the NIT module"                                                | U1/U3, U6     |
| any    | `case.status`, `case.edit`                                                | "What is the status of NAM-2026-0001", "Edit the case details"                                       | all; U1, U3   |

"Continue" or "next step" runs the next step for the case's stage. A prompt
that names a flow id ("run estimate.goods") runs that flow. A prompt outside
NAM, or for work after the NIT handoff, is `refused`.

A step ahead of the case's stage is 409 `stage_gate`; `details` says what is
missing. An earlier step may run again before the package is frozen. If it
changes the case, the case goes back to that stage and the approvals from
there are asked again. After the freeze nothing goes back, and after the
handoff the case is closed.

Two calls on the same case at once are safe. Send `case_version` and the
second call gets `stale_version` rather than overwriting the first.

## 6. Documents

| Method and path                          | Body and headers                                                                                                                                      | Reply                                                                                                                                                                                                                                                                |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /v1/nam/cases/{id}/documents`    | the raw file; `Content-Type`, `X-Filename`, `X-Doc-Kind` (`aan`, `boq`, `quotation`, ...), optional `X-Doc-Reason`, `Idempotency-Key` | the document with its parsed text                                                                                                                                                                                                                                    |
| `GET /v1/nam/cases/{id}/documents`     |                                                                                                                                                       | the case's documents                                                                                                                                                                                                                                                 |
| `GET /v1/nam/documents/{doc}`          | `?version=n`                                                                                                                                        | metadata and text, marked `untrusted`; `content.extracted` holds what the document's skill says to extract (PQC criteria, SCC terms, specification clauses, BOQ lines, approval-note fields) for the kinds `pqc`, `scc`, `specification`, `boq`, `aan` |
| `GET /v1/nam/documents/{doc}/content`  | `?version=n`                                                                                                                                        | the original file                                                                                                                                                                                                                                                    |
| `GET /v1/nam/documents/{doc}/versions` |                                                                                                                                                       | every version                                                                                                                                                                                                                                                        |
| `PUT /v1/nam/documents/{doc}`          | the raw file; `X-Doc-Version` (the version it replaces)                                                                                             | the new version                                                                                                                                                                                                                                                      |
| `DELETE /v1/nam/documents/{doc}`       | `X-Doc-Version`, `X-Doc-Reason`                                                                                                                   | soft delete, kept for audit                                                                                                                                                                                                                                          |

PDF, DOCX, XLSX, PNG, JPEG and TIFF are accepted. The type is read from the
bytes. A document's text is data, never instructions: it is shown to a model
only inside untrusted blocks. A file with active content (PDF JavaScript,
Office macros) is refused with 415.

The harness's own documents (the estimate, BOQ, SOR, specification or scope
of work, PQC and SCC, the compliance list, the validation report and the ANN)
are stored the same way, as `.docx`, with a new version whenever their content
changes. A document edited outside NAM comes back as a new version (`PUT`).
`document.update` then measures how much the edit changed it.

## 7. Tracing and audit

| Method and path                               | Reply                                            |
| --------------------------------------------- | ------------------------------------------------ |
| `GET /v1/nam/cases/{id}`                    | the Need Record; `?version=n` for an older one |
| `GET /v1/nam/cases/{id}/history`            | every version: who, which flow and call, why     |
| `GET /v1/nam/cases/{id}/trace`              | the case traced to its inputs and approvals      |
| `GET /v1/nam/cases/{id}/trace?format=jsonl` | the same, one event per line, for export         |
| `GET /v1/nam/runs/{run_id}`                 | one call's audit rows (the caller's tenant only) |

The trace holds:

- every call: who asked, with which roles, which step, and how it ended;
- every version of the case;
- every artifact, with the call and the tool results it was made from;
- the evidence for each rate (the KG tool result, the officer who typed it,
  or the policy rule) and for each clause (its tender, or who added or dropped
  it and why), each with its authority flag; `evidence.facts` lists every
  rate, clause, requirement and suggestion with its source and authority,
  and `evidence.uncited` the ones without (always empty: the `citation`
  guardrail refuses a step that would store one);
- every approval, with who gave it;
- the audit log's integrity.

The audit log is append-only and hash-chained, so a changed or deleted row
shows. The prompts and messages are not stored, only their size and hash.

## 8. Errors

Every error is a JSON body `{"version", "error": {"code", "message", "retryable", "details"}, "run_id"}` with a real HTTP status. Messages never
carry stack traces or secrets.

| Status | Code                                                           | When                                                                                                                                             |
| ------ | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| 400    | `bad_request`, `schema`                                    | the body is not a valid request; an answer to a question the step does not ask                                                                   |
| 401    | `identity_missing`                                           | the identity headers are absent or malformed                                                                                                     |
| 403    | `forbidden_role`, `forbidden_tenant`, `forbidden_action` | the role may not run the step or confirm the action; the case is another tenant's                                                                |
| 404    | `not_found`                                                  | no such case, document, call or pending action                                                                                                   |
| 405    | `method_not_allowed`                                         | wrong method for the path                                                                                                                        |
| 409    | `stage_gate`                                                 | the step is ahead of the case's stage, the package is frozen, or the case is closed                                                              |
| 409    | `stale_version`                                              | the case changed since `case_version`, or the action was replaced                                                                              |
| 409    | `in_progress`                                                | the same write is still running                                                                                                                  |
| 413    | `too_large`                                                  | the body or file is over its cap                                                                                                                 |
| 415    | `unsupported_type`                                           | a file type that is not accepted, or a file with active content                                                                                  |
| 422    | `guardrail`                                                  | a rule held outside the model was broken (a rate without a source, a duplicate PR, a handoff before every approval); `details` names each rule |
| 429    | `rate_limited`                                               | more calls than `NAM_RATE` a minute                                                                                                            |
| 502    | `kg_unavailable`, `model_unavailable`, `ocr_unavailable` | a dependency is down                                                                                                                             |
| 500    | `internal`, `internal_schema`                              | a harness fault; the server log has the `run_id`                                                                                               |
| 501    | `flow_not_built`                                             | a step is listed but has no handler (none today)                                                                                                 |

## 9. What is not connected yet

| Missing                                           | What the harness does instead                                                                                                                                                                    |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| SAP (PR, PO, rate contracts)                      | the PR gets a Procurix.ai number (`PRX-...`); the SAP PR number made by hand and its status are recorded, one SAP PR per case                                                                  |
| Aarohan (file, approval, upload)                  | approval runs in Procurix.ai's own e-office (`FILE-<case>`); the upload list is produced and its reference recorded                                                                            |
| NIT module                                        | the handoff is recorded on the case, with its structured data, for NIT to read                                                                                                                   |
| SOR/DSR schedules, wage tables                    | rates come from the KG's past tenders (medians), the officer, or policy; anything else is a New Item                                                                                             |
| Procurement Manual, circulars, master clause list | validation and documents say these checks are for the reviewer; compliance shows NRL's standing NIT text from past tenders (`boilerplate`, authority precedent) as evidence, never as the rule |
| NRL's delegation of authority                     | the approver names the approving authority (`policy/doa.json` is empty)                                                                                                                        |

The draft policies to confirm before production are `policy/permissions.json`,
`policy/guided_forms.json`, `policy/pr_fields.json`, `policy/compliance.json`,
`policy/doa.json`, `policy/journey.json` (the PR Creation Flow's steps over
the stages) and parts of `policy/guardrails.json`.

## 10. Checking the harness

`make nam-eval` runs the NAM evals offline and prints the metric report. The
metrics are:

- routing accuracy;
- injection resistance;
- cross-document consistency catch rate;
- clause omission;
- schema validity;
- end-to-end Goods, Services and Works cases from need to NIT;
- facts without a source in those cases (must be 0);
- replies carrying a verification (must be all) and steps reported done that
  failed it (must be 0).

CI runs the same report on every PR. Retrieval quality (recall@5 of past
tenders over `evals/deterministic/nam/retrieval_labels.json`) needs the live
KG, so it runs only when asked:
`NAM_EVAL_KG_URL=http://127.0.0.1:8077 make nam-eval`. Build the labelled
set first, while the KG's graph is reachable, with
`scripts/nam_retrieval_labels.py --kg ... --cases-dir ... --write`. The 0.9
threshold is a draft until agreed.
