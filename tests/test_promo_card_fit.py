"""Geometry checks for the drawtext filters emitted for promo cards."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from readme2demo import promo, promo_script
from readme2demo.brand import escape_drawtext, title_card_drawtext
from readme2demo.config import Config
from readme2demo.types import AgentResult, CommandEntry, CommandLog, PromoScene, PromoScript


def _script(text: str) -> PromoScript:
    return PromoScript(
        total_duration_s=6.0,
        scenes=[
            PromoScene(kind="title_card", text=text, duration_s=2.0),
            PromoScene(
                kind="demo_segment", start_s=0.0, end_s=2.0, duration_s=2.0
            ),
            PromoScene(kind="end_card", text=text, duration_s=2.0),
        ],
    )


def _violations(text: str) -> list[str]:
    # Geometry fixtures explicitly back any command-shaped spans in their text.
    candidates = promo_script.command_candidates(text)
    return promo_script._card_violations(
        "title", _script(text).scenes[0], set(candidates),
        CommandLog(
            engine="claude-code", result=AgentResult(outcome="success"),
            entries=[CommandEntry(cmd=c, exit_code=0) for c in candidates],
        ), "",
    )


def _graph(text: str, style: str, run_dir: Path, cfg: Config) -> str:
    argv = promo.build_promo_argv(
        _script(text), run_dir, cfg, source_duration_s=2.0, style=style
    )
    return argv[argv.index("-filter_complex") + 1]


def _card_filters(graph: str, input_index: int) -> list[str]:
    card = graph.split(f"[{input_index}:v]", 1)[1].split(",setsar=1", 1)[0]
    return re.findall(r"drawtext=.*?:y=\(h-text_h\)/2(?:[+-]\d+)?", card)


def _assert_preserved(filters: list[str], text: str) -> None:
    # Match authored newline boundaries while permitting only inserted breaks.
    chunks = iter(f.removeprefix("drawtext=text=").split(":expansion=none", 1)[0]
                  for f in filters)
    for paragraph in text.split("\n"):
        expected = escape_drawtext(paragraph)
        actual = next(chunks, None)
        assert actual is not None, paragraph
        while len(actual) < len(expected):
            part = next(chunks, None)
            assert part is not None, paragraph
            actual += part
        assert actual == expected
    assert next(chunks, None) is None


def _assert_geometry(filters: list[str]) -> list[tuple[float, float, float, float]]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("requires ffmpeg with drawtext for real font measurements")
    assert filters
    measurements = []
    for fragment in filters:
        # Preserve the emitted font, size, text and geometry; log x/w/h/y.
        measured = fragment.replace("x=(w-text_w)/2", "x=print((w-text_w)/2)")
        y = fragment.rsplit(":y=", 1)[1]
        measured = measured.rsplit(":y=", 1)[0] + (
            f":y=0*print(text_w)+0*print(text_h)+print({y})"
        )
        measurements.append(measured)
    result = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "repeat+info", "-f", "lavfi",
            "-i", f"color=s={promo.CANVAS_W}x{promo.CANVAS_H}",
            "-vf", ",".join(measurements), "-frames:v", "1", "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=10, check=True,
    )
    values = re.findall(r"^(-?[0-9]+\.[0-9]+)$", result.stderr, re.MULTILINE)
    # drawtext evaluates x twice, producing x/w/h/y/x for each filter.
    assert len(values) == len(filters) * 5, result.stderr
    geometry = []
    for i, fragment in enumerate(filters):
        x, width, height, y_pos = map(float, values[i * 5:i * 5 + 4])
        assert x >= promo.CARD_MARGIN, (width, fragment)
        assert x + width <= promo.CANVAS_W - promo.CARD_MARGIN, fragment
        assert y_pos >= promo.CARD_MARGIN, (height, fragment)
        assert y_pos + height <= promo.CANVAS_H - promo.CARD_MARGIN, fragment
        geometry.append((x, width, height, y_pos))
    return geometry


@pytest.mark.parametrize("style", ["minimal", "tech", "energetic"])
@pytest.mark.parametrize(
    "text",
    [
        "alphacrack/readme2demo — verified in a fresh container",
        "W" * 119,
        "W" * 120,
    ],
    ids=["repo-title", "boundary-minus-one", "boundary"],
)
def test_card_argv_lines_fit_canvas(text: str, style: str, tmp_path: Path) -> None:
    """Regression: accepted long cards emit lines inside the canvas margins."""
    assert _violations(text) == []
    graph = _graph(text, style, tmp_path, Config())
    for index in (1, 2):
        filters = _card_filters(graph, index)
        _assert_preserved(filters, text)
        _assert_geometry(filters)


@pytest.mark.parametrize("length, accepted", [(119, True), (120, True), (121, False)])
def test_card_validator_length_boundary(length: int, accepted: bool) -> None:
    """Regression: the card validator accepts its boundary and rejects its near miss."""
    assert (_violations("W" * length) == []) is accepted


@pytest.mark.parametrize("style", ["minimal", "tech", "energetic"])
@pytest.mark.parametrize("newlines", [False, True], ids=["one-paragraph", "newlines"])
@pytest.mark.parametrize("wide", [False, True], ids=["ascii", "wide-unicode"])
@pytest.mark.parametrize("metacharacters", [False, True], ids=["plain", "escaping"])
def test_wrapped_card_axis_product(
    style: str, newlines: bool, wide: bool, metacharacters: bool, tmp_path: Path,
) -> None:
    """Regression: wrapping preserves text and geometry across interacting alphabets."""
    seed = "Ｗ界😀" if wide else "W"
    if metacharacters:
        seed += ":'\\[],;%"
    text = (seed * 120)[:120]
    if newlines:
        text = text[:60] + "\n" + text[61:]
    assert _violations(text) == []
    graph = _graph(text, style, tmp_path, Config())
    for index in (1, 2):
        filters = _card_filters(graph, index)
        _assert_preserved(filters, text)
        _assert_geometry(filters)


@pytest.mark.parametrize("style", ["minimal", "tech", "energetic"])
@pytest.mark.parametrize(
    "text", ["W\n" * 59 + "WW", "W" + "\n" * 118 + "W", "W" + "\n" * 119],
    ids=["dense-text", "empty-lines", "trailing-empty-lines"],
)
def test_dense_newlines_reduce_fontsize(text: str, style: str, tmp_path: Path) -> None:
    """Regression: dense explicit lines shrink and remain vertically inside the frame."""
    assert _violations(text) == []
    filters = _card_filters(_graph(text, style, tmp_path, Config()), 1)
    _assert_preserved(filters, text)
    sizes = [int(re.search(r":fontsize=(\d+):", f)[1]) for f in filters]
    assert max(sizes) < promo.STYLES[style].card_fontsize
    _assert_geometry(filters)


@pytest.mark.parametrize("text", [" " + "W" * 119, "W" + "\n" * 119])
def test_raw_card_budget_includes_whitespace(text: str) -> None:
    """Regression: padding cannot admit an unbounded number of explicit lines."""
    assert _violations(text) == []
    assert _violations(text + "\n")


@pytest.mark.parametrize("tabs", [1, 10], ids=["single", "repeated"])
@pytest.mark.parametrize("prefix", ["", "i", "ii", "iii"],
                         ids=["phase-zero", "phase-one", "phase-two", "phase-three"])
@pytest.mark.parametrize("font", [None, "DejaVu Sans Mono", "DejaVu Serif"],
                         ids=["default", "mono", "serif"])
@pytest.mark.parametrize("style", ["minimal", "tech", "energetic"])
def test_tab_stops_fit_canvas_across_phases(
    tabs: int, prefix: str, font: str | None, style: str, tmp_path: Path,
) -> None:
    """Regression: repeated tabs fit even when the first tab starts between stops."""
    text = prefix + "\t" * tabs + "W"
    assert _violations(text) == []
    graph = _graph(text, style, tmp_path, Config(brand_font=font))
    all_filters = []
    for index in (1, 2):
        filters = _card_filters(graph, index)
        _assert_preserved(filters, text)
        all_filters.extend(filters)
    _assert_geometry(all_filters)


@pytest.mark.parametrize("style", ["minimal", "tech", "energetic"])
def test_validator_budget_tracks_renderer_capacity(
    style: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: changing usable height changes the accepted and rendered line budget."""
    monkeypatch.setattr(promo, "CANVAS_H", 68)
    monkeypatch.setattr(promo, "CARD_TEXT_H", 20)
    # At the font floor, four lines occupy 18px; five would need 22px.
    spec = importlib.util.spec_from_file_location(
        "readme2demo._promo_script_capacity_test", promo_script.__file__,
    )
    assert spec is not None and spec.loader is not None
    validator = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, validator)
    spec.loader.exec_module(validator)
    text = "W\n\n\n"
    log = CommandLog(engine="claude-code", result=AgentResult(outcome="success"))
    assert validator._card_violations("title", _script(text).scenes[0], set(), log, "") == []
    assert validator._card_violations(
        "title", _script(text + "\n").scenes[0], set(), log, "",
    )
    filters = _card_filters(_graph(text, style, tmp_path, Config()), 1)
    _assert_preserved(filters, text)
    _assert_geometry(filters)


