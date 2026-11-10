"""Shared eval plumbing: a scripted fake LLM client for offline tests, and a
real-NAM harness factory for live ones."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from types import SimpleNamespace


def _has_key() -> bool:
    """True when the ACTIVE provider (NAM_PROVIDER) has its key set, so live
    evals run on whatever the user actually configured (anthropic, openrouter,
    gemini, ...), not only on ANTHROPIC_API_KEY."""
    from nam_agent.config import load_settings
    from nam_agent.loop.models import PROVIDERS

    settings = load_settings()
    provider = PROVIDERS.get(settings.provider)
    return bool(settings.api_key or (provider and os.getenv(provider.key_env)))


HAS_KEY = _has_key()


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, args: dict, call_id: str = "tu_1"):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)


def response(blocks, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        content=blocks,
    )


class ScriptedClient:
    """Plays back a fixed list of responses — the 'model' for offline tests."""

    def __init__(self, script: list):
        self._script = list(script)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        return self._script.pop(0)


def make_agent(home: Path, client=None, conn=None, **settings_overrides):
    """Build a NAM harness with an isolated home dir; optionally swap in a fake client."""
    from nam_agent.app import NamAgent
    from nam_agent.config import Settings

    # A test must describe its own world. `nam_agent/config.py` calls load_dotenv()
    # at import, so every Settings() default is quietly seeded from the
    # maintainer's .env. Each switch below changes what a turn DOES, and is
    # pinned off unless a test asks for it:
    #   fast_greetings   answer a pure greeting from the intent node with no
    #                    model call, so a scripted client's first response is
    #                    never consumed
    #   domain_only      answer only procurement work (nam_agent/nam/domain.py):
    #                    on wherever NAM_KG_URL is set, and a message the word
    #                    rules cannot decide costs one model call
    known = {f.name for f in dataclasses.fields(Settings)}
    for switch in ("fast_greetings", "domain_only"):
        if switch in known:
            settings_overrides.setdefault(switch, False)
    # the KG is the machine's own (NAM_KG_URL in .env): a test that wants one
    # names it, so an offline eval never waits on a real server
    settings_overrides.setdefault("kg_url", "")
    # and so are the KG tools under test (NAM_KG_TOOLS) and the KG's own API
    # (NAM_KG_API_URL, "" reads the KG through kg_url): a test names them
    for name in ("kg_tools", "kg_api_url", "nam_source"):
        if name in known:
            settings_overrides.setdefault(name, "")
    # and what NAM is in the chat (NAM_CHAT): the PR Creation Flow,
    # unless a test of the earlier driver names it
    if "nam_chat" in known:
        settings_overrides.setdefault("nam_chat", "flow")
    settings = Settings(home=home, **settings_overrides)
    if client is not None and not settings.api_key:
        settings.api_key = "offline"  # never read the real key for scripted runs
    return NamAgent(settings=settings, client=client, conn=conn)
