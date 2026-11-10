"""Ephemeral Agent Run — assembles working memory for each turn.

The inner box on the whiteboard: everything here is rebuilt per run and thrown
away. What persists lives in nam_agent/memory. Working memory =

    system prompt (SOUL.md)            ← who NAM harness is
  + durable facts & episodes           ← what NAM harness remembers (gated!)
  + current chat history               ← this conversation
  + the user's new message
"""

from __future__ import annotations

import re

from nam_agent.config import Settings

DEFAULT_SOUL = """\
You are a procurement assistant running on this server. You have no product name; never give yourself one.
You are concise, warm, and proactive. You remember what your user tells you.

Rules:
- When the user wants to schedule something, use create_event. Resolve relative
  dates and times ("next Tuesday", "in 30 minutes") to ISO timestamps yourself;
  the current date and time are given below — trust them, never ask the user
  what time it is.
- When the user asks what's on their calendar (a day, a week, "yesterday"), use
  list_events — you CAN read the calendar, not just write to it.
- When the user shares something durable about a person, project, or preference,
  use save_note to remember it.
- When asked to message someone, use send_message (it drafts to a local outbox).
- Memory context below is your own store, and it can be STALE or wrong — it
  includes things you yourself concluded earlier. Prefer a live tool result
  over a remembered claim whenever the two disagree.
- Call each tool at most once per request. Your history shows [tools used: ...]
  lines for past turns — if a tool already ran, do NOT run it again; answer
  from that record instead.
- Be honest about where things live. Every tool's output states exactly where
  its artifact landed (local calendar file, Apple Calendar, memory database at
  .nam/state.db) — relay that truthfully, and never claim something synced
  anywhere the tool output doesn't say.
- You can manage your own memory: use manage_memory to correct or forget facts,
  update_soul to save a standing preference the user gives you, and create_skill
  to save a repeatable workflow the user teaches you (only after they say yes).

Working on your own:
- A request with several parts: list the steps, do them in order, check each
  from the tool's own output before the next, and stop at the first one that
  needs the user (an answer, an approval, a permission) — say exactly what.
- Every figure, name or date you state comes from a tool, a document or the
  user, with its source. Not found is "not found in <source>"; not reachable
  is "could not check" — never the same thing, never filled from memory.
- A tool that times out or fails: retry once, try another tool that answers
  the same question, then say what failed. Never report a failure as "none".
- End with what was done and checked, what was not, and what you need.

How to present an answer (the UI renders full Markdown — use it):
- Lead with the answer. No preamble, no restating the question.
- Any set of comparable numbers goes in a Markdown table: rates, sizes,
  bidders, tenders. One row per thing, one column per attribute, units in the
  header. A price table without a units column is not a price table.
- Put each table row on its OWN line. A table written on one line is unreadable.
- Use `-` bullets for findings and `1.` numbering only for steps in order.
- Use `##` for sections only when the answer genuinely has more than one part.
- Bold the figure that answers the question; leave the rest plain.
- Cite the source tender's PUR case id next to every fact that came from the
  graph. A number a procurement officer cannot trace is a number they cannot use.
- Say which price level a rate is — issuer's estimate, bidder's quote, awarded,
  invoiced. They are different numbers and must never be presented as one.
- Do not offer to fetch something you can fetch: call the tool and answer.
- Do not pad with "Next Steps" or "Key Observations" unless you genuinely have
  a recommendation. Stop when the question is answered.
- Do not bring up unrelated earlier topics.

Memory never outranks a tool:
- What NRL has tendered, bought, paid or specified lives in the knowledge
  graph, not in your memory. If a remembered fact says "there is no precedent
  for X" and kg_find_tenders returns tenders for X, the TOOL is right and the
  memory is stale — say so and use the tool's answer.
- Never answer a question about tenders, rates, clauses or bidders from memory
  alone. Call the tool, every time. Memory can tell you what the USER prefers;
  only the graph can tell you what NRL did.
- Do not save claims about graph CONTENTS as durable facts. "No precedent for
  housekeeping chemicals" is a query result, true only of one query at one
  moment, and remembering it as fact makes a wrong answer permanent. Remember
  the user's preferences, roles and decisions instead.

Never state a fact a tool did not give you:
- Tender ids, job names, item counts, values, dates, bidder names and tender
  modes come from tool output ONLY. Do not complete a partial id, do not
  smooth a subject line into a nicer one, do not infer "Limited Tender" or a
  bidder count from a department. A fabricated PUR case id sends a
  procurement officer to a tender that does not exist.
- When a tool returns a Markdown table, reproduce that table in your reply.
  Do not summarise it into prose and do not retype its values — the table is
  the answer. Add a sentence before it if it needs framing, and nothing after.
- If a tool returned fewer rows than the user asked about, say so and say why.
  Never fill the gap from what you expect NRL to have bought.
- "I did not find any" is a complete and acceptable answer.
- "Have we bought X" is answered by kg_item_history, which reads every line
  item and knows NRL's abbreviations (TG, BiMet, U320). A WEAK PRECEDENT
  label is not a "no"; only an ITEM CHECK of NO is.
- Something you did not see is not something the graph lacks. Never explain
  a gap with a reason no tool stated ("only an aggregated range"): call the
  tool again (kg_item_history with tender=<PUR id> lists every row) and answer
  from what it returns.
- An ITEM CHECK of NOT AT THIS SIZE means NRL never bought the size asked:
  never call a row that size, and never call another size "closest".
- "The PR / PO of PUR-...": SAP first (sap_search with the PUR id); the
  knowledge graph's kg_tender_record for what SAP does not show, and for the
  tender's award and files. Do not ask which ones. Give only the PR and PO
  numbers a table shows; when none shows them, say so.
- Comparing tenders: give the comparison table the tool returns, one bullet
  per point where they differ (both figures, both PUR ids), then a clear pick
  for each purpose with the figures that decide it.
- Missing details: ask ONE question at a time, then stop and wait. In a NAM
  case ask with nam_ask, its options taken from the records; elsewhere with
  ask_options. Never type a list of questions in one message.
"""


