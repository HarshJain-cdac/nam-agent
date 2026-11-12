"""Model access — eight providers, one loop, zero framework.

The loop speaks one dialect: Anthropic's Messages shape (system/messages/tools
in, content blocks out). Providers plug in two ways:

  anthropic wire format (native)     → Anthropic, Kimi/Moonshot, GLM/Z.ai, MiniMax
  openai wire format (thin adapter)  → OpenAI, Google Gemini, DeepSeek, OpenRouter

Pick with NAM_PROVIDER=anthropic|openai|gemini|deepseek|minimax|kimi|glm|openrouter
and set that provider's API key in .env. Override the model ids with NAM_MODEL /
NAM_SMALL_MODEL if the defaults below age out — they're just strings. This
matters most for openrouter: it's a single key in front of hundreds of models,
so NAM_MODEL=<vendor>/<model> (e.g. "google/gemini-3.5-flash") picks whichever
one you want — and its defaults below are $0 ":free" ids, so it works with no
spend at all (rate-limited). The dashboard Settings tab lists the live catalog.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from types import SimpleNamespace

from nam_agent.config import Settings

log = logging.getLogger("nam_agent.models")


@dataclass(frozen=True)
class ProviderEndpoint:
    """One official regional endpoint and its matching model catalog."""

    label: str
    base_url: str
    catalog_url: str | None = None


@dataclass(frozen=True)
class Provider:
    kind: str        # 'anthropic' or 'openai' — the wire format
    key_env: str     # which env var holds the key
    base_url: str | None
    model: str       # default main model (the loop)
    small_model: str  # default cheap model (retrieval gate + consolidation)
    # Where to LIST this provider's models (the Settings picker). openai-wire
    # providers get {base_url}/models automatically; set this for providers
    # whose chat endpoint and catalog endpoint differ (e.g. kimi talks the
    # anthropic wire but lists models on its OpenAI-compatible API). The
    # defaults above are just starting points — any listed model is one click.
    catalog_url: str | None = None
    # The two models the chat switcher pins by default for this provider: a
    # flagship (top quality) and a fast one (cheap/low-latency). Distinct from
    # model/small_model — e.g. anthropic's loop default is sonnet-5, but the
    # flagship you'd showcase is opus-4.8. Blank falls back to model/small_model.
    flagship: str = ""
    fast: str = ""
    # Providers with separately-issued regional keys keep their endpoint choice
    # in a provider-specific env var.  NAM_BASE_URL remains the global custom
    # override for backwards compatibility, but must not leak across providers.
    base_url_env: str = ""
    endpoints: tuple[ProviderEndpoint, ...] = ()

    def default_pair(self) -> list[str]:
        """[flagship, fast], deduped — the switcher's default picks."""
        pair = [self.flagship or self.model, self.fast or self.small_model]
        return list(dict.fromkeys(m for m in pair if m))

    def configured_base_url(self) -> str | None:
        """Provider-scoped endpoint, falling back to the built-in default."""
        configured = os.getenv(self.base_url_env, "").strip() if self.base_url_env else ""
        return configured or self.base_url

    def catalog_for(self, base_url: str | None) -> str | None:
        """Return the catalog paired with *base_url*'s region."""
        normalized = (base_url or "").rstrip("/")
        for endpoint in self.endpoints:
            if endpoint.base_url.rstrip("/") == normalized:
                return endpoint.catalog_url
        return self.catalog_url


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider("anthropic", "ANTHROPIC_API_KEY", None,
                          "claude-sonnet-5", "claude-haiku-4-5-20251001",
                          catalog_url="https://api.anthropic.com/v1/models",
                          flagship="claude-opus-4-8", fast="claude-sonnet-5"),
    # The gpt-5.6 REASONING models (luna/sol/terra) can't use function tools on
    # /v1/chat/completions (they need /v1/responses), so every NAM harness turn 400s on
    # them. That constraint still holds — what changed is where the escape hatch
    # is: the whole `-chat-latest` line (5.3, 5.2, 5.1 and the bare gpt-5 alias)
    # is now 404 deprecated, so "fall back to the previous -chat-latest" is not
    # an option any more. gpt-5.5 is the newest plain model that returns a
    # tool_call on /v1/chat/completions; gpt-4.1-mini is a cheap tool-capable
    # gate. A `-latest` alias is deliberately NOT used — it silently changes
    # under a pinned benchmark, and test_openai_default_is_tool_capable rejects
    # one. base_url is None (SDK default) so point the picker at the catalog.
    "openai":    Provider("openai", "OPENAI_API_KEY", None,
                          "gpt-5.5", "gpt-4.1-mini",
                          catalog_url="https://api.openai.com/v1/models"),
    # one key, every lab's models, and a $0 tier: the default models below are
    # free ids (":free" suffix). Rate-limited (~50 req/day without credits).
    "openrouter": Provider("openai", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1",
                           "nvidia/nemotron-3-super-120b-a12b:free",
                           "google/gemma-4-26b-a4b-it:free"),
    "gemini":    Provider("openai", "GEMINI_API_KEY",
                          "https://generativelanguage.googleapis.com/v1beta/openai/",
                          "gemini-3.5-flash", "gemini-3.1-flash-lite",
                          # Google's Pro tier isn't "gemini-3.5-pro" (that id
                          # 404s); the current Pro is gemini-3.1-pro-preview.
                          flagship="gemini-3.1-pro-preview", fast="gemini-3.5-flash"),
    "deepseek":  Provider("openai", "DEEPSEEK_API_KEY", "https://api.deepseek.com",
                          "deepseek-v4-pro", "deepseek-v4-pro"),
    "minimax":   Provider("anthropic", "MINIMAX_API_KEY", "https://api.minimaxi.com/anthropic",
                          "MiniMax-M3", "MiniMax-M2",
                          catalog_url="https://api.minimaxi.com/anthropic/v1/models",
                          base_url_env="MINIMAX_BASE_URL",
                          endpoints=(
                              ProviderEndpoint("China", "https://api.minimaxi.com/anthropic",
                                               "https://api.minimaxi.com/anthropic/v1/models"),
                              ProviderEndpoint("Global", "https://api.minimax.io/anthropic",
                                               "https://api.minimax.io/anthropic/v1/models"),
                          )),
    # K3 is the flagship default; the gate/summarizer stays on cheap K2.6
    # (the live catalog has no plain "kimi-k2.7" — only -code variants; we
    # checked). Override with NAM_SMALL_MODEL=kimi-k3 if your key is K3-only.
    "kimi":      Provider("anthropic", "MOONSHOT_API_KEY", "https://api.moonshot.ai/anthropic",
                          "kimi-k3", "kimi-k2.6",
                          catalog_url="https://api.moonshot.ai/v1/models",
                          flagship="kimi-k3", fast="kimi-k2.7-code-highspeed",
                          base_url_env="MOONSHOT_BASE_URL",
                          endpoints=(
                              ProviderEndpoint("Global", "https://api.moonshot.ai/anthropic",
                                               "https://api.moonshot.ai/v1/models"),
                              ProviderEndpoint("China", "https://api.moonshot.cn/anthropic",
                                               "https://api.moonshot.cn/v1/models"),
                          )),
    "glm":       Provider("anthropic", "ZHIPU_API_KEY", "https://api.z.ai/api/anthropic",
                          "glm-5.2", "glm-5-turbo",
                          base_url_env="ZHIPU_BASE_URL",
                          endpoints=(
                              ProviderEndpoint("Global", "https://api.z.ai/api/anthropic"),
                              ProviderEndpoint("China", "https://open.bigmodel.cn/api/anthropic"),
                          )),
    # xAI Grok on its OpenAI-compatible endpoint. The model ids below are
    # starting points — add XAI_API_KEY and the picker lists the live catalog
    # (the authoritative source); pin whatever the current flagship/fast are.
    "xai":       Provider("openai", "XAI_API_KEY", "https://api.x.ai/v1",
                          "grok-4", "grok-4-fast",
                          catalog_url="https://api.x.ai/v1/models"),
    # OpenCode — OpenAI-compatible platform. Two endpoints: "zen" and "go"
    # share the same platform key. zen offers free models (default:
    # deepseek-v4-flash-free); go uses the standard deepseek-v4-flash.
    # The live catalog (GET /models) lists whatever the endpoint serves,
    # and the picker is the authoritative menu.
    "opencode_zen": Provider("openai", "OPENCODE_ZEN_API_KEY",
                               "https://opencode.ai/zen/v1",
                               "deepseek-v4-flash-free", "deepseek-v4-flash-free"),
    "opencode_go":  Provider("openai", "OPENCODE_GO_API_KEY",
                               "https://opencode.ai/zen/go/v1",
                               "deepseek-v4-flash", "deepseek-v4-flash"),
}


# Where each provider's key actually comes from. Pointing at ".env.example"
# was useless advice for anyone who installed from PyPI — that file only exists
# in a git checkout, so the one instruction the message gave could not be
# followed by the people most likely to need it.
KEY_URLS = {
    "anthropic": "https://console.anthropic.com/settings/keys",
    "openai": "https://platform.openai.com/api-keys",
    "gemini": "https://aistudio.google.com/apikey",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "openrouter": "https://openrouter.ai/keys",
    "kimi": "https://platform.moonshot.ai/console/api-keys",
    "glm": "https://z.ai/manage-apikey/apikey-list",
    "minimax": "https://platform.minimaxi.com/user-center/basic-information",
    "xai": "https://console.x.ai",
    "opencode_zen": "https://opencode.ai/zen",
    "opencode_go": "https://opencode.ai/zen",
}


def _no_key_message(name: str, key_env: str) -> str:
    """Say what to set, where to get it, and WHICH file we read.

    The old message named one env var and pointed at a file that does not exist
    off a git checkout. Three things were missing and each one cost a search:
    the URL to get a key, the absolute path of the .env actually in play, and
    the fact that NAM harness speaks to eleven providers, not one.
    """
    from nam_agent.config import DOTENV_PATH

    # Name the variable in BOTH branches. "add the line there" without saying
    # which line is the same dead end as pointing at .env.example was.
    where = (f"Add it to {DOTENV_PATH}:\n"
             f"    {key_env}=your-key-here"
             if DOTENV_PATH else
             f"No .env found from {os.getcwd()} upward — create one here:\n"
             f"    echo '{key_env}=your-key-here' >> .env")
    url = KEY_URLS.get(name)
    return (
        f"No API key for provider '{name}'.\n\n"
        f"  1. Get a key: {url}\n" if url else f"No API key for provider '{name}'.\n\n"
    ) + (
        f"  2. {where}\n\n"
        f"Other providers: {', '.join(sorted(PROVIDERS))}\n"
        f"Switch with NAM_PROVIDER=<name> and that provider's key."
    )


def _families(provider: Provider) -> set[str]:
    """The model FAMILIES this provider ships — "claude", "grok", "gemini"...

    Taken from the provider's own four defaults rather than a hand-kept list, so
    a new provider is covered the moment it is added. Aggregators whose ids are
    vendor-namespaced ("anthropic/claude-3" on openrouter) are excluded by the
    caller: for them the first segment names a VENDOR, not the host.
    """
    names = (provider.model, provider.small_model, provider.flagship, provider.fast)
    return {n.split("-")[0].lower() for n in names if n and "/" not in n}


def _belongs_elsewhere(model: str, provider_name: str) -> bool:
    """Is this model name positively another provider's?

    Deliberately asks the POSITIVE question. "Does it not look like ours?" would
    drop anything unfamiliar — a legitimately odd id, a preview name, a model
    added since — and silently downgrade a deliberate choice. This only fires
    when the family is one some OTHER provider actually owns, which is the case
    that produces a 400 rather than a surprise.
    """
    family = model.split("-")[0].lower()
    if "/" in model or not family:
        return False
    owner = {f: name for name, p in PROVIDERS.items() if "/" not in (p.model or "x")
             for f in _families(p)}.get(family)
    return bool(owner) and owner != provider_name


def get_client(settings: Settings):
    """Build the client for settings.provider and fill in default model ids.
    Returns anything with .messages.create(...) in the Anthropic shape."""
    provider = PROVIDERS.get(settings.provider)
    if provider is None:
        raise SystemExit(f"Unknown NAM_PROVIDER '{settings.provider}'. "
                         f"Pick one of: {', '.join(PROVIDERS)}")

    # .strip() so a trailing newline/space from a copy-paste doesn't corrupt the
    # auth header (headers are latin-1; a stray non-ASCII char errors cryptically).
    api_key = (settings.api_key or os.getenv(provider.key_env, "")).strip()
    if not api_key:
        raise SystemExit(_no_key_message(settings.provider, provider.key_env))
    try:
        api_key.encode("latin-1")
    except UnicodeEncodeError:
        raise SystemExit(
            f"{provider.key_env} contains a non-ASCII character (e.g. a smart quote "
            f"or arrow from a bad paste). Re-paste the key with no spaces or line breaks."
        )

    # A model name belongs to the provider it was configured FOR. NAM_MODEL and
    # NAM_SMALL_MODEL are global, so code that switches provider — the arena
    # races ten of them — carried anthropic's gate model to xAI, which answers
    # `400 Model not found: claude-haiku-4-5-20251001`. The retrieval gate then
    # FAILS OPEN by design, so it retrieved on every single turn for every
    # non-anthropic model instead of deciding, and reported that as a normal
    # "retrieve". A silent permanent failure wearing the costume of a healthy
    # decision.
    #
    # So: a value INHERITED from the env for a different provider is dropped
    # (the provider's own default fills in below); a value the caller passed
    # explicitly is kept, because that is a choice, not a leak. The two are
    # distinguishable exactly when the setting still equals the env string.
    for attr in ("model", "small_model"):
        inherited = os.getenv(f"NAM_{attr.upper()}", "").strip()
        if inherited and getattr(settings, attr) == inherited \
                and _belongs_elsewhere(inherited, settings.provider):
            setattr(settings, attr, "")

    settings.model = settings.model or provider.model
    settings.small_model = settings.small_model or provider.small_model
    base_url = settings.base_url or provider.configured_base_url()

    # a hung network call must never freeze a turn silently
    timeout = float(os.getenv("NAM_LLM_TIMEOUT", "120"))

    if provider.kind == "anthropic":
        import anthropic

        kwargs: dict = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        client = anthropic.Anthropic(**kwargs)
        # fixed temperature here too (the Messages API has no seed)
        fixed = {k: v for k, v in sampling().items() if k == "temperature"}
        if fixed and getattr(client, "messages", None) is not None:
            import functools

            client.messages.create = functools.partial(client.messages.create, **fixed)
            client.messages.stream = functools.partial(client.messages.stream, **fixed)
        return client
    return OpenAICompatClient(api_key=api_key, base_url=base_url, timeout=timeout,
                              fallback_model=settings.model)


