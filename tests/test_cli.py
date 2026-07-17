"""Tests for CLI input resolution: repo is optional, -gr flag, guide-only runs.

Pure — no typer runtime, no docker, no network. Exercises the ``_resolve_repo``
helper that reconciles the positional repo, ``-gr/--github-repo``, and the
``-s/--step-by-step`` guide.
"""

from pathlib import Path

import pytest
import typer

from readme2demo.cli import (
    PRESET_MODEL_UNSET,
    _apply_engine_image,
    _apply_provider,
    _normalize_preset_argv,
    _resolve_repo,
    _select_preset,
)
from typer.testing import CliRunner
from readme2demo.cli import app

_URL = "https://github.com/owner/repo"

runner = CliRunner()

def test_positional_repo_only() -> None:
    assert _resolve_repo(_URL, None, None) == _URL


def test_gr_flag_only() -> None:
    assert _resolve_repo(None, _URL, None) == _URL


def test_both_spellings_agree() -> None:
    assert _resolve_repo(_URL, _URL, None) == _URL


def test_conflicting_repo_spellings_raise() -> None:
    with pytest.raises(typer.BadParameter):
        _resolve_repo(_URL, "https://github.com/other/repo", None)


def test_guide_only_returns_none() -> None:
    # No repo, guide supplied -> guide-only run.
    assert _resolve_repo(None, None, Path("guide.md")) is None


def test_neither_repo_nor_guide_raises() -> None:
    with pytest.raises(typer.BadParameter):
        _resolve_repo(None, None, None)


def test_repo_and_guide_both_returns_repo() -> None:
    # "both" case: the repo is returned; the guide flows through cfg.step_by_step
    # separately, so both are taken into account downstream.
    assert _resolve_repo(_URL, None, Path("g.md")) == _URL
    assert _resolve_repo(None, _URL, Path("g.md")) == _URL


# -- provider presets (--gemini / --openai / --anthropic) -------------------------


