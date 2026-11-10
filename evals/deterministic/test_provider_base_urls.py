"""Regional Base URL choices for providers with separate domestic/global APIs."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nam_agent.config import Settings
from nam_agent.loop import models
from nam_agent.loop.models import PROVIDERS

EXPECTED_ENDPOINTS = {
    "minimax": [
        ("China", "https://api.minimaxi.com/anthropic",
         "https://api.minimaxi.com/anthropic/v1/models"),
        ("Global", "https://api.minimax.io/anthropic",
         "https://api.minimax.io/anthropic/v1/models"),
    ],
    "glm": [
        ("Global", "https://api.z.ai/api/anthropic", None),
        ("China", "https://open.bigmodel.cn/api/anthropic", None),
    ],
    "kimi": [
        ("Global", "https://api.moonshot.ai/anthropic",
         "https://api.moonshot.ai/v1/models"),
        ("China", "https://api.moonshot.cn/anthropic",
         "https://api.moonshot.cn/v1/models"),
    ],
}


def test_regional_provider_endpoint_choices_match_official_urls():
    for name, expected in EXPECTED_ENDPOINTS.items():
        provider = PROVIDERS[name]
        assert [(e.label, e.base_url, e.catalog_url) for e in provider.endpoints] == expected
        assert provider.base_url_env


@pytest.mark.parametrize(
    ("provider_name", "base_url_env", "selected_url"),
    [
        ("minimax", "MINIMAX_BASE_URL", "https://api.minimax.io/anthropic"),
        ("glm", "ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/anthropic"),
        ("kimi", "MOONSHOT_BASE_URL", "https://api.moonshot.cn/anthropic"),
    ],
)
def test_client_uses_provider_specific_base_url(
    monkeypatch, tmp_path, provider_name, base_url_env, selected_url
):
    captured = {}

    class StubAnthropicClient:
        def __init__(self, *, api_key, base_url, timeout):
            captured.update(api_key=api_key, base_url=base_url, timeout=timeout)

    provider = PROVIDERS[provider_name]
    monkeypatch.setenv(provider.key_env, "test-key")
    monkeypatch.setenv(base_url_env, selected_url)
    monkeypatch.delenv("NAM_BASE_URL", raising=False)
    monkeypatch.setitem(
        __import__("sys").modules,
        "anthropic",
        SimpleNamespace(Anthropic=StubAnthropicClient),
    )

    client = models.get_client(Settings(
        provider=provider_name, api_key="", base_url=None, model="",
        small_model="", home=tmp_path,
    ))

    assert isinstance(client, StubAnthropicClient)
    assert captured["base_url"] == selected_url