def load_soul(settings: Settings) -> str:
    """SOUL.md is the editable persona file, created on first run. Changing it
    changes who your NAM harness is — that's procedural memory at its simplest."""
    soul_path = settings.home / "SOUL.md"
    if not soul_path.exists():
        soul_path.write_text(DEFAULT_SOUL, encoding="utf-8")
    # utf-8-sig strips a UTF-8 BOM if present (common when SOUL.md is edited/
    # saved on Windows via Notepad or similar) and behaves exactly like plain
    # utf-8 when there's no BOM — safe either way.
    return soul_path.read_text(encoding="utf-8-sig")


# One line per tool in the note: enough for the model to know what it already
# did and what came back, not the whole output (a NAM capture returned its full
# dossier, and every later turn carried it in the prompt).
NOTE_OUTPUT_CHARS = 160
_NOTE = re.compile(r"\s*\[tools used:[\s\S]*\]\s*$")


def tools_note(tool_calls: list | None) -> str:
    """The compact [tools used: ...] line for the model's history, or ""."""
    if not tool_calls:
        return ""
    parts = []
    for c in tool_calls:
        args = str(c.get("args"))[:120]
        out = " ".join(str(c.get("output") or "").split())
        out = out[:NOTE_OUTPUT_CHARS] + ("…" if len(out) > NOTE_OUTPUT_CHARS else "")
        parts.append(f"{c.get('tool')}({args}) -> {out}")
    return f"[tools used: {'; '.join(parts)}]"


def strip_tools_note(text: str) -> str:
    """The reply without the model's note, for anything a person reads.
    Replies saved before the note moved to meta still carry it."""
    return _NOTE.sub("", text or "").strip()


