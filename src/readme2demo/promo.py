"""Promo compositor — a shareable cut assembled from the run's OWN ``demo.mp4``.

Slice 2 of the ``--promo-video`` epic (#114): the deterministic, $0, no-API-key
compositing engine. It takes the grounded scene list (``promo_script.json``,
slice 1 / #169) and turns it into ONE ffmpeg invocation that plays inside the
render container — title card, speed-ramped highlight cuts of the real demo,
burned-in captions, end card — and writes ``promo.mp4`` next to ``demo.mp4``.

Layering (the boundary this module defends):

* :func:`build_promo_argv` is **pure**. Script + run dir + config + presets in,
  a docker/ffmpeg argv out. No ``subprocess``, no filesystem, no LLM — so the
  cut math is fixture-testable in the sub-second suite, exactly like
  ``sandbox.py``'s argv building and ``distill.step_timestamps``.
* :func:`render_promo` is the **thin** runner: measure, build, execute, gate.
  It runs ffmpeg the way ``render._generate_gif_preview`` already does
  (``docker run --rm -v <run_dir>:/vhs --entrypoint ffmpeg <image> …``), so
  host ffmpeg is never assumed, and it is **best-effort**: a promo failure
  returns ``None`` and never fails the run. ``demo.mp4`` is the artifact of
  record; the promo is marketing on top of it.

Grounding (the one non-negotiable invariant), enforced structurally:

* **The only footage input is the run's own** ``demo.mp4``. Every built argv is
  audited by :func:`assert_only_demo_footage` before it is returned: the input
  list may contain exactly one video (``/vhs/demo.mp4``), synthetic solid-color
  ``lavfi`` card backgrounds, and — when configured — the validated raster
  brand logo, forced to a single still frame. Any other ``-i``, any filtergraph
  that names a source of its own (``movie=``/``amovie=``/a graph read from a
  file), any bind mount other than the run dir (plus the logo file, read-only)
  in ANY of docker's mount spellings, and any path under the run-dir mount that
  is written other than ``/vhs/promo.mp4`` raises :class:`PromoError`. The audit
  also *checks* — rather than assumes — that the container runs
  ``--network none``, so ffmpeg cannot fetch a remote source even if a future
  edit tried. A promo that could splice in foreign footage would defeat the
  whole "we replay what your repo provably did" claim.
* **Every ``[start_s, end_s]`` is bounds-checked** against the real duration of
  ``demo.mp4``, measured with ffprobe (host, else inside the container). A
  segment past the end of the video is a grounding violation, not something to
  clamp and continue; an unmeasurable duration means no promo at all. Every
  float that reaches a comparison is required to be *finite* first
  (:func:`_finite`): ``NaN`` compares False against every ``<``/``>``/``<=``, so
  an un-checked ``NaN`` would slide through the bounds check AND turn the
  duration gate into a no-op.
* **At least one ``demo_segment``** per script, or the cut is refused.
* **Card and caption text is passed through verbatim** from the script. Every
  string that reaches the filtergraph goes through ``brand.title_card_drawtext``
  (and therefore ``brand.escape_drawtext``) — escaping is never reimplemented
  here, and text is never rewritten. This module has no LLM in it and never
  synthesizes command text.

Nothing here touches the four protected artifacts (``tutorial.md``,
``step_by_step.md``, ``commands.sh``, ``demo.tape``) or ``demo.mp4`` itself.

**Trust boundary — read this before calling** :func:`render_promo`: this module
does NOT check ``manifest.verified``. It trusts its caller to have established
that the run's replay passed, exactly as it trusts the caller to hand it a
``demo.mp4`` that came from the render stage. What it enforces on its own is
narrower and mechanical: the footage in the promo is bit-for-bit a span of the
``demo.mp4`` sitting in the run dir. Deliberately NOT here: the verified-run
gate (a run whose replay did not pass gets no promo, mirroring the render
stage's ``manifest.verified`` skip) belongs where the script is generated (#169)
and where the pipeline wires this module in (#230). The promo is an optional
post-render output, not a stage in ``manifest.STAGES``.

``PromoScene.step_index`` is carried and deliberately never read here. It is
provenance for the script generator (#169) and the wiring stage (#230) — "which
guide step this cut came from" — while the grounding in this module is the
measured ``[start_s, end_s]`` span checked against ``demo.mp4``'s real ffprobe
duration. Trusting a step index instead would be a second, weaker notion of
where footage comes from: an index can be right while the span is wrong, and it
is the span that ffmpeg actually cuts.
"""

from __future__ import annotations

import math
import posixpath
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import render
from .brand import logo_overlay, title_card_drawtext
from .config import Config

# ``PromoScene``/``PromoScript`` are stage contracts, so they live in types.py —
# "the single source of truth" — and are imported, never re-declared. This
# module carried a local fallback copy of both while slice 1 (#169) was still in
# flight; it was deleted the moment that branch landed, because two definitions
# of a stage contract is exactly the drift types.py exists to prevent.
from .types import PromoScene, PromoScript

#: Artifact this module writes, next to (never over) ``demo.mp4``.
PROMO_ARTIFACT = "promo.mp4"
#: Best-effort diagnostics for a skipped promo (why there is no promo.mp4).
PROMO_LOG = "promo.log"

#: Mount points inside the compositing container. The run dir mount mirrors
#: ``render._generate_gif_preview``; the logo gets its own read-only mount
#: because ``brand.logo_overlay`` returns a HOST path and only the run dir is
#: visible to the container.
CONTAINER_RUN_DIR = "/vhs"
CONTAINER_LOGO_DIR = "/brand"

#: ffmpeg input index of the demo video. Always 0: the footage source is the
#: first input, before any synthetic card background.
DEMO_INPUT = 0
#: Filtergraph label of the finished promo stream (what ``-map`` selects).
OUT_LABEL = "vout"

#: Canvas + framerate, matching ``templates/demo.tape.j2`` (``Set Width 1200``,
#: ``Set Height 700``, ``Set Framerate 24``) so segments of the real demo are
#: composited 1:1 with no resampling in the common case.
CANVAS_W = 1200
CANVAS_H = 700
FPS = 24

#: Wall-clock limit for one compositing run (seconds). Generous for a 60s cut
#: re-encoded at ``-preset veryfast``, but bounded: this is a best-effort extra.
PROMO_TIMEOUT_S = 900

#: Size floor for the finished promo, shared with the render stage's gate.
MIN_PROMO_BYTES = render.MIN_ARTIFACT_BYTES
#: Fractional + absolute slack when comparing the rendered duration with the
#: duration the filtergraph math predicts (encoder/frame rounding).
DURATION_TOLERANCE_FRAC = 0.20
DURATION_TOLERANCE_S = 1.0

