"""Which model answers which NAM task, with fallbacks, and a record of every
attempt.

THE TASKS
  A task is one place in the harness that asks a model something on its own,
  outside the agent's turn. The model only ever picks or proposes; the
  harness checks the answer:

    domain    after a turn that used no tool, whether the agent's reply
              answered a request that is not procurement work from general
              knowledge (domain.py): "ok" or "strayed", nothing else; a
              strayed reply goes back to the agent once. Small model.

  The harness API's flows (nam_agent.nam.run, playbooks/) ask five more:

    route     one flow id from a closed list, when the prompt is ambiguous
              (router.py). Small model.
    followup  the next narrowing question in need capture (followup.py).
              Main model.
    review    ambiguous or conflicting clauses in the drafts, on top of the
              code's own checks (playbooks/review.py). Main model.
    intent    what an officer's message means while a case is open
              (understand.py): one meaning from a closed list. Small model.
    quantity  a quantity and unit for the quantity sheet's lines that no past
              tender gives (playbooks/quantity.py), marked as the AI's and
              approved by the officer at the quantity gate. Main model.

  The chat's PR Creation Flow is the agent's own turn, its tools checked in
  code (stage_tools.py). A new call site adds its task to TASKS.

THE CONFIGURATION
  NAM_MODELS="route=model-a,model-b; followup=model-c"
  Per task, the models to try in order: the first is used, the next only
  when the one before failed. A task left out gets the small model then the
  main one for a "small" task, the main then the small one for a "main"
  task. A name that is not a task is ignored with a warning, so a setting
  written for another version never stops the server.

ONE CALL (complete)
  For each model of the task's chain: no reply in the task's time, or an
  error, goes to the next model; a reply cut off at its token limit, or
  empty, is asked once more for a shorter reply, then the next model. When
  every model failed, ModelUnavailable says why.

THE FEEDBACK LOOP (checked)
  An answer is used only once the call site's check accepts it. A rejected
  answer goes back to the model with the reason, at most RETRIES more times;
  each rejection is an audit row.

THE RECORD
  Every attempt is one row in the audit log (audit.py, kind "model"): the
  model, its place in the chain, the outcome, tokens and time. The prompt
  itself is not stored, only its length and hash.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from types import SimpleNamespace

from nam_agent.nam import audit

log = logging.getLogger("nam_agent.nam.llm")

@dataclass(frozen=True)
class Task:
    name: str
    purpose: str
    tier: str                 # "small" or "main": which model it gets by default
    max_tokens: int           # a ceiling: reasoning models need headroom to think
    timeout_s: float          # per attempt
    reasoning: str = ""       # a hint for providers that take one ("low")


TASKS: dict[str, Task] = {t.name: t for t in (
    # the agent stays on procurement work (nam_agent/nam/domain.py): its reply is
    # checked after a turn with no tool; a slow or absent model keeps it
    Task("domain", "whether a reply strayed from procurement work", "small", 400, 10.0,
         "low"),
    Task("route", "pick one flow id from a closed list", "small", 600, 20.0, "low"),
    # 20s an attempt: a local model that has not answered by then rarely does,
    # and the tenders' own question is there at once (followup.ASK_BUDGET_S)
    Task("followup", "the next narrowing question in need capture", "main", 1500, 20.0, "low"),
    Task("review", "ambiguous or conflicting clauses the code checks miss", "main", 2000,
         60.0, "low"),
    Task("quantity", "a quantity and unit for lines the tenders do not give", "main", 4000,
         60.0, "low"),
    Task("intent", "what the officer's message means in the open flow", "small", 600,
         15.0, "low"),
)}

SHORTER = ("\n\nYour last reply was cut off at its length limit. Reply again, much "
           "shorter, with only what was asked.")


class ModelUnavailable(Exception):
    """No model of the task's chain gave a usable reply."""


@dataclass(frozen=True)
class Call:
    model: str
    system: str
    user: str
    max_tokens: int
    reasoning: str = ""


@dataclass(frozen=True)
class Reply:
    text: str
    truncated: bool = False
    tokens_in: int = 0
    tokens_out: int = 0


