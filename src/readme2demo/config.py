"""Configuration: CLI flags > readme2demo.toml > defaults."""

from __future__ import annotations

import re
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


# Brand kit: canonical accent-color form is ``#RRGGBB`` (six hex digits). This
# is the exact syntax ffmpeg's color parser accepts, so the config value can be
# passed straight through to drawtext ``fontcolor=`` with no translation. The
# shorter ``#RGB`` form is intentionally NOT accepted: ffmpeg would reject it
# later, inside the render container, which is a worse place to discover a typo.
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Raster suffixes ffmpeg can decode as a drawtext/overlay input. SVG and other
# vector formats are excluded on purpose — typical ffmpeg builds cannot
# rasterize them, so accepting one would only defer a confusing failure.
_BRAND_LOGO_SUFFIXES = (".png", ".jpg", ".jpeg")


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
    # Number of plain-script retries after the first verify attempt, before
    # falling back to the distiller feedback loop (0 = no retries, 1 attempt
    # total; N = N retries, N+1 attempts total; negative values clamp to 0).
    verify_retries: int = 1
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

    # Brand kit — presentation-only styling for the promo/social cuts (#114
    # slice 2, #116). These feed the pure ffmpeg-fragment helpers in brand.py;
    # they NEVER reach the grounding path (tutorial.md, step_by_step.md,
    # commands.sh, demo.tape). All optional, all validated at load time so a
    # set-but-broken value fails fast — same fail-fast philosophy as the ingest
    # infeasibility gate — instead of blowing up mid-render in a container.
    brand_logo: Optional[Path] = None  # raster logo (.png/.jpg/.jpeg) for title/end cards + corner overlay
    brand_color: str = "#7C6BF2"  # hex accent (#RRGGBB) for drawtext title/end-card text
    brand_font: Optional[str] = None  # drawtext font= NAME; resolves inside the render container, so not host-verifiable

    @field_validator("brand_logo")
    @classmethod
    def _validate_brand_logo(cls, v: Optional[Path]) -> Optional[Path]:
        """Fail fast on a set-but-unusable brand logo.

        The file must exist and be a raster image ffmpeg can decode
        (``.png``/``.jpg``/``.jpeg``). Vector formats such as ``.svg`` are
        rejected here rather than deferring the failure to ffmpeg inside the
        render container, where it would surface far from its cause.
        """
        if v is None:
            return v
        if not v.exists():
            raise ValueError(f"brand_logo file not found: {v}")
        if v.suffix.lower() not in _BRAND_LOGO_SUFFIXES:
            allowed = "/".join(_BRAND_LOGO_SUFFIXES)
            raise ValueError(
                f"brand_logo must be a raster image ({allowed}); got "
                f"{v.suffix or v.name!r}. Vector formats (e.g. .svg) are "
                "rejected because typical ffmpeg builds cannot rasterize them."
            )
        return v

    @field_validator("brand_color")
    @classmethod
    def _validate_brand_color(cls, v: str) -> str:
        """Require a canonical ``#RRGGBB`` hex accent color.

        This is exactly what ffmpeg's color parser accepts, so the value can be
        handed to drawtext ``fontcolor=`` unchanged. Names (``"red"``), a
        missing ``#``, wrong digit counts, and non-hex digits are all rejected.
        """
        if not _HEX_COLOR_RE.match(v):
            raise ValueError(
                f"brand_color must be a hex color like '#RRGGBB' (e.g. "
                f"'#7C6BF2'); got {v!r}"
            )
        return v

    @field_validator("brand_font")
    @classmethod
    def _validate_brand_font(cls, v: Optional[str]) -> Optional[str]:
        """Only guard against an empty/whitespace font name.

        Font availability cannot be verified on the host: drawtext resolves
        ``font=`` where ffmpeg runs, which per current render.py practice is
        inside the render container, not here.
        """
        if v is not None and not v.strip():
            raise ValueError("brand_font, if set, must be a non-empty font name")
        return v

    @field_validator("formats", mode="after")
    @classmethod
    def _validate_formats(cls, value: list[str]) -> list[str]:
        """Reject unknown or not-yet-implemented output formats at load time.

        Shares the registry check with the ``--formats`` CLI parser so a bad
        name in readme2demo.toml fails exactly like a bad flag would.
        """
        from readme2demo.formats import _validate_format_names

        return _validate_format_names(list(value))

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
        if path.exists():
            if tomllib is not None:
                with open(path, "rb") as f:
                    data.update(tomllib.load(f))
            else:
                warnings.warn(
                    f"Found {path} but no TOML parser is available — "
                    "pip install tomli (Python <3.11) or upgrade to Python 3.11+",
                    UserWarning,
                    stacklevel=2,
                )
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)