#: Floor on the segment budget: however much of a preset the cards eat, the
#: demo footage keeps at least this many seconds. Without it, cards longer than
#: the preset would drive the fit factor (and the speed-up) to infinity.
#: The speed-up itself is capped separately by each preset's ``max_pace``.
MIN_SEGMENT_BUDGET_S = 3.0

#: Float tolerance for the segment bounds check (guards float round-tripping
#: through JSON; it is NOT a clamp — anything beyond this is a hard error).
_BOUNDS_EPS = 1e-6

#: A synthetic solid-colour lavfi source: the ONLY generated input the audit
#: accepts. ``movie=``/``concat``/anything with frames of its own is refused.
_LAVFI_COLOR_RE = re.compile(r"^color=c=(?:#|0x)?[0-9A-Za-z@.]+:s=\d+x\d+:r=\d+$")

#: Raster suffixes the logo overlay may use (mirrors ``config._BRAND_LOGO_SUFFIXES``;
#: re-checked here because a ``Config.model_construct`` bypasses the validator).
_RASTER_SUFFIXES = (".png", ".jpg", ".jpeg")

#: Demuxer forced onto the brand-logo input. The suffix check is an EXTENSION
#: check and ffmpeg decodes by CONTENT, so an mp4 named ``logo.png`` would
#: otherwise animate over every card. ``-f image2`` pins the still-image
#: demuxer, and ``build_filtergraph`` additionally trims the logo stream to its
#: first frame — content cannot animate through either door.
_LOGO_INPUT_FORMAT = "image2"

#: Filtergraph half of the same guarantee: whatever the logo input decodes to,
#: only its first frame reaches the overlay.
LOGO_STILL_FILTERS = "trim=end_frame=1,setpts=PTS-STARTPTS"

#: Docker's bind-mount spellings. ``-v``/``--volume`` take one ``host:container``
#: spec and are the only form the builder emits, so they are the only form the
#: audit can check against an allow-list. ``--mount``'s CSV and
#: ``--volumes-from``'s inherited mount set cannot be audited from the argv
#: alone (``--mount type=bind,source=/anywhere/stock.mp4,target=/vhs/demo.mp4``
#: would SHADOW the run's own footage with a foreign file while every ``-i``
#: still read ``/vhs/demo.mp4``), so they are refused outright rather than
#: parsed. Any other mount-ish flag fails closed via :func:`_looks_like_mount_flag`.
_BIND_FLAGS = frozenset({"-v", "--volume"})
_UNAUDITABLE_MOUNT_FLAGS = frozenset({"--mount", "--volumes-from"})

#: Docker network flag spellings; the audit requires the value to be ``none``.
_NETWORK_FLAGS = frozenset({"--network", "--net"})

#: ffmpeg flags whose value IS a filtergraph (audited by :func:`_assert_graph_grounded`).
_GRAPH_VALUE_FLAGS = frozenset(
    {"-filter_complex", "-lavfi", "-vf", "-af", "-filter", "-filter:v", "-filter:a"}
)

#: A filtergraph may not name a source of its own. ``movie=``/``amovie=`` open a
#: file that the ``-i`` audit never sees. Anchored to the positions where
#: ffmpeg actually recognises a filter name — start of graph, or after
#: ``;``/``,``/``]`` — which is both sufficient (nowhere else is a filter name)
#: and free of false positives: caption text reaches the graph through
#: ``brand.escape_drawtext``, which escapes exactly those separators.
#: The optional ``@id`` is NOT decoration: ffmpeg's grammar is
#: ``name[@id]=args``, and ``movie@1=/x.mp4[bg]`` decodes exactly like
#: ``movie=`` — verified against ffmpeg 8.1.2 — so a check anchored on ``movie=``
#: alone is one character away from being no check at all. The id is matched as
#: "anything up to the ``=``" rather than a guessed charset: over-refusing at a
#: filter-name position costs a promo, under-refusing costs the guarantee.
_GRAPH_SOURCE_RE = re.compile(r"(?:^|(?<![\\])[;,\]])\s*a?movie\s*(?:@[^=]*)?\s*=")


class PromoError(RuntimeError):
    """Raised when a promo cannot be composited from verified footage alone."""


# --- presets: data, not branching ---------------------------------------------


@dataclass(frozen=True)
class StylePreset:
    """Look and pacing of a cut. Pure data — no behaviour, no conditionals."""

    pace: float  # floor speed multiplier applied to demo segments (setpts)
    xfade_s: float  # crossfade length between consecutive scenes
    transition: str  # ffmpeg xfade transition name
    card_bg: str  # lavfi colour for title/end-card backgrounds
    card_fontsize: int
    caption_fontsize: int


@dataclass(frozen=True)
class DurationPreset:
    """Length budget of a cut. Pure data — see :class:`StylePreset`."""

    target_s: float
    tolerance_s: float  # how far the finished promo may sit from target_s
    max_pace: float  # speed-up ceiling when the segments overflow the budget


#: Style presets. ``tech`` is the default; ``minimal`` keeps real time and long
#: dissolves; ``energetic`` runs hot with hard cuts.
STYLES: dict[str, StylePreset] = {
    "tech": StylePreset(
        pace=1.25,
        xfade_s=0.4,
        transition="fade",
        card_bg="0x101018",
        card_fontsize=54,
        caption_fontsize=30,
    ),
    "minimal": StylePreset(
        pace=1.0,
        xfade_s=0.6,
        transition="fade",
        card_bg="black",
        card_fontsize=48,
        caption_fontsize=26,
    ),
    "energetic": StylePreset(
        pace=1.6,
        xfade_s=0.2,
        transition="wipeleft",
        card_bg="0x1A0B2E",
        card_fontsize=60,
        caption_fontsize=34,
    ),
}

#: Duration presets, in seconds. Keys are the accepted ``duration_preset`` values.
DURATIONS: dict[int, DurationPreset] = {
    15: DurationPreset(target_s=15.0, tolerance_s=6.0, max_pace=6.0),
    30: DurationPreset(target_s=30.0, tolerance_s=12.0, max_pace=4.0),
    60: DurationPreset(target_s=60.0, tolerance_s=24.0, max_pace=3.0),
}

DEFAULT_STYLE = "tech"
DEFAULT_DURATION = 30


def style_preset(name: str) -> StylePreset:
    """Look up a style preset by name.

    Raises:
        PromoError: if ``name`` is not one of :data:`STYLES`.
    """
    try:
        return STYLES[name]
    except KeyError:
        raise PromoError(
            f"unknown promo style {name!r}; expected one of {sorted(STYLES)}"
        ) from None


def duration_preset(seconds: int) -> DurationPreset:
    """Look up a duration preset (15/30/60).

    Raises:
        PromoError: if ``seconds`` is not one of :data:`DURATIONS`.
    """
    try:
        return DURATIONS[seconds]
    except KeyError:
        raise PromoError(
            f"unknown promo duration preset {seconds!r}; expected one of {sorted(DURATIONS)}"
        ) from None


# --- pure helpers --------------------------------------------------------------


def _f(value: float) -> str:
    """Format a float for an ffmpeg argument, deterministically."""
    return f"{value:.3f}"


def _finite(value: float, what: str) -> float:
    """Return ``value`` as a float, or raise if it is not finite.

    THE guard in front of every numeric gate in this module. ``NaN`` compares
    ``False`` against ``<``, ``>`` and ``<=`` alike, so a single ``NaN`` in a
    script silently walks through the segment bounds check, poisons the
    predicted duration, and turns :func:`validate_promo`'s
    ``abs(duration - expected_s) > slack`` into a gate that accepts any length.
    ``inf`` is refused for the same reason. JSON has no ``NaN`` literal, but
    every parser in reach (``json``, pydantic) accepts the bare token, so this
    is data-reachable, not theoretical.

    Raises:
        PromoError: if ``value`` is not a finite number.
    """
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        raise PromoError(f"{what} must be a number (got {value!r})") from None
    if not math.isfinite(as_float):
        raise PromoError(
            f"{what} must be a finite number (got {value!r}) — NaN/inf compares "
            "False against every bounds and duration check, so it is refused "
            "rather than silently disabling them"
        )
    return as_float


def _span(scene: PromoScene) -> tuple[float, float]:
    """``(start_s, end_s)`` of a demo segment.

    Raises:
        PromoError: if either bound is missing — an unbounded segment is not a
            cut of the real video, it is a request to trust the script — or if
            either bound is not finite (see :func:`_finite`).
    """
    if scene.start_s is None or scene.end_s is None:
        raise PromoError(
            "demo_segment scene is missing start_s/end_s — every cut must name "
            "an explicit span of demo.mp4"
        )
    return (
        _finite(scene.start_s, "demo_segment start_s"),
        _finite(scene.end_s, "demo_segment end_s"),
    )


def validate_script(script: PromoScript, *, source_duration_s: float) -> None:
    """Structural + grounding validation of a promo script. Pure.

    Checks, all of them hard errors (this module never clamps a bad value into
    a plausible one):

    * every float is finite — ``NaN``/``inf`` are refused BEFORE any comparison,
      because they pass all of them (see :func:`_finite`),
    * at least one ``demo_segment`` scene — a promo with no real footage is not
      a promo of this run (the epic's "enforced, not suggested"),
    * positive ``duration_s`` on every scene and a positive
      ``total_duration_s``,
    * every ``demo_segment`` span is ordered and inside
      ``[0, source_duration_s]`` — the measured length of the run's own mp4,
    * title/end cards carry text (the whole point of a card).

    Raises:
        PromoError: on the first violation, naming the scene index.
    """
    source_duration_s = _finite(source_duration_s, f"{render.PRIMARY_ARTIFACT} duration")
    if source_duration_s <= 0:
        raise PromoError(
            f"demo.mp4 duration {source_duration_s!r} is not usable — refusing to "
            "composite segments that cannot be bounds-checked"
        )
    if not script.scenes:
        raise PromoError("promo script has no scenes")
    if _finite(script.total_duration_s, "promo script total_duration_s") <= 0:
        raise PromoError(
            f"promo script total_duration_s must be > 0 (got {script.total_duration_s})"
        )

    segments = 0
    for i, scene in enumerate(script.scenes):
        if _finite(scene.duration_s, f"scene {i} ({scene.kind}): duration_s") <= 0:
            raise PromoError(f"scene {i} ({scene.kind}): duration_s must be > 0")
        if scene.kind == "demo_segment":
            segments += 1
            start, end = _span(scene)
            if start < 0:
                raise PromoError(f"scene {i}: start_s {start} is negative")
            if end <= start:
                raise PromoError(f"scene {i}: end_s {end} must be greater than start_s {start}")
            if end > source_duration_s + _BOUNDS_EPS:
                raise PromoError(
                    f"scene {i}: [{start}, {end}] runs past the end of "
                    f"{render.PRIMARY_ARTIFACT} ({source_duration_s:.3f}s). A promo may "
                    "only show footage the run actually produced — fix the script, "
                    "the cut is not clamped."
                )
        elif not (scene.text or "").strip():
            raise PromoError(f"scene {i} ({scene.kind}): card scenes need text")
    if segments == 0:
        raise PromoError(
            "promo script contains no demo_segment scenes — at least one cut of "
            "the verified demo.mp4 is mandatory in every promo"
        )


def segment_pace(
    script: PromoScript,
    *,
    style: str = DEFAULT_STYLE,
    preset: int = DEFAULT_DURATION,
) -> float:
    """Speed multiplier applied to every demo segment (the "speed ramp"). Pure.

    The style preset sets a floor; the duration preset sets the budget. When the
    script's natural segment time overflows what is left after the cards, the
    pace rises to fit — capped by the preset's ``max_pace`` so the footage stays
    legible. Spans themselves are never trimmed: the promo shows the exact
    ``[start_s, end_s]`` the script asked for, just faster.

    Raises:
        PromoError: on an unknown preset or a non-finite duration/span. This is
            a public entry point, so it re-checks finiteness rather than
            assuming :func:`validate_script` ran first.
    """
    st = style_preset(style)
    dp = duration_preset(preset)
    natural = 0.0
    cards = 0.0
    for i, scene in enumerate(script.scenes):
        if scene.kind == "demo_segment":
            start, end = _span(scene)
            natural += end - start
        else:
            cards += _finite(scene.duration_s, f"scene {i} ({scene.kind}): duration_s")
    budget = max(dp.target_s - cards, MIN_SEGMENT_BUDGET_S)
    return round(min(max(st.pace, natural / budget), dp.max_pace), 3)


@dataclass(frozen=True)
class _Timeline:
    """Per-scene played durations plus the xfade junctions between them."""

    pace: float
    durations: list[float]  # played length of each scene, in scene order
    fades: list[tuple[float, float]]  # (fade_s, offset_s) for junction i -> i+1
    total_s: float


