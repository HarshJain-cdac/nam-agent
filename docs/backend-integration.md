# Connecting the NAM harness to a backend

This guide is for the team that connects the NAM harness to a software
backend (Procurix.ai or any other). It covers how to run the harness, how to
reach it, and every endpoint with its request and reply.

The harness is one HTTP server on **one port, 7777**. On that port it serves:

- **the chat** (`/api/chat`): the officer types a need, and the agent works
  the PR Creation Flow, item by item on SAP's records, to a Purchase Request;
- **the harness API** (`/v1/nam/*`): one call per NAM step, for screens that
  drive the steps themselves;
- **the NAM screens' routes** (`/api/nam/*`, `/api/nit/*`, `/api/tender/*`);
- **a reference chat page** at `/`, which uses only the chat endpoints below.

The harness does not authenticate anyone. The backend authenticates the
user and passes who they are in three headers (section 3).

## 1. Run it

```
cd nam-agent
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env          # then fill in the model and NAM_KG_URL
.venv/bin/nam-agent serve      # http://127.0.0.1:7777
```

`nam-agent serve --host 0.0.0.0 --port 7777` overrides the settings. The
server prints the model it uses for each task, and refuses to start when its
port is taken rather than moving to another one.

The settings that matter for a deployment (all in `.env.example`):

| Variable | Default | What it sets |
|---|---|---|
| `NAM_HOST` | `127.0.0.1` | the address the server listens on. `0.0.0.0` for another machine to reach it, with `NAM_SERVER_KEY` set. |
| `NAM_PORT` | `7777` | the port. The older names `NAM_DASHBOARD_PORT` and `PORT` are read too. |
| `NAM_SERVER_KEY` | none | a shared secret. When set, every `/api/*` and `/v1/*` call must send `Authorization: Bearer <key>`, so only the backend can use the server. |
| `NAM_CORS_ORIGIN` | `*` | the browser origin allowed to call the server directly (only needed when a browser, not the backend, calls it). |
| `NAM_KG_URL` | none | the MCP server that reads SAP and the tender knowledge graph, e.g. `http://<mcp-host>:8091/mcp`. **Required**: without it the NAM tools are not registered. |
| `NAM_PROVIDER`, `NAM_BASE_URL`, `NAM_API_KEY`, `NAM_MODEL`, `NAM_SMALL_MODEL` | | the model server (`openai` for an OpenAI-compatible server such as vLLM) |
| `NAM_HOME` | `.nam` | where the harness keeps `state.db` (chats, cases, audit), each case's sheets and the traces. Back this folder up. |

Run it under a process manager so it restarts. A systemd unit:

```
[Unit]
Description=NAM harness
After=network.target

[Service]
WorkingDirectory=/opt/nam-agent
EnvironmentFile=/opt/nam-agent/.env
ExecStart=/opt/nam-agent/.venv/bin/nam-agent serve
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Behind nginx, turn buffering off for the stream (the server also sends
`X-Accel-Buffering: no`), and allow long turns:

```
location /nam/ {
    proxy_pass http://127.0.0.1:7777/;
    proxy_buffering off;
    proxy_read_timeout 600s;
}
```

## 2. Check it

```
curl -s http://127.0.0.1:7777/health
{"ok": true, "version": "nam.v1"}

