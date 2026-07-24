"""Configuration: CLI flags > readme2demo.toml > defaults."""

from __future__ import annotations

import sys
import warnings
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Agent engine
    engine: str = "claude-code"  # or "openhands" (used by --gemini/--openai/--anthropic)
    model: str = "claude-sonnet-5"  # model for planner/distiller/tutorial LLM calls
    llm_backend: str = "auto"  # auto | api | claude-cli | gemini | openai (presets set one; claude-cli = host `claude -p`, self-hosted only)
    max_turns: int = 60
    agent_timeout_s: int = 1500
    budget_usd: float = 5.0

    # Sandbox
    base_image: str = "readme2demo/base:latest"
    network: str = "bridge"
    # SECURITY TRADEOFF, off by default: mount the host Docker socket into the
    # agent/verify/render containers. Required for tools whose demos manage
    # containers themselves (toolhive, testcontainers, docker-compose repos).
    # The socket pierces the sandbox — only enable for repos you trust.
    allow_docker_socket: bool = False
    memory: str = "4g"
    cpus: str = "2"
    pids_limit: int = 512
    # Compatibility shim for configs written before v0.6.1. The value is no
    # longer used, but accepting it avoids breaking existing TOML files.
    vhs_image: Optional[str] = Field(default=None, exclude=True, repr=False)

    # Stages
    # --dry-run: stop after ingest/planning (feasibility verdict + blockers),
    # skipping the paid agent stage and everything downstream.
    dry_run: bool = False
    verify_timeout_s: int = 900
    verify_retries: int = 1  # plain script retries before distiller feedback loop
    distill_retries: int = 1  # distiller feedback loops on verify failure
    skip_video: bool = False
    # Selected output formats (registry in formats.py). Surface only in
    # this slice — the pipeline still keys render off skip_video alone.
    formats: list[str] = Field(default_factory=lambda: ["demo", "gif"])

    # Optional user-supplied step-by-step guide (-s/--step-by-step): injected
    # into the cloned repo so the planner and agent treat it as authoritative,
    # and used as the source the demo video is built from.
    step_by_step: Optional[Path] = None

    # Layout
    runs_dir: Path = Field(default_factory=lambda: Path("runs"))

    @model_validator(mode="before")
    @classmethod
    def _warn_deprecated_vhs_image(cls, data: Any) -> Any:
        if isinstance(data, dict) and "vhs_image" in data:
            warnings.warn(
                "'vhs_image' is deprecated and ignored; use 'base_image' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
        return data


    @field_validator("formats", mode="after")
    @classmethod
    def _validate_formats(cls, value: list[str]) -> list[str]:
        from readme2demo.formats import _validate_format_names

        return _validate_format_names(list(value))

    @classmethod
    def load(cls, toml_path: Optional[Path] = None, **overrides: Any) -> "Config":
        """Build config from optional TOML file plus explicit overrides.

        ``overrides`` with value ``None`` are ignored so CLI flags that were
        not passed don't clobber TOML values.
        """
        data: dict[str, Any] = {}
        path = toml_path or Path("readme2demo.toml")
        if toml_path is not None and not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        if path.exists() and tomllib is not None:
            with open(path, "rb") as f:
                data.update(tomllib.load(f))
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)
