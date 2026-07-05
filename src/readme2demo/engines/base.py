"""Pluggable agent engine interface.

An engine is the AI agent that reads the README and makes the quickstart work
*inside* the sandbox. The engine choice affects only (a) the command executed
in the container and (b) how the raw transcript is parsed. Everything
downstream consumes the normalized CommandLog and never knows which agent ran.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from readme2demo.types import CommandLog

# Canonical in-container paths (engines and the agent runner agree on these).
PROMPT_CONTAINER_PATH = "/task/prompt.md"
TRANSCRIPT_CONTAINER_PATH = "/work/.r2d/transcript.ndjson"


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

    @abstractmethod
    def required_env(self) -> list[str]:
        """Env var names that must be set on the host for this engine to run.

        They are forwarded into the sandbox at exec time (never baked into the
        image or written to disk).
        """

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