@pytest.mark.parametrize(
    "style, lines, expected_size",
    [
        ("minimal", 9, 48), ("minimal", 10, 44),
        ("tech", 8, 54), ("tech", 9, 49),
        ("energetic", 7, 60), ("energetic", 8, 55),
    ],
    ids=["minimal-fit", "minimal-overflow", "tech-fit", "tech-overflow",
         "energetic-fit", "energetic-overflow"],
)
def test_vertical_fit_stops_at_first_fitting_size(
    style: str, lines: int, expected_size: int, tmp_path: Path,
) -> None:
    """Regression: adjacent line counts preserve size or shrink only to the first fit."""
    # Independently solve (lines-1)*round(1.4*size)+2*size <= 652 at each style cap.
    text = "\n".join(["W"] * lines)
    assert _violations(text) == []
    graph = _graph(text, style, tmp_path, Config())
    all_filters = []
    for index in (1, 2):
        filters = _card_filters(graph, index)
        _assert_preserved(filters, text)
        sizes = {int(re.search(r":fontsize=(\d+):", f)[1]) for f in filters}
        assert sizes == {expected_size}
        all_filters.extend(filters)
    _assert_geometry(all_filters)


@pytest.mark.parametrize("style", ["minimal", "tech", "energetic"])
@pytest.mark.parametrize("font", [None, "DejaVu Sans Mono", "DejaVu Serif"])
def test_width_model_covers_glyph_alphabets(
    style: str, font: str | None, tmp_path: Path,
) -> None:
    """Regression: conservative wrapping also bounds symbols, tabs and accented glyphs."""
    texts = [chr(n) for n in range(33, 127)] + [
        "x\t", "Ω", "Ж", "—", "é", "é", "界", "Ｗ", "🙂",
    ]
    script = _script("Verified")
    script.scenes = [PromoScene(kind="title_card", text=t, duration_s=2.0) for t in texts] + [
        script.scenes[1],
    ]
    argv = promo.build_promo_argv(
        script, tmp_path, Config(brand_font=font), source_duration_s=2.0, style=style
    )
    graph = argv[argv.index("-filter_complex") + 1]
    filters = [_card_filters(graph, i + 1)[0] for i in range(len(texts))]
    geometry = _assert_geometry(filters)
    fontsize = promo.STYLES[style].card_fontsize
    for text, fragment, (_, width, _, _) in zip(texts, filters, geometry):
        _assert_preserved([fragment], text)
        assert sum(promo._card_char_width(ch, fontsize) for ch in text) >= width, (
            text, font, fontsize, width,
        )