_THINK_END = "</think>"
_TOOL_CALL = re.compile(r"<tool_call>(.*?)(?:</tool_call>|$)", re.DOTALL)
_XML_FUNCTION = re.compile(r"<function=([^>\s]+)>(.*?)(?:</function>|$)", re.DOTALL)
_XML_PARAM = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.DOTALL)


def _refuses_tools(exc: Exception) -> bool:
    """The server cannot take `tools` (vLLM without a tool-call parser)."""
    m = str(exc).lower()
    return ("enable-auto-tool-choice" in m or "tool-call-parser" in m
            or "tool choice requires" in m or "does not support tools" in m)


def _not_served(exc: Exception) -> bool:
    """The server does not have this model (404 on the model)."""
    m = str(exc).lower()
    return (getattr(exc, "status_code", None) == 404 or "404" in m) and \
        ("does not exist" in m or "not found" in m or "model" in m)


def _param_value(raw: str):
    """A Qwen XML parameter arrives as bare text. Numbers, booleans, lists and
    objects come back as JSON; anything that isn't valid JSON is a string."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def recover_text_tool_calls(text: str, tool_names: set[str]) -> tuple[str, list[dict]]:
    """Pull tool calls out of plain reply text, for endpoints that don't parse them.

    A vLLM server started without --tool-call-parser (and --reasoning-parser)
    hands back Qwen's raw output as `content`: its reasoning, a stray
    `</think>`, then the call as text, with `tool_calls` empty. The loop sees
    no call and ends the turn, so the user reads only the reasoning. This finds
    calls in both formats Qwen emits, the Qwen3-Coder XML
    (`<function=NAME><parameter=P>V</parameter></function>`) and the Hermes
    JSON (`{"name": ..., "arguments": {...}}`), each inside `<tool_call>`.

    Returns (text with the reasoning and the call markup removed, calls), each
    call a dict {name, input}. Only calls to a tool in `tool_names` count, so
    prose that merely mentions a tag is left alone.
    """
    if _THINK_END in text:
        text = text.rsplit(_THINK_END, 1)[1]
    calls: list[dict] = []
    for body in _TOOL_CALL.findall(text):
        found = [(name, {k: _param_value(v) for k, v in _XML_PARAM.findall(inner)})
                 for name, inner in _XML_FUNCTION.findall(body)]
        if not found:
            try:
                obj = json.loads(body.strip())
            except ValueError:
                obj = None
            if isinstance(obj, dict) and isinstance(obj.get("name"), str):
                args = obj.get("arguments", obj.get("parameters", {}))
                if isinstance(args, str):
                    args = _param_value(args)
                found = [(obj["name"], args if isinstance(args, dict) else {})]
        calls += [{"name": n, "input": a} for n, a in found if n in tool_names]
    if calls:
        text = _TOOL_CALL.sub("", text)
    return text.strip(), calls


def _recovered_blocks(text: str, tools) -> tuple[list, bool]:
    """Content blocks for a reply that carried no native tool_calls: the
    cleaned text, plus any tool calls recovered from it. The bool says whether
    any were recovered (i.e. whether the stop reason is tool_use)."""
    names = {t["name"] for t in tools or []}
    clean, calls = recover_text_tool_calls(text, names) if text else ("", [])
    blocks = [SimpleNamespace(type="text", text=clean)] if clean else []
    blocks += [SimpleNamespace(type="tool_use", id=f"call_text_{i}", name=c["name"],
                               input=c["input"], extra=None)
               for i, c in enumerate(calls)]
    return blocks, bool(calls)


def sampling() -> dict:
    """Temperature and seed for every model call: NAM_TEMPERATURE (default 0)
    and NAM_SEED (default 7). Left to the server, each model samples at its
    own default (often 1.0), so the same question got a different answer each
    time and from each model. An empty value sends nothing for that setting."""
    out: dict = {}
    for key, env, default, cast in (("temperature", "NAM_TEMPERATURE", "0", float),
                                    ("seed", "NAM_SEED", "7", int)):
        raw = os.getenv(env, default).strip()
        if raw:
            try:
                out[key] = cast(raw)
            except ValueError:
                log.warning("%s=%r is not a number; not sent", env, raw)
    return out


BAD_ARGUMENTS = "__arguments_not_json__"


def _arguments(raw: str | None) -> dict:
    """A tool call's arguments. Broken JSON from the model (it happens with
    small models) is not a crash: the call carries the raw text, and the
    registry answers it with an error the model can correct."""
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {BAD_ARGUMENTS: str(raw)[:400]}
    return value if isinstance(value, dict) else {BAD_ARGUMENTS: str(raw)[:400]}


class OpenAICompatClient:
    """Speaks the Anthropic Messages shape the loop expects, backed by an
    OpenAI-style chat.completions API. ~60 lines is the entire difference
    between the two wire formats — worth reading once.
    """

    # Two server shortcomings are handled here, once each, and remembered for
    # the client's life, so the user never sees them on a turn:
    #   a vLLM server started without --enable-auto-tool-choice and
    #   --tool-call-parser refuses any request with `tools` (HTTP 400). The
    #   client then switches to TEXT TOOLS: the tools are described in the
    #   system prompt, the model writes <tool_call>{...}</tool_call>, and
    #   recover_text_tool_calls reads it back (NAM_TOOL_MODE=text forces it,
    #   =native turns the fallback off).
    #   a model the server does not serve (404, e.g. a small model it never
    #   loaded) is answered by `fallback_model` (the main model) instead.
    text_tools = False
    fallback_model: str | None = None
    _missing: frozenset = frozenset()

    def __init__(self, api_key: str, base_url: str | None = None, timeout: float = 120.0,
                 fallback_model: str | None = None):
        import openai

        # one retry, not the SDK's two: a hung server must not hold a turn for
        # three full timeouts
        self._client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout,
                                     max_retries=1)
        self.messages = SimpleNamespace(create=self._create, stream=self._stream)
        self.fallback_model = fallback_model
        mode = os.getenv("NAM_TOOL_MODE", "auto").strip().lower()
        self.text_tools = mode == "text"
        self._tool_fallback = mode != "native"
        # the same question gets the same answer: fixed sampling (sampling())
        self.sampling = sampling()

    def _to_openai(self, *, model, messages, max_tokens, system=None, tools=None) -> dict:
        if tools and self.text_tools:
            return self._to_openai_text(model=model, messages=messages, max_tokens=max_tokens,
                                        system=system, tools=tools)
        oai_messages = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                oai_messages.append({"role": message["role"], "content": content})
            elif message["role"] == "assistant":
                # anthropic content blocks → assistant text + tool_calls
                text = "".join(b.text for b in content if getattr(b, "type", "") == "text")
                calls = []
                for b in content:
                    if getattr(b, "type", "") != "tool_use":
                        continue
                    call = {"id": b.id, "type": "function",
                            "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                    extra = getattr(b, "extra", None)   # Gemini thought_signature
                    if extra:
                        call["extra_content"] = extra
                    calls.append(call)
                entry: dict = {"role": "assistant", "content": text or None}
                if calls:
                    entry["tool_calls"] = calls
                oai_messages.append(entry)
            else:
                # anthropic tool_result blocks → one 'tool' message each
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        oai_messages.append({
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": block["content"],
                        })

        kwargs: dict = {"model": model, "messages": oai_messages,
                        "max_completion_tokens": max_tokens}
        if tools:
            kwargs["tools"] = [
                {"type": "function",
                 "function": {"name": t["name"], "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in tools
            ]
        return kwargs

    def _to_openai_text(self, *, model, messages, max_tokens, system=None, tools=()) -> dict:
        """The same request for a server that cannot take `tools`: the tools
        described in the system prompt (compactly: a schema is sent once per
        call, so its size is paid every call), earlier calls and results as
        text in the conversation."""
        lines = ["", "Tools. To use one, reply with nothing but one line per call:",
                 '<tool_call>{"name": "<tool>", "arguments": {...}}</tool_call>',
                 "Then wait for its result. Available:"]
        for t in tools:
            params = (t.get("input_schema") or {}).get("properties") or {}
            need = (t.get("input_schema") or {}).get("required") or []
            sig = ", ".join(f"{k}{'' if k in need else '?'}: {v.get('type', 'any')}"
                            for k, v in params.items())
            lines.append(f"- {t['name']}({sig}): {t['description'][:300]}")
        # the tool list first: it is the same every call, so a server that
        # caches prompt prefixes (vLLM --enable-prefix-caching) computes it
        # once; what changes each turn (time, memory, skills) comes after
        oai = [{"role": "system", "content": "\n".join(lines[1:]) + "\n\n" + (system or "")}]
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                oai.append({"role": message["role"], "content": content})
            elif message["role"] == "assistant":
                text = "".join(b.text for b in content if getattr(b, "type", "") == "text")
                calls = "".join(
                    "<tool_call>" + json.dumps({"name": b.name, "arguments": b.input})
                    + "</tool_call>" for b in content if getattr(b, "type", "") == "tool_use")
                oai.append({"role": "assistant", "content": (text + calls) or "(no reply)"})
            else:
                results = [f"<tool_result>{b['content']}</tool_result>" for b in content
                           if isinstance(b, dict) and b.get("type") == "tool_result"]
                oai.append({"role": "user", "content": "\n".join(results) or "(no result)"})
        return {"model": model, "messages": oai, "max_completion_tokens": max_tokens}

    def _call(self, kwargs: dict, **extra):
        """Run chat.completions.create with the max_tokens key-name fallback
        (older OpenAI-compatible endpoints only know max_tokens, not the newer
        max_completion_tokens). Only retry when the error is ABOUT that param —
        retrying on any error masked the real failure (e.g. a gpt-5.x call would
        fail for some other reason, then the max_tokens retry buried it under a
        confusing 'use max_completion_tokens' message). A model the server
        does not serve goes to the fallback model, once and then always."""
        if kwargs.get("model") in self._missing and self.fallback_model:
            kwargs = {**kwargs, "model": self.fallback_model}
        try:
            return self._call_once(kwargs, **extra)
        except Exception as exc:
            model = kwargs.get("model")
            if not (self.fallback_model and model != self.fallback_model and _not_served(exc)):
                raise
            log.warning("model %s is not served by this endpoint: using %s instead",
                        model, self.fallback_model)
            self._missing = self._missing | {model}
            return self._call_once({**kwargs, "model": self.fallback_model}, **extra)

    def _call_once(self, kwargs: dict, **extra):
        # fixed sampling unless the caller set its own; a model that refuses a
        # setting (some reasoning models take only temperature 1) loses that
        # setting, once and then always
        fixed = getattr(self, "sampling", {})
        kwargs = {**{k: v for k, v in fixed.items() if k not in kwargs}, **kwargs}
        try:
            return self._call_sampled(kwargs, **extra)
        except Exception as exc:
            dropped = [k for k in fixed if k in str(exc).lower() and k in kwargs]
            if not dropped:
                raise
            log.warning("the model refuses %s: sending it without", ", ".join(dropped))
            for k in dropped:
                fixed.pop(k, None)
                kwargs.pop(k, None)
            return self._call_sampled(kwargs, **extra)

    def _call_sampled(self, kwargs: dict, **extra):
        try:
            return self._client.chat.completions.create(**kwargs, **extra)
        except Exception as exc:
            m = str(exc).lower()
            if "max_completion_tokens" not in m and "max_tokens" not in m:
                raise
            k = dict(kwargs)
            k["max_tokens"] = k.pop("max_completion_tokens", None)
            return self._client.chat.completions.create(**k, **extra)

    def _with_tools(self, build, send):
        """send(build()), and when the server refuses tools, the same request
        again in text-tool mode (remembered)."""
        try:
            return send(build())
        except Exception as exc:
            if self.text_tools or not getattr(self, "_tool_fallback", True) \
                    or not _refuses_tools(exc):
                raise
            log.warning("the endpoint refuses tool calling (%s): describing tools in the "
                        "prompt instead", str(exc)[:120])
            self.text_tools = True
            return send(build())

    def _create(self, *, model, messages, max_tokens, system=None, tools=None):
        return self.create(model=model, messages=messages, max_tokens=max_tokens,
                           system=system, tools=tools)

    def create(self, *, model, messages, max_tokens, system=None, tools=None, extra=None):
        """messages.create with provider options the Anthropic shape has no
        place for, e.g. extra={"reasoning_effort": "low"} (nam_agent/nam/llm.py)."""
        response = self._with_tools(
            lambda: self._to_openai(model=model, messages=messages, max_tokens=max_tokens,
                                    system=system, tools=tools),
            lambda kwargs: self._call(kwargs, **(extra or {})))
        return self._message(response, model, tools)

    def _message(self, response, model, tools):
        """One chat.completions response → the Anthropic-shaped message.
        `tools` needs only each tool's "name" (for recovering text calls)."""
        if not getattr(response, "choices", None):
            # some OpenAI-compatible endpoints (e.g. OpenRouter on a rate
            # limit) return 200 with an error body and no choices: surface
            # that message instead of dying on a TypeError below
            err = getattr(response, "error", None) or "endpoint returned no choices"
            raise RuntimeError(f"{model}: {err}")
        choice = response.choices[0].message
        blocks = []
        recovered = False
        if not choice.tool_calls:
            blocks, recovered = _recovered_blocks(choice.content or "", tools)
        elif choice.content:
            blocks.append(SimpleNamespace(type="text", text=choice.content))
        for call in choice.tool_calls or []:
            blocks.append(SimpleNamespace(
                type="tool_use", id=call.id, name=call.function.name,
                input=_arguments(call.function.arguments),
                # Gemini's thinking models attach a thought_signature here and
                # REQUIRE it echoed back with the tool call next turn, else the
                # follow-up 400s ("missing a thought_signature"). Carry it so
                # _to_openai can put it back. None for every other provider.
                extra=getattr(call, "extra_content", None),
            ))
        usage = getattr(response, "usage", None)
        # a reply cut off at max_tokens says so, as Anthropic's does
        cut = getattr(response.choices[0], "finish_reason", "") == "length"
        return SimpleNamespace(
            stop_reason="tool_use" if choice.tool_calls or recovered
            else "max_tokens" if cut else "end_turn",
            usage=SimpleNamespace(
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0),
            ),
            content=blocks,
        )

    def _stream(self, *, model, messages, max_tokens, system=None, tools=None):
        """Anthropic-shaped streaming over an OpenAI chat.completions stream —
        same two-format bridge as _create, but yielding text as it arrives.
        Used by the loop when stream=True (e.g. the dashboard's live chat)."""
        args = {"model": model, "messages": messages, "max_tokens": max_tokens,
                "system": system, "tools": tools}
        return _OpenAIStream(self, self._to_openai(**args), args)


