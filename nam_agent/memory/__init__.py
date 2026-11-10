"""Memory facade — the three pillars behind one small interface.

    procedural  SKILL.md files      how to act
    semantic    facts table (FTS5)  what is durably true
    episodic    episodes table      what happened, when

Plus the two agents that manage them:
    retrieval_gate   decides IF a turn needs memory   (hero moment #1)
    consolidation    distills chats into facts, every N exchanges
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import anthropic

from nam_agent.config import Settings
from nam_agent.memory import consolidation, retrieval_gate
from nam_agent.memory.episodic.store import SqliteEpisodeStore
from nam_agent.memory.procedural.loader import SkillLoader
from nam_agent.memory.semantic.store import SqliteFactStore


def bundled_skill_dirs() -> list[Path]:
    """The skills that ship with the harness: the NAM knowledge skills (PQC,
    SCC, specification, BOQ, approval note, extraction), which the harness
    loads too (nam_agent/nam/skillset.py), so the chat and the harness cannot
    disagree about what a PQC is."""
    here = Path(__file__).resolve()
    return [p for p in (here.parents[1] / "nam" / "skills",) if p.is_dir()]


class Memory:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, client: anthropic.Anthropic,
                 episode_store=None):
        # episode_store: an already-built store, for tests
        self.conn = conn
        self.settings = settings
        self.client = client
        self.facts = self._make_fact_store(conn, settings)
        self.episodes = episode_store if episode_store is not None else self._make_episode_store(conn, settings)
        self.skills = SkillLoader([*bundled_skill_dirs(), settings.home / "skills"])

    @staticmethod
    def _make_fact_store(conn, settings):
        return SqliteFactStore(conn)

    @staticmethod
    def _make_episode_store(conn, settings):
        return SqliteEpisodeStore(conn)

    # ---- retrieval (gated — see retrieval_gate.py for why)
    def gated_retrieve(self, message: str, notify=None) -> str:
        retrieve, query, reason = retrieval_gate.should_retrieve(
            self.client, self.settings.small_model, message
        )
        if notify:
            notify("gate", {"decision": "retrieve" if retrieve else "skip", "reason": reason})
        if not retrieve:
            return ""
        found = self.facts.search(query, self.settings.retrieval_top_k)
        found += self.episodes.search(query, top_k=3)
        return "\n".join(found)

    # ---- procedural
    def matching_skills(self, message: str) -> str:
        return "\n\n".join(f"### {s.name}\n{body}" for s, body in self.skills.load(message))

    # ---- write paths
    def log_chat(self, user_message: str, reply: str, session_id: str = "default",
                 source: str = "cli", meta: dict | None = None) -> None:
        import json as _json
        self.conn.execute(
            "INSERT INTO chat_log (role, content, session_id, source) VALUES ('user', ?, ?, ?)",
            (user_message, session_id, source),
        )
        # meta (gate/latency/iterations/tools) rides on the assistant row so a
        # reopened thread can render the full turn card, not just the text.
        self.conn.execute(
            "INSERT INTO chat_log (role, content, session_id, source, meta) VALUES ('assistant', ?, ?, ?, ?)",
            (reply, session_id, source, _json.dumps(meta) if meta else None),
        )
        self.conn.commit()

    # ---- sessions (a chat's history, reloaded when a turn switches to it)
    def session_history(self, session_id: str) -> list[tuple[str, str]]:
        """The (user, assistant) exchanges of one past session, in order — used
        to reload working memory when the user switches back to a conversation."""
        import json

        from nam_agent.runtime.session import strip_tools_note

        rows = self.conn.execute(
            "SELECT role, content, meta FROM chat_log WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        pairs, pending = [], None
        for r in rows:
            if r["role"] == "user":
                pending = r["content"]
            elif pending is not None:
                # The model gets its [tools used: ...] note back: from meta,
                # or, for a reply saved before the note moved there, cut down
                # from the reply itself.
                try:
                    note = (json.loads(r["meta"] or "{}") or {}).get("tools_note") or ""
                except (TypeError, ValueError):
                    note = ""
                reply = strip_tools_note(r["content"])
                if not note and reply != (r["content"] or "").strip():
                    old = r["content"][len(reply):].strip()
                    note = old if len(old) <= 400 else old[:399] + "…]"
                pairs.append((pending, f"{reply}\n{note}" if note else reply))
                pending = None
        return pairs

    def list_sessions(self) -> list[dict]:
        """One row per conversation: id, first user message (the title), message
        count, and when it started — newest first."""
        rows = self.conn.execute(
            """SELECT session_id,
                      COUNT(*) AS messages,
                      MIN(created_at) AS started_at,
                      MAX(created_at) AS last_at
               FROM chat_log GROUP BY session_id ORDER BY last_at DESC"""
        ).fetchall()
        out = []
        for r in rows:
            first = self.conn.execute(
                "SELECT content FROM chat_log WHERE session_id = ? AND role = 'user' ORDER BY id LIMIT 1",
                (r["session_id"],),
            ).fetchone()
            out.append({
                "id": r["session_id"],
                "title": (first["content"][:60] if first else "(empty)"),
                "messages": r["messages"],
                "started_at": r["started_at"],
                "last_at": r["last_at"],
            })
        return out

    def export_markdown(self) -> None:
        """Mirror memory to a human-readable MEMORY.md next to state.db — so the
        whiteboard's `~/.nam/MEMORY.md` box is literally real, and "your memory
        is a file you can open" is true. state.db stays the queryable source of
        truth; this file is a generated view, refreshed after each turn."""
        facts = self.conn.execute(
            "SELECT subject, content FROM facts ORDER BY subject, id"
        ).fetchall()
        eps = self.conn.execute(
            "SELECT happened_at, summary FROM episodes ORDER BY happened_at DESC, id DESC"
        ).fetchall()
        lines = [
            "# NAM harness memory",
            "",
            ("_A human-readable mirror of what NAM harness remembers. The source of truth is "
            "`state.db` (the `facts` and `episodes` tables, keyword-searchable via FTS5); "
            "this file is regenerated after every turn._"),
            "",
            f"## Facts — semantic memory ({len(facts)})",
            "",
        ]
        lines += [f"- **{f['subject']}** — {f['content']}" for f in facts] or ["_none yet_"]
        lines += ["", f"## Episodes — episodic memory ({len(eps)})", ""]
        lines += [f"- **{e['happened_at']}** — {e['summary']}" for e in eps] or ["_none yet_"]
        (self.settings.home / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def maybe_consolidate(self, notify=None) -> None:
        new_facts = consolidation.consolidate_if_due(
            self.conn,
            self.client,
            self.settings.small_model,
            self.settings.consolidate_every,
            self.facts,
            self.episodes,
        )
        if new_facts and notify:
            notify("consolidation", {"new_facts": new_facts})
