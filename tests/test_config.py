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