def test_wrap_prefers_word_boundaries_without_trimming(tmp_path: Path) -> None:
    """Regression: wrapping retains spaces and breaks between words when possible."""
    text = "readable words " * 8
    filters = _card_filters(_graph(text, "tech", tmp_path, Config()), 1)
    chunks = [f.removeprefix("drawtext=text=").split(":expansion=none", 1)[0]
              for f in filters]
    assert len(chunks) > 1
    assert all(chunk.endswith(" ") for chunk in chunks)
    _assert_preserved(filters, text)
    _assert_geometry(filters)


def test_unbounded_multiline_card_is_refused() -> None:
    """Regression: text outside the validator budget cannot overflow at the font floor."""
    text = "W\n" * 500
    assert _violations(text)
    with pytest.raises(promo.PromoError, match="fit"):
        promo._card_drawtexts(text, Config(), fontsize=54)


def test_real_fit_card_at_model_boundary_may_wrap_stays_inside_margins(tmp_path: Path) -> None:
    """Regression: cards fitting real fonts may wrap and must remain inside margins."""
    text = "z" * 30
    cfg = Config()
    style = "energetic"
    assert _violations(text) == []
    _assert_geometry([
        title_card_drawtext(text, cfg, fontsize=promo.STYLES[style].card_fontsize),
    ])
    # Lead ruling: identity applies to conservative-budget fits, not real-font fits.
    graph = _graph(text, style, tmp_path, cfg)
    for index in (1, 2):
        filters = _card_filters(graph, index)
        _assert_preserved(filters, text)
        _assert_geometry(filters)


def test_fitting_cards_match_base_filtergraphs(tmp_path: Path) -> None:
    """Regression: fitting cards emit the base filtergraph byte for byte in every style."""
    # Captured with build_filtergraph from base 5818f77, not the new wrapper.
    fixtures = json.loads(
        (Path(__file__).parent / "fixtures" / "promo_card_fit_graphs.json").read_text(
            encoding="utf-8"
        )
    )
    for case in fixtures:
        graph = _graph(case["text"], case["style"], tmp_path, Config())
        assert graph == case["graph"]
        _assert_geometry(_card_filters(graph, 1))