def _timeline(script: PromoScript, *, style: str, preset: int) -> _Timeline:
    """Compute the promo timeline. Pure; shared by the graph and the gate."""
    st = style_preset(style)
    pace = segment_pace(script, style=style, preset=preset)

    durations: list[float] = []
    for i, scene in enumerate(script.scenes):
        if scene.kind == "demo_segment":
            start, end = _span(scene)
            durations.append(round((end - start) / pace, 3))
        else:
            durations.append(
                round(_finite(scene.duration_s, f"scene {i} ({scene.kind}): duration_s"), 3)
            )

    fades: list[tuple[float, float]] = []
    acc = durations[0]
    for played in durations[1:]:
        # An xfade may never be longer than half of either side, or the
        # transition would swallow a whole scene.
        fade = round(min(st.xfade_s, 0.5 * min(acc, played)), 3)
        fades.append((fade, round(acc - fade, 3)))
        acc = round(acc + played - fade, 3)
    return _Timeline(pace=pace, durations=durations, fades=fades, total_s=round(acc, 3))


def expected_promo_duration_s(
    script: PromoScript,
    *,
    style: str = DEFAULT_STYLE,
    preset: int = DEFAULT_DURATION,
) -> float:
    """Duration the built filtergraph will produce, in seconds. Pure.

    The promo counterpart of ``render.expected_min_duration_s``: computed from
    the same numbers that go into the graph, so :func:`validate_promo` can tell
    a truncated render from a correct one.
    """
    return _timeline(script, style=style, preset=preset).total_s


@dataclass(frozen=True)
class _InputPlan:
    """Which ffmpeg input index (and split label) each scene draws from."""

    card_input: dict[int, int]  # scene index -> ffmpeg input index
    card_ordinal: dict[int, int]  # scene index -> nth card (logo split label)
    segment_ordinal: dict[int, int]  # scene index -> nth segment (source split label)
    logo_index: Optional[int]


def _input_plan(script: PromoScript, cfg: Config) -> _InputPlan:
    """Assign input indices deterministically: demo, then cards, then logo."""
    card_input: dict[int, int] = {}
    card_ordinal: dict[int, int] = {}
    segment_ordinal: dict[int, int] = {}
    next_index = DEMO_INPUT + 1
    for i, scene in enumerate(script.scenes):
        if scene.kind == "demo_segment":
            segment_ordinal[i] = len(segment_ordinal)
        else:
            card_ordinal[i] = len(card_ordinal)
            card_input[i] = next_index
            next_index += 1
    # The logo rides on the cards only; with no cards there is nothing to
    # overlay, so the input is omitted entirely.
    logo_index = next_index if (card_input and logo_overlay(cfg) is not None) else None
    return _InputPlan(
        card_input=card_input,
        card_ordinal=card_ordinal,
        segment_ordinal=segment_ordinal,
        logo_index=logo_index,
    )


def _container_logo_path(cfg: Config) -> Optional[str]:
    """Where the brand logo is mounted inside the compositing container."""
    if cfg.brand_logo is None:
        return None
    return f"{CONTAINER_LOGO_DIR}/logo{Path(cfg.brand_logo).suffix.lower()}"


#: Vertical spacing between stacked card lines, as a multiple of the fontsize.
_CARD_LINE_SPACING = 1.4


def _card_drawtexts(text: str, cfg: Config, *, fontsize: int) -> list[str]:
    """One ``drawtext`` fragment per line of a title/end card.

    A card carries several lines in one ``text`` field (repo name, then the
    "verified" line, then the install command), and drawtext draws one line per
    filter, so the text is split on newlines and the lines are stacked around
    the centre. The text of each line is handed to ``brand.title_card_drawtext``
    verbatim — this function chooses geometry, never content.
    """
    lines = text.split("\n")
    step = round(fontsize * _CARD_LINE_SPACING)
    fragments = []
    for k, line in enumerate(lines):
        offset = int(round((k - (len(lines) - 1) / 2) * step))
        y = "(h-text_h)/2" if offset == 0 else f"(h-text_h)/2{offset:+d}"
        fragments.append(title_card_drawtext(line, cfg, fontsize=fontsize, y=y))
    return fragments


def _caption_drawtext(text: str, cfg: Config, *, fontsize: int) -> str:
    """A lower-third caption fragment for a demo segment.

    Reuses ``brand.title_card_drawtext`` (and therefore ``escape_drawtext``) for
    the text, colour and font, then adds a translucent box so the caption stays
    readable over terminal output. Escaping is never reimplemented here.
    """
    base = title_card_drawtext(
        text,
        cfg,
        fontsize=fontsize,
        x="(w-text_w)/2",
        y="h-text_h-64",
    )
    return base + ":box=1:boxcolor=black@0.6:boxborderw=14"


def build_filtergraph(
    script: PromoScript,
    cfg: Config,
    *,
    source_duration_s: float,
    style: str = DEFAULT_STYLE,
    preset: int = DEFAULT_DURATION,
) -> str:
    """Build the ``-filter_complex`` graph for the promo. Pure.

    Shape (labels are stable, which is what makes the graph assertable in
    tests): the single footage input is ``split`` once per demo segment; each
    segment is ``trim``-med to its verified span, speed-ramped with ``setpts``,
    letterboxed onto the canvas and captioned; each card is drawn onto its own
    solid-colour ``lavfi`` input (with the brand logo overlaid when configured);
    finally every scene is chained with ``xfade`` into ``[vout]``.

    Raises:
        PromoError: if the script fails :func:`validate_script`.
    """
    validate_script(script, source_duration_s=source_duration_s)
    st = style_preset(style)
    plan = _input_plan(script, cfg)
    tl = _timeline(script, style=style, preset=preset)
    logo = logo_overlay(cfg)

    parts: list[str] = []
    # One split branch per segment: a filtergraph pad may only be consumed once.
    n_segments = len(plan.segment_ordinal)
    parts.append(
        f"[{DEMO_INPUT}:v]split={n_segments}"
        + "".join(f"[seg{j}]" for j in range(n_segments))
    )
    if plan.logo_index is not None:
        # The logo is trimmed to its FIRST FRAME before it is split. config.py
        # validates the logo by file EXTENSION and ffmpeg decodes by CONTENT, so
        # an mp4 named ``logo.png`` would otherwise play foreign moving frames
        # over every card. One frame is all an overlay needs (overlay's default
        # eof_action=repeat holds it for the whole card), so this costs nothing
        # for a real logo and structurally cannot animate for a fake one. The
        # ``-f image2`` on the input side (build_promo_argv) is the second door.
        n_cards = len(plan.card_ordinal)
        parts.append(
            f"[{plan.logo_index}:v]{LOGO_STILL_FILTERS},split={n_cards}"
            + "".join(f"[lg{c}]" for c in range(n_cards))
        )

    for i, scene in enumerate(script.scenes):
        if scene.kind == "demo_segment":
            start, end = _span(scene)
            chain = [
                f"trim=start={_f(start)}:end={_f(end)}",
                f"setpts=(PTS-STARTPTS)/{_f(tl.pace)}",
                f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease",
                f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2",
                "setsar=1",
                f"fps={FPS}",
            ]
            if (scene.text or "").strip():
                chain.append(_caption_drawtext(scene.text or "", cfg, fontsize=st.caption_fontsize))
            chain.append("format=yuv420p")
            parts.append(f"[seg{plan.segment_ordinal[i]}]" + ",".join(chain) + f"[v{i}]")
            continue

        card = (
            f"[{plan.card_input[i]}:v]"
            + ",".join(_card_drawtexts(scene.text or "", cfg, fontsize=st.card_fontsize))
            + ",setsar=1,format=yuv420p"
        )
        if logo is not None and plan.logo_index is not None:
            _, overlay = logo
            parts.append(card + f"[c{i}]")
            parts.append(f"[c{i}][lg{plan.card_ordinal[i]}]{overlay},format=yuv420p[v{i}]")
        else:
            parts.append(card + f"[v{i}]")

    prev = "v0"
    for j, (fade, offset) in enumerate(tl.fades, start=1):
        out = f"x{j}"
        parts.append(
            f"[{prev}][v{j}]xfade=transition={st.transition}"
            f":duration={_f(fade)}:offset={_f(offset)}[{out}]"
        )
        prev = out
    # Pin the output label so -map is independent of the scene count.
    parts.append(f"[{prev}]null[{OUT_LABEL}]")
    return ";".join(parts)


