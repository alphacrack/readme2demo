"""Thin LLM client for the non-agent passes (planner, distiller, tutorial).

Backends, selected via :func:`set_backend` (wired to config/CLI):

- ``api``        — Anthropic SDK; needs ANTHROPIC_API_KEY. Best for scale,
                   concurrency, and multi-tenant/hosted use.
- ``claude-cli`` — shells out to ``claude -p`` on the HOST, so it rides your
                   Claude Code subscription (Pro/Max plans include a monthly
                   Agent SDK credit that covers the ``claude -p`` command).
                   Fully supported for self-hosted, single-operator runs —
                   you run it against your own repos on your own machine.
                   NOT for a multi-tenant hosted service: per Anthropic's
                   terms, subscription / claude.ai-login auth may not power a
                   product offered to other end users — use ``api`` there.
                   Practical caveats: subscription rate/usage caps apply, and
                   it's slower (one host subprocess, 600s timeout, serial).
- ``gemini``     — Google Gemini via the ``google-genai`` SDK; needs
                   GEMINI_API_KEY. Wired up by the ``--gemini`` preset, which
                   also runs the sandboxed agent on the OpenHands engine so a
                   whole run can happen off the Claude subscription/harness.
                   Never auto-selected — you opt in explicitly. The model name
                   is never hardcoded: ``--gemini <model>`` / ``--model`` /
                   GEMINI_MODEL env var, or a loud error.
- ``openai``     — OpenAI via the ``openai`` SDK; needs OPENAI_API_KEY. Wired
                   up by the ``--openai`` preset (OpenHands agent + OpenAI
                   passes). Never auto-selected; like Gemini, no model name is
                   hardcoded: ``--openai <model>`` / ``--model`` /
                   OPENAI_MODEL env var, or a loud error.
- ``auto``       — ``api`` if ANTHROPIC_API_KEY is set, else ``claude-cli``
                   if the ``claude`` binary is on PATH.

The provider presets (``--gemini`` / ``--openai`` / ``--anthropic``) are
described by :data:`PROVIDERS`; each pairs one of these backends with the
OpenHands engine and bridges the provider's key into litellm-style
``LLM_API_KEY``/``LLM_MODEL`` (see :func:`apply_provider_session`).

All calls return usage cost so the manifest can aggregate one number per run.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal, Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Rough public pricing (USD per million tokens), used only for cost *estimates*
# in the manifest. Update as needed; correctness of the pipeline never depends
# on these numbers.
_PRICES: dict[str, tuple[float, float]] = {
    "default": (3.0, 15.0),
    # Gemini public list prices (input, output) — estimates only. Unlisted
    # models fall back to "default"; there is deliberately NO default Gemini
    # *model name* anywhere in this codebase (see _provider_model).
    "gemini-3.5-flash": (1.50, 9.0),
    "gemini-3-flash-preview": (0.50, 3.0),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.1-pro-preview": (2.0, 12.0),
    # OpenAI public list prices — estimates only, same rules as above.
    "gpt-5.1": (1.25, 10.0),
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
}


@dataclass
class LLMResponse:
    text: str
    cost_usd: float


class LLMError(RuntimeError):
    pass


Backend = Literal["auto", "api", "claude-cli", "gemini", "openai"]

_BACKENDS = ("auto", "api", "claude-cli", "gemini", "openai")

_backend: Backend = "auto"

_CLI_TIMEOUT_S = 600
_CLI_NEXT_STEP_HINT = (
    " Check login with `claude -p hello`, or switch to "
    "`--llm-backend api` with ANTHROPIC_API_KEY set."
)


@dataclass(frozen=True)
class ProviderSpec:
    """One provider preset (``--gemini`` / ``--openai`` / ``--anthropic``).

    A preset runs the sandboxed agent on the OpenHands engine (litellm-style
    ``LLM_API_KEY``/``LLM_MODEL``) and the planner/distiller/tutorial passes on
    ``backend``, all authenticated by the single ``key_env`` variable.
    """

    name: str            # preset/flag name, e.g. "gemini" (flag --gemini)
    title: str           # human name for error messages, e.g. "Gemini"
    backend: str         # llm backend for the planner/distiller/tutorial passes
    key_env: str         # host env var holding the provider API key
    model_env: str       # host env var naming the model (fallback)
    litellm_prefix: str  # LLM_MODEL prefix the OpenHands engine expects
    model_prefixes: tuple[str, ...]  # names recognized as this provider's models
    # Fallback model when neither a flag nor model_env names one. Only
    # Anthropic has one (the repo-wide config default); Google and OpenAI
    # retire model names with hard 404s, so guessing is a loud error instead
    # (a --gemini run once died in ingest on a stale hardcoded default).
    default_model: Optional[str] = None
    models_url: Optional[str] = None  # where current model names are listed
    extra: Optional[str] = None      # pip extra that carries the SDK, if any


PROVIDERS: dict[str, ProviderSpec] = {
    "gemini": ProviderSpec(
        name="gemini", title="Gemini", backend="gemini",
        key_env="GEMINI_API_KEY", model_env="GEMINI_MODEL",
        litellm_prefix="gemini", model_prefixes=("gemini",),
        models_url="https://ai.google.dev/gemini-api/docs/models",
        extra="gemini",
    ),
    "openai": ProviderSpec(
        name="openai", title="OpenAI", backend="openai",
        key_env="OPENAI_API_KEY", model_env="OPENAI_MODEL",
        litellm_prefix="openai",
        model_prefixes=("gpt", "chatgpt", "o1", "o3", "o4"),
        models_url="https://platform.openai.com/docs/models",
        extra="openai",
    ),
    "anthropic": ProviderSpec(
        name="anthropic", title="Anthropic", backend="api",
        key_env="ANTHROPIC_API_KEY", model_env="ANTHROPIC_MODEL",
        litellm_prefix="anthropic", model_prefixes=("claude",),
        default_model="claude-sonnet-5",  # keep in sync with Config.model
    ),
}

# Backends that require a specific key at resolve time (fail in preflight, not
# mid-run). claude-cli authenticates via the local `claude` login instead.
_BACKEND_KEY_ENVS = {
    "api": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
}

# Backends whose SDK ships as an optional extra: (import name, pip name,
# sentinel attribute a compatible version exports). Checked by check_sdk at
# preflight AND at call time — a missing SDK must fail before a run directory
# is created, not in the ingest stage.
_BACKEND_SDKS = {
    "gemini": ("google.genai", "google-genai", "Client"),
    "openai": ("openai", "openai", "OpenAI"),
}


def check_sdk(backend: str) -> None:
    """Fail fast when ``backend`` needs an SDK that cannot serve it.

    Called from CLI preflight so a missing/broken optional dependency is a
    preflight error, not a dead run (a --openai run once burned its ingest
    stage on this). Three cases, each with its own actionable message: the
    SDK is absent (install hint); it is present but fails to import — usually
    a broken transitive dependency, so the real ImportError is quoted; it
    imports but lacks the symbol we use (too old — upgrade hint). Backends
    whose SDK is a core dependency are a no-op.
    """
    sdk = _BACKEND_SDKS.get(backend)
    if sdk is None:
        return
    module, pip_name, sentinel = sdk
    spec = PROVIDERS[backend]
    try:
        mod = importlib.import_module(module)
    except ImportError as e:
        failed = (getattr(e, "name", None) or "").split(".")[0]
        if failed == module.split(".")[0]:
            raise LLMError(
                f"{pip_name} is not installed. Install the {spec.title} extra: "
                f"pip install 'readme2demo[{spec.extra}]' (or pip install {pip_name})."
            ) from e
        raise LLMError(
            f"{pip_name} is installed but failed to import "
            f"({type(e).__name__}: {e}). Reinstall it: "
            f"pip install -U 'readme2demo[{spec.extra}]'."
        ) from e
    if not hasattr(mod, sentinel):
        raise LLMError(
            f"{pip_name} is installed but too old for readme2demo (no "
            f"{module}.{sentinel}). Upgrade it: pip install -U "
            f"'readme2demo[{spec.extra}]' (or pip install -U {pip_name})."
        )


def check_model(backend: str, model: Optional[str]) -> None:
    """Preflight: for provider backends, prove a model name is resolvable.

    A bare ``--llm-backend gemini``/``openai`` (no preset, no model named
    anywhere) would otherwise burn the run dir and die in ingest on "No
    <provider> model specified". No-op for backends without a ProviderSpec
    (``api`` resolves models through the config default; claude-cli likewise).
    """
    spec = PROVIDERS.get(backend)
    if spec is not None:
        _provider_model(spec, model)


def set_backend(name: str) -> None:
    """Select the LLM backend for all subsequent calls (set once at startup)."""
    global _backend
    if name not in _BACKENDS:
        raise LLMError(
            f"Unknown LLM backend {name!r} ({' | '.join(_BACKENDS)})"
        )
    _backend = name  # type: ignore[assignment]


def resolve_backend(name: Optional[str] = None) -> str:
    """Resolve 'auto' to a concrete backend based on the environment."""
    b = name or _backend
    if b != "auto":
        key = _BACKEND_KEY_ENVS.get(b)
        if key and not os.environ.get(key):
            hint = (
                "Export it, or use the development backend: "
                "--llm-backend claude-cli"
                if b == "api"
                else f"Export it to use the {b} backend "
                f"(--{b}, or --llm-backend {b})."
            )
            raise LLMError(f"{key} is not set. {hint}")
        return b
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    if shutil.which("claude"):
        return "claude-cli"
    raise LLMError(
        "No LLM backend available: set ANTHROPIC_API_KEY (api backend) or "
        "install and log in to the Claude Code CLI (claude-cli backend, runs "
        "on your subscription)."
    )


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    inp, out = _PRICES.get(model, _PRICES["default"])
    return round((input_tokens * inp + output_tokens * out) / 1_000_000, 6)


def _complete_api(system: str, user: str, model: str, max_tokens: int) -> LLMResponse:
    import anthropic  # imported lazily so unit tests don't need the package configured

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LLMError(
            "ANTHROPIC_API_KEY is not set. Export it, or use the development "
            "backend: --llm-backend claude-cli"
        )
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(block.text for block in msg.content if block.type == "text")
    cost = _estimate_cost(model, msg.usage.input_tokens, msg.usage.output_tokens)
    return LLMResponse(text=text, cost_usd=cost)


_CREDENTIAL_ENV_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
_CREDENTIAL_RE = re.compile(r"[A-Za-z0-9_\-.~+/=]{20,512}")


def _sanitized_env() -> dict[str, str]:
    """Environment for the host ``claude -p`` subprocess.

    Drops credential vars whose values are clearly corrupted (whitespace,
    ANSI escapes — usually captured interactive output). The local ``claude``
    login still authenticates the call, so dropping is safe; forwarding
    garbage produces cryptic "invalid header value" API errors.
    """
    env = dict(os.environ)
    for var in _CREDENTIAL_ENV_VARS:
        value = env.get(var)
        if value and not _CREDENTIAL_RE.fullmatch(value.strip()):
            env.pop(var)
    return env


def _complete_cli(system: str, user: str, model: str) -> LLMResponse:
    """Subscription backend: one-shot ``claude -p`` on the host.

    Rides the local Claude Code auth (subscription or key). Supported for
    self-hosted runs; not for powering a hosted service to other users (see
    module docstring). The combined system+user prompt goes in via stdin to
    dodge argv length limits; output is Claude Code's ``--output-format json``
    envelope.
    """
    if shutil.which("claude") is None:
        raise LLMError(
            "claude CLI not found on PATH — install Claude Code or use the "
            "api backend (export ANTHROPIC_API_KEY)."
        )
    prompt = f"<system-instructions>\n{system}\n</system-instructions>\n\n{user}"
    cmd = ["claude", "-p", "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=_CLI_TIMEOUT_S, errors="replace", env=_sanitized_env(),
        )
    except subprocess.TimeoutExpired:
        raise LLMError(f"claude -p timed out after {_CLI_TIMEOUT_S}s." + _CLI_NEXT_STEP_HINT) from None
    if proc.returncode != 0:
        raise LLMError(
            f"claude -p failed ({proc.returncode}): {(proc.stderr or proc.stdout)[:500]}."
            + _CLI_NEXT_STEP_HINT
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"claude -p returned non-JSON output: {proc.stdout[:300]!r}." + _CLI_NEXT_STEP_HINT) from e
    if envelope.get("is_error"):
        raise LLMError(f"claude -p reported an error: {envelope.get('result', '')[:500]}." + _CLI_NEXT_STEP_HINT)
    return LLMResponse(
        text=envelope.get("result", ""),
        cost_usd=float(envelope.get("total_cost_usd") or 0.0),
    )


def _provider_model(spec: ProviderSpec, model: Optional[str]) -> str:
    """Resolve the model name for a provider preset/backend.

    An explicit name wins (a litellm-style ``<provider>/`` prefix is stripped
    to the bare SDK name). The repo's default Claude config name counts as
    "not specified" for non-Anthropic providers — it leaks in from config
    defaults. Then the provider's model env var decides, then the spec's
    ``default_model`` (Anthropic only). With none of those, raise — never
    guess a name the provider may have retired (a --gemini run once died in
    ingest with a 404 on a stale hardcoded default).
    """
    name = (model or "").strip()
    if name.startswith(f"{spec.litellm_prefix}/"):
        name = name.split("/", 1)[1]
    if not name or (spec.name != "anthropic" and name.startswith("claude")):
        name = os.environ.get(spec.model_env, "").strip() or (spec.default_model or "")
    if not name:
        url_note = f" Current names: {spec.models_url}" if spec.models_url else ""
        raise LLMError(
            f"No {spec.title} model specified. Pass one with --{spec.name} "
            f"<model> (or --model), or export {spec.model_env}.{url_note}"
        )
    return name


def _complete_gemini(system: str, user: str, model: str, max_tokens: int) -> LLMResponse:
    """Gemini backend: one-shot ``generate_content`` via the google-genai SDK.

    Reads GEMINI_API_KEY; the system prompt rides ``system_instruction`` and
    the user prompt is the content. Usage metadata drives the cost estimate.
    """
    check_sdk("gemini")
    from google import genai  # imported lazily; only needed for this backend
    from google.genai import types as genai_types

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise LLMError(
            "GEMINI_API_KEY is not set. Export it to use the Gemini backend "
            "(--gemini, or --llm-backend gemini)."
        )

    model = _provider_model(PROVIDERS["gemini"], model)
    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=model,
        contents=user,
        config=genai_types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
        ),
    )
    try:
        text = resp.text or ""
    except Exception:  # noqa: BLE001 — .text raises when a response has no text part
        text = ""
    usage = getattr(resp, "usage_metadata", None)
    in_tok = getattr(usage, "prompt_token_count", 0) or 0
    out_tok = getattr(usage, "candidates_token_count", 0) or 0
    cost = _estimate_cost(model, in_tok, out_tok)
    return LLMResponse(text=text, cost_usd=cost)


def _complete_openai(system: str, user: str, model: str, max_tokens: int) -> LLMResponse:
    """OpenAI backend: one-shot chat completion via the ``openai`` SDK.

    Reads OPENAI_API_KEY; the system prompt rides the system message.
    ``max_completion_tokens`` (not the deprecated ``max_tokens``) so reasoning
    models accept the call. Usage drives the cost estimate.
    """
    check_sdk("openai")
    from openai import OpenAI  # imported lazily; only needed for this backend

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise LLMError(
            "OPENAI_API_KEY is not set. Export it to use the OpenAI backend "
            "(--openai, or --llm-backend openai)."
        )

    model = _provider_model(PROVIDERS["openai"], model)
    client = OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=model,
        max_completion_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    text = ""
    if resp.choices:
        text = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "prompt_tokens", 0) or 0
    out_tok = getattr(usage, "completion_tokens", 0) or 0
    return LLMResponse(text=text, cost_usd=_estimate_cost(model, in_tok, out_tok))


def apply_provider_session(provider: str, model: Optional[str] = None) -> str:
    """Wire the current process to run on ``provider`` for this session.

    Returns the concrete model for the planner/distiller/tutorial passes,
    resolved by :func:`_provider_model` (explicit name > the provider's model
    env var > the spec's default, if any; raises LLMError when nothing names a
    model). Also bridges the provider's API key into the OpenHands engine's
    litellm-style ``LLM_API_KEY`` / ``LLM_MODEL`` when those are unset, so the
    sandboxed agent runs on the same credentials. Only fills values that are
    absent, so an explicit ``LLM_API_KEY`` / ``LLM_MODEL`` still wins.
    """
    spec = PROVIDERS[provider]
    resolved = _provider_model(spec, model)
    key = os.environ.get(spec.key_env)
    if key:
        os.environ.setdefault("LLM_API_KEY", key)
        os.environ.setdefault("LLM_MODEL", f"{spec.litellm_prefix}/{resolved}")
    return resolved


def complete(system: str, user: str, model: str, max_tokens: int = 8192) -> LLMResponse:
    """Single-turn completion via the configured backend."""
    backend = resolve_backend()
    if backend == "api":
        return _complete_api(system, user, model, max_tokens)
    if backend == "gemini":
        return _complete_gemini(system, user, model, max_tokens)
    if backend == "openai":
        return _complete_openai(system, user, model, max_tokens)
    return _complete_cli(system, user, model)


def extract_json(text: str) -> str:
    """Pull the first JSON object out of a response (handles ```json fences)."""
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise LLMError(f"No JSON object found in response: {text[:200]!r}")
    _, end = json.JSONDecoder().raw_decode(text[start:])
    return text[start : start + end]


def complete_json(
    system: str,
    user: str,
    model: str,
    schema: Type[T],
    max_tokens: int = 8192,
    retries: int = 2,
) -> tuple[T, float]:
    """Completion that must validate against ``schema``.

    On parse/validation failure, retries with the error appended so the model
    can correct itself. Returns (validated model, total cost).
    """
    total_cost = 0.0
    prompt = user
    last_err: Exception | None = None
    for _ in range(retries + 1):
        resp = complete(system, prompt, model, max_tokens)
        total_cost += resp.cost_usd
        try:
            payload = json.loads(extract_json(resp.text))
            return schema.model_validate(payload), total_cost
        except (json.JSONDecodeError, ValidationError, LLMError) as e:
            last_err = e
            prompt = (
                f"{user}\n\nYour previous response failed validation with:\n{e}\n"
                f"Respond again with ONLY a valid JSON object matching the schema."
            )
    raise LLMError(f"LLM response failed validation after retries: {last_err}")
