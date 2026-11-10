"""Semantic memory — durable facts, keyword-searched with SQLite FTS5.

The Hermes insight from the whiteboard: "keyword top-k, no embedding". For a
single user's facts, ranked keyword search (BM25) is fast, fully local, and —
crucially for teaching — you can read the whole index with sqlite3.
Want vectors? Set NAM_SEMANTIC_STORE=supabase (see supabase_store.py).
"""

from __future__ import annotations

import re
import sqlite3

# The scripts unicode61 does not segment. It counts these as alphanumeric but
# puts no word boundary between them, so a whole run — 「阿历克斯喜欢游泳」 — is
# indexed as ONE term. An exact match on a name inside that run can therefore
# never hit, which is why these tokens, and only these, are searched as
# prefixes. Turning *every* token into `token*` would quietly make "car" match
# "carpet": a worse failure than this one, because it still looks like it works.
_UNSEGMENTED = re.compile(
    "["
    "\u3040-\u309f"  # Hiragana
    "\u30a0-\u30ff"  # Katakana
    "\u3400-\u4dbf"  # CJK Unified Ideographs Extension A
    "\u4e00-\u9fff"  # CJK Unified Ideographs
    "\uf900-\ufaff"  # CJK Compatibility Ideographs
    "]"
)


def _fts_query(text: str) -> str:
    r"""User text isn't a valid FTS5 query (quotes/punctuation break MATCH).
    Reduce it to `word OR word OR ...` over alphanumeric tokens.

    "Alphanumeric" has to mean what the *index* means by it. facts_fts and
    episodes_fts declare no tokenizer, so they get FTS5's default — unicode61,
    which keeps every Unicode alphanumeric and folds diacritics ("München" is
    stored as `munchen`). An ASCII-only `[a-zA-Z0-9]` disagreed with that
    index, and the index was not the side that was wrong:

      * accented Latin was truncated to a fragment — "Müller" became `ller`,
        which matches nothing, so German, French, Spanish, Portuguese, Turkish
        and Vietnamese users got silently wrong answers;
      * every non-Latin script reduced to "" — and an empty query is not a
        no-op. SqliteEpisodeStore.search() reads it as "just give me the recent
        ones", so a user asking about Сергей got an unrelated English episode
        handed to the model under the heading "Relevant memory". NAM harness wasn't
        skipping memory for those users, it was confidently supplying someone
        else's.

    `[^\W_]` is the same set unicode61 keeps. Underscore is excluded because
    unicode61 treats it as a separator, so `nam_agent_agent` is two terms in the
    index and has to be two terms here too, or it matches nothing.

    Everything stays lowercased, and that is load-bearing beyond tidiness:
    FTS5's AND/OR/NOT/NEAR are operators only in UPPERCASE, so lowercasing is
    what stops a user's "Alex and Bob" from parsing as a boolean expression.
    """
    words = re.findall(r"[^\W_]{2,}", text.lower())
    if not words:
        return ""
    return " OR ".join(
        f"{w}*" if _UNSEGMENTED.search(w) else w for w in dict.fromkeys(words)
    )


class SqliteFactStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, subject: str, content: str, source: str = "user") -> None:
        self.conn.execute(
            "INSERT INTO facts (subject, content, source) VALUES (?,?,?)",
            (subject.lower().strip(), content, source),
        )
        self.conn.commit()

    def search(self, query: str, top_k: int = 4) -> list[str]:
        fts = _fts_query(query)
        if not fts:
            return []
        rows = self.conn.execute(
            "SELECT f.subject, f.content FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts, top_k),
        ).fetchall()
        return [f"[{r['subject']}] {r['content']}" for r in rows]

    # --- CRUD: humans (dashboard) and the agent (manage_memory tool) edit memory.
    # The facts_au / facts_ad triggers keep the FTS index in sync automatically.
    def list(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, subject, content, source, created_at FROM facts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_with_ids(self, query: str, top_k: int = 8) -> list[dict]:
        fts = _fts_query(query)
        if not fts:
            return self.list(top_k)
        rows = self.conn.execute(
            "SELECT f.id, f.subject, f.content FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?",
            (fts, top_k),
        ).fetchall()
        return [dict(r) for r in rows]

    def update(self, fact_id: int, content: str, subject: str | None = None,
               reason: str = "updated") -> bool:
        """Replace a fact. The old version is closed into fact_history (with
        when it was true), not overwritten, and the fact keeps its id."""
        old = self.conn.execute(
            "SELECT subject, content, source, COALESCE(valid_from, created_at) AS since "
            "FROM facts WHERE id=?", (fact_id,)).fetchone()
        if old is None:
            return False
        subject = old["subject"] if subject is None else subject.lower().strip()
        if (subject, content) == (old["subject"], old["content"]):
            return True
        self.conn.execute(
            "INSERT INTO fact_history (fact_id, subject, content, source, valid_from, reason) "
            "VALUES (?,?,?,?,?,?)",
            (fact_id, old["subject"], old["content"], old["source"], old["since"], reason))
        self.conn.execute(
            "UPDATE facts SET content=?, subject=?, valid_from=datetime('now') WHERE id=?",
            (content, subject, fact_id))
        self.conn.commit()
        return True

    def delete(self, fact_id: int) -> bool:
        """Forget a fact: the fact and its earlier versions both go."""
        cur = self.conn.execute("DELETE FROM facts WHERE id=?", (fact_id,))
        self.conn.execute("DELETE FROM fact_history WHERE fact_id=?", (fact_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def history(self, fact_id: int) -> list[dict]:
        """The fact's earlier versions, oldest first, each with when it was true."""
        rows = self.conn.execute(
            "SELECT subject, content, valid_from, valid_to, reason FROM fact_history "
            "WHERE fact_id=? ORDER BY id", (fact_id,)).fetchall()
        return [dict(r) for r in rows]

    def settle(self, timeout: float = 120.0) -> bool:
        """Already settled. The row and its FTS5 index land in one transaction,
        so a fact is searchable the instant add() returns. The hosted backends
        have to work for this."""
        return True
