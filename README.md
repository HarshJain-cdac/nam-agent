# NAM harness

**An agentic harness for NRL's Need Assessment Module (NAM): from an
officer's need to a Purchase Request, on SAP's own records.**

The officer types a need or a job description. The agent takes out the
materials it names, counts each one in SAP, asks which go forward, and
narrows each chosen item, one question at a time, to one past PR. Each
choice goes into a cart; the PUR cases that hold the most of the materials
are ranked; and each cart item gets its PR. Every step is the agent's own
decision, and every gate is checked in code.

- **Agentic, not scripted.** The manual "PR Creation Flow in NAM" is the
  agent's guide each turn, not a rule engine.
- **Data, not invention.** Every option and every number comes from SAP's
  records; a question the records do not bear out is refused, with the reason.
- **Memory in the chat.** The case (need, materials, cart, answers, PRs) is in
  the agent's instructions every turn, and every change is a version, so undo
  goes back as far as the officer wants.
- **One server, one port.** The chat (`/api/chat`), the harness API
  (`/v1/nam/*`) and the NAM screens' routes (`/api/nam/*`) on port 7777.
- **Identity from outside.** The backend authenticates the user and sends
  `X-NAM-Tenant`, `X-NAM-User` and `X-NAM-Roles`; each chat and case is its
  user's alone.

## Run it

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env              # the model server, NAM_KG_URL, NAM_SERVER_KEY
.venv/bin/nam-agent serve         # http://127.0.0.1:7777
```

`make eval` runs every deterministic eval (no model, no network).

## The code

| Path | What it is |
|---|---|
| `nam_agent/server/` | the HTTP server: the chat, the routes, `/v1/nam/*`, and the reference chat page (`static/`) |
| `nam_agent/nam/` | NAM: the PR Creation Flow's tools and gates (`stage_tools.py`, `flow.py`), the case (`case.py`, `cart.py`), the cards (`cards.py`), SAP's records (`narrow.py`, `kg_mcp.py`), the harness API (`api.py`), the policies and schemas |
| `nam_agent/app.py` | one chat turn: the agent's instructions, the loop, the checks on its reply |
| `nam_agent/loop/` | the tool-calling loop and the model providers |
| `nam_agent/runtime/` | the chat's working memory (`session.py`) and the traces |
| `nam_agent/memory/`, `nam_agent/tools/` | the skills loader and stores; the ask-a-question tool and the tool registry |
| `evals/deterministic/` | the evals: every behaviour, offline |

## Read more

| | |
|---|---|
| [docs/backend-integration.md](docs/backend-integration.md) | **connecting a backend**: run, identity, every endpoint with its request and reply |
| [docs/nam/integration.md](docs/nam/integration.md) | the PR Creation Flow: its steps, tools, gates, cards and the cart |
| [docs/nam/harness-api.md](docs/nam/harness-api.md) | the harness API (`/v1/nam/*`) in full: fields, schemas, errors |

## Licence

MIT, see [LICENSE](LICENSE). The fonts in `nam_agent/server/static/fonts/` are
under the SIL Open Font License 1.1.