def test_gemini_preset_sets_openhands_and_backend(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-from-env")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    engine, model, backend = _apply_provider("gemini", None, None, None)
    assert engine == "openhands"
    assert model == "gemini-from-env"  # bare --gemini: model from env, not code
    assert backend == "gemini"
    import os

    # Bridges the Gemini key into the OpenHands engine's litellm env.
    assert os.environ["LLM_API_KEY"] == "gk-test"
    assert os.environ["LLM_MODEL"] == "gemini/gemini-from-env"


def test_openai_preset_sets_openhands_and_backend(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-from-env")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    engine, model, backend = _apply_provider("openai", None, None, None)
    assert engine == "openhands"
    assert model == "gpt-from-env"  # bare --openai: model from env, not code
    assert backend == "openai"
    import os

    assert os.environ["LLM_API_KEY"] == "sk-openai-test"
    assert os.environ["LLM_MODEL"] == "openai/gpt-from-env"


def test_anthropic_preset_sets_openhands_and_api_backend(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    engine, model, backend = _apply_provider("anthropic", None, None, None)
    assert engine == "openhands"
    assert model == "claude-sonnet-5"  # bare --anthropic: the config default is fine
    assert backend == "api"  # passes ride the existing Anthropic SDK backend
    import os

    assert os.environ["LLM_API_KEY"] == "sk-ant-test"
    assert os.environ["LLM_MODEL"] == "anthropic/claude-sonnet-5"


def test_gemini_preset_value_names_the_model(monkeypatch):
    # `--gemini <model>` wins over the GEMINI_MODEL env var.
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-from-env")
    engine, model, backend = _apply_provider(
        "gemini", None, None, None, "gemini-3-flash-preview"
    )
    assert (engine, model, backend) == ("openhands", "gemini-3-flash-preview", "gemini")


def test_gemini_preset_model_flag_names_the_model(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    engine, model, backend = _apply_provider("gemini", None, "gemini-3.5-flash", None)
    assert (engine, model, backend) == ("openhands", "gemini-3.5-flash", "gemini")


def test_gemini_preset_no_model_anywhere_raises(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    with pytest.raises(typer.BadParameter, match="No Gemini model specified"):
        _apply_provider("gemini", None, None, None)


def test_openai_preset_no_model_anywhere_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    with pytest.raises(typer.BadParameter, match="No OpenAI model specified"):
        _apply_provider("openai", None, None, None)


def test_gemini_preset_conflicting_models_raise(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    with pytest.raises(typer.BadParameter, match="given twice"):
        _apply_provider(
            "gemini", None, "gemini-3.5-flash", None, "gemini-3-flash-preview"
        )


def test_openai_preset_conflicting_models_raise(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    with pytest.raises(typer.BadParameter, match="given twice"):
        _apply_provider("openai", None, "gpt-5.1", None, "gpt-5-mini")


def test_gemini_preset_agreeing_models_ok(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    _, model, _ = _apply_provider(
        "gemini", None, "gemini-3.5-flash", None, "gemini-3.5-flash"
    )
    assert model == "gemini-3.5-flash"


def test_gemini_preset_allows_explicit_openhands(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    engine, _, _ = _apply_provider("gemini", "openhands", "gemini-3.5-flash", "gemini")
    assert engine == "openhands"


def test_gemini_conflicts_with_llm_backend(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    with pytest.raises(typer.BadParameter):
        _apply_provider("gemini", None, "gemini-3.5-flash", "api")


def test_openai_conflicts_with_llm_backend(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    with pytest.raises(typer.BadParameter):
        _apply_provider("openai", None, "gpt-5.1", "gemini")


def test_gemini_conflicts_with_engine(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    with pytest.raises(typer.BadParameter):
        _apply_provider("gemini", "claude-code", "gemini-3.5-flash", None)


def test_select_preset_none_given():
    assert _select_preset(None, None, None) is None


def test_select_preset_unwraps_sentinel():
    assert _select_preset(PRESET_MODEL_UNSET, None, None) == ("gemini", None)
    assert _select_preset(None, "gpt-5.1", None) == ("openai", "gpt-5.1")
    assert _select_preset(None, None, PRESET_MODEL_UNSET) == ("anthropic", None)


def test_select_preset_mutually_exclusive():
    with pytest.raises(typer.BadParameter, match="mutually exclusive"):
        _select_preset("gemini-3.5-flash", "gpt-5.1", None)
    with pytest.raises(typer.BadParameter, match="mutually exclusive"):
        _select_preset(PRESET_MODEL_UNSET, None, PRESET_MODEL_UNSET)


# -- preset optional-value argv normalization ---------------------------------------
# typer drops click's flag_value support, so cli._normalize_preset_argv rewrites
# argv before parsing (applied in the app's __call__; CliRunner bypasses it).


def test_normalize_bare_gemini_at_end():
    assert _normalize_preset_argv(["run", "url", "--gemini"]) == [
        "run", "url", "--gemini", PRESET_MODEL_UNSET,
    ]


def test_normalize_bare_preset_before_positional():
    # Must NOT let a bare preset flag swallow the repo URL.
    for flag in ("--gemini", "--openai", "--anthropic"):
        assert _normalize_preset_argv(["run", flag, "https://github.com/x/y"]) == [
            "run", flag, PRESET_MODEL_UNSET, "https://github.com/x/y",
        ]


def test_normalize_bare_preset_before_flag():
    for flag in ("--gemini", "--openai", "--anthropic"):
        assert _normalize_preset_argv(["run", "u", flag, "--skip-video"]) == [
            "run", "u", flag, PRESET_MODEL_UNSET, "--skip-video",
        ]


@pytest.mark.parametrize(
    "flag, model",
    [
        ("--gemini", "gemini-3-flash-preview"),
        ("--openai", "gpt-5.1"),
        ("--openai", "o4-mini"),
        ("--openai", "chatgpt-4o-latest"),
        ("--anthropic", "claude-sonnet-5"),
    ],
)
def test_normalize_preset_with_model_untouched(flag, model):
    args = ["run", "u", flag, model]
    assert _normalize_preset_argv(args) == args


def test_normalize_preset_equals_form_untouched():
    # Escape hatch for model names outside the provider's known prefixes.
    for arg in ("--gemini=my-tuned-model", "--openai=my-ft-model"):
        args = ["run", "u", arg]
        assert _normalize_preset_argv(args) == args


def test_normalize_without_preset_is_identity():
    args = ["run", "https://github.com/x/y", "--skip-video"]
    assert _normalize_preset_argv(args) == args


# -- engine default sandbox image ---------------------------------------------------


def test_openhands_engine_gets_its_own_image():
    from readme2demo.config import Config

    cfg = _apply_engine_image(Config(engine="openhands"))
    assert cfg.base_image == "readme2demo/openhands:latest"


def test_explicit_base_image_wins_over_engine_default():
    from readme2demo.config import Config

    cfg = _apply_engine_image(Config(engine="openhands", base_image="my/img:1"))
    assert cfg.base_image == "my/img:1"


def test_claude_engine_keeps_standard_base_image():
    from readme2demo.config import Config

    cfg = _apply_engine_image(Config(engine="claude-code"))
    assert cfg.base_image == "readme2demo/base:latest"


def test_unknown_engine_passes_through():
    from readme2demo.config import Config

    cfg = _apply_engine_image(Config(engine="not-a-real-engine"))
    assert cfg.base_image == "readme2demo/base:latest"  # preflight reports the engine


def test_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() != ""


def test_resume_rejects_missing_run_dir(tmp_path):
    missing = tmp_path / "missing-run"
    result = runner.invoke(app, ["resume", str(missing)])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_resume_rejects_file_run_dir(tmp_path):
    file_path = tmp_path / "not-a-run-dir"
    file_path.write_text("not a directory")
    result = runner.invoke(app, ["resume", str(file_path)])
    assert result.exit_code != 0
    assert "directory" in result.output.lower()


# -- regression: run glow-20260710-162012 (missing SDK + Rich-eaten hint) -----------


def test_regression_missing_openai_sdk_fails_preflight(monkeypatch, capsys):
    """Regression (run glow-20260710-162012): --openai without the openai
    package passed preflight and died in ingest — after a run dir was already
    created. Preflight must catch the missing SDK first, and the printed hint
    must keep its literal '[openai]' extra (Rich once parsed it as markup and
    rendered the useless `pip install 'readme2demo'`).
    """
    import shutil as shutil_mod
    import sys

    from readme2demo import cli as cli_mod
    from readme2demo import llm
    from readme2demo.config import Config

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-abcdefghijklmnop")
    monkeypatch.setitem(sys.modules, "openai", None)  # import -> ImportError
    monkeypatch.setattr(shutil_mod, "which", lambda _: "/usr/bin/docker")
    try:
        with pytest.raises(typer.Exit):
            cli_mod._preflight(Config(llm_backend="openai"))
    finally:
        llm.set_backend("auto")  # _preflight sets module state; reset it
    assert "readme2demo[openai]" in capsys.readouterr().out


def test_preflight_rejects_unknown_backend_cleanly(monkeypatch, capsys):
    # A bad llm_backend in readme2demo.toml must be a clean preflight ✗,
    # not an unhandled LLMError traceback (set_backend sits inside the try).
    import shutil as shutil_mod

    from readme2demo import cli as cli_mod
    from readme2demo.config import Config

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-abcdefghijklmnop")
    monkeypatch.setattr(shutil_mod, "which", lambda _: "/usr/bin/docker")
    with pytest.raises(typer.Exit):
        cli_mod._preflight(Config(llm_backend="gpt"))
    assert "Unknown LLM backend" in capsys.readouterr().out


def test_bare_llm_backend_gemini_without_model_fails_preflight(monkeypatch, capsys):
    # --llm-backend gemini without the --gemini preset resolves no model; that
    # must surface at preflight, not after the run dir exists.
    import shutil as shutil_mod
    import sys
    import types as pytypes

    from readme2demo import cli as cli_mod
    from readme2demo import llm
    from readme2demo.config import Config

    google = pytypes.ModuleType("google")
    genai = pytypes.ModuleType("google.genai")
    genai.Client = object  # importable, compatible — isolates the model gate
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-abcdefghijklmnop")
    monkeypatch.setattr(shutil_mod, "which", lambda _: "/usr/bin/docker")
    try:
        with pytest.raises(typer.Exit):
            cli_mod._preflight(Config(llm_backend="gemini"))
    finally:
        llm.set_backend("auto")
    assert "No Gemini model specified" in capsys.readouterr().out


def test_regression_report_keeps_bracketed_error_text(tmp_path):
    """Regression (run glow-20260710-162012): the run summary showed
    `pip install 'readme2demo'` because Rich swallowed the [openai] tag from
    the stage error. summarize output must be escaped before console.print.
    """
    import json

    manifest_data = {
        "run_id": "glow-20260710-162012-33fc72",
        "stages": {
            "ingest": {
                "status": "failed",
                "error": "pip install 'readme2demo[openai]'",
            }
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest_data))
    result = runner.invoke(app, ["report", str(tmp_path)])
    assert result.exit_code == 0
    assert "readme2demo[openai]" in result.output

def test_regression_report_json_with_recorded_stages(tmp_path):
    """Regression: `report --json` crashed with AttributeError on any manifest
    that had recorded stages (#29 iterated the stages dict as a list of
    objects), and read nonexistent `cost`/`commit` fields so cost was always
    0.0 and commit always null. Must emit one entry per stage plus the real
    total_cost_usd / commit_sha values. (The old test used empty stages, so
    CI stayed green through the breakage.)
    """
    import json

    manifest_data = {
        "run_id": "test-run-123",
        "verified": True,
        "total_cost_usd": 1.5,
        "commit_sha": "abcdef123456",
        "stages": {
            "ingest": {"status": "completed"},
            "agent": {"status": "failed"},
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest_data))

    result = runner.invoke(app, ["report", str(tmp_path), "--json"])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["verified"] is True
    assert parsed["cost"] == 1.5
    assert parsed["commit"] == "abcdef123456"
    assert {"name": "ingest", "status": "completed"} in parsed["stages"]
    assert {"name": "agent", "status": "failed"} in parsed["stages"]
    