def _flag_name(token: str) -> str:
    """The flag part of a token, so ``--volume=/a:/b`` audits as ``--volume``."""
    return token.split("=", 1)[0]


def _flag_value(argv: list[str], i: int) -> tuple[Optional[str], Optional[int]]:
    """``(value, value_index)`` of the flag at ``argv[i]``.

    Handles both spellings docker accepts — ``--volume /a:/b`` and
    ``--volume=/a:/b`` — because an audit that understands only one of them
    audits only half the command lines that can be built. ``value_index`` is
    ``None`` for the ``=``-joined form (the value has no token of its own).
    """
    if "=" in argv[i]:
        return argv[i].split("=", 1)[1], None
    if i + 1 < len(argv):
        return argv[i + 1], i + 1
    return None, None


def _looks_like_mount_flag(name: str) -> bool:
    """True for any flag that smells like it makes a host path visible.

    Deliberately broader than :data:`_BIND_FLAGS` + :data:`_UNAUDITABLE_MOUNT_FLAGS`
    so an unrecognised spelling FAILS CLOSED. Docker's mount surface grows; an
    audit that only knows ``-v`` is a comment, not a check.
    """
    bare = name.lstrip("-").lower()
    return bare in {"v", "volume", "volumes-from", "mount"} or "mount" in bare or "volume" in bare


def _under_run_mount(token: str) -> Optional[str]:
    """Normalised path if ``token`` names a location inside the run-dir mount."""
    path = token[5:] if token.startswith("file:") else token
    if not path.startswith("/"):
        return None
    norm = posixpath.normpath(path)
    if norm == CONTAINER_RUN_DIR or norm.startswith(CONTAINER_RUN_DIR + "/"):
        return norm
    return None


def _assert_graph_grounded(flag: str, graph: str) -> None:
    """Refuse a filtergraph that opens a source of its own.

    ``-i`` is not the only way into ffmpeg: ``movie=``/``amovie=`` decode a file
    named INSIDE the graph, which the input audit never sees. Not reachable from
    script text today (``brand.escape_drawtext`` escapes every separator a
    caption would need to break out of ``drawtext=text=``), which is exactly why
    it is worth pinning: this function is defence in depth against the future
    edit, not against today's data.
    """
    if _GRAPH_SOURCE_RE.search(graph):
        raise PromoError(
            f"promo refuses a filtergraph source in {flag}: movie=/amovie= decodes a "
            "file the input audit never sees, so the only footage source stays the "
            f"run's own {render.PRIMARY_ARTIFACT}"
        )


