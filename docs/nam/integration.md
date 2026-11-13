# NAM harness: the PR Creation Flow

NAM raises a Purchase Request (PR) for goods the way the manual "PR Creation
Flow in NAM" sets it out. The officer types a need in the NAM harness's chat. The agent
works the manual's three stages with its own tools, and the harness keeps the
case and checks every gate in code. The chat is the way in: `POST /api/chat`
or `POST /api/chat/stream` on the harness server (port 7777;
[../backend-integration.md](../backend-integration.md) has every endpoint).

The harness API is the other way in, for the Procurix.ai backend and any
other client: `/v1/nam/*` and the `/api/nam/*` routes, on the same server,
routes run the stage machine through `nam_agent.nam.run()`. It has its own guide,
[harness-api.md](harness-api.md). Both ways read the same SAP records and the
same identity headers.

## 1. Run it

```
nam-agent serve         # the chat and the API on http://127.0.0.1:7777
```

Settings come from the environment:

| Variable | Default | What it sets |
|---|---|---|
| `NAM_KG_URL` | none | the KG MCP server that serves SAP's records (`sap_search`), e.g. `http://<mcp-host>:8091/mcp`. NAM's flow counts and narrows these records. Without it NAM is off. |
| `NAM_KG_API_URL` | `http://127.0.0.1:8077` | the tender knowledge graph's precedent API. Read only when its `kg_*` tools are given (`NAM_KG_TOOLS`): by default they are off. |
| `NAM_KG_TOOLS` | `sap_search,nam` | the tools the agent gets, comma separated. The default is SAP alone: `sap_search` and the NAM tools; the knowledge graph's `kg_*` tools are off, and the agent's guide says SAP is its one source. `all` gives every tool; naming a `kg_*` tool turns the knowledge graph back on. |
| `NAM_DOMAIN_ONLY` | on where `NAM_KG_URL` is set | the chat answers only NRL procurement work (`nam_agent/nam/domain.py`). `0` turns it off, `1` turns it on anywhere. |
| `NAM_CHAT_MEMORY` | off where NAM runs | whether chats feed the shared memory (facts and episodes) and whether a turn recalls from it. Off by default where `NAM_KG_URL` is set: many officers use one server, and each chat's own history and its NAM case are its memory. `on` turns the shared memory back on. |
| `NAM_MODELS` | the small model, then the main one | the models for the `domain` check, in the order tried: `domain=m1,m2`. |
| `NAM_CHAT` | `flow` | what NAM is in the chat: `flow`, the PR Creation Flow below; `driver`, the earlier need-capture driver that runs in code (`nam_agent/nam/drive.py`), for clients built on it |
| `NAM_PROVIDER`, `NAM_MODEL`, `NAM_SMALL_MODEL` | the provider's defaults | the model provider and its two models |

## Chats

Every chat is its own. A client that connects sends its own chat id as
`session_id` with each message (the Procurix UI sends `px-<needId>`); a client
that sends none gets a new chat, `nam-chat-<date>-<8 hex>`, returned as
`session_id` in the reply to send with its next message. A chat begun with the
identity headers is that user's alone. The chat page
keeps its chat id in the browser, sends it with every message, and lists only
the chats that browser started, so two people on one server never share a
chat. Each NAM case belongs to the chat that opened it.

When several materials are taken forward, every follow-up question carries
which one it narrows ("Material 2 of 3 · valve"), shown above it.

## 2. The flow

The main output is the need understood. The officer types a need or a job
description; the agent takes out the materials it names, and SAP counts each
one. The officer chooses the items that go forward; each is narrowed, one at
a time, to one material code and put in a cart. Then the PUR cases are ranked
by how many of the materials each holds, and each cart item gets its own PR.
The estimate is there when the officer asks.

The agent reads the steps and the chat's case in its instructions every turn
(`nam_agent/nam/flow.py`). It decides each next step itself; the steps are a
guide, not a script. Each step has one tool (`nam_agent/nam/stage_tools.py`):