curl -s http://127.0.0.1:7777/v1/nam/health -H "Authorization: Bearer $NAM_SERVER_KEY"
{"version": "nam.v1", "ok": true, "kg": {"configured": true, "reachable": true}, "model": {...}, ...}
```

`/health` answers without a key or identity, for a load balancer.
`/v1/nam/health` also says whether SAP / the knowledge graph answers, and
which model runs each task.

## 3. Who the user is

The backend's auth component authenticates the user, then sets three
headers on every call:

| Header | Example | Meaning |
|---|---|---|
| `X-NAM-Tenant` | `nrl` | the organisation the user acts for |
| `X-NAM-User` | `u123` | the user id |
| `X-NAM-Roles` | `U1` or `U1,U3` | the user's NAM roles (U1 officer ... U7 approver; `nam_agent/nam/policy/permissions.json`) |

The harness trusts these headers as given, so **only the backend may reach
the port**: keep it on loopback or a private network, and set
`NAM_SERVER_KEY`.

- On `/v1/nam/*` the three headers are required (401 without them).
- On the chat they are optional, but a deployment should always send them.
  A chat started with them belongs to that user: no other user, and no
  caller without the headers, can send to it, read it or list it (403).
- A case belongs to the tenant that opened it.
- A partial or malformed set of headers is 401.

## 4. The chat

### POST /api/chat

One turn, one JSON reply.

Request:

```json
{"message": "we need gaskets for the crude unit pumps",
 "session_id": "nam-chat-20261110-3f7b95ea"}
```

| Field | Required | Meaning |
|---|---|---|
| `message` | yes | what the officer typed, or the text of a button they pressed (section 4.3) |
| `session_id` | no | the chat. Leave it out on the first message: the harness starts a new chat and returns its id. Send that id with every later message of the same chat. A backend may also name its own ids (letters, digits and `._:-`, up to 80), e.g. `px-<needId>`. |
| `case_id`, `screen` | no | the NAM case a screen has open beside the chat (`NAM-2026-0001`), so "this" means that case |

Reply:

```json
{
  "session_id": "nam-chat-20261110-3f7b95ea",
  "case_id": "NAM-2026-0007",
  "reply": "Is this goods, services or works?",
  "ask_options": {"question": "Is this goods, services or works?",
                  "options": ["Goods", "Services", "Works"], "field": "nam"},
  "tools": [{"tool": "nam_need", "status": "ok", "summary": "...", "args": {...}, "output": "..."}],
  "iterations": 3, "latency_ms": 8120, "model": "gpt-oss-20b"
}
```

| Field | Meaning |
|---|---|
| `session_id` | the chat; send it with the next message |
| `case_id` | the NAM case this chat opened, or `null` before the need opens one |
| `reply` | the agent's answer, Markdown |
| `ask_options` | present when the agent asks a question with choices: show one button per option. With `"multi": true` several may be chosen. `progress`, when present, says which of the chosen materials the question is about: `{"n": 1, "of": 2, "item": "gasket"}`, shown as "Material 1 of 2 · gasket". |
| `cards` | present when the turn shows SAP records, PUR cases, a PR or a cart addition (section 4.2) |
| `tools` | the steps the agent took, for a "how this answer was made" view; `status` is `ok`, `warn` or `error` |
| `error` | present instead of `reply` when the turn failed |

A turn takes from a few seconds to a few minutes (the model and SAP are
called several times); set the client timeout to at least 300 seconds.

### POST /api/chat/stream

The same turn as server-sent events (`Content-Type: text/event-stream`), for
a screen that shows the reply as it is written. Same request. Each event is
one line `data: {json}` followed by a blank line, with a `kind`:

| `kind` | Fields | Show it as |
|---|---|---|
| `text` | `delta` | the next piece of the reply |
| `reasoning` | | "thinking" |
| `tool_start` | `tool` | the step that is running |
| `tool` | `tool`, `args`, `output` | a step finished |
| `ask_options` | as in `/api/chat` | the question's buttons |
| `nam_cards` | `group`, `title`, `cards`, ... | a group of cards (section 4.2) |
| `intent`, `gate`, `domain` | `reason` | how the turn was handled (optional to show) |
| `llm` | `iteration`, `stop_reason`, `usage` | one model call and its tokens (optional to show) |
| `done` | everything `/api/chat` returns | the end of the turn: replace the streamed text with `reply` |

The stream always ends with `done`, also on failure (with `error`).

### 4.2 The cards

A `nam_cards` event (or an entry of `cards`) is one group, drawn the same way
whatever its `group`:

| `group` | What it holds |
|---|---|
| `records` | the past PRs that match the item still being narrowed: each record on ONE PR, the best by SAP's data (the latest year, then PR + PO before PR ongoing before a cancelled PO), the other PRs it was on named as "also on PR ..."; one card per PR, the matching records inside it |
| `cart` | "Added to the cart": what was just put in the cart (item, code, PR, PUR, quantity, last rate, "1 of 3 in the cart"); and "Your cart": every item, when all are in and the officer is asked whether that is everything |
| `pr` | one item's PR, or the common PR, with the fields still missing marked |
| `purs` | PUR cases, with how many of the need's materials each holds |

Each card has `title`, `badge`, `status`, `year`, `subtitle`, `fields`
(`[label, value, state]`, state `missing` marks a field still to give),
`chips`, `items` (each with `title`, `code`, `detail`, `match` and `send`),
`note`, `foot` and `action`. A records group with `by_year` is shown under
year headings, as it comes.

### 4.3 Buttons are messages

Every choice is sent back as the next message of the same chat, with the
button's own text. The harness reads its own buttons in code, before the
model, so these exact strings matter:

| The officer presses | Send as `message` |
|---|---|
| an option of `ask_options` | the option's text; several chosen: the texts joined with `"; "` |
| "Continue with this" on a records card, with one of its items selected | that item's `send` |
| "Show me something else" on a records card | the card's `action.other` |
| "Yes, add it" / "No, show the others" (confirming a pick typed in words) | that text |
| "Yes, go with Goods" / "Change to Services" ... (confirming the category the agent guessed) | that text |
| "That's everything" / "Add more items" (when every item is in the cart) | that text |
| "Undo last step" | `Undo the last step` |

Typed answers are fine too ("the first one", "go with the 2023 PR"): a pick
the officer types is confirmed with a "Yes, add it" question before it goes in
the cart.

### POST /api/session

The chats, with the same identity headers.

| Request | Reply |
|---|---|
| `{"action": "new"}` | `{"ok": true, "session_id": "nam-chat-..."}`: an id for a new chat |
| `{"action": "history", "id": "<session_id>"}` | `{"ok": true, "history": [{"role": "user" or "assistant", "content", "meta"}]}`; an assistant row's `meta` holds that turn's `cards`, `ask` and `tools`, to draw a reopened chat as it was |
| `{"action": "case", "id": "<session_id>"}` | `{"ok": true, "case": {...} or null}`: the chat's NAM case, with its cart node as `case.node` |
| `{"action": "list"}` | `{"ok": true, "chats": [{"id", "title", "messages", "last_at"}]}`: the user's chats, newest first |

Another user's chat is 403 `{"error": "this chat is not yours"}`.

### The case and its cart node

`case.node` (also `GET /api/nam/case/{case_id}` → `node`) is the state of the
need, rebuilt from the case on every read:

```json
{"case_id": "NAM-2026-0007", "need": "gaskets and a pressure gauge for P-101",
 "category": "goods", "now": "pressure gauge", "next": null,
 "items": [{"item": "gasket", "status": "in the cart", "found": 12, "still_in": 1,
            "types": [{"type": "spiral wound", "count": 7}], "details": ["size: 4 inch"],
            "cart": {"code": "...", "short_text": "...", "quantity": 10, "unit": "NOS", "pur": "PUR-..."}},
           {"item": "pressure gauge", "status": "now", "cart": null, ...}]}
```

`GET /api/nam/case/{case_id}/pr.xlsx` and `/estimate.xlsx` return the case's
sheets once they are written.

## 5. The harness API: /v1/nam/*

For screens that run the NAM steps themselves (the Procurix wizard), one call
per step, the way one would call an LLM. All calls need the three identity
headers. [nam/harness-api.md](nam/harness-api.md) has every field, the
schemas and the error codes; in short:

| Method and path | What it does |
|---|---|
| `POST /v1/nam/run` | one NAM step: `{"system", "messages", "case_id"?, "case_version"?, "answer"?, "confirm"?, "documents"?, "options"?}` → `{status, case_id, case_version, stage, flow, result, questions, pending_action, warnings, next, verification, run_id}` |
| `GET /v1/nam/cases/{id}` | the Need Record (`?version=n` for an older one) |
| `GET /v1/nam/cases/{id}/history` | every version: who, which step, why, what changed |
| `GET /v1/nam/cases/{id}/trace` | the case traced to its inputs and approvals (`?format=jsonl` to export) |
| `POST /v1/nam/cases/{id}/documents` | upload a document: the raw file as the body, with `Content-Type`, `X-Filename`, `X-Doc-Kind` (and `X-Doc-Reason`, `Idempotency-Key`) |
| `GET /v1/nam/cases/{id}/documents` | the case's documents |
| `GET /v1/nam/documents/{doc}` | a document's metadata and text (`?version=n`) |
| `GET /v1/nam/documents/{doc}/content` | the original file |
| `GET /v1/nam/documents/{doc}/versions` | every version |
| `PUT /v1/nam/documents/{doc}` | a new version, with `X-Doc-Version` (the one it replaces) |
| `DELETE /v1/nam/documents/{doc}` | soft delete, with `X-Doc-Version` and `X-Doc-Reason` |
| `GET /v1/nam/runs/{run_id}` | one call's audit rows |
| `GET /v1/nam/health` | liveness, SAP / KG reachability, the model per task |
| `GET /v1/nam/schema` | every request, response and result schema |

Errors are JSON with a real HTTP status:
`{"error": {"code", "message", "retryable", "details"}, "run_id"}`
(400 bad_request, 401 identity_missing, 403 forbidden_tenant, 404 not_found,
409 stale_version, 413 too_large, 415 unsupported_type, 422 guardrail,
429 rate_limited, 502 kg_unavailable).

## 6. The NAM screens' routes: /api/nam/*

The routes the Procurix NAM screens were built on. They run the same steps
through the same entry point as `/v1/nam/run`, as the reserved `local`
tenant with the role the payload names. Prefer `/v1/nam/*` (with the
identity headers) for new screens.

| Method and path | What it does |
|---|---|
| `POST /api/nam/capture` | need capture: `{"text"}`, then `{"need_id", "answer": {"question_id", "value"}}` per follow-up question, `skip_followups`, `undo`, `category` |
| `POST /api/nam/run` | any later step: the `/v1/nam/run` request plus `"role"` (U1 ... U7) |
| `POST /api/nam/edit` | a screen's edits as the step's structured answer: `{"need_id", "kind", "role", ...}` |
| `POST /api/nam/need`, `/classify`, `/similar`, `/update`, `/select`, `/material`, `/reference`, `/replicate`, `/case`, `/compare` | the earlier need routes (`nam_agent/server/nam_routes.py` has each one's fields) |
| `POST /api/nam/drafts` | save a screen's unsent edits: `{"need_id", "section", "data"}` |
| `POST /api/nam/docx` | an edited document as .docx: `{"title", "blocks", "filename"}` |
| `GET /api/nam/workflow[?need_id=]` | the stages S1-S10 and, for a case, where it stands |
| `GET /api/nam/register` | the Need Assessment register: one row per case, and the KPI cards |
| `GET /api/nam/needs` | the recent needs ("Your drafts") |
| `GET /api/nam/need?need_id=` | one need and the fields still to ask about |
| `GET /api/nam/dossier?need_id=` | the need's dossier, as Markdown |
| `GET /api/nam/drafts?need_id=` | the saved edits |
| `GET /api/nam/output?need_id=&kind=requirement\|estimate\|pr` | the .xlsx outputs |
| `GET /api/nam/pr_fields`, `GET /api/nam/norms` | the PR field policy, the PQC norms |
| `GET /api/nam/case/{id}[/pr.xlsx\|/estimate.xlsx]` | the chat flow's case (section 4) |
| `GET /api/nit/{case}.json`, `GET /api/nit/{case}.pdf[?draft=1&download=1]` | the generated NIT sheet and its PDF |
| `POST /api/nit/{case}/field` | accept one suggested NIT entry: `{"field", "value": "accept"}` |
| `GET /api/tender/{case}/documents`, `GET /api/tender/{case}/nit.pdf` | a past tender's documents and its scanned notice |

## 7. The client file

`nam_agent/client.py` is one file, standard library only. Copy
it into a Python backend:

```python
from nam_client import NamClient, NamCallError

nam = NamClient("http://127.0.0.1:7777", tenant="nrl", user=user.id, roles=user.nam_roles,
                server_key=settings.NAM_SERVER_KEY)

turn = nam.chat("we need gaskets for the crude unit pumps")      # a new chat
turn = nam.chat("Goods", session_id=turn["session_id"])          # its next message
case = nam.chat_case(turn["session_id"])                         # the case and its cart node
mine = nam.chats()                                               # this user's chats

reply = nam.run("Assess need for request #123",                  # the harness API
                [{"role": "user", "content": "We need 20 ring joint gaskets R24"}])
```

An error reply raises `NamCallError` with `status`, `code` and `message`.

From any other language the calls are plain HTTP and JSON:

```
curl -s http://127.0.0.1:7777/api/chat \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $NAM_SERVER_KEY" \
  -H 'X-NAM-Tenant: nrl' -H 'X-NAM-User: u123' -H 'X-NAM-Roles: U1' \
  -d '{"message": "we need gaskets for the crude unit pumps"}'
```

## 8. Data, logs and backups

Everything the harness keeps is under `NAM_HOME` (default `.nam` in the
folder it runs from):

| Path | What |
|---|---|
| `state.db` | the chats, who owns each, the cases and their versions, the audit |
| `nam/<case_id>/` | each case's `pr.xlsx` and `estimate.xlsx` |
| `traces/<date>.jsonl` | every turn: the tools called, the tokens used |

Back up `NAM_HOME`; nothing else holds state. The server logs each request
to standard error.
