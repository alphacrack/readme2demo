"""Unit tests for the promo compositor (promo.py, #170).

Two things are pinned here, in the style of ``tests/test_sandbox.py``:

1. **The exact ffmpeg argv and filtergraph** built from a fixture script. The
   builder is pure, so every cut decision is assertable with no Docker, no
   ffmpeg, no network — drift shows up as a literal diff.
2. **The structural refusal**: the compositor may source footage ONLY from the
   run's own ``demo.mp4``. Each way of smuggling in other frames (a second
   video input, a ``movie=`` lavfi source, an extra bind mount, a "logo" that is
   really a video) has a test named for it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from readme2demo import promo
from readme2demo.config import Config
from readme2demo.promo import (
    PromoError,
    PromoScene,
    PromoScript,
    assert_only_demo_footage,
    build_filtergraph,
    build_promo_argv,
    expected_promo_duration_s,
    render_promo,
    segment_pace,
    validate_promo,
    validate_script,
)

IMAGE = "readme2demo/base:latest"
SOURCE_DURATION = 120.0


# --- fixtures -----------------------------------------------------------------


def _script() -> PromoScript:
    """The minimal three-scene script the exact-argv tests are pinned against."""
    return PromoScript(
        version=1,
        total_duration_s=8.0,
        scenes=[
            PromoScene(kind="title_card", text="r2d\nVerified", duration_s=2.0),
            PromoScene(
                kind="demo_segment",
                text="pip install .",
                step_index=0,
                start_s=1.0,
                end_s=5.0,
                duration_s=4.0,
            ),
            PromoScene(kind="end_card", text="pip install readme2demo", duration_s=2.0),
        ],
    )


@pytest.fixture
def fixture_script(fixtures_dir: Path) -> PromoScript:
    """The checked-in representative script (contract-by-example for #169)."""
    return PromoScript.model_validate_json(
        (fixtures_dir / "promo_script.json").read_text(encoding="utf-8")
    )


def _png(tmp_path: Path) -> Path:
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n")
    return logo


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeDocker:
    """Records every argv handed to subprocess.run; never touches Docker."""

    def __init__(self, result: object | None = None) -> None:
        self.calls: list[list[str]] = []
        self._result = result

    def __call__(self, cmd: list[str], **kwargs: object) -> _FakeProc:
        self.calls.append(list(cmd))
        if isinstance(self._result, BaseException):
            raise self._result
        if callable(self._result):
            return self._result(cmd)  # type: ignore[no-any-return]
        return _FakeProc()


# --- the exact argv, pinned ----------------------------------------------------

_TITLE_CARD = (
    "drawtext=text=r2d:fontcolor=#7C6BF2:fontsize=54:x=(w-text_w)/2:y=(h-text_h)/2-38"
    ",drawtext=text=Verified:fontcolor=#7C6BF2:fontsize=54:x=(w-text_w)/2:y=(h-text_h)/2+38"
)
_CAPTION = (
    "drawtext=text=pip install .:fontcolor=#7C6BF2:fontsize=30"
    ":x=(w-text_w)/2:y=h-text_h-64:box=1:boxcolor=black@0.6:boxborderw=14"
)
_END_CARD = (
    "drawtext=text=pip install readme2demo:fontcolor=#7C6BF2:fontsize=54"
    ":x=(w-text_w)/2:y=(h-text_h)/2"
)

EXPECTED_GRAPH = ";".join(
    [
        "[0:v]split=1[seg0]",
        f"[1:v]{_TITLE_CARD},setsar=1,format=yuv420p[v0]",
        (
            "[seg0]trim=start=1.000:end=5.000,setpts=(PTS-STARTPTS)/1.250"
            ",scale=1200:700:force_original_aspect_ratio=decrease"
            ",pad=1200:700:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24"
            f",{_CAPTION},format=yuv420p[v1]"
        ),
        f"[2:v]{_END_CARD},setsar=1,format=yuv420p[v2]",
        "[v0][v1]xfade=transition=fade:duration=0.400:offset=1.600[x1]",
        "[x1][v2]xfade=transition=fade:duration=0.400:offset=4.400[x2]",
        "[x2]null[vout]",
    ]
)


class TestExactArgv:
    def test_exact_default_promo_argv(self, tmp_run_dir: Path) -> None:
        """The full docker/ffmpeg argv, pinned. Any drift in the compositing
        contract — mount, limits, entrypoint, encoder, output path — shows up
        here as an exact diff (pattern: test_sandbox.test_exact_default_run_argv)."""
        argv = build_promo_argv(
            _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION
        )
        assert argv == [
            "docker", "run", "--rm",
            "-v", f"{tmp_run_dir.resolve()}:/vhs",
            "--memory", "4g",
            "--cpus", "2",
            "--network", "none",
            "--entrypoint", "ffmpeg",
            IMAGE,
            "-y", "-loglevel", "error",
            "-i", "/vhs/demo.mp4",
            "-f", "lavfi", "-t", "2.000",
            "-i", "color=c=0x101018:s=1200x700:r=24",
            "-f", "lavfi", "-t", "2.000",
            "-i", "color=c=0x101018:s=1200x700:r=24",
            "-filter_complex", EXPECTED_GRAPH,
            "-map", "[vout]",
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "/vhs/promo.mp4",
        ]

    def test_exact_filtergraph(self, tmp_run_dir: Path) -> None:
        """The graph text alone, so cut math can be reviewed without the argv."""
        graph = build_filtergraph(_script(), Config(), source_duration_s=SOURCE_DURATION)
        assert graph == EXPECTED_GRAPH

    def test_output_is_promo_mp4_and_demo_is_only_read(self, tmp_run_dir: Path) -> None:
        """demo.mp4 appears only as an input; the sole output is promo.mp4, so a
        promo can never overwrite the artifact of record."""
        argv = build_promo_argv(
            _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION
        )
        assert argv[-1] == "/vhs/promo.mp4"
        # demo.mp4 appears exactly once, and only as the value of an -i flag.
        positions = [i for i, tok in enumerate(argv) if tok == "/vhs/demo.mp4"]
        assert len(positions) == 1
        assert argv[positions[0] - 1] == "-i"

    def test_resource_limits_come_from_config(self, tmp_run_dir: Path) -> None:
        cfg = Config(memory="8g", cpus="4", base_image="custom/img:1")
        argv = build_promo_argv(_script(), tmp_run_dir, cfg, source_duration_s=SOURCE_DURATION)
        assert argv[argv.index("--memory") + 1] == "8g"
        assert argv[argv.index("--cpus") + 1] == "4"
        assert argv[argv.index("--entrypoint") + 2] == "custom/img:1"

    def test_network_is_none(self, tmp_run_dir: Path) -> None:
        """Compositing reads only what is mounted. No network means ffmpeg
        cannot fetch a remote source even if a future edit tried to."""
        argv = build_promo_argv(
            _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION
        )
        assert argv[argv.index("--network") + 1] == "none"

    def test_image_override_is_used(self, tmp_run_dir: Path) -> None:
        argv = build_promo_argv(
            _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION,
            image="other/img:2",
        )
        assert argv[argv.index("--entrypoint") + 2] == "other/img:2"


# --- purity --------------------------------------------------------------------


class TestBuilderIsPure:
    def test_builder_runs_no_subprocess(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The builder is pure: if it ever shells out, this test fails loudly."""
        def _boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("build_promo_argv must not run subprocesses")

        monkeypatch.setattr(promo.subprocess, "run", _boom)
        build_promo_argv(_script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION)

    def test_builder_needs_nothing_on_disk(self) -> None:
        """No I/O either: a run dir that does not exist still builds an argv."""
        argv = build_promo_argv(
            _script(), Path("/nonexistent/run"), Config(), source_duration_s=SOURCE_DURATION
        )
        assert argv[argv.index("-v") + 1] == "/nonexistent/run:/vhs"

    def test_builder_is_deterministic(self, tmp_run_dir: Path) -> None:
        a = build_promo_argv(_script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION)
        b = build_promo_argv(_script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION)
        assert a == b


# --- structural refusal: only the run's own demo.mp4 ---------------------------


class TestStructuralRefusal:
    def test_foreign_video_input_rejected(self, tmp_run_dir: Path) -> None:
        """A second video input is refused — the whole grounding claim rests on
        the promo showing nothing but the run's own render."""
        argv = build_promo_argv(
            _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION
        )
        argv += ["-i", "/vhs/broll.mp4"]
        with pytest.raises(PromoError, match="foreign video input"):
            assert_only_demo_footage(argv, tmp_run_dir, Config())

    def test_http_input_rejected(self, tmp_run_dir: Path) -> None:
        argv = build_promo_argv(
            _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION
        )
        argv += ["-i", "https://example.com/stock.mp4"]
        with pytest.raises(PromoError, match="foreign video input"):
            assert_only_demo_footage(argv, tmp_run_dir, Config())

    def test_lavfi_movie_source_rejected(self, tmp_run_dir: Path) -> None:
        """lavfi is allowed ONLY for solid-colour card backgrounds; `movie=`
        would decode arbitrary frames through the same door."""
        argv = build_promo_argv(
            _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION
        )
        argv += ["-f", "lavfi", "-i", "movie=/tmp/stock.mp4"]
        with pytest.raises(PromoError, match="generated input"):
            assert_only_demo_footage(argv, tmp_run_dir, Config())

    def test_lavfi_testsrc_rejected(self, tmp_run_dir: Path) -> None:
        argv = build_promo_argv(
            _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION
        )
        argv += ["-f", "lavfi", "-i", "testsrc=size=1200x700:rate=24"]
        with pytest.raises(PromoError, match="generated input"):
            assert_only_demo_footage(argv, tmp_run_dir, Config())

    def test_extra_bind_mount_rejected(self, tmp_run_dir: Path) -> None:
        """Only the run dir (and the logo file) may be visible: any other mount
        would put foreign footage within reach of a relative path."""
        argv = build_promo_argv(
            _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION
        )
        argv.insert(3, "-v")
        argv.insert(4, "/home/me/footage:/footage")
        with pytest.raises(PromoError, match="bind mount"):
            assert_only_demo_footage(argv, tmp_run_dir, Config())

    def test_missing_demo_input_rejected(self, tmp_run_dir: Path) -> None:
        """A cut with no footage input at all (cards only) is not a promo of
        this run."""
        argv = ["docker", "run", "--rm", "-v", f"{tmp_run_dir.resolve()}:/vhs", "x"]
        with pytest.raises(PromoError, match="exactly once"):
            assert_only_demo_footage(argv, tmp_run_dir, Config())

    def test_writing_over_demo_mp4_rejected(self, tmp_run_dir: Path) -> None:
        """The compositor may only write its own artifact: demo.mp4 is the
        artifact of record and a promo must never be able to clobber it."""
        argv = build_promo_argv(
            _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION
        )
        argv[-1] = "/vhs/demo.mp4"
        with pytest.raises(PromoError, match="promo must write /vhs/promo.mp4"):
            assert_only_demo_footage(argv, tmp_run_dir, Config())

    def test_video_disguised_as_brand_logo_rejected(self, tmp_path: Path) -> None:
        """Config's validator already rejects a non-raster logo; the compositor
        re-checks, because a Config built with model_construct bypasses it."""
        cfg = Config.model_construct(brand_logo=tmp_path / "logo.mp4", brand_color="#7C6BF2")
        argv = ["docker", "run", "--rm", "-v", f"{tmp_path}:/vhs", "-i", "/vhs/demo.mp4"]
        with pytest.raises(PromoError, match="still raster image"):
            assert_only_demo_footage(argv, tmp_path, cfg)

    def test_builder_audits_its_own_output(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard (#170): the audit is wired INTO the builder, so a
        future graph change that adds an input cannot ship unnoticed."""
        real = promo.build_filtergraph

        def _sneaky(*args: object, **kwargs: object) -> str:
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(promo, "build_filtergraph", _sneaky)
        calls: list[str] = []
        monkeypatch.setattr(
            promo,
            "assert_only_demo_footage",
            lambda argv, run_dir, cfg: calls.append("audited"),
        )
        build_promo_argv(_script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION)
        assert calls == ["audited"]


# --- grounding: script validation ----------------------------------------------


class TestScriptValidation:
    def test_zero_demo_segments_rejected(self) -> None:
        """At least one real demo segment is mandatory in every cut."""
        script = PromoScript(
            total_duration_s=4.0,
            scenes=[
                PromoScene(kind="title_card", text="r2d", duration_s=2.0),
                PromoScene(kind="end_card", text="pip install readme2demo", duration_s=2.0),
            ],
        )
        with pytest.raises(PromoError, match="no demo_segment scenes"):
            validate_script(script, source_duration_s=SOURCE_DURATION)

    def test_segment_past_end_of_video_rejected(self) -> None:
        """The bounds check is a grounding check: footage that does not exist
        cannot be shown, and the span is NOT clamped into range."""
        script = _script()
        script.scenes[1].end_s = 130.0
        with pytest.raises(PromoError, match="runs past the end"):
            validate_script(script, source_duration_s=SOURCE_DURATION)

    def test_segment_ending_exactly_at_duration_accepted(self) -> None:
        script = _script()
        script.scenes[1].start_s = 100.0
        script.scenes[1].end_s = SOURCE_DURATION
        validate_script(script, source_duration_s=SOURCE_DURATION)

    def test_negative_start_rejected(self) -> None:
        script = _script()
        script.scenes[1].start_s = -1.0
        with pytest.raises(PromoError, match="negative"):
            validate_script(script, source_duration_s=SOURCE_DURATION)

    def test_reversed_span_rejected(self) -> None:
        script = _script()
        script.scenes[1].start_s = 9.0
        script.scenes[1].end_s = 4.0
        with pytest.raises(PromoError, match="greater than start_s"):
            validate_script(script, source_duration_s=SOURCE_DURATION)

    def test_unbounded_segment_rejected(self) -> None:
        """A segment with no span is a request to trust the script."""
        script = _script()
        script.scenes[1].end_s = None
        with pytest.raises(PromoError, match="missing start_s/end_s"):
            validate_script(script, source_duration_s=SOURCE_DURATION)

    def test_zero_duration_scene_rejected(self) -> None:
        script = _script()
        script.scenes[0].duration_s = 0.0
        with pytest.raises(PromoError, match="duration_s must be > 0"):
            validate_script(script, source_duration_s=SOURCE_DURATION)

    def test_card_without_text_rejected(self) -> None:
        script = _script()
        script.scenes[0].text = "  "
        with pytest.raises(PromoError, match="card scenes need text"):
            validate_script(script, source_duration_s=SOURCE_DURATION)

    def test_empty_script_rejected(self) -> None:
        with pytest.raises(PromoError, match="no scenes"):
            validate_script(PromoScript(total_duration_s=5.0, scenes=[]), source_duration_s=10.0)

    def test_non_positive_total_duration_rejected(self) -> None:
        script = _script()
        script.total_duration_s = 0.0
        with pytest.raises(PromoError, match="total_duration_s must be > 0"):
            validate_script(script, source_duration_s=SOURCE_DURATION)

    def test_unmeasured_source_duration_rejected(self) -> None:
        """Without a measured demo.mp4 there is nothing to bounds-check against,
        so the cut is refused rather than composited on trust."""
        with pytest.raises(PromoError, match="not usable"):
            validate_script(_script(), source_duration_s=0.0)

    def test_builder_enforces_bounds_too(self, tmp_run_dir: Path) -> None:
        """The bounds check is not optional: it runs on the argv path as well."""
        script = _script()
        script.scenes[1].end_s = 500.0
        with pytest.raises(PromoError, match="runs past the end"):
            build_promo_argv(script, tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION)


# --- presets are data ----------------------------------------------------------


class TestPresets:
    def test_preset_tables_are_the_documented_sets(self) -> None:
        assert sorted(promo.DURATIONS) == [15, 30, 60]
        assert sorted(promo.STYLES) == ["energetic", "minimal", "tech"]

    def test_15_and_60_produce_different_argv(self, fixture_script: PromoScript) -> None:
        """A tighter budget speeds the segments up; the difference is visible in
        the setpts factor, not hidden in branching logic."""
        short = build_promo_argv(
            fixture_script, Path("/run"), Config(), source_duration_s=SOURCE_DURATION, preset=15
        )
        long = build_promo_argv(
            fixture_script, Path("/run"), Config(), source_duration_s=SOURCE_DURATION, preset=60
        )
        assert short != long
        assert "setpts=(PTS-STARTPTS)/3.333" in short[short.index("-filter_complex") + 1]
        assert "setpts=(PTS-STARTPTS)/1.250" in long[long.index("-filter_complex") + 1]

    def test_tech_and_energetic_produce_different_argv(
        self, fixture_script: PromoScript
    ) -> None:
        tech = build_promo_argv(
            fixture_script, Path("/run"), Config(), source_duration_s=SOURCE_DURATION, style="tech"
        )
        energetic = build_promo_argv(
            fixture_script, Path("/run"), Config(), source_duration_s=SOURCE_DURATION,
            style="energetic",
        )
        assert tech != energetic
        assert "transition=fade" in tech[tech.index("-filter_complex") + 1]
        assert "transition=wipeleft" in energetic[energetic.index("-filter_complex") + 1]
        assert "color=c=0x1A0B2E:s=1200x700:r=24" in energetic

    def test_style_sets_the_pace_floor(self, fixture_script: PromoScript) -> None:
        assert segment_pace(fixture_script, style="minimal", preset=60) == 1.0
        assert segment_pace(fixture_script, style="energetic", preset=60) == 1.6

    def test_pace_is_capped_so_footage_stays_legible(self) -> None:
        """A very long segment in a 15s cut is capped at the preset's max_pace
        rather than sped up into an unreadable blur."""
        script = PromoScript(
            total_duration_s=15.0,
            scenes=[
                PromoScene(
                    kind="demo_segment", text="build", start_s=0.0, end_s=600.0, duration_s=600.0
                )
            ],
        )
        assert segment_pace(script, preset=15) == promo.DURATIONS[15].max_pace

    def test_unknown_style_rejected(self, tmp_run_dir: Path) -> None:
        with pytest.raises(PromoError, match="unknown promo style"):
            build_promo_argv(
                _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION, style="neon"
            )

    def test_unknown_duration_preset_rejected(self, tmp_run_dir: Path) -> None:
        with pytest.raises(PromoError, match="unknown promo duration preset"):
            build_promo_argv(
                _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION, preset=45
            )


# --- graph shape ---------------------------------------------------------------


class TestFiltergraph:
    def test_source_is_split_once_per_segment(self, fixture_script: PromoScript) -> None:
        """A filtergraph pad may be consumed once, so N cuts of one video need
        an N-way split — the mechanism that keeps ONE input serving every cut."""
        graph = build_filtergraph(fixture_script, Config(), source_duration_s=SOURCE_DURATION)
        assert graph.startswith("[0:v]split=2[seg0][seg1];")

    def test_segments_trim_the_verified_spans_verbatim(
        self, fixture_script: PromoScript
    ) -> None:
        graph = build_filtergraph(fixture_script, Config(), source_duration_s=SOURCE_DURATION)
        assert "trim=start=12.500:end=24.500" in graph
        assert "trim=start=61.000:end=79.000" in graph

    def test_caption_text_is_escaped_not_rewritten(self) -> None:
        """Caption text is passed through verbatim, with escaping delegated to
        brand.escape_drawtext (via title_card_drawtext) — a colon in a command
        must survive as text, not truncate the filter."""
        script = _script()
        script.scenes[1].text = "run: readme2demo run <url>"
        graph = build_filtergraph(script, Config(), source_duration_s=SOURCE_DURATION)
        assert r"text=run\: readme2demo run <url>" in graph

    def test_multi_line_card_stacks_one_drawtext_per_line(self) -> None:
        graph = build_filtergraph(_script(), Config(), source_duration_s=SOURCE_DURATION)
        assert "text=r2d:" in graph
        assert "text=Verified:" in graph

    def test_brand_logo_is_overlaid_on_cards(self, tmp_path: Path) -> None:
        """The logo is a still raster from config, mounted read-only and split
        across the cards — the only extra input the audit tolerates."""
        logo = _png(tmp_path)
        cfg = Config(brand_logo=logo)
        argv = build_promo_argv(_script(), tmp_path, cfg, source_duration_s=SOURCE_DURATION)
        assert "-v" in argv and f"{logo.resolve()}:/brand/logo.png:ro" in argv
        assert "/brand/logo.png" in argv
        graph = argv[argv.index("-filter_complex") + 1]
        assert "[3:v]split=2[lg0][lg1]" in graph
        assert "[c0][lg0]overlay=W-w-24:24,format=yuv420p[v0]" in graph

    def test_no_logo_input_without_brand_logo(self, tmp_run_dir: Path) -> None:
        argv = build_promo_argv(
            _script(), tmp_run_dir, Config(), source_duration_s=SOURCE_DURATION
        )
        assert "/brand" not in " ".join(argv)

    def test_expected_duration_matches_xfade_math(self) -> None:
        """The gate's expectation is computed from the same numbers the graph
        uses: scene lengths minus each crossfade overlap."""
        assert expected_promo_duration_s(_script()) == pytest.approx(6.4)

    def test_xfade_never_swallows_a_short_scene(self) -> None:
        script = PromoScript(
            total_duration_s=1.0,
            scenes=[
                PromoScene(kind="title_card", text="r2d", duration_s=0.4),
                PromoScene(
                    kind="demo_segment", text="go", start_s=0.0, end_s=0.5, duration_s=0.5
                ),
            ],
        )
        graph = build_filtergraph(script, Config(), source_duration_s=SOURCE_DURATION)
        assert "duration=0.200:offset=0.200" in graph


# --- the thin runner -----------------------------------------------------------


def _installed(run_dir: Path) -> None:
    """Give the run dir a plausible demo.mp4."""
    (run_dir / "demo.mp4").write_bytes(b"\x00" * (11 * 1024))


class TestRenderPromo:
    def test_runs_ffmpeg_in_the_container_and_returns_the_promo(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _installed(tmp_run_dir)
        monkeypatch.setattr(promo, "source_duration_s", lambda *a, **k: SOURCE_DURATION)
        monkeypatch.setattr(promo.shutil, "which", lambda name: None)

        def _write(cmd: list[str]) -> _FakeProc:
            (tmp_run_dir / "promo.mp4").write_bytes(b"\x00" * (11 * 1024))
            return _FakeProc()

        fake = _FakeDocker(_write)
        monkeypatch.setattr(promo.subprocess, "run", fake)

        out = render_promo(tmp_run_dir, _script(), Config())
        assert out == tmp_run_dir / "promo.mp4"
        argv = fake.calls[0]
        assert argv[:3] == ["docker", "run", "--rm"]
        assert argv[argv.index("--entrypoint") + 1] == "ffmpeg"

    def test_failure_returns_none_and_never_raises(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Best-effort, like the GIF preview: a broken promo must never fail the
        run, and demo.mp4 stays untouched."""
        _installed(tmp_run_dir)
        before = (tmp_run_dir / "demo.mp4").read_bytes()
        monkeypatch.setattr(promo, "source_duration_s", lambda *a, **k: SOURCE_DURATION)
        monkeypatch.setattr(
            promo.subprocess, "run", _FakeDocker(lambda cmd: _FakeProc(1, "", "boom"))
        )
        assert render_promo(tmp_run_dir, _script(), Config()) is None
        assert (tmp_run_dir / "demo.mp4").read_bytes() == before
        assert "boom" in (tmp_run_dir / "promo.log").read_text()

    def test_missing_drawtext_gets_an_actionable_hint(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """drawtext (cards + captions) needs an ffmpeg built with libfreetype,
        which `command -v ffmpeg` does not prove — say so in promo.log."""
        _installed(tmp_run_dir)
        monkeypatch.setattr(promo, "source_duration_s", lambda *a, **k: SOURCE_DURATION)
        monkeypatch.setattr(
            promo.subprocess,
            "run",
            _FakeDocker(lambda cmd: _FakeProc(1, "", "No such filter: 'drawtext'")),
        )
        assert render_promo(tmp_run_dir, _script(), Config()) is None
        assert "libfreetype" in (tmp_run_dir / "promo.log").read_text()

    def test_missing_font_gets_an_actionable_hint(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """drawtext resolves fonts inside the container, which the host cannot
        verify (see brand.py) — name the fix instead of the raw error."""
        _installed(tmp_run_dir)
        monkeypatch.setattr(promo, "source_duration_s", lambda *a, **k: SOURCE_DURATION)
        monkeypatch.setattr(
            promo.subprocess,
            "run",
            _FakeDocker(lambda cmd: _FakeProc(1, "", "Cannot find a valid font for the family")),
        )
        assert render_promo(tmp_run_dir, _script(), Config()) is None
        assert "brand_font" in (tmp_run_dir / "promo.log").read_text()

    def test_timeout_returns_none(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _installed(tmp_run_dir)
        monkeypatch.setattr(promo, "source_duration_s", lambda *a, **k: SOURCE_DURATION)
        monkeypatch.setattr(
            promo.subprocess,
            "run",
            _FakeDocker(subprocess.TimeoutExpired(cmd="docker", timeout=1)),
        )
        assert render_promo(tmp_run_dir, _script(), Config()) is None

    def test_missing_demo_mp4_returns_none(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(promo.subprocess, "run", _FakeDocker())
        assert render_promo(tmp_run_dir, _script(), Config()) is None
        assert "demo.mp4 not found" in (tmp_run_dir / "promo.log").read_text()

    def test_unmeasurable_duration_refuses_to_composite(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No ffprobe measurement means no bounds check, and an un-bounds-checked
        cut is exactly the grounding hole this module exists to close."""
        _installed(tmp_run_dir)
        monkeypatch.setattr(promo, "source_duration_s", lambda *a, **k: None)
        fake = _FakeDocker()
        monkeypatch.setattr(promo.subprocess, "run", fake)
        assert render_promo(tmp_run_dir, _script(), Config()) is None
        assert fake.calls == []
        assert "could not measure" in (tmp_run_dir / "promo.log").read_text()

    def test_ungrounded_script_produces_no_promo(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _installed(tmp_run_dir)
        script = _script()
        script.scenes[1].end_s = 999.0
        monkeypatch.setattr(promo, "source_duration_s", lambda *a, **k: SOURCE_DURATION)
        fake = _FakeDocker()
        monkeypatch.setattr(promo.subprocess, "run", fake)
        assert render_promo(tmp_run_dir, script, Config()) is None
        assert fake.calls == []  # ffmpeg is never even invoked


class TestSourceDuration:
    def test_host_ffprobe_is_preferred(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(promo.shutil, "which", lambda name: "/usr/bin/ffprobe")
        monkeypatch.setattr(promo.render, "_mp4_duration_s", lambda path, probe: 42.5)
        fake = _FakeDocker()
        monkeypatch.setattr(promo.subprocess, "run", fake)
        assert promo.source_duration_s(tmp_run_dir, Config()) == 42.5
        assert fake.calls == []

    def test_container_ffprobe_argv_when_host_lacks_it(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ffprobe runs in the render image over the same /vhs mount, so a host
        without ffmpeg installed still gets a real measurement."""
        monkeypatch.setattr(promo.shutil, "which", lambda name: None)
        fake = _FakeDocker(lambda cmd: _FakeProc(0, "123.75\n"))
        monkeypatch.setattr(promo.subprocess, "run", fake)
        assert promo.source_duration_s(tmp_run_dir, Config()) == 123.75
        assert fake.calls[0] == [
            "docker", "run", "--rm",
            "-v", f"{tmp_run_dir.resolve()}:/vhs",
            "--network", "none",
            "--entrypoint", "ffprobe",
            IMAGE,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            "/vhs/demo.mp4",
        ]

    def test_docker_missing_is_none_not_an_exception(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No host ffprobe AND no docker: measurement fails quietly, and the
        caller then refuses to composite (rather than guessing a bound)."""
        monkeypatch.setattr(promo.shutil, "which", lambda name: None)
        monkeypatch.setattr(promo.subprocess, "run", _FakeDocker(FileNotFoundError("docker")))
        assert promo.source_duration_s(tmp_run_dir, Config()) is None

    def test_ffprobe_failure_is_none(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(promo.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            promo.subprocess, "run", _FakeDocker(lambda cmd: _FakeProc(1, "", "no such file"))
        )
        assert promo.source_duration_s(tmp_run_dir, Config()) is None

    def test_unreadable_duration_is_none(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(promo.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            promo.subprocess, "run", _FakeDocker(lambda cmd: _FakeProc(0, "N/A"))
        )
        assert promo.source_duration_s(tmp_run_dir, Config()) is None


class TestValidatePromo:
    def test_size_gate(self, tmp_run_dir: Path) -> None:
        promo_mp4 = tmp_run_dir / "promo.mp4"
        promo_mp4.write_bytes(b"\x00" * 32)
        with pytest.raises(PromoError, match="too small"):
            validate_promo(promo_mp4, 6.4)

    def test_missing_file(self, tmp_run_dir: Path) -> None:
        with pytest.raises(PromoError, match="missing after compositing"):
            validate_promo(tmp_run_dir / "promo.mp4", 6.4)

    def test_truncated_render_is_an_error(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mirrors render.validate_outputs: an incomplete video is worse than
        no video."""
        promo_mp4 = tmp_run_dir / "promo.mp4"
        promo_mp4.write_bytes(b"\x00" * (11 * 1024))
        monkeypatch.setattr(promo.shutil, "which", lambda name: "/usr/bin/ffprobe")
        monkeypatch.setattr(promo.render, "_mp4_duration_s", lambda path, probe: 1.0)
        with pytest.raises(PromoError, match="did not composite fully"):
            validate_promo(promo_mp4, 30.0)

    def test_overflowing_the_preset_is_an_error(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running over budget means the fit failed (max_pace was not enough)."""
        promo_mp4 = tmp_run_dir / "promo.mp4"
        promo_mp4.write_bytes(b"\x00" * (11 * 1024))
        monkeypatch.setattr(promo.shutil, "which", lambda name: "/usr/bin/ffprobe")
        monkeypatch.setattr(promo.render, "_mp4_duration_s", lambda path, probe: 48.0)
        with pytest.raises(PromoError, match="overflowing the 30s preset"):
            validate_promo(promo_mp4, 48.0, preset=30)

    def test_short_but_complete_promo_is_accepted(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The preset is a budget, not a quota: a run with little footage gets a
        short promo rather than a padded one. Deliberately one-sided — failing an
        honest short cut would create pressure to pad it with something the run
        never did."""
        promo_mp4 = tmp_run_dir / "promo.mp4"
        promo_mp4.write_bytes(b"\x00" * (11 * 1024))
        monkeypatch.setattr(promo.shutil, "which", lambda name: "/usr/bin/ffprobe")
        monkeypatch.setattr(promo.render, "_mp4_duration_s", lambda path, probe: 6.4)
        assert validate_promo(promo_mp4, 6.4, preset=30) == promo_mp4

    def test_unreadable_promo_duration_is_an_error(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file ffprobe cannot read is not a promo, however big it is."""
        promo_mp4 = tmp_run_dir / "promo.mp4"
        promo_mp4.write_bytes(b"\x00" * (11 * 1024))
        monkeypatch.setattr(promo.shutil, "which", lambda name: "/usr/bin/ffprobe")
        monkeypatch.setattr(promo.render, "_mp4_duration_s", lambda path, probe: None)
        with pytest.raises(PromoError, match="could not read duration"):
            validate_promo(promo_mp4, 6.4)

    def test_in_tolerance_passes(
        self, tmp_run_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        promo_mp4 = tmp_run_dir / "promo.mp4"
        promo_mp4.write_bytes(b"\x00" * (11 * 1024))
        monkeypatch.setattr(promo.shutil, "which", lambda name: "/usr/bin/ffprobe")
        monkeypatch.setattr(promo.render, "_mp4_duration_s", lambda path, probe: 28.9)
        assert validate_promo(promo_mp4, 28.8) == promo_mp4

    def test_without_host_ffprobe_only_size_is_checked(self, tmp_run_dir: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
        promo_mp4 = tmp_run_dir / "promo.mp4"
        promo_mp4.write_bytes(b"\x00" * (11 * 1024))
        monkeypatch.setattr(promo.shutil, "which", lambda name: None)
        assert validate_promo(promo_mp4, 6.4) == promo_mp4


# --- the checked-in fixture is the contract-by-example --------------------------


def test_fixture_script_parses_and_composites(fixtures_dir: Path) -> None:
    """The fixture in tests/fixtures/promo_script.json is the #169 contract by
    example: it must stay loadable and compositable as that slice lands."""
    raw = json.loads((fixtures_dir / "promo_script.json").read_text(encoding="utf-8"))
    script = PromoScript.model_validate(raw)
    assert [s.kind for s in script.scenes] == [
        "title_card", "demo_segment", "demo_segment", "end_card",
    ]
    argv = build_promo_argv(script, Path("/run"), Config(), source_duration_s=SOURCE_DURATION)
    assert argv[-1] == "/vhs/promo.mp4"