def assert_only_demo_footage(argv: list[str], run_dir: Path, cfg: Config) -> None:
    """Refuse any argv that could pull in footage other than the run's demo.mp4.

    THE structural guarantee of this module, applied to the finished command
    line rather than trusted to a comment. One left-to-right walk, then three
    aggregate checks; every rule below is a way frames that the run did not
    produce could otherwise reach ``promo.mp4``:

    * exactly one ``-i`` names ``/vhs/demo.mp4`` (the run's own render), with no
      forced demuxer — ``-f concat -i /vhs/demo.mp4`` would read the artifact of
      record as a PLAYLIST of arbitrary files,
    * every other ``-i`` is either a synthetic solid-colour ``lavfi`` source
      (``-f lavfi -i color=…``) or the configured raster brand logo at its
      read-only mount, forced to the still-image demuxer,
    * no filtergraph names a source of its own (``movie=``/``amovie=``), and a
      graph read from a FILE (``-filter_complex_script``) is refused outright
      because the audit cannot see it,
    * the only bind mounts are the run directory and that logo file — in the
      ``-v``/``--volume`` spelling only. ``--mount``/``--volumes-from`` (and any
      unrecognised mount-ish flag) are refused rather than parsed: a
      ``--mount type=bind,source=…,target=/vhs/demo.mp4`` would shadow the run's
      own file while every ``-i`` still innocently read ``/vhs/demo.mp4``,
    * ``--network none`` is present and is actually ``none`` — the docstrings
      lean on it, so it is verified rather than assumed,
    * ``/vhs/promo.mp4`` is the ONE path under the run mount that is written.
      ffmpeg accepts N outputs and ``-y`` is set, so checking only the last
      token would let a second output truncate ``demo.mp4`` the moment ffmpeg
      opened it.

    Scope boundary, deliberately: a path OUTSIDE the run-dir mount is not
    policed. The run dir is the only writable mount and the container is
    ``--rm``, so anything ffmpeg wrote elsewhere dies with it — it cannot reach
    ``demo.mp4`` or a published artifact, which is what this audit is about.

    Raises:
        PromoError: naming the offending input, graph, mount or output.
    """
    run_dir = Path(run_dir).resolve()
    source = f"{CONTAINER_RUN_DIR}/{render.PRIMARY_ARTIFACT}"
    promo_out = f"{CONTAINER_RUN_DIR}/{PROMO_ARTIFACT}"
    logo_path = _container_logo_path(cfg)

    if cfg.brand_logo is not None and Path(cfg.brand_logo).suffix.lower() not in _RASTER_SUFFIXES:
        raise PromoError(
            f"brand_logo {str(cfg.brand_logo)!r} is not a still raster image "
            f"({'/'.join(_RASTER_SUFFIXES)}) — the promo refuses it as a possible "
            "footage source"
        )

    # ``-v`` here is docker's bind-mount flag: the ffmpeg side of the argv
    # deliberately spells its verbosity ``-loglevel`` (never ``-v``), so this
    # scan cannot confuse the two — and if a future edit did use ffmpeg's ``-v``,
    # it would be refused as an unrecognised mount, which is the safe direction.
    allowed_mounts = {f"{run_dir}:{CONTAINER_RUN_DIR}"}
    if cfg.brand_logo is not None and logo_path is not None:
        allowed_mounts.add(f"{Path(cfg.brand_logo).resolve()}:{logo_path}:ro")

    footage = 0
    network: Optional[str] = None
    fmt: Optional[str] = None
    read_indices: set[int] = set()  # tokens consumed as an input or a mount spec
    value_indices: set[int] = set()  # tokens consumed as ANY flag's value
    i = 0
    while i < len(argv):
        token = argv[i]
        name = _flag_name(token)
        value, value_index = _flag_value(argv, i)
        step = 1 if value_index is None else 2
        # Value slots are recorded per branch, never for every token: a token
        # that merely FOLLOWS a non-flag (the image name, say) is not a value.
        slot = {value_index} if value_index is not None else set()

        if name == "-f":
            fmt = value
            value_indices |= slot
            i += step
            continue

        if name == "-i":
            if value is None:
                raise PromoError("promo argv ends with a dangling -i")
            value_indices |= slot
            read_indices |= slot
            if fmt == "lavfi":
                if not _LAVFI_COLOR_RE.match(value):
                    raise PromoError(
                        f"promo refuses generated input {value!r}: only solid-colour "
                        "lavfi card backgrounds are allowed"
                    )
            elif value == source:
                if fmt is not None:
                    raise PromoError(
                        f"promo refuses forced input format {fmt!r} on {source!r}: an "
                        "indirecting demuxer (concat, image2, …) would read the "
                        "artifact of record as a list of other files"
                    )
                footage += 1
            elif logo_path is not None and value == logo_path:
                if fmt != _LOGO_INPUT_FORMAT:
                    raise PromoError(
                        f"promo refuses brand logo input {value!r} without "
                        f"-f {_LOGO_INPUT_FORMAT}: the logo is validated by file "
                        "extension but decoded by content, so the still-image "
                        "demuxer is what keeps a disguised video from animating"
                    )
            else:
                raise PromoError(
                    f"promo refuses foreign video input {value!r}: the only footage "
                    f"source is the run's own {render.PRIMARY_ARTIFACT}"
                )
            fmt = None
            i += step
            continue

        if name.startswith("-filter") or name in _GRAPH_VALUE_FLAGS:
            if "script" in name:
                raise PromoError(
                    f"promo refuses {name}: a filtergraph read from a file is invisible "
                    "to this audit, so its sources cannot be checked"
                )
            if name not in _GRAPH_VALUE_FLAGS:
                raise PromoError(f"promo refuses unrecognised filter flag {name}")
            if value is None:
                raise PromoError(f"promo argv ends with a dangling {name}")
            value_indices |= slot
            read_indices |= slot
            _assert_graph_grounded(name, value)
            i += step
            continue

        if token.startswith("-") and _looks_like_mount_flag(name):
            if name in _UNAUDITABLE_MOUNT_FLAGS:
                raise PromoError(
                    f"promo refuses {name}: only -v/--volume mounts can be audited "
                    f"against the allow-list, and {name} could shadow "
                    f"{source!r} with a foreign file while every -i still read it"
                )
            if name not in _BIND_FLAGS:
                raise PromoError(
                    f"promo refuses unrecognised mount flag {name}: an audit that only "
                    "knows the spellings it was written for is not an audit"
                )
            if value not in allowed_mounts:
                raise PromoError(
                    f"promo refuses bind mount {value!r}: only the run directory "
                    "(and the brand logo, read-only) may be visible to the compositor"
                )
            value_indices |= slot
            read_indices |= slot
            i += step
            continue

        if name in _NETWORK_FLAGS:
            network = value
            value_indices |= slot
            i += step
            continue

        i += 1

    # BACKSTOP, position-independent by design. A left-to-right walk skips each
    # flag's value slot, so a flag hidden in another flag's value is invisible to
    # it: ``-f -v /anywhere:/vhs`` makes ``-v`` look like a format name, and the
    # mount is never audited. The audit this replaced swept for ``-v`` over the
    # whole argv precisely because position is not something an audit should have
    # to trust; this keeps that property for every family it knows. No argv this
    # module builds puts a flag in a value slot, so the rule costs nothing.
    for k in sorted(value_indices):
        hidden = argv[k]
        # Refuse ANY flag-shaped token in a value slot, not just the families
        # named above: an executed-probe re-audit showed `-f` and `--network`
        # were unguarded, so `-vf -f concat` hid the demuxer flag from the
        # forced-demuxer rule and `-vf --network bridge` hid the network flag
        # from its gate. The complete inventory of value-slot tokens this
        # module ever emits is {lavfi, image2, none, /vhs/demo.mp4,
        # /brand/logo.png, <host>:/vhs, color=..., <graph>} — none begins with
        # "-", so the broad rule is provably false-positive-free here.
        if hidden.startswith("-"):
            raise PromoError(
                f"promo refuses {hidden!r} in the value slot of {argv[k - 1]!r}: a flag "
                "hidden in another flag's value would never be audited, which is how "
                "an unchecked mount or input gets in"
            )

    if footage != 1:
        raise PromoError(
            f"promo argv must read {source!r} exactly once as its footage source "
            f"(found {footage})"
        )

    # Every remaining token that resolves under the run-dir mount is something
    # ffmpeg would OPEN FOR WRITING, and ffmpeg truncates an output when it opens
    # it: a second output naming demo.mp4 destroys the artifact of record before
    # a single frame is written. Position is not the check — presence is.
    written = [
        token for k, token in enumerate(argv)
        if k not in read_indices and _under_run_mount(token) is not None
    ]
    extra = [token for token in written if _under_run_mount(token) != promo_out]
    if extra:
        raise PromoError(
            f"promo must write {promo_out}, not {extra[0]!r} — ffmpeg accepts several "
            f"outputs and truncates each as it opens it, so {render.PRIMARY_ARTIFACT} "
            "and the published guide artifacts are never written by the compositor"
        )
    if len(written) != 1 or argv[-1] != promo_out:
        raise PromoError(
            f"promo must write {promo_out} exactly once, as its final argument "
            f"(found {written!r})"
        )

    if network != "none":
        raise PromoError(
            f"promo refuses to run with --network {network!r}: compositing reads only "
            "what is mounted, and a network namespace would let ffmpeg fetch a remote "
            "source through an http/rtmp input"
        )


