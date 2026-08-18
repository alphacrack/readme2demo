"""Pluggable agent engine interface.

An engine is the AI agent that reads the README and makes the quickstart work
*inside* the sandbox. The engine choice affects only (a) the command executed
in the container and (b) how the raw transcript is parsed. Everything
downstream consumes the normalized CommandLog and never knows which agent ran.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Optional

from readme2demo.types import CommandLog

# Canonical in-container paths (engines and the agent runner agree on these).
PROMPT_CONTAINER_PATH = "/task/prompt.md"
TRANSCRIPT_CONTAINER_PATH = "/work/.r2d/transcript.ndjson"

# Placeholder credentials (#234). One shared definition of what a stand-in
# credential looks like, so the key-injecting proxy, the env-swap plumbing and
# the credential canary all agree instead of each hand-rolling one. The prefix
# mirrors the real public shape per var (some in-sandbox CLIs consume the
# placeholder directly, so a missing ``sk-ant-`` prefix could fail the run
# with a cryptic auth error); the body is all-``x`` — base64url-safe,
# obviously non-secret, and greppable.
_PLACEHOLDER_PREFIXES = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-",
}
_PLACEHOLDER_GENERIC_PREFIX = "r2d-placeholder-"
_PLACEHOLDER_BODY = "x" * 32


def placeholder_credential(var: str) -> str:
    """Fixed deterministic placeholder for the credential env var ``var``.

    Pure: no env access, no I/O. The result passes the repo's one format gate
    (``_CREDENTIAL_RE`` in ``engines/claude_code.py``) and mirrors the real
    public shape of the credential (``sk-ant-api03-...`` for an API key,
    ``sk-ant-oat01-...`` for an OAuth token).

    An unknown/unsupported var name returns a GENERIC all-``x`` placeholder
    that still passes the format gate — it does NOT raise. The consumers
    operate over whatever credential vars an engine declares, and a total
    function keeps substitution safe-by-default: an unrecognised var becomes
    a safe placeholder, not a crash.
    """
    prefix = _PLACEHOLDER_PREFIXES.get(var, _PLACEHOLDER_GENERIC_PREFIX)
    return prefix + _PLACEHOLDER_BODY


def is_placeholder_credential(value: str) -> bool:
    """True iff ``value`` is a placeholder produced by :func:`placeholder_credential`.

    The matched inverse of the generator: ``False`` for a real-shaped key
    (``sk-ant-test-...`` fixtures included) and for arbitrary strings.
    """
    for prefix in (*_PLACEHOLDER_PREFIXES.values(), _PLACEHOLDER_GENERIC_PREFIX):
        if value.startswith(prefix):
            body = value[len(prefix):]
            return bool(body) and set(body) == {"x"}
    return False


# Matches env var NAMES that look like credentials. Deliberately an
# unanchored, case-insensitive substring match: over-inclusive in the safe
# direction (fail-closed) — a name like MONKEY_CONFIG also matches KEY, and
# treating a plain config var as a credential is harmless, while missing a
# real credential is not. Distinct from the _CREDENTIAL_RE in claude_code.py
# / llm.py, which matches a credential VALUE's shape, not its name.
_CREDENTIAL_NAME_RE = re.compile(r"KEY|TOKEN|SECRET", re.I)


@dataclass
class Limits:
    max_turns: int = 60
    timeout_s: int = 1500
    budget_usd: float = 5.0


class EngineError(RuntimeError):
    pass


class AgentEngine(ABC):
    """One AI agent backend (claude-code, openhands, ...)."""

    name: ClassVar[str]

    # Sandbox image this engine needs its runtime baked into, or None when the
    # standard base image already carries it (claude-code). Applied by the CLI
    # only when the user set no base_image anywhere — an explicit choice wins.
    default_image: ClassVar[Optional[str]] = None

    def check_image(self, image: str) -> None:
        """Preflight probe that ``image`` can actually run this engine.

        Default: no-op. Engines whose runtime is NOT in the standard base
        image override this to fail fast — with build instructions — before
        a run directory is created, instead of dying mid-run with a bare
        exit 127 and no transcript.

        Raises:
            EngineError: when the image cannot run this engine.
        """

    @abstractmethod
    def required_env(self) -> list[str]:
        """Env var names that must be set on the host for this engine to run.

        They are forwarded into the sandbox at exec time (never baked into the
        image or written to disk).
        """

    def credential_env_vars(self) -> set[str]:
        """Env var NAMES this engine treats as the model credential.

        Transport-agnostic seam: consumers (e.g. an egress proxy that swaps
        the real credential for a placeholder before forwarding) use this to
        know which forwarded vars hold the secret. Default is fail-closed:
        every name in :meth:`required_env` matching KEY|TOKEN|SECRET
        (case-insensitive) counts, so an engine that forgets to override
        still has its secret-named vars treated as credentials. Engines whose
        credential lives outside ``required_env`` (alternative auth methods)
        MUST override.
        """
        return {k for k in self.required_env() if _CREDENTIAL_NAME_RE.search(k)}

    def resolve_env(self) -> dict[str, str]:
        """Collect the env vars to forward into the sandbox.

        Default: every name in :meth:`required_env` must be set. Engines with
        alternative auth methods (e.g. API key OR OAuth token) override this.

        Raises:
            EngineError: with a clear message when something is missing.
        """
        import os

        missing = [k for k in self.required_env() if not os.environ.get(k)]
        if missing:
            raise EngineError(
                f"Engine {self.name!r} requires env vars that are not set: "
                f"{', '.join(missing)}"
            )
        return {k: os.environ[k] for k in self.required_env()}

    @abstractmethod
    def build_command(self, limits: Limits) -> str:
        """Shell command (run via ``bash -lc`` inside the container) that starts
        the agent with the prompt at PROMPT_CONTAINER_PATH and writes its raw
        transcript to TRANSCRIPT_CONTAINER_PATH."""

    @abstractmethod
    def parse_transcript(self, transcript_path: Path) -> CommandLog:
        """Parse the engine's raw transcript into the normalized CommandLog.

        Must be pure and deterministic (no LLM calls) so it is unit-testable
        against fixture transcripts.
        """


_REGISTRY: dict[str, type[AgentEngine]] = {}


def register(cls: type[AgentEngine]) -> type[AgentEngine]:
    """Class decorator: ``@register`` on each engine implementation."""
    _REGISTRY[cls.name] = cls
    return cls


def get_engine(name: str) -> AgentEngine:
    # Import implementations lazily to avoid import cycles.
    from readme2demo.engines import claude_code, openhands  # noqa: F401

    try:
        return _REGISTRY[name]()
    except KeyError:
        raise EngineError(
            f"Unknown engine {name!r}. Available: {sorted(_REGISTRY)}"
        ) from None