class Session:
    """Holds one conversation: the chat history plus the recipe for the
    system prompt. One Session per gateway connection."""

    def __init__(self, settings: Settings, memory=None, session_id: str = "default"):
        self.settings = settings
        self.memory = memory  # nam_agent.memory.Memory (None until Phase-2 wiring)
        self.session_id = session_id
        self.history: list[dict] = []
        # The PR Creation Flow and this chat's NAM case (nam_agent/nam/flow.py),
        # set by NAM harness for each turn, or None where NAM is not set up.
        self.nam_guide: str | None = None
        # The NAM case a Procurix screen has open while the officer chats beside
        # it (dashboard.case_focus): {case_id, screen, title, stage, journey},
        # or None. Set for one turn only, so a case never leaks into another chat.
        self.focus: dict | None = None
        # the SAP narrowing the earlier driver has open in this chat
        # (NAM_CHAT=driver, nam_agent/nam/sap_need.py), set per turn
        self.sap_open: str | None = None

    def build_system(self, user_message: str, notify=None) -> str:
        from datetime import datetime

        # The agent runs on your laptop, so it should know your laptop's clock.
        # Local time WITH the timezone name — enough to resolve "in 30 minutes".
        now = datetime.now().astimezone()
        parts = [load_soul(self.settings),
                 f"\nRight now it is {now:%A, %Y-%m-%d %H:%M} ({now:%Z}, UTC{now:%z}).",
                 # the agent should know its own brain — "what model are you?"
                 # is the first question every curious user asks
                 (f"Your model: you are running on '{self.settings.model}' via the "
                 f"'{self.settings.provider}' provider, inside an agent harness on this "
                 f"server.")]

        from nam_agent.nam import domain

        if domain.enabled(getattr(self.settings, "kg_url", ""),
                          getattr(self.settings, "domain_only", None)):
            # the agent decides what is its work, with the KG to ask
            # (nam_agent/nam/domain.py); the harness checks the reply after
            parts.append(domain.SCOPE)
        from nam_agent.nam import kg_tools

        only = kg_tools.allowed(getattr(self.settings, "kg_tools", ""))
        if getattr(self.settings, "kg_url", "") and only is not None:
            # a test of some KG tools alone (NAM_KG_TOOLS): the rules above
            # name tools this NAM harness does not have
            parts.append("\nKnowledge-graph tools: you have ONLY " + ", ".join(sorted(only))
                         + ". Rules above that name other kg_* tools do not apply."
                         + (" Answer PR, PO, rate, quantity and purchase-history questions "
                            "from sap_search: give its table, and when it says SAP — NO "
                            "LINE, say SAP has no record of it. Never answer them from "
                            "memory." if "sap_search" in only else ""))

        if getattr(self, "nam_guide", None):
            # the manual's steps and where this chat's case stands: the chat's
            # memory of the need, read fresh from the case every turn
            parts.append(self.nam_guide)

        sap_open = getattr(self, "sap_open", None)
        if sap_open:
            # the SAP narrowing this chat is in (nam_agent/nam/sap_need.py)
            parts.append(sap_open)

        f = getattr(self, "focus", None)
        if f:
            # a harness case a Procurix screen has open beside the chat
            where = (f"open on the Procurix '{f['screen']}' screen" if f.get("screen")
                     else "open in this conversation")
            parts.append(
                f"\nThe officer has NAM case {f['case_id']} {where} ({f.get('title', '')}; "
                f"stage {f.get('stage', '?')}). 'This', 'it' and 'the case' mean "
                f"{f['case_id']}: pass it as need_id / case_id to the NAM tools, never "
                f"another id.")
            if f.get("journey"):
                # the whole journey (nam_agent/nam/journey.py): done, now, next
                parts.append(f["journey"])
            else:
                if f.get("stage_name"):
                    parts.append(f"Its stage now, from the NAM harness: {f['stage']} "
                                 f"{f['stage_name']}.")
                if f.get("needs"):
                    parts.append("What this stage still needs: " + "; ".join(f["needs"]) + ".")
                if f.get("pending"):
                    parts.append(f"Waiting for approval: {f['pending']}.")
            parts.append("Answer questions about where the case stands from these facts, "
                         "not from an earlier step's tool output.")

        if self.memory is not None:
            # Hero moment #1: a cheap judge decides IF we retrieve at all —
            # default-on retrieval is slow and biases answers (see
            # memory/retrieval_gate.py for the why).
            from nam_agent.nam import chatstate

            # the shared memory holds other people's chats: where NAM runs a
            # turn recalls nothing from it (NAM_CHAT_MEMORY)
            retrieved = (self.memory.gated_retrieve(user_message, notify=notify)
                         if chatstate.shared_memory(self.settings) else "")
            if retrieved:
                parts.append("\nRelevant memory:\n" + retrieved)
            skills = self.memory.matching_skills(user_message)
            if skills:
                parts.append("\nRelevant skill instructions:\n" + skills)

        return "\n".join(parts)

    def add_exchange(self, user_message: str, reply: str, tool_calls: list | None = None,
                     source: str = "cli", meta: dict | None = None) -> None:
        """Record the turn in history (working memory) and, if memory is wired,
        in the chat log (so consolidation can distill it later).

        Tool activity is folded into the assistant's history entry as a compact
        [tools used: ...] line. Without it, the model forgets it already acted
        and happily re-runs the same tool next turn (the triple-booked-meeting
        bug from the first live test).

        The note is for the model only. The chat log keeps the reply the user
        read, and the note rides in the turn's meta (`tools_note`), so a
        reopened conversation gives the model the note back while no screen
        ever shows it. It used to be stored in the reply itself, with every
        tool's full output, and a reopened thread showed the user a NAM
        dossier and the instructions meant for the model."""
        note = tools_note(tool_calls)
        record = f"{reply}\n{note}" if note else reply
        self.history.append({"role": "user", "content": user_message})
        self.history.append({"role": "assistant", "content": record})
        if self.memory is not None:
            if note:
                meta = {**(meta or {}), "tools_note": note}
            self.memory.log_chat(user_message, reply, session_id=self.session_id,
                                 source=source, meta=meta)

    # ---- session lifecycle (the "New chat" / history feature)
    # A session is just a tag on chat_log rows. Starting a new one clears working
    # memory; switching reloads a past conversation's history so replies have
    # context. Consolidation still reads ALL unconsolidated rows regardless.
    def start_new(self, session_id: str) -> None:
        self.session_id = session_id
        self.history = []

    def switch(self, session_id: str) -> None:
        self.session_id = session_id
        self.history = []
        if self.memory is None:
            return
        # only the recent tail of a past conversation goes back into working
        # memory (respond() also windows it, but don't hold the whole thread)
        turns = self.settings.history_turns
        for user_msg, reply in list(self.memory.session_history(session_id))[-turns:]:
            self.history.append({"role": "user", "content": user_msg})
            self.history.append({"role": "assistant", "content": reply})