"""Thin LLM client for the non-agent passes (planner, distiller, tutorial).

Two backends, selected via :func:`set_backend` (wired to config/CLI):

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
- ``auto``       — ``api`` if ANTHROPIC_API_KEY is set, else ``claude-cli``
                   if the ``claude`` binary is on PATH.

All calls return usage cost so the manifest can aggregate one number per run.
"""

from __future__ import annotations

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
}


@dataclass
class LLMResponse:
    text: str
    cost_usd: float


class LLMError(RuntimeError):
    pass


Backend = Literal["auto", "api", "claude-cli"]

_backend: Backend = "auto"

_CLI_TIMEOUT_S = 600


def set_backend(name: str) -> None:
    """Select the LLM backend for all subsequent calls (set once at startup)."""
    global _backend
    if name not in ("auto", "api", "claude-cli"):
        raise LLMError(f"Unknown LLM backend {name!r} (auto | api | claude-cli)")
    _backend = name  # type: ignore[assignment]


def resolve_backend(name: Optional[str] = None) -> str:
    """Resolve 'auto' to a concrete backend based on the environment."""
    b = name or _backend
    if b != "auto":
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
        raise LLMError(f"claude -p timed out after {_CLI_TIMEOUT_S}s") from None
    if proc.returncode != 0:
        raise LLMError(
            f"claude -p failed ({proc.returncode}): {(proc.stderr or proc.stdout)[:500]}"
        )
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"claude -p returned non-JSON output: {proc.stdout[:300]!r}") from e
    if envelope.get("is_error"):
        raise LLMError(f"claude -p reported an error: {envelope.get('result', '')[:500]}")
    return LLMResponse(
        text=envelope.get("result", ""),
        cost_usd=float(envelope.get("total_cost_usd") or 0.0),
    )


def complete(system: str, user: str, model: str, max_tokens: int = 8192) -> LLMResponse:
    """Single-turn completion via the configured backend."""
    backend = resolve_backend()
    if backend == "api":
        return _complete_api(system, user, model, max_tokens)
    return _complete_cli(system, user, model)


def extract_json(text: str) -> str:
    """Pull the first JSON object out of a response (handles ```json fences)."""
    fence = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise LLMError(f"No JSON object found in response: {text[:200]!r}")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise LLMError("Unbalanced JSON object in response")


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