| Step | Tool | What the harness checks |
|---|---|---|
| 1a-1c category, need, materials | `nam_need` | the need opens the case (one word, or only its materials, is enough). The agent judges goods, services or works from the need and gives its reason (`category=`, `why=`); unless the officer said it themselves, the harness asks them to confirm or change it ("This looks like goods: ... Shall we go ahead with goods, or change it?", buttons **Yes, go with Goods** / **Change to Services** / **Change to Works**) before anything is searched. With no guess it asks plainly. Then each material is searched in SAP (`sap_search`) and counted: materials (a material code each), service lines, lines, and how they were found (by short text, by long text only, by meaning). An outage is never reported as "no record". |
| 1d choose the items | `nam_related`, `nam_choose` | `nam_related` reads, from SAP, every material on the need's latest past PRs (up to 3 per material, 6 in all) with how many of those PRs each was on. The agent judges which of them belong to this need (used or fitted with the officer's materials on the same job, not merely on the same PR) and offers only those with `nam_choose(suggest=[{name, why, keys}])`: the harness refuses a suggestion whose keys are not on those PRs, searches each in SAP, and adds it as an option marked "often bought with these". The options are the materials with their SAP counts, the suggestions, and "All of them"; the choice sets the order the items are worked in. A suggestion the officer leaves out leaves no trace in the case. A one-material need is asked this way only when something belongs. No question about an item comes before the choice. |
| 1e-1f follow-up questions | `nam_ask` | each option carries words its records carry; the harness places every record under the first option whose words it carries and shows the counts. It refuses an option no record carries, an option every record carries, a label number a record does not carry, and a question where fewer than two options get records; the records no option reaches go under "Something else". |
| 1f-1g answers and defaults | `nam_answer` | an answer narrows the records (an option, words, or a material code); an answer that fits nothing asks one clarifying question. A detail taken from NRL's standard value is marked default and needs its source. |
| 1h into the cart | `nam_pick`, `nam_cart` | only a record still in after the answers, and only once three or fewer are left; each item ends on ONE past PR: a record bought on several goes on the best by SAP's data (the latest, then PR + PO first), unless the officer names another; the agent is given the PRs side by side to explain the difference when asked. Then the next chosen item. `remove=true` takes an item out. A card click adds once: a second pick of the same item in that turn is refused. With every chosen item in, the harness shows the whole cart ("Your cart") and asks once "Is that everything, or do you want to add more to this cart?" (**That's everything** / **Add more items**); the agent summarises the cart in plain words. The PRs wait for the answer (`nam_cart`). After "That's everything" the need is understood: the agent sums it up in plain everyday words (what each item is, how many, roughly what it cost last time, the past PR and PO it follows) and stops. The PR is raised only when the officer asks for it (they name the PR or ask to raise it); until then `nam_pr` refuses and no PR field is asked. The PUR cases come only when the officer asks. More items go into the same case and cart (`nam_need(items=[...], add=true)`), worked the same way, and the review comes back once they are in. |
| undo | `nam_undo` | the officer's acts (an answer, a detail, the choice, a material in the cart, a PR field) are taken back one at a time, by the case's versions; each undo goes one further back. A confirmed PR is never taken back. |
| 1j matching PURs (only when the officer asks) | `nam_tenders` | the PUR cases ranked by how many of the need's materials each holds: the cart's picked codes, and every record of a material not taken forward. The PUR holding the most is 100%; one holding all of them is 100% only when it holds all. A PUR whose PR or PO SAP does not show is pointed to the knowledge graph (`kg_tender_record`). With `item=`, one item's past tenders. |
| estimate (optional) | `nam_estimate` | the cart's last PO rates, plus 7% for every year to the evaluation date; YRS and CAGR give Cleared, AHR, Manual review or New item. The lock waits until every flagged line is accepted, and writes `estimate.xlsx`. |
| 3a-3b one PR per item | `nam_pr` | each cart item's PR: its price is the locked estimate's, or the last price brought up to today with its result; a flagged price needs the officer's accept or rate. Every mandatory field (delivery date, plant, storage location, justification) before the confirmation; the fields given for one item's PR are offered for the next, and said so. SAP's PR interface is not connected, so a confirmed PR is "ready for SAP", written to `pr.xlsx` (one row per confirmed PR), and has no PR number. |
| any | `nam_case` | the case as it stands, or another case resumed in this chat |

