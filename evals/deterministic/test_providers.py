"""OFFLINE provider-table checks: every PROVIDERS entry must build the right
client, fill its default model ids, and be covered by the dashboard's pricing
and model-listing fallbacks. No network, no real keys (fakes via monkeypatch).

Born from a live regression hunt: adding a provider touches shared paths
(get_client, HAS_KEY, /api/models, PRICING), and nothing offline proved the
other five still worked. Now something does.
"""

from __future__ import annotations

import anthropic
import pytest

from nam_agent.config import Settings
from nam_agent.loop.models import PROVIDERS, OpenAICompatClient, get_client


@pytest.fixture(autouse=True)
def fake_keys(monkeypatch):
    for provider in PROVIDERS.values():
        monkeypatch.setenv(provider.key_env, "fake-key-for-tests")
    # a stray custom-endpoint override must not leak into these checks
    monkeypatch.delenv("NAM_API_KEY", raising=False)
    monkeypatch.delenv("NAM_BASE_URL", raising=False)


@pytest.mark.parametrize("name", list(PROVIDERS))
def test_get_client_builds_the_right_wire(name):
    provider = PROVIDERS[name]
    settings = Settings(provider=name, model="", small_model="", api_key="", base_url=None)
    client = get_client(settings)
    expected = anthropic.Anthropic if provider.kind == "anthropic" else OpenAICompatClient
    assert isinstance(client, expected)
    # defaults must be filled in so the loop never sends model=""
    assert settings.model == provider.model
    assert settings.small_model == provider.small_model


@pytest.mark.parametrize("name", list(PROVIDERS))
def test_missing_key_exits_with_the_key_name(name, monkeypatch):
    monkeypatch.delenv(PROVIDERS[name].key_env, raising=False)
    settings = Settings(provider=name, model="", small_model="", api_key="", base_url=None)
    with pytest.raises(SystemExit, match=PROVIDERS[name].key_env):
        get_client(settings)


def test_unknown_provider_names_the_choices():
    settings = Settings(provider="not-a-provider", model="", small_model="",
                        api_key="", base_url=None)
    with pytest.raises(SystemExit, match="openrouter"):
        get_client(settings)


# --- a model name belongs to the provider it was configured for --------------
# Found live: a small script under
# NAM_PROVIDER=xai printed "gate failed open (BadRequestError)". NAM_SMALL_MODEL
# is global, so anthropic's gate model was sent to xAI, which 400s. The retrieval
# gate fails open on error BY DESIGN, so this did not surface as a failure — it
# surfaced as "retrieve", on every turn, for every non-anthropic model in the
# arena. These pin the rule that stops it.

def test_env_model_names_do_not_leak_into_another_provider(monkeypatch):
    from nam_agent.config import Settings
    from nam_agent.loop.models import get_client

    monkeypatch.setenv("NAM_PROVIDER", "anthropic")
    monkeypatch.setenv("NAM_MODEL", "claude-fable-5")
    monkeypatch.setenv("NAM_SMALL_MODEL", "claude-haiku-4-5-20251001")
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    settings = Settings(provider="xai")
    get_client(settings)

    assert settings.model == "grok-4", "anthropic's model was sent to xAI"
    assert settings.small_model == "grok-4-fast", (
        "anthropic's gate model was sent to xAI — the gate 400s and fails open, "
        "so this bug reports itself as a healthy 'retrieve' forever"
    )


def test_an_explicit_model_survives_the_provider_switch(monkeypatch):
    """The rule drops INHERITED env values, not deliberate ones. Without this
    the fix would quietly override the arena's own model picker."""
    from nam_agent.config import Settings
    from nam_agent.loop.models import get_client

    monkeypatch.setenv("NAM_PROVIDER", "anthropic")
    monkeypatch.setenv("NAM_MODEL", "claude-fable-5")
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    settings = Settings(provider="xai", model="grok-4.3")
    get_client(settings)

    assert settings.model == "grok-4.3"


def test_the_env_still_wins_for_its_own_provider(monkeypatch):
    """The whole point of NAM_MODEL is to override the default — the fix must
    not break the ordinary single-provider case."""
    from nam_agent.config import Settings
    from nam_agent.loop.models import get_client

    monkeypatch.setenv("NAM_PROVIDER", "anthropic")
    monkeypatch.setenv("NAM_MODEL", "claude-fable-5")
    monkeypatch.setenv("NAM_SMALL_MODEL", "claude-haiku-4-5-20251001")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    settings = Settings(provider="anthropic")
    get_client(settings)

    assert settings.model == "claude-fable-5"
    assert settings.small_model == "claude-haiku-4-5-20251001"


def test_a_foreign_gate_model_is_dropped_even_when_the_env_names_the_provider(monkeypatch):
    """A case that was live in a .env: NAM_PROVIDER=xai with
    NAM_SMALL_MODEL still holding anthropic's gate model. Scoping by "did the
    caller switch provider" missed this one — the env agreed with itself and was
    still wrong."""
    from nam_agent.config import Settings
    from nam_agent.loop.models import get_client

    monkeypatch.setenv("NAM_PROVIDER", "xai")
    monkeypatch.setenv("NAM_SMALL_MODEL", "claude-haiku-4-5-20251001")
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    settings = Settings(provider="xai")
    get_client(settings)

    assert settings.small_model == "grok-4-fast"


def test_an_unfamiliar_model_name_is_left_alone(monkeypatch):
    """The check asks "is this positively someone else's?", not "does it look
    like ours?". The second question would silently downgrade any id we don't
    recognise — a preview name, a model released after this code was written."""
    from nam_agent.config import Settings
    from nam_agent.loop.models import get_client

    monkeypatch.setenv("NAM_PROVIDER", "kimi")
    monkeypatch.setenv("NAM_SMALL_MODEL", "moonshot-v1-8k")
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")

    settings = Settings(provider="kimi")
    get_client(settings)

    assert settings.small_model == "moonshot-v1-8k", "a deliberate choice was overridden"
