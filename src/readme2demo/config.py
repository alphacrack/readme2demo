"""Configuration: CLI flags > readme2demo.toml > defaults."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


class Config(BaseModel):
    # Agent engine
    engine: str = "claude-code"  # or "openhands" (used by --gemini/--openai/--anthropic)
    model: str = "claude-sonnet-5"  # model for planner/distiller/tutorial LLM calls
    llm_backend: str = "auto"  # auto | api | claude-cli | gemini | openai (presets set one; claude-cli = host `claude -p`, self-hosted only)
    max_turns: int = 60
    agent_timeout_s: int = 1500
    budget_usd: float = 5.0

    # Sandbox
    base_image: str = "readme2demo/base:latest"
    vhs_image: str = "ghcr.io/charmbracelet/vhs:latest"
    network: str = "bridge"
    # SECURITY TRADEOFF, off by default: mount the host Docker socket into the
    # agent/verify/render containers. Required for tools whose demos manage
    # containers themselves (toolhive, testcontainers, docker-compose repos).
    # The socket pierces the sandbox — only enable for repos you trust.
    allow_docker_socket: bool = False
    memory: str = "4g"
    cpus: str = "2"
    pids_limit: int = 512

    # Stages
    verify_timeout_s: int = 900
    verify_retries: int = 1  # plain script retries before distiller feedback loop
    distill_retries: int = 1  # distiller feedback loops on verify failure
    skip_video: bool = False

    # Optional user-supplied step-by-step guide (-s/--step-by-step): injected
    # into the cloned repo so the planner and agent treat it as authoritative,
    # and used as the source the demo video is built from.
    step_by_step: Optional[Path] = None

    # Layout
    runs_dir: Path = Field(default_factory=lambda: Path("runs"))

    @classmethod
    def load(cls, toml_path: Optional[Path] = None, **overrides: Any) -> "Config":
        """Build config from optional TOML file plus explicit overrides.

        ``overrides`` with value ``None`` are ignored so CLI flags that were
        not passed don't clobber TOML values.
        """
        data: dict[str, Any] = {}
        path = toml_path or Path("readme2demo.toml")
        if path.exists() and tomllib is not None:
            with open(path, "rb") as f:
                data.update(tomllib.load(f))
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)