def parse_chains(spec: str, model: str, small_model: str) -> dict[str, tuple[str, ...]]:
    """NAM_MODELS -> {task: models in order}. Raises ValueError on a
    task that does not exist or an entry that is not task=models."""
    chains = {}
    for name, task in TASKS.items():
        first, second = (small_model, model) if task.tier == "small" else (model, small_model)
        chains[name] = tuple(dict.fromkeys(m for m in (first, second) if m))
    for entry in (spec or "").replace("\n", ";").split(";"):
        if not entry.strip():
            continue
        name, sep, models = entry.partition("=")
        name = name.strip()
        if not sep:
            raise ValueError(f"NAM_MODELS: '{entry.strip()[:60]}' is not task=model,...")
        if name not in TASKS:
            log.warning("NAM_MODELS: no task '%s' (tasks: %s); ignored", name[:40],
                        ", ".join(TASKS))
            continue
        listed = tuple(dict.fromkeys(m.strip() for m in models.split(",") if m.strip()))
        if not listed:
            raise ValueError(f"NAM_MODELS: task '{name}' names no model")
        chains[name] = listed
    return chains


@dataclass
class Models:
    """The models per task and how to reach them. `transport(Call) -> Reply`
    is the one place a request leaves the harness."""

    transport: Callable[[Call], Reply]
    chains: dict[str, tuple[str, ...]]

    def chain(self, task: str) -> tuple[str, ...]:
        return self.chains.get(task, ())

    @classmethod
    def from_settings(cls, client, settings) -> Models:
        """Over nam_agent's own client. Raises ValueError on a bad NAM_MODELS."""
        chains = parse_chains(getattr(settings, "nam_models", ""), settings.model,
                              settings.small_model)
        return cls(transport=client_transport(client), chains=chains)

    @classmethod
    def scripted(cls, ask: Callable[[str, str], str], name: str = "scripted") -> Models:
        """One (system, user) -> text callable for every task: evals, and
        callers that bring their own model."""
        return cls(transport=lambda call: Reply(text=ask(call.system, call.user) or ""),
                   chains={t: (name,) for t in TASKS})


def client_transport(client) -> Callable[[Call], Reply]:
    """Call -> Reply over a client in the Anthropic Messages shape (nam_agent's
    get_client). A provider that refuses the reasoning hint is asked without
    it from then on."""
    state = {"hint": True}

    def send(call: Call) -> Reply:
        kwargs = {"model": call.model, "max_tokens": call.max_tokens, "system": call.system,
                  "messages": [{"role": "user", "content": call.user}]}
        create = getattr(client, "create", None)       # OpenAICompatClient: takes extra=
        if call.reasoning and state["hint"] and callable(create):
            try:
                return _reply(create(**kwargs, extra={"reasoning_effort": call.reasoning}))
            except Exception as exc:
                if "reasoning" not in str(exc).lower():
                    raise
                state["hint"] = False
        return _reply(client.messages.create(**kwargs))

    return send


def _reply(message) -> Reply:
    usage = getattr(message, "usage", None) or SimpleNamespace()
    text = "".join(b.text for b in getattr(message, "content", None) or []
                   if getattr(b, "type", "") == "text")
    return Reply(text=text, truncated=getattr(message, "stop_reason", "") == "max_tokens",
                 tokens_in=int(getattr(usage, "input_tokens", 0) or 0),
                 tokens_out=int(getattr(usage, "output_tokens", 0) or 0))


def models_of(ctx) -> Models | None:
    """The context's models: its own, or its scripted `ask_model`, or none."""
    if getattr(ctx, "models", None) is not None:
        return ctx.models
    if getattr(ctx, "ask_model", None) is not None:
        return Models.scripted(ctx.ask_model)
    return None


def _timed(send: Callable[[Call], Reply], call: Call, timeout_s: float) -> Reply:
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(send, call).result(timeout=timeout_s)
    finally:
        pool.shutdown(wait=False)       # a hung call must not hold the request


def complete(ctx, task: str, system: str, user: str) -> tuple[str, str]:
    """(text, model that gave it). Raises ModelUnavailable."""
    spec = TASKS[task]
    models = models_of(ctx)
    chain = models.chain(task) if models is not None else ()
    if not chain:
        raise ModelUnavailable(f"no model configured for {task}")
    digest = hashlib.sha256(f"{system}\n{user}".encode()).hexdigest()[:16]
    failures = []
    for position, model in enumerate(chain):
        for attempt in (1, 2):
            call = Call(model=model, system=system, max_tokens=spec.max_tokens,
                        user=user if attempt == 1 else user + SHORTER, reasoning=spec.reasoning)
            started = time.monotonic()
            reply, outcome = None, "ok"
            try:
                reply = _timed(models.transport, call, spec.timeout_s)
            except FutureTimeout:
                outcome = "timeout"
            except Exception as exc:  # noqa: BLE001 - any failure hands over to the next model
                outcome = f"unavailable:{type(exc).__name__}"
            if reply is not None:
                if reply.truncated:
                    outcome = "truncated"
                elif not reply.text.strip():
                    outcome = "empty"
            ms = int((time.monotonic() - started) * 1000)
            _record(ctx, task, model, position, attempt, outcome, ms, reply, call, digest)
            if outcome == "ok":
                return reply.text, model
            failures.append(f"{model}: {outcome}")
            if outcome not in ("truncated", "empty"):
                break                   # an outage: straight to the next model
    raise ModelUnavailable(f"{task}: " + "; ".join(failures)[:400])


