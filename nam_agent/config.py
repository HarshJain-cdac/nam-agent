"""Configuration — every knob is an env var, documented in .env.example.

No settings framework: a dataclass read once at startup. If you can read this
file, you know everything NAM harness can be configured to do.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def _load_env() -> str:
    """Find the user's .env the way the user expects: from where they ARE.

    Bare `load_dotenv()` searches upward from the file that called it, not from
    the working directory. Inside a git checkout that is invisible — config.py
    lives in the project, so walking up from it lands on the project's .env and
    everything works. Installed from PyPI it walks up from site-packages,
    reaches the filesystem root, and finds nothing: the user stands in a folder
    holding a perfectly good .env and NAM harness reports "No API key". Reported from
    a clean install on 2026-07-31, from inside the repo folder itself.

    usecwd=True is the whole fix. The walk upward is kept on purpose, so
    running `nam_agent` from a subdirectory of your project still finds the .env at
    its root — the same rule git, npm and pytest already taught everyone.

    Returns the path that was loaded (empty string if none).
    """
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path)
    return path


DOTENV_PATH = _load_env()


@dataclass
class Settings:
    # --- LLM: pick a provider, set its key. See nam_agent/loop/models.py PROVIDERS.
    provider: str = field(default_factory=lambda: os.getenv("NAM_PROVIDER", "anthropic"))
    # Explicit overrides (optional): key, endpoint, and model ids. Left empty,
    # the provider's own key env var and default models are used.
    api_key: str = field(default_factory=lambda: os.getenv("NAM_API_KEY", ""))
    base_url: str | None = field(default_factory=lambda: os.getenv("NAM_BASE_URL") or None)
    model: str = field(default_factory=lambda: os.getenv("NAM_MODEL", ""))
    # Cheap model used by the retrieval gate and the consolidation summarizer.
    small_model: str = field(default_factory=lambda: os.getenv("NAM_SMALL_MODEL", ""))
    # Providers turned off (comma-separated ids).
    disabled_providers: frozenset[str] = field(default_factory=lambda: frozenset(
        p.strip() for p in os.getenv("NAM_DISABLED_PROVIDERS", "").split(",") if p.strip()))

    # --- Home: where the harness keeps its state (state.db, the cases, traces).
    home: Path = field(default_factory=lambda: Path(os.getenv("NAM_HOME", ".nam")))

    # --- Loop guardrails
    max_iterations: int = field(default_factory=lambda: int(os.getenv("NAM_MAX_ITERATIONS", "10")))
    # Headroom matters for REASONING models (kimi-k3, gpt-5.x, gemini-*-pro):
    # they spend output tokens thinking before the answer, so a low cap makes
    # them hit stop_reason=max_tokens mid-thought and return an EMPTY reply
    # (watched kimi-k3 do exactly that at 2048). 8192 leaves room to think AND
    # answer; it's a ceiling, not a target, so efficient models still cost the same.
    max_tokens: int = field(default_factory=lambda: int(os.getenv("NAM_MAX_TOKENS", "8192")))
    # Working memory is a SLIDING WINDOW (like context RAM): only the last N
    # turns go into the prompt. Older turns aren't lost — they're in state.db,
    # distilled into facts by consolidation, and pulled back by the retrieval
    # gate when relevant. Without this cap a long chat resends its whole
    # history every turn.
    history_turns: int = field(default_factory=lambda: int(os.getenv("NAM_HISTORY_TURNS", "12")))

    # --- Memory
    # Consolidate (distill chats into durable facts) only after N new exchanges.
    consolidate_every: int = field(default_factory=lambda: int(os.getenv("NAM_CONSOLIDATE_EVERY", "6")))
    retrieval_top_k: int = field(default_factory=lambda: int(os.getenv("NAM_RETRIEVAL_TOP_K", "4")))
    # --- NAM: the SAP / knowledge-graph MCP server and the PR Creation Flow
    # The MCP server that reads SAP and the tender knowledge graph, e.g.
    # http://<mcp-host>:8091/mcp. Unset, none of the SAP, KG or flow tools
    # register: there is nothing to read.
    kg_url: str = field(default_factory=lambda: os.getenv("NAM_KG_URL", "").strip())
    # NRL's tender knowledge graph, its read-only
    # precedent API: the kg_* tools read it (nam_agent/nam/kg_tools.py), while
    # sap_search stays on NAM_KG_URL. "mcp" reads the KG through NAM_KG_URL.
    kg_api_url: str = field(default_factory=lambda: os.getenv(
        "NAM_KG_API_URL", "http://127.0.0.1:8077").strip())
    # Which KG tools the agent is given (nam_agent/nam/kg_tools.py), comma separated.
    # The default is SAP alone: sap_search and NAM's PR Creation Flow tools
    # ("sap_search,nam"); the tender knowledge graph's kg_* tools are off. "all"
    # gives every one; a list names them ("sap_search,nam,kg_tender_record").
    kg_tools: str = field(default_factory=lambda: os.getenv("NAM_KG_TOOLS",
                                                            "sap_search,nam").strip())
    # Only NRL procurement work is answered (nam_agent/nam/domain.py): "" (the
    # default) means on wherever kg_url is set; "1" on anywhere, "0" off.
    domain_only: str = field(default_factory=lambda: os.getenv("NAM_DOMAIN_ONLY", "").strip())
    # Whether chats feed the shared memory (facts and episodes) and whether a
    # turn recalls from it. "" (the default) is off wherever NAM is set up
    # (kg_url): many officers use one server, and each chat's own history and
    # its NAM case are its memory, so nothing of one person's chat reaches
    # another's. "on" turns the shared memory back on, "off" turns it off
    # anywhere (nam_agent/nam/chatstate.py shared_memory).
    nam_chat_memory: str = field(default_factory=lambda: os.getenv("NAM_CHAT_MEMORY", "")
                                 .strip().lower())
    # Where NAM's need capture reads from (nam_agent/nam/sap_need.py): "tender"
    # (the default) narrows the past tenders; "sap" narrows SAP's PR and PO
    # lines (sap_search) to one material, with its last PR, PO and PUR case.
    nam_source: str = field(default_factory=lambda: os.getenv("NAM_SOURCE", "").strip())
    # What NAM is in the chat: "flow" (the default), the PR Creation Flow the
    # agent works with its tools (nam_agent/nam/flow.py); "driver", the earlier
    # need-capture driver that runs in code (nam_agent/nam/drive.py), for clients
    # built on it. The server's /api/nam/* and /v1/nam/* serve both.
    nam_chat: str = field(default_factory=lambda: os.getenv("NAM_CHAT", "flow").strip())
    # The server (`nam-agent serve`, nam_agent/server/): every endpoint on one
    # port, 7777. Loopback by default: identity and roles arrive from the
    # backend's auth component, so only the backend should reach this port.
    # The older names (NAM_DASHBOARD_HOST/PORT, PORT) are read too. The body
    # cap is checked before a byte of the body is read.
    nam_host: str = field(default_factory=lambda: (
        os.getenv("NAM_HOST") or os.getenv("NAM_DASHBOARD_HOST") or "127.0.0.1").strip())
    nam_port: int = field(default_factory=lambda: int(
        os.getenv("NAM_PORT") or os.getenv("NAM_DASHBOARD_PORT") or os.getenv("PORT") or "7777"))
    nam_max_body: int = field(
        default_factory=lambda: int(os.getenv("NAM_MAX_BODY", str(1024 * 1024)))
    )
    # Documents (nam_agent/nam/docs/): the upload cap, the OCR engine as
    # "module:Class" (none by default: pages without text are marked so), and
    # how many letters and digits a page needs before its text counts.
    nam_max_doc: int = field(
        default_factory=lambda: int(os.getenv("NAM_MAX_DOC", str(25 * 1024 * 1024)))
    )
    nam_ocr_backend: str = field(
        default_factory=lambda: os.getenv("NAM_OCR_BACKEND", "").strip()
    )
    nam_ocr_min_chars: int = field(
        default_factory=lambda: int(os.getenv("NAM_OCR_MIN_CHARS", "20"))
    )
    # The model per NAM task, in the order tried: "domain=m1,m2"
    # (nam_agent/nam/llm.py). A task left out gets the small model then the main.
    nam_models: str = field(default_factory=lambda: os.getenv("NAM_MODELS", "").strip())
    # Calls (run, document upload or update) per tenant and user per minute;
    # 0 turns the limit off. Over it: 429 rate_limited, retryable.
    nam_rate: int = field(default_factory=lambda: int(os.getenv("NAM_RATE", "120")))
    # Answer a pure greeting ("hi", "thanks", "ok") from the intent node, with
    # no model call at all. On by default because the inference endpoint here
    # queues — one call has been measured between 1.6s and 21s — and making
    # someone wait that long to be told "Hello" is the worst latency in the
    # product. Set NAM_FAST_GREETINGS=0 to send greetings through the model
    # like any other message. Only messages made ENTIRELY of greeting words
    # qualify; see nam_agent/tools/intent.py.
    fast_greetings: bool = field(
        default_factory=lambda: os.getenv("NAM_FAST_GREETINGS", "1") not in ("0", "false", "no")
    )
    # --- Tracing (JSONL always; OTel exports if an endpoint is set)
    otel_endpoint: str = field(
        default_factory=lambda: os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    )

    def ensure_home(self) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "traces").mkdir(exist_ok=True)
        (self.home / "outbox").mkdir(exist_ok=True)
        return self.home


def load_settings() -> Settings:
    return Settings()
