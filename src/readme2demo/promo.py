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
  brand logo. Any other ``-i``, and any bind mount other than the run dir (plus
  the logo file, read-only), raises :class:`PromoError` — as does any output
  path other than ``/vhs/promo.mp4``. The container also runs with
  ``--network none``, so ffmpeg cannot fetch a remote source even if a future
  edit tried. A promo that could splice in foreign footage would defeat the
  whole "we replay what your repo provably did" claim.
* **Every ``[start_s, end_s]`` is bounds-checked** against the real duration of
  ``demo.mp4``, measured with ffprobe (host, else inside the container). A
  segment past the end of the video is a grounding violation, not something to
  clamp and continue; an unmeasurable duration means no promo at all.
* **At least one ``demo_segment``** per script, or the cut is refused.
* **Card and caption text is passed through verbatim** from the script. Every
  string that reaches the filtergraph goes through ``brand.title_card_drawtext``
  (and therefore ``brand.escape_drawtext``) — escaping is never reimplemented
  here, and text is never rewritten. This module has no LLM in it and never
  synthesizes command text.

Nothing here touches the four protected artifacts (``tutorial.md``,
``step_by_step.md``, ``commands.sh``, ``demo.tape``) or ``demo.mp4`` itself.

Deliberately NOT here: the verified-run gate (a run whose replay did not pass
gets no promo, mirroring the render stage's ``manifest.verified`` skip) belongs
where the script is generated (#169) and where the pipeline wires this module in
(#230). The promo is an optional post-render output, not a stage in
``manifest.STAGES``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import render
from .brand import logo_overlay, title_card_drawtext
from .config import Config

# --- PENDING DEPENDENCY (#169) -------------------------------------------------
# ``PromoScene``/``PromoScript`` belong to types.py (its docstring makes it "the
# single source of truth" for stage contracts) and land with slice 1 (#169).
# This branch STACKS on that one: the import below is the real contract, and the
# fallback exists only so this slice's pure tests run standalone before #169
# merges. DELETE THE ``except ImportError`` BLOCK AT MERGE — two definitions of
# a stage contract is exactly the drift types.py exists to prevent.
try:  # pragma: no cover - exercised by whichever branch is present
    from .types import PromoScene, PromoScript  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - dropped when #169 lands
    from typing import Literal

    from pydantic import BaseModel

    class PromoScene(BaseModel):  # type: ignore[no-redef]
        """TEMPORARY local copy of the #169 contract — see the note above."""

        kind: Literal["title_card", "demo_segment", "end_card"]
        text: Optional[str] = None
        step_index: Optional[int] = None
        start_s: Optional[float] = None
        end_s: Optional[float] = None
        duration_s: float

    class PromoScript(BaseModel):  # type: ignore[no-redef]
        """TEMPORARY local copy of the #169 contract — see the note above."""

        version: int = 1
        total_duration_s: float
        scenes: list[PromoScene]


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


def _span(scene: PromoScene) -> tuple[float, float]:
    """``(start_s, end_s)`` of a demo segment.

    Raises:
        PromoError: if either bound is missing — an unbounded segment is not a
            cut of the real video, it is a request to trust the script.
    """
    if scene.start_s is None or scene.end_s is None:
        raise PromoError(
            "demo_segment scene is missing start_s/end_s — every cut must name "
            "an explicit span of demo.mp4"
        )
    return float(scene.start_s), float(scene.end_s)


def validate_script(script: PromoScript, *, source_duration_s: float) -> None:
    """Structural + grounding validation of a promo script. Pure.

    Checks, all of them hard errors (this module never clamps a bad value into
    a plausible one):

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
    if source_duration_s <= 0:
        raise PromoError(
            f"demo.mp4 duration {source_duration_s!r} is not usable — refusing to "
            "composite segments that cannot be bounds-checked"
        )
    if not script.scenes:
        raise PromoError("promo script has no scenes")
    if script.total_duration_s <= 0:
        raise PromoError(
            f"promo script total_duration_s must be > 0 (got {script.total_duration_s})"
        )

    segments = 0
    for i, scene in enumerate(script.scenes):
        if scene.duration_s <= 0:
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
    """
    st = style_preset(style)
    dp = duration_preset(preset)
    natural = 0.0
    cards = 0.0
    for scene in script.scenes:
        if scene.kind == "demo_segment":
            start, end = _span(scene)
            natural += end - start
        else:
            cards += scene.duration_s
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
    for scene in script.scenes:
        if scene.kind == "demo_segment":
            start, end = _span(scene)
            durations.append(round((end - start) / pace, 3))
        else:
            durations.append(round(float(scene.duration_s), 3))

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
        n_cards = len(plan.card_ordinal)
        parts.append(
            f"[{plan.logo_index}:v]split={n_cards}"
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


def assert_only_demo_footage(argv: list[str], run_dir: Path, cfg: Config) -> None:
    """Refuse any argv that could pull in footage other than the run's demo.mp4.

    THE structural guarantee of this module, applied to the finished command
    line rather than trusted to a comment: walk the argv and require that

    * exactly one ``-i`` names ``/vhs/demo.mp4`` (the run's own render),
    * every other ``-i`` is either a synthetic solid-colour ``lavfi`` source
      (``-f lavfi -i color=…``) or the configured raster brand logo at its
      read-only mount point,
    * the only bind mounts are the run directory and that logo file,
    * the output is ``/vhs/promo.mp4`` — the compositor writes its own artifact
      and can never overwrite ``demo.mp4``, the artifact of record.

    Anything else — a second video, a ``movie=`` lavfi source, a mount of some
    other directory — raises. Combined with ``--network none`` on the container,
    the compositor structurally cannot show frames the run did not produce.

    Raises:
        PromoError: naming the offending input or mount.
    """
    run_dir = Path(run_dir).resolve()
    source = f"{CONTAINER_RUN_DIR}/{render.PRIMARY_ARTIFACT}"
    logo_path = _container_logo_path(cfg)

    if cfg.brand_logo is not None and Path(cfg.brand_logo).suffix.lower() not in _RASTER_SUFFIXES:
        raise PromoError(
            f"brand_logo {str(cfg.brand_logo)!r} is not a still raster image "
            f"({'/'.join(_RASTER_SUFFIXES)}) — the promo refuses it as a possible "
            "footage source"
        )

    footage = 0
    lavfi = False
    i = 0
    while i < len(argv) - 1:
        token = argv[i]
        if token == "-f":
            lavfi = argv[i + 1] == "lavfi"
            i += 2
            continue
        if token == "-i":
            value = argv[i + 1]
            if lavfi:
                if not _LAVFI_COLOR_RE.match(value):
                    raise PromoError(
                        f"promo refuses generated input {value!r}: only solid-colour "
                        "lavfi card backgrounds are allowed"
                    )
            elif value == source:
                footage += 1
            elif logo_path is not None and value == logo_path:
                pass
            else:
                raise PromoError(
                    f"promo refuses foreign video input {value!r}: the only footage "
                    f"source is the run's own {render.PRIMARY_ARTIFACT}"
                )
            lavfi = False
            i += 2
            continue
        i += 1

    if footage != 1:
        raise PromoError(
            f"promo argv must read {source!r} exactly once as its footage source "
            f"(found {footage})"
        )

    # ``-v`` here is docker's bind-mount flag: the ffmpeg side of the argv
    # deliberately spells its verbosity ``-loglevel`` (never ``-v``), so this
    # scan cannot confuse the two.
    allowed_mounts = {f"{run_dir}:{CONTAINER_RUN_DIR}"}
    if cfg.brand_logo is not None and logo_path is not None:
        allowed_mounts.add(f"{Path(cfg.brand_logo).resolve()}:{logo_path}:ro")
    for j, token in enumerate(argv[:-1]):
        if token == "-v" and argv[j + 1] not in allowed_mounts:
            raise PromoError(
                f"promo refuses bind mount {argv[j + 1]!r}: only the run directory "
                "(and the brand logo, read-only) may be visible to the compositor"
            )

    output = argv[-1] if argv else ""
    if output != f"{CONTAINER_RUN_DIR}/{PROMO_ARTIFACT}":
        raise PromoError(
            f"promo must write {CONTAINER_RUN_DIR}/{PROMO_ARTIFACT}, not {output!r} — "
            f"{render.PRIMARY_ARTIFACT} and the published guide artifacts are never "
            "written by the compositor"
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
        argv += ["-i", logo_path]
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
        if duration is not None:
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
        return float((proc.stdout or "").strip())
    except ValueError:
        return None


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
    """Record why there is no promo. Best-effort, like the promo itself."""
    try:
        (Path(run_dir) / PROMO_LOG).write_text(message + "\n", encoding="utf-8")
    except OSError:  # pragma: no cover - unwritable run dir
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

    BEST-EFFORT, exactly like ``render._generate_gif_preview``: every failure
    mode — no ``demo.mp4``, an unmeasurable duration, an ungrounded script, a
    non-zero ffmpeg, a timeout, a promo that fails its size/duration gate —
    returns ``None`` and writes the reason to ``promo.log``. A promo must never
    fail the run, and it never touches ``demo.mp4`` or any published guide
    artifact (the only output path in the argv is ``/vhs/promo.mp4``).

    Args:
        run_dir: run directory holding ``demo.mp4``; mounted at ``/vhs``.
        script: the grounded scene list (``promo_script.json``).
        cfg: pipeline config (image, resource limits, brand kit).
        style: one of :data:`STYLES`.
        preset: one of :data:`DURATIONS` (15/30/60).
        image: optional image override (must carry ffmpeg/ffprobe).

    Returns:
        Path to the validated ``promo.mp4``, or ``None``.
    """
    run_dir = Path(run_dir).resolve()
    try:
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
    except (PromoError, subprocess.TimeoutExpired, OSError) as e:
        _log_skip(run_dir, f"promo skipped: {e}")
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