def build_promo_argv(
    script: PromoScript,
    run_dir: Path,
    cfg: Config,
    *,
    source_duration_s: float,
    style: str = DEFAULT_STYLE,
    preset: int = DEFAULT_DURATION,
    image: Optional[str] = None,
) -> list[str]:
    """Build the full ``docker run … ffmpeg …`` argv for the promo. PURE.

    No ``subprocess``, no filesystem access, no LLM: the same
    argv-builder-as-unit-of-test contract ``sandbox.py`` follows, so every cut
    decision is assertable against a fixture script.

    Args:
        script: the grounded scene list (``promo_script.json``).
        run_dir: the run directory; it is mounted at ``/vhs`` and holds both the
            source ``demo.mp4`` and the ``promo.mp4`` output.
        cfg: pipeline config — supplies the image, resource limits and brand kit.
        source_duration_s: measured duration of the run's ``demo.mp4``. REQUIRED
            (keyword-only, no default) so no caller can skip the bounds check by
            omitting it.
        style: one of :data:`STYLES`.
        preset: one of :data:`DURATIONS` (15/30/60).
        image: optional image override; defaults to ``cfg.base_image``.

    Returns:
        The argv, audited by :func:`assert_only_demo_footage` before return.

    Raises:
        PromoError: on an invalid/ungrounded script, an unknown preset, or an
            argv that would admit foreign footage.
    """
    run_dir = Path(run_dir).resolve()
    st = style_preset(style)
    duration_preset(preset)  # validate early: unknown preset is an error, not a default
    graph = build_filtergraph(
        script, cfg, source_duration_s=source_duration_s, style=style, preset=preset
    )
    plan = _input_plan(script, cfg)
    logo = logo_overlay(cfg)
    logo_path = _container_logo_path(cfg)

    argv = ["docker", "run", "--rm", "-v", f"{run_dir}:{CONTAINER_RUN_DIR}"]
    if plan.logo_index is not None and logo is not None and cfg.brand_logo is not None:
        # brand.logo_overlay yields a HOST path; only the run dir is mounted, so
        # the logo file gets its own read-only mount and the input is rewritten
        # to that mount point. The overlay fragment itself is used verbatim.
        argv += ["-v", f"{cfg.brand_logo.resolve()}:{logo_path}:ro"]
    argv += [
        # Same limits as the rest of the pipeline; network is dropped entirely
        # because compositing reads only what is mounted (see module docstring).
        "--memory", cfg.memory,
        "--cpus", cfg.cpus,
        "--network", "none",
        "--entrypoint", "ffmpeg",
        image or cfg.base_image,
        "-y", "-loglevel", "error",
        "-i", f"{CONTAINER_RUN_DIR}/{render.PRIMARY_ARTIFACT}",
    ]
    for i in sorted(plan.card_input, key=lambda k: plan.card_input[k]):
        argv += [
            "-f", "lavfi",
            "-t", _f(script.scenes[i].duration_s),
            "-i", f"color=c={st.card_bg}:s={CANVAS_W}x{CANVAS_H}:r={FPS}",
        ]
    if plan.logo_index is not None and logo_path is not None:
        # ``-f image2`` pins the still-image demuxer: config.py validates the
        # logo by EXTENSION and ffmpeg decodes by CONTENT, so an mp4 named
        # logo.png must not be able to animate over the cards. The filtergraph
        # trims the same stream to one frame (LOGO_STILL_FILTERS) — belt and
        # braces, and assert_only_demo_footage requires this flag.
        argv += ["-f", _LOGO_INPUT_FORMAT, "-i", logo_path]
    argv += [
        "-filter_complex", graph,
        "-map", f"[{OUT_LABEL}]",
        "-an",  # v1 promo is silent (VHS renders no audio track anyway)
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        f"{CONTAINER_RUN_DIR}/{PROMO_ARTIFACT}",
    ]

    assert_only_demo_footage(argv, run_dir, cfg)
    return argv


# --- thin runner ---------------------------------------------------------------


def source_duration_s(run_dir: Path, cfg: Config, image: Optional[str] = None) -> Optional[float]:
    """Measure ``demo.mp4`` with ffprobe: host first, then the render container.

    ffprobe is the ONLY accepted source of the bound (see the module docstring):
    ``step_timestamps.json`` records tape-clock LOWER bounds that deliberately
    exclude VHS ``Wait`` time, so using it as an upper bound would be a guess
    about the artifact rather than a measurement of it. Host ffprobe is tried
    first because it is free; otherwise the base image — which the render stage
    already requires to carry ffmpeg — probes the file over the same
    ``/vhs`` mount. Returns ``None`` when neither can measure it, and callers
    must then refuse to composite.
    """
    mp4 = Path(run_dir).resolve() / render.PRIMARY_ARTIFACT
    host = shutil.which("ffprobe")
    if host is not None:
        duration = render._mp4_duration_s(mp4, host)
        # ffprobe prints "nan" for some malformed files and float() accepts it;
        # a NaN bound would pass every segment check instead of failing one.
        if duration is not None and math.isfinite(duration):
            return duration
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{Path(run_dir).resolve()}:{CONTAINER_RUN_DIR}",
        "--network", "none",
        "--entrypoint", "ffprobe",
        image or cfg.base_image,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        f"{CONTAINER_RUN_DIR}/{render.PRIMARY_ARTIFACT}",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=120
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        measured = float((proc.stdout or "").strip())
    except ValueError:
        return None
    return measured if math.isfinite(measured) else None


def validate_promo(promo_mp4: Path, expected_s: float, preset: int = DEFAULT_DURATION) -> Path:
    """Size + duration gate for ``promo.mp4``, mirroring ``render.validate_outputs``.

    Two checks, in the spirit of "an incomplete video is worse than no video":

    1. the rendered duration must match what the filtergraph predicted (tight —
       this is what catches a truncated or half-composited cut), and
    2. it must not OVERFLOW its duration preset by more than the preset's
       tolerance.

    The preset check is deliberately one-sided: the preset is a *budget*, so
    running over means the fit failed (the speed-up hit ``max_pace`` and the
    segments still did not fit), while running under simply means the verified
    footage was short. Failing an honest short cut would create pressure to pad
    the promo with something the run did not do — the exact incentive this
    project exists to refuse. The duration check is skipped entirely when the
    host has no ffprobe (the same concession ``render.validate_outputs`` makes).

    Raises:
        PromoError: if the file is missing, too small, or the wrong length.
    """
    dp = duration_preset(preset)
    expected_s = _finite(expected_s, "expected promo duration")
    if not promo_mp4.exists():
        raise PromoError(f"{promo_mp4.name}: missing after compositing")
    size = promo_mp4.stat().st_size
    if size <= MIN_PROMO_BYTES:
        raise PromoError(f"{promo_mp4.name}: too small ({size} bytes, need > {MIN_PROMO_BYTES})")

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return promo_mp4
    duration = render._mp4_duration_s(promo_mp4, ffprobe)
    if duration is None:
        raise PromoError(f"{promo_mp4.name}: ffprobe could not read duration")
    if not math.isfinite(duration):
        raise PromoError(
            f"{promo_mp4.name}: ffprobe could not read duration — {duration!r} is not "
            "a finite number, and comparing against it would accept any length"
        )
    slack = expected_s * DURATION_TOLERANCE_FRAC + DURATION_TOLERANCE_S
    if abs(duration - expected_s) > slack:
        raise PromoError(
            f"{promo_mp4.name} is {duration:.1f}s but the filtergraph predicts "
            f"{expected_s:.1f}s (±{slack:.1f}s) — the cut did not composite fully"
        )
    if duration > dp.target_s + dp.tolerance_s:
        raise PromoError(
            f"{promo_mp4.name} is {duration:.1f}s, overflowing the {dp.target_s:.0f}s "
            f"preset (+{dp.tolerance_s:.0f}s allowed) — the cut does not fit its budget"
        )
    return promo_mp4


