"""Pure ffmpeg-fragment builders for the brand kit (presentation only).

This module turns the brand-kit :class:`~readme2demo.config.Config` fields
(``brand_logo`` / ``brand_color`` / ``brand_font``) into ffmpeg filter and
input-argument *fragment strings* for the promo compositor (#114 slice 2) and
the social cut (#116) to splice into their own filtergraphs. It builds strings
only — no ``subprocess``, no docker, no I/O of any kind. The filesystem checks
already happened in ``config.py``'s validators, and running ffmpeg belongs to
the consumer's render container (see ``render.py`` ``_generate_gif_preview``,
which runs ffmpeg via ``--entrypoint ffmpeg`` inside that container). Keeping
this module pure is what lets it stay inside the sub-second test suite.

Grounding note (the one non-negotiable invariant): brand-kit output is
presentation framing wrapped *around* verified demo segments — title/end cards
and a corner logo. Nothing here touches ``distill``/``tutorial``/``normalize``/
``verify`` or any published artifact (``tutorial.md``, ``step_by_step.md``,
``commands.sh``, ``demo.tape``). Routing brand text through the guide or the
tape would be a grounding violation, not a feature.

Escaping model: :func:`escape_drawtext` applies a single backslash-escape pass,
which is correct when the finished filtergraph is passed as one argument to
ffmpeg via a subprocess list (no intervening shell) — the convention already
used in ``render.py``. The fragment builders here embed the escaped text
UNQUOTED, so a consumer that instead wraps values in quotes or routes the
filtergraph through a shell owns that extra escaping level.
"""

from __future__ import annotations

from typing import Optional

from .config import Config

# ffmpeg drawtext / filtergraph metacharacters, in the order they must be
# applied (backslash first, so escapes we add below are not re-escaped).
#   \  escape char (drawtext option value + filtergraph)
#   %  drawtext text-expansion introducer (%{...})
#   :  filter-option separator
#   '  quote char
#   [ ] , ;  filtergraph pad-label / filter / chain separators — repo names and
#            version strings genuinely contain brackets and commas, so a literal
#            one must not be read as graph syntax.
_DRAWTEXT_ESCAPES: tuple[tuple[str, str], ...] = (
    ("\\", r"\\"),
    ("%", r"\%"),
    (":", r"\:"),
    ("'", r"\'"),
    ("[", r"\["),
    ("]", r"\]"),
    (",", r"\,"),
    (";", r"\;"),
)


def escape_drawtext(text: str) -> str:
    r"""Escape ffmpeg drawtext ``text=`` metacharacters.

    Backslash-escapes the characters that are special to drawtext's text
    option and to filtergraph parsing (``\``, ``%``, ``:``, ``'``, ``[``,
    ``]``, ``,``, ``;``) so an arbitrary string — a repo name like
    ``user/repo: v2.0 [beta]`` or an install command — can be spliced into a
    ``drawtext=text=...`` fragment without being misread as filter syntax.

    Single-pass escaping, intended for a filtergraph passed to ffmpeg as one
    subprocess argument (no shell layer). Backslash is escaped first so the
    escapes introduced for the other characters are not doubled.
    """
    for raw, esc in _DRAWTEXT_ESCAPES:
        text = text.replace(raw, esc)
    return text


def title_card_drawtext(
    text: str,
    cfg: Config,
    *,
    fontsize: int = 48,
    x: str = "(w-text_w)/2",
    y: str = "(h-text_h)/2",
) -> str:
    """Build a ``drawtext=...`` filter fragment for a title/end card.

    Applies the configured ``brand_color`` (``fontcolor=``) and, when set,
    ``brand_font`` (``font=``). ``text`` is escaped via :func:`escape_drawtext`
    so repo names containing ``:`` or quotes are safe. The card text is centred
    by default; ``x``/``y``/``fontsize`` are ffmpeg expressions the caller may
    override. Returns a single filter string for the consumer to place in its
    filtergraph — this builds the fragment, it does not run ffmpeg.
    """
    opts = [f"text={escape_drawtext(text)}"]
    if cfg.brand_font:
        opts.append(f"font={cfg.brand_font}")
    opts.append(f"fontcolor={cfg.brand_color}")
    opts.append(f"fontsize={fontsize}")
    opts.append(f"x={x}")
    opts.append(f"y={y}")
    return "drawtext=" + ":".join(opts)


def logo_overlay(
    cfg: Config,
    *,
    x: str = "W-w-24",
    y: str = "24",
) -> Optional[tuple[list[str], str]]:
    """Fragments for compositing the brand logo, or ``None`` when unset.

    Returns ``(input_args, overlay)`` where ``input_args`` is the extra ffmpeg
    input the caller appends to its command (``["-i", "<logo path>"]``) and
    ``overlay`` is the ``overlay=x:y`` filter core to splice between the
    appropriate pad labels. Defaults place the logo 24px from the top-right
    corner (``W``/``H`` = main video, ``w``/``h`` = overlay). Returns ``None``
    when ``brand_logo`` is unset, so a consumer can skip the overlay entirely.

    No file I/O: the path was validated at ``Config.load`` time, and it is
    emitted as a standalone subprocess argument (not into the filtergraph), so
    it needs no drawtext escaping.
    """
    if cfg.brand_logo is None:
        return None
    input_args = ["-i", str(cfg.brand_logo)]
    overlay = f"overlay={x}:{y}"
    return input_args, overlay
