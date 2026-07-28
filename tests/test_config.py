"""Unit tests for Config.load — CLI flags > readme2demo.toml > defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from readme2demo.config import Config


def _write_toml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- defaults -----------------------------------------------------------------


class TestDefaults:
    def test_defaults_without_any_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)  # no implicit readme2demo.toml in cwd
        cfg = Config.load()
        assert cfg.engine == "claude-code"
        assert cfg.llm_backend == "auto"
        assert cfg.max_turns == 60
        assert cfg.base_image == "readme2demo/base:latest"
        assert cfg.network == "bridge"
        assert cfg.allow_docker_socket is False  # security tradeoff stays opt-in
        assert cfg.memory == "4g"
        assert cfg.cpus == "2"
        assert cfg.pids_limit == 512
        assert cfg.dry_run is False
        assert cfg.verify_timeout_s == 900
        assert cfg.verify_retries == 1
        assert cfg.distill_retries == 1
        assert cfg.skip_video is False
        assert cfg.step_by_step is None
        assert cfg.runs_dir == Path("runs")
        # brand kit: all optional, with a documented hex accent default
        assert cfg.brand_logo is None
        assert cfg.brand_color == "#7C6BF2"
        assert cfg.brand_font is None

    def test_implicit_toml_in_cwd_is_picked_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _write_toml(tmp_path / "readme2demo.toml", "max_turns = 7\n")
        assert Config.load().max_turns == 7


# --- toml parsing -------------------------------------------------------------


class TestTomlParsing:
    def test_values_parsed_and_coerced(self, tmp_path: Path) -> None:
        toml = _write_toml(
            tmp_path / "r2d.toml",
            'engine = "openhands"\n'
            "max_turns = 30\n"
            "budget_usd = 2.5\n"
            "allow_docker_socket = true\n"
            'memory = "8g"\n'
            'runs_dir = "custom-runs"\n',
        )
        cfg = Config.load(toml)
        assert cfg.engine == "openhands"
        assert cfg.max_turns == 30
        assert cfg.budget_usd == 2.5
        assert cfg.allow_docker_socket is True
        assert cfg.memory == "8g"
        assert cfg.runs_dir == Path("custom-runs")  # str coerced to Path

    def test_step_by_step_string_coerced_to_path(self, tmp_path: Path) -> None:
        toml = _write_toml(tmp_path / "r2d.toml", 'step_by_step = "docs/guide.md"\n')
        assert Config.load(toml).step_by_step == Path("docs/guide.md")

    def test_explicit_toml_path_missing_raises(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.toml"
        with pytest.raises(FileNotFoundError, match="nope.toml"):
            Config.load(missing)

    def test_wrongly_typed_toml_value_raises(self, tmp_path: Path) -> None:
        toml = _write_toml(tmp_path / "r2d.toml", 'max_turns = "lots"\n')
        with pytest.raises(ValidationError):
            Config.load(toml)


# --- precedence: flags > toml > defaults ---------------------------------------


class TestPrecedence:
    def test_flag_beats_toml(self, tmp_path: Path) -> None:
        toml = _write_toml(tmp_path / "r2d.toml", "max_turns = 30\n")
        assert Config.load(toml, max_turns=10).max_turns == 10

    def test_none_flag_does_not_clobber_toml(self, tmp_path: Path) -> None:
        """CLI flags the user did not pass arrive as None and must fall
        through to the TOML value, not overwrite it."""
        toml = _write_toml(tmp_path / "r2d.toml", "max_turns = 30\n")
        assert Config.load(toml, max_turns=None).max_turns == 30

    def test_falsy_but_not_none_flag_still_beats_toml(self, tmp_path: Path) -> None:
        """Only None means 'flag not passed': explicit False/0 must win."""
        toml = _write_toml(
            tmp_path / "r2d.toml", "dry_run = true\nverify_retries = 5\n"
        )
        cfg = Config.load(toml, dry_run=False, verify_retries=0)
        assert cfg.dry_run is False
        assert cfg.verify_retries == 0

    def test_toml_beats_default(self, tmp_path: Path) -> None:
        toml = _write_toml(tmp_path / "r2d.toml", 'network = "none"\n')
        assert Config.load(toml).network == "none"

    def test_flag_beats_default_without_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        assert Config.load(max_turns=3).max_turns == 3


# --- unknown keys ---------------------------------------------------------------


class TestUnknownKeys:
    def test_stale_vhs_image_is_accepted_with_deprecation_warning(
        self, tmp_path: Path
    ) -> None:
        toml = _write_toml(tmp_path / "r2d.toml", 'vhs_image = "old/image:tag"\n')
        with pytest.warns(DeprecationWarning, match="vhs_image.*deprecated"):
            cfg = Config.load(toml)
        assert cfg.base_image == "readme2demo/base:latest"
        assert "vhs_image" not in cfg.model_dump()

    def test_unknown_toml_key_raises(self, tmp_path: Path) -> None:
        toml = _write_toml(tmp_path / "r2d.toml", 'does_not_exist = "x"\nmax_turns = 5\n')
        with pytest.raises(ValidationError, match="does_not_exist"):
            Config.load(toml)

    def test_typoed_toml_key_raises_and_names_bad_key(self, tmp_path: Path) -> None:
        toml = _write_toml(tmp_path / "r2d.toml", "max_turn = 99\n")
        with pytest.raises(ValidationError, match="max_turn"):
            Config.load(toml)

    def test_unknown_override_kwarg_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValidationError, match="totally_unknown"):
            Config.load(totally_unknown="x")


# --- brand kit (#173) ----------------------------------------------------------


class TestBrandColor:
    def test_default_brand_color_passes_its_own_validator(self) -> None:
        """Regression (#173): the documented brand_color default must itself
        satisfy the hex-color validator, or every command would fail at load."""
        assert Config().brand_color == "#7C6BF2"

    @pytest.mark.parametrize("value", ["#7C6BF2", "#000000", "#ffffff", "#AbCdEf"])
    def test_valid_hex_accepted(self, value: str) -> None:
        """Regression (#173): canonical #RRGGBB (any case) is accepted."""
        assert Config(brand_color=value).brand_color == value

    @pytest.mark.parametrize(
        "value",
        [
            "red",  # named color, no '#'
            "7C6BF2",  # missing leading '#'
            "#12345",  # only five digits
            "#1234567",  # seven digits
            "#GGGGGG",  # non-hex digits
            "#7c6bf",  # short by one
            "",  # empty
        ],
    )
    def test_invalid_hex_rejected(self, value: str) -> None:
        """Regression (#173): non-#RRGGBB strings fail fast at Config build."""
        with pytest.raises(ValidationError, match="brand_color"):
            Config(brand_color=value)

    def test_short_rgb_form_is_rejected(self) -> None:
        """Regression (#173): #RGB is deliberately NOT accepted — ffmpeg's
        color parser wants #RRGGBB, so we reject the short form on the host
        instead of deferring the failure into the render container."""
        with pytest.raises(ValidationError, match="brand_color"):
            Config(brand_color="#abc")


class TestBrandLogo:
    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        """Regression (#173): a set-but-nonexistent brand_logo fails fast at
        Config.load, not later inside someone's render container."""
        missing = tmp_path / "nope.png"
        with pytest.raises(ValidationError, match="not found"):
            Config(brand_logo=missing)

    def test_svg_rejected_as_non_raster(self, tmp_path: Path) -> None:
        """Regression (#173): .svg (and any non-raster suffix) is rejected
        because typical ffmpeg builds cannot rasterize vector formats."""
        svg = tmp_path / "logo.svg"
        svg.write_text("<svg/>", encoding="utf-8")
        with pytest.raises(ValidationError, match="raster"):
            Config(brand_logo=svg)

    @pytest.mark.parametrize("suffix", [".png", ".jpg", ".jpeg", ".JPG", ".PNG"])
    def test_raster_suffixes_accepted(self, tmp_path: Path, suffix: str) -> None:
        """Regression (#173): existing .png/.jpg/.jpeg files (case-insensitive)
        are accepted and coerced to Path."""
        logo = tmp_path / f"logo{suffix}"
        logo.write_bytes(b"\x89PNG\r\n")
        cfg = Config(brand_logo=str(logo))
        assert cfg.brand_logo == logo
        assert isinstance(cfg.brand_logo, Path)

    def test_unset_is_fine(self) -> None:
        assert Config().brand_logo is None


class TestBrandFont:
    def test_valid_name_accepted(self) -> None:
        assert Config(brand_font="DejaVu Sans").brand_font == "DejaVu Sans"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_or_whitespace_rejected(self, value: str) -> None:
        """Regression (#173): brand_font, if present, must be a real name;
        emptiness is the only thing the host can meaningfully check (font
        availability resolves where ffmpeg runs)."""
        with pytest.raises(ValidationError, match="brand_font"):
            Config(brand_font=value)

    def test_unset_is_fine(self) -> None:
        assert Config().brand_font is None


class TestBrandKitFromToml:
    def test_all_three_fields_load_from_toml(self, tmp_path: Path) -> None:
        """Regression (#173): brand_logo/brand_color/brand_font all round-trip
        through Config.load from readme2demo.toml (str -> Path coercion for the
        logo, like step_by_step)."""
        logo = tmp_path / "brand.png"
        logo.write_bytes(b"\x89PNG\r\n")
        toml = _write_toml(
            tmp_path / "r2d.toml",
            f'brand_logo = "{logo}"\n'
            'brand_color = "#123456"\n'
            'brand_font = "Inter"\n',
        )
        cfg = Config.load(toml)
        assert cfg.brand_logo == logo
        assert cfg.brand_color == "#123456"
        assert cfg.brand_font == "Inter"

    def test_bad_brand_color_in_toml_raises_at_load(self, tmp_path: Path) -> None:
        """Regression (#173): validators fire during Config.load, so a broken
        brand_color in the toml is caught before any agent cost is incurred."""
        toml = _write_toml(tmp_path / "r2d.toml", 'brand_color = "purple"\n')
        with pytest.raises(ValidationError, match="brand_color"):
            Config.load(toml)