def _hint(stderr: str) -> str:
    """Turn the known ffmpeg failure modes into actionable advice.

    ``drawtext`` (cards and captions) needs an ffmpeg built with libfreetype and
    a font resolvable *inside* the container — neither is guaranteed by the
    render image's ``command -v ffmpeg`` preflight, and the raw error ("No such
    filter") does not say what to do about it.
    """
    if "No such filter: 'drawtext'" in stderr:
        return (
            "\nhint: this image's ffmpeg was built without drawtext (libfreetype). "
            "Rebuild the base image (docker build --no-cache -t readme2demo/base:latest "
            "images/base/) or point --base-image at one whose ffmpeg has it."
        )
    if "Cannot find a valid font" in stderr or "Fontconfig error" in stderr:
        return (
            "\nhint: drawtext found no usable font in the container. Set brand_font "
            "to a family the image actually has, or install fontconfig + a font in "
            "images/base/."
        )
    return ""


def _log_skip(run_dir: Path, message: str) -> None:
    """Record why there is no promo. Best-effort, like the promo itself.

    Catches everything: this runs on the failure path of a function that
    promises never to fail the run, so it must not become the thing that does.
    ``run_dir`` may be whatever the caller passed (an unresolvable path is one
    of the failures being logged), so even building the path can raise.
    """
    try:
        (Path(run_dir) / PROMO_LOG).write_text(message + "\n", encoding="utf-8")
    except Exception:  # pragma: no cover - unwritable/unusable run dir
        pass


def render_promo(
    run_dir: Path,
    script: PromoScript,
    cfg: Config,
    *,
    style: str = DEFAULT_STYLE,
    preset: int = DEFAULT_DURATION,
    image: Optional[str] = None,
) -> Optional[Path]:
    """Composite ``promo.mp4`` from the run's ``demo.mp4``; ``None`` on failure.

    BEST-EFFORT, exactly like ``render._generate_gif_preview``: EVERY failure
    mode returns ``None`` and writes the reason to ``promo.log`` — no
    ``demo.mp4``, an unmeasurable duration, an ungrounded script, a non-zero
    ffmpeg, a timeout, a promo that fails its size/duration gate, and equally
    the ones nobody enumerated. That is why the guard is a bare ``except
    Exception`` covering the whole body rather than a list of expected types: a
    caption holding a NUL byte makes ``subprocess.run`` raise ``ValueError``,
    which a ``(PromoError, TimeoutExpired, OSError)`` guard would let escape and
    fail the run this function promises never to fail. "Best-effort" is a
    guarantee about the caller, so it has to be total. ``demo.mp4`` and the
    published guide artifacts are never touched (the only output path in the
    argv is ``/vhs/promo.mp4``).

    This function does NOT check ``manifest.verified`` — see the module
    docstring's trust boundary: the caller establishes that the run's replay
    passed, this module only guarantees the footage is the run's own.

    Args:
        run_dir: run directory holding ``demo.mp4``; mounted at ``/vhs``.
        script: the grounded scene list (``promo_script.json``).
        cfg: pipeline config (image, resource limits, brand kit).
        style: one of :data:`STYLES`.
        preset: one of :data:`DURATIONS` (15/30/60).
        image: optional image override (must carry ffmpeg/ffprobe).

    Returns:
        Path to the validated ``promo.mp4``, or ``None``. THE RETURN VALUE IS
        THE SIGNAL, not the presence of the file: when ffmpeg succeeds but the
        size/duration gate then fails, the un-validated ``promo.mp4`` is left in
        the run dir on purpose (it is the evidence ``promo.log`` refers to). A
        caller that publishes by globbing the run dir would ship exactly the
        truncated cut this module refused.
    """
    try:
        # Inside the guard: resolve() hits the filesystem and can raise, and a
        # promise of "never fails the run" that starts before the try is a
        # promise with a hole in it.
        run_dir = Path(run_dir).resolve()
        source = run_dir / render.PRIMARY_ARTIFACT
        if not source.exists():
            raise PromoError(f"{render.PRIMARY_ARTIFACT} not found in {run_dir}")
        measured = source_duration_s(run_dir, cfg, image=image)
        if measured is None:
            raise PromoError(
                f"could not measure {render.PRIMARY_ARTIFACT} with ffprobe — refusing "
                "to composite segments that cannot be bounds-checked"
            )
        argv = build_promo_argv(
            script,
            run_dir,
            cfg,
            source_duration_s=measured,
            style=style,
            preset=preset,
            image=image,
        )
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=PROMO_TIMEOUT_S
        )
        if proc.returncode != 0:
            tail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
            raise PromoError(f"ffmpeg exited with {proc.returncode}:\n{tail}{_hint(tail)}")
        expected = expected_promo_duration_s(script, style=style, preset=preset)
        return validate_promo(run_dir / PROMO_ARTIFACT, expected, preset=preset)
    except Exception as e:  # noqa: BLE001 - total by contract; see the docstring
        _log_skip(run_dir, f"promo skipped: {type(e).__name__}: {e}")
        return None


__all__ = [
    "DEFAULT_DURATION",
    "DEFAULT_STYLE",
    "DURATIONS",
    "PROMO_ARTIFACT",
    "PROMO_LOG",
    "STYLES",
    "DurationPreset",
    "PromoError",
    "StylePreset",
    "assert_only_demo_footage",
    "build_filtergraph",
    "build_promo_argv",
    "duration_preset",
    "expected_promo_duration_s",
    "render_promo",
    "segment_pace",
    "source_duration_s",
    "style_preset",
    "validate_promo",
    "validate_script",
]