class _OpenAIStream:
    """A context manager mirroring anthropic's messages.stream(): iterate
    .text_stream for text deltas, then .get_final_message() for the assembled
    Anthropic-shaped response (text + reassembled tool calls + usage)."""

    def __init__(self, client: OpenAICompatClient, kwargs: dict, args: dict | None = None):
        self._client = client
        self._kwargs = kwargs
        self._args = args                   # to rebuild the request in text-tool mode
        # the tool names, whichever way the tools travel (native or in the prompt)
        self._names = ([{"name": t["name"]} for t in (args or {}).get("tools") or []]
                       or [{"name": t["function"]["name"]} for t in kwargs.get("tools") or []])
        self._text: list[str] = []
        self._tools: dict[int, dict] = {}   # index → {id, name, args}
        self._usage = None
        # called once when the model starts sending reasoning (vLLM's
        # reasoning_content), so a live UI says "Reasoning" only when it is
        self.on_reasoning = None
        self._reasoned = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        def build():
            if self._args is not None:
                self._kwargs = self._client._to_openai(**self._args)
            return self._kwargs

        def send(kwargs):
            # the first chunk is where a refused request raises
            chunks = iter(self._client._call(kwargs, stream=True,
                                             stream_options={"include_usage": True}))
            return chunks, next(chunks, None)

        if self._args is not None:
            stream, first = self._client._with_tools(build, send)
        else:
            stream, first = send(self._kwargs)
        yield from self._consume(stream, first)

    def _consume(self, stream, first):
        import itertools

        stream = itertools.chain([first] if first is not None else [], stream)
        for chunk in stream:
            if getattr(chunk, "usage", None):
                self._usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if not self._reasoned and (getattr(delta, "reasoning_content", None)
                                       or getattr(delta, "reasoning", None)):
                self._reasoned = True
                if self.on_reasoning:
                    self.on_reasoning()
            if getattr(delta, "content", None):
                self._text.append(delta.content)
                # a call arriving as text (see recover_text_tool_calls) is
                # markup, not reply: stop showing text once it starts
                if "<tool_call>" not in "".join(self._text):
                    yield delta.content
            for tc in (getattr(delta, "tool_calls", None) or []):
                slot = self._tools.setdefault(tc.index, {"id": None, "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments

    def get_final_message(self):
        blocks = []
        text = "".join(self._text)
        recovered = False
        if not self._tools:
            names = self._names
            blocks, recovered = _recovered_blocks(text, names)
            if not blocks and names:
                # Some vLLM builds drop a text tool call from the STREAM (the
                # tokens are counted but never sent), leaving only reasoning
                # and nothing after it. The same request without streaming
                # does return the call, so ask once more that way.
                return self._client._message(
                    self._client._call(self._kwargs), self._kwargs["model"], names)
        elif text:
            blocks.append(SimpleNamespace(type="text", text=text))
        for slot in self._tools.values():
            blocks.append(SimpleNamespace(
                type="tool_use", id=slot["id"], name=slot["name"],
                input=json.loads(slot["args"] or "{}")))
        usage = self._usage
        return SimpleNamespace(
            stop_reason="tool_use" if self._tools or recovered else "end_turn",
            usage=SimpleNamespace(
                input_tokens=getattr(usage, "prompt_tokens", 0),
                output_tokens=getattr(usage, "completion_tokens", 0)),
            content=blocks,
        )
