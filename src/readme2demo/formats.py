"""Output-format registry and pure CLI/TOML parsing for multi-format selection.

Surface-only slice of #117: declares which formats exist, which are implemented,
and validates user selection. Does **not** change what the pipeline renders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class FormatError(ValueError):
    """Raised when a formats selection is unknown or not yet implemented."""


@dataclass(frozen=True)
class FormatSpec:
    """One selectable output format (row in the formats registry)."""

    name: str
    implemented: bool
    description: str
    # Tracking issue for unimplemented formats; shown in error messages.
    tracking_issue: Optional[int] = None


FORMATS: dict[str, FormatSpec] = {
    "demo": FormatSpec(
        name="demo",
        implemented=True,
        description="Primary demo video (demo.mp4)",
    ),
    "gif": FormatSpec(
        name="gif",
        implemented=True,
        description="GIF preview of the demo (demo.gif)",
    ),
    "podcast": FormatSpec(
        name="podcast",
        implemented=False,
        description="Podcast / audio narration of the demo",
        tracking_issue=111,
    ),
    "promo": FormatSpec(
        name="promo",
        implemented=False,
        description="Short promo cut of the demo video",
        tracking_issue=114,
    ),
    "social": FormatSpec(
        name="social",
        implemented=False,
        description="Social-media cut of the demo video",
        tracking_issue=116,
    ),
}


def parse_formats(value: str) -> list[str]:
    """Parse a comma-separated formats string into a validated name list.

    Splits on commas, strips whitespace, lowercases, drops empties, and
    de-duplicates while preserving order. Raises :class:`FormatError` for
    unknown names or declared-but-unimplemented formats.
    """
    if value is None:
        raise FormatError("formats value is required")
    raw_parts = [p.strip().lower() for p in value.split(",")]
    names: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        if not part:
            continue
        if part in seen:
            continue
        seen.add(part)
        names.append(part)
    if not names:
        raise FormatError(
            "no formats specified; known formats: "
            + ", ".join(sorted(FORMATS))
        )
    return _validate_format_names(names)


def _validate_format_names(names: list[str]) -> list[str]:
    """Validate a list of format names against the registry."""
    implemented = sorted(n for n, s in FORMATS.items() if s.implemented)
    known = sorted(FORMATS)
    for name in names:
        spec = FORMATS.get(name)
        if spec is None:
            raise FormatError(
                f"unknown format {name!r}. Known formats: {', '.join(known)}"
            )
        if not spec.implemented:
            issue = (
                f" — tracked in #{spec.tracking_issue}"
                if spec.tracking_issue
                else ""
            )
            raise FormatError(
                f"format {name!r} is not implemented yet{issue}. "
                f"Implemented formats: {', '.join(implemented)}"
            )
    return names