The chat page keeps how each answer was made (its stages, the tools and their
raw output) in a "How this answer was made" dropdown above the reply, closed
until the officer opens it.

Every number beside an option is counted from SAP and says what it counts:
"0-200 °C (7 materials)", "HE cover (1 service line)", or both. SAP gives the
numbers; the knowledge graph gives a PUR case's own data that SAP's lines do
not show.

While several records are in, `nam_search` and `nam_answer` give the agent a
digest to choose its next question from, not a table to paste: the types SAP
holds (the first word of each short text that not every record shares: SPW,
RTJ, CAF for gaskets), the words and values that split them with how many records carry
each, and the records in short. Codes that share a short text are listed with the words of each one's
long text the others lack, so the next question asks about that. The records
appear as a table once three or fewer are left to pick from. A detail recorded with `field=` that some of the
records carry narrows them too; a count or a date never does.

A tool that refuses says why, and the agent puts it right and calls it again.
The item is not searched again while its question is open, and the agent
cannot answer its own question in the turn it asked it.
After three refusals in a row the tool tells the agent to stop and ask the
officer in plain words. While a case is open, the generic `ask_options` tool
shows nothing: every choice the officer sees comes from `nam_ask`. A reply
that leaves out the estimate, a PR or the PUR ranking gets that table
added under "From the case" (`nam_agent/nam/answer_data.py`). When a turn asks
nothing, the harness asks the open question again with its options.
Every tool call is one row in the hash-chained audit log (`nam_agent/nam/audit.py`).

## What the officer sees

- **Choices as buttons, several at once.** Every question `nam_ask` and
  `nam_choose` show is multi-select on the chat page: the officer toggles
  one or more and sends them, as "a; b"; an answer of several options keeps
  the records of each. Goods, services or works is one choice.
- **A click is read by the harness.** A message that is exactly one of the
  choices the harness showed (or several of them) is read with its tool
  before the model's turn (`stage_tools.read_click`), so it never depends on
  the model calling the tool. The officer's own words stay the agent's to
  read.