def _record(ctx, task, model, position, attempt, outcome, ms, reply, call, digest) -> None:
    code = "" if outcome == "ok" else outcome.split(":")[0]
    audit.record(ctx.conn, kind="model", name=task, effect="model",
                 status="ok" if outcome == "ok" else "error",
                 error_code={"unavailable": "model_unavailable"}.get(code, code),
                 attempts=attempt, ms=ms,
                 detail={"model": model[:120], "position": position, "fallback": position > 0,
                         "outcome": outcome[:80], "prompt_chars": len(call.user),
                         "prompt_sha256": digest,
                         "tokens_in": reply.tokens_in if reply else 0,
                         "tokens_out": reply.tokens_out if reply else 0})


def asker(ctx, task: str) -> Callable[[str, str], str] | None:
    """(system, user) -> text for one task, or None with no model. The
    callable raises ModelUnavailable, and `.last` is the model that answered."""
    if models_of(ctx) is None or not models_of(ctx).chain(task):
        return None

    def ask(system: str, user: str) -> str:
        text, ask.last = complete(ctx, task, system, user)
        return text

    ask.last, ask.ctx, ask.task = "", ctx, task
    return ask


def json_object(text: str) -> dict | None:
    """The first {...} in a model's reply, parsed, or None."""
    import json

    if "{" not in (text or ""):
        return None
    try:
        out = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, TypeError):
        return None
    return out if isinstance(out, dict) else None


RETRIES = 2              # a rejected answer is sent back this many times at most
FEEDBACK = ("\n\nYour last answer was not used: {why}. Answer again, exactly in the format "
            "asked for.")


class Rejected(Exception):
    """The model answered, and the harness's check refused the answer."""


def checked(ask: Callable[[str, str], str], system: str, user: str,
            check: Callable[[str], object], retries: int = RETRIES):
    """The model's answer once `check` accepts it: check(text) returns the
    value to use, or raises Rejected(why). A rejected answer goes back to the
    model with its reason, at most `retries` more times, each rejection an
    audit row. Raises ModelUnavailable (no model answered) or Rejected (the
    last reason, when every answer was refused)."""
    why = ""
    for attempt in range(1, retries + 2):
        text = ask(system, user if not why else user + FEEDBACK.format(why=why[:300]))
        try:
            return check(text)
        except Rejected as exc:
            why = str(exc) or "the answer was refused"
            rejected(ask, attempt, why)
    raise Rejected(why)


def rejected(ask, attempt: int, why: str) -> None:
    """One audit row for an answer the harness refused (outcome "rejected")."""
    ctx = getattr(ask, "ctx", None)
    if ctx is None or getattr(ctx, "conn", None) is None:
        return
    audit.record(ctx.conn, kind="model", name=getattr(ask, "task", "") or "model",
                 effect="model", status="error", error_code="rejected", attempts=attempt,
                 detail={"model": str(getattr(ask, "last", ""))[:120], "outcome": "rejected",
                         "reason": why[:200]})


def client_asker(client, settings, conn, task: str) -> Callable[[str, str], str] | None:
    """asker() for code outside a harness call (the agent's nam_capture_need
    tool): nam_agent's client, the configured chain, the audit log in `conn`.
    None without a client or with a bad NAM_MODELS (logged)."""
    if client is None:
        return None
    try:
        models = Models.from_settings(client, settings)
    except ValueError as exc:
        log.warning("%s; NAM model calls are off", exc)
        return None
    return asker(SimpleNamespace(conn=conn, models=models), task)


def configured(models: Models | None) -> dict:
    """What /v1/nam/health shows: the models per task."""
    if models is None:
        return {"configured": False, "tasks": {}}
    return {"configured": any(models.chains.values()),
            "tasks": {t: list(models.chain(t)) for t in TASKS}}