- **A question always has its buttons.** When a turn shows none (the
  agent's were refused, or it typed one), the open question is asked again
  with its buttons; with several records in and no question open, the
  harness asks one from the types SAP holds for the item (`narrow.suggest`).
- **Other materials are not types.** SAP's search also finds other materials
  by meaning (a thermocouple for "temperature gauge"). The digest marks a
  kind no record of which names the item; a kind that is the item's
  initials (TG) is the item itself and comes first, and the tender's title
  is never read as a record's own words. A question that calls another
  material a "type of" the item is refused, with the reason.
- **One copy of the options.** When the buttons show a question, the options
  the model typed out under it are dropped from the reply.
- **An item is finished on one PR.** The matches are cards of the PRs that
  hold them (a card per PR: its tender, PO, status, year, and each record
  with that PR's quantity and rate); PR and PO numbers are written as SAP
  writes them, ten digits (0010082938). A record in several PRs shows its PRs
  and waits for the officer's; `nam_pick(key, pr=...)` takes that PR. A card's
  "Continue with this" is read by the harness as exactly that record and PR.
- **The officer confirms what goes in the cart.** "Continue with this" on a
  card is the officer's own choice and adds the record at once. A choice in
  their own words ("the first one") is read by the agent, then shown back as
  a card with "Add this <item> to the cart?" and Yes, add it / No, show the
  others; it is not in the cart until they say yes. Every addition shows an
  "Added to the cart" card: the item, code, PR, PUR, quantity, last rate and
  how many of the chosen items are in.
- **Records no question can split** (they differ only in their codes) are
  shown as cards by the harness when the agent's questions were refused.
- **Nothing more once the PR is chosen.** The questions are to find the PR
  that best matches the need: once the officer continues with it, the item
  is in the cart with that PR's own quantity (the officer may change it) and
  the next item starts. The item's PR is drafted quietly, not shown at the
  pick.
- **No codes as choices.** A list of material codes the model offers as
  options is dropped from its reply; with several records in and no question
  open, the harness asks one by the types, or else by the values that split
  the records.
- **Every question names its material** ("Material 1 of 3 · valve", also 1 of
  1), the quantity's too.
- **Goods, services or works is asked** whenever a need opens a case, unless
  the officer's own words say which: the agent's guess is not taken.
- **Questions first, then the matches.** For every item the questions come
  first; its matching records are shown as cards once it is described: one
  record left, or nothing left in the records to tell them apart, or when
  the agent judges the description complete and calls `nam_matches`. A turn
  that asks a question shows no cards with it.
- **Cards, not tables** (`nam_agent/nam/cards.py`, drawn by `render.js`): the
  matches once the item is described, the PUR cases (`nam_tenders`), and
  each item's PR with its missing fields marked. The records come as the PUR
  cases that hold them, under a heading for each year: the PUR, its tender's
  title, chips (how many items match, its PR and PO), its status, then each
  matching record with its code, how many of the officer's answers it
  carries, and its last rate, best match first; "Start with this" sends the
  chosen record. A turn shows each group once. The reply does not repeat
  them as tables, and `POST /api/chat` returns them as `cards`. A chat
  reopened later shows them again.
- **Continue, something else, undo.** A records card has "Continue with
  this" (the chosen record goes to the cart) and "Show me something else":
  the first time, the closest other record SAP holds for the item (`nam_other`:
  one whose short text names the item, carrying the most of the officer's
  answers, the latest first); the second time, the last answer is taken back
  and the specification is asked again with a new follow-up. The latest reply
  of a NAM turn has "Undo last step" (`nam_undo`). The harness reads these
  three buttons itself (`stage_tools.read_click`).
- **Out of steps.** A turn that runs out of steps on the need gives the
  officer its open question with its buttons, not the loop's limit.
- **Status and order.** A card's status comes from SAP's lines: PR + PO, PR
  ongoing, or PR ongoing with its PO cancelled. Cards come the latest year
  first, then by that status; the PUR ranking keeps its match first.
- **One item at a time, its PR after it.** When an item goes into the cart,
  its PR is drafted and shown at once (what is still missing on it), then the
  next item starts. The officer is never asked to choose a material code: an
  option only a code carries is refused.

## The officer leads

At any point the officer can change the requirement, and the agent changes the
case with its tools, so the record the backend reads (`case_id`, the case and
its cart node) always matches the chat:

| The officer | The agent's tool | The case |
|---|---|---|
| gives a different need | `nam_need(need, items)` | a new case; the old one is closed ("replaced by ..."); its category is asked again |
| starts over | `nam_need(..., new=true)` | a new case; the old one closed |
| wants more materials | `nam_need(items, add=true)` | the same case and cart, the counts grow |
| wants fewer | `nam_need(remove=[...])` | out of the case and its cart |
| stops | `nam_cancel(reason)` | closed: nothing of it is asked again; the chat's `case_id` becomes `null` |
| changes the category, the choice, a record, a PR, a quantity | `nam_need(category)`, `nam_choose`, `nam_pick(remove=true)`, `nam_other`, `nam_pick(pr=...)`, `nam_pick(quantity=...)` | changed in place |
| takes a step back | `nam_undo` | the last act undone; it reopens a cancelled or replaced case |

A confirmed PR stays: it is never removed, replaced or cancelled. While a case
is open, talk about it is never treated as outside NAM.

## One PR for several items

With two or more items in the cart, no PR is confirmed until the officer
has said whether they want one common PR for them (the agent asks, in its
own words; when some materials were not taken forward it also asks whether
to add them or finish with these). `nam_pr(action='combine', items=[...])`
makes the common PR: one line per item, one set of the officer's fields
(what any item's PR already had is offered), every reference tender; the
items' own PRs are then "merged". `nam_pr(common=true, ...)` sets, confirms or
shows it, and `nam_pr(action='split')` keeps each item's own PR. The
confirmation writes one row per line to `pr.xlsx`.

Every NAM reply is held to the case and to SAP in code (`stage_tools.check_reply`):
a line naming a PR or PO number SAP holds nowhere for the case (nor this
turn's tools returned) is taken out; "SAP has no line for PR X" when the case
holds PR X is corrected (SAP's search finds records by what they say, not by
their number); an item said to be in the cart that is not is corrected. The
agent explains a record only from SAP's text, never from general knowledge.
No pick is made in the turn the officer answered unless they chose a card,
a second new case in one turn is the same case, and "Show me something else"
names the item its card is about. While a case is open, no raw SAP table is
added to a reply, and SAP's data is labelled as SAP's.

A reply that says a PR was confirmed, sent to SAP, or a purchase order made,
when the case does not bear it out, gets the case's own state added under it
(`stage_tools.claims`): a confirmation is the tool's, on the officer's word,
and NAM raises no purchase order.

## The cart node

The first thing the agent reads every turn is the cart node
(`nam_agent/nam/cart.py`): one short block the harness rebuilds from the case,
never the model's own account of it.

| Part | What it holds |
|---|---|
| Required | every item the need names, in order, with where it stands: waiting for the category, waiting for the choice, now, waiting, in the cart, PR ready for SAP, not taken forward |
| Types | the types SAP holds for each item, kept as found when it was searched |
| Details | each answer and detail the officer gave for the item |
| In cart | the one material code picked, with its short text, quantity, unit and PUR case |
| Now, next | the item being worked and the one after it |

Only `nam_pick` puts an item in the cart and only `nam_answer` adds a detail;
an undo takes both back, because the node is read from the case's current
version. `nam_case` returns it first, and `GET /api/nam/case/<id>` carries it
as `node`. While an item has an open question and the officer has replied,
`nam_ask` refuses a new question until the reply is read with `nam_answer`.

## 3. The case

`nam_agent/nam/case.py` keeps one case per need, linked to the chat that opened it.
Every write is a new version in `nam_flow_case_versions`; a write based on an old
version is refused. The case holds the need, each item's records and the ones
still in, the answers and details, the open question, the choice of items,
the cart, the matching PURs, each item's PR and the estimate. Each write
names its act, which is how `nam_undo` goes back over the officer's acts.
The agent reads it every turn, so the chat remembers where the need stands.

`GET /api/nam/case/<NAM-YYYY-NNNN>` returns the case as JSON, and
`GET /api/nam/case/<id>/estimate.xlsx` and `/pr.xlsx` return its sheets.

## 4. Identity and permissions

Another component authenticates the caller and sets three headers on each
request (D4). NAM harness consumes them and never authenticates anyone:

| Header | Example |
|---|---|
| `X-NAM-Tenant` | `nrl` |
| `X-NAM-User` | `u123` |
| `X-NAM-Roles` | `U1,U3` or `officer,authorized_user` |

A request without them is this machine's own officer (tenant `local`, role
U1). A partial or malformed set gets HTTP 401. A case belongs to the tenant
that opened it, and no other tenant can read or change it.
`nam_agent/nam/policy/permissions.json` says which roles may do each act: `need`,
`estimate.build`, `estimate.lock`, `pr.fill`, `pr.confirm`, `case.read`. The
matrix is a draft for NRL to correct.
