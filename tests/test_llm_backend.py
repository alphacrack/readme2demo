"""Tests for LLM backend selection and the claude-cli (subscription) backend."""

import json
import subprocess

import pytest

from readme2demo import llm
from readme2demo.engines.base import EngineError
from readme2demo.engines.claude_code import ClaudeCodeEngine
from readme2demo.llm import LLMError


@pytest.fixture(autouse=True)
def reset_backend():
    llm.set_backend("auto")
    yield
    llm.set_backend("auto")


# -- resolve_backend ------------------------------------------------------------


def test_auto_prefers_api_when_key_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert llm.resolve_backend() == "api"


def test_auto_falls_back_to_cli(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: "/usr/local/bin/claude")
    assert llm.resolve_backend() == "claude-cli"


def test_auto_raises_when_nothing_available(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: None)
    with pytest.raises(LLMError, match="No LLM backend available"):
        llm.resolve_backend()


def test_explicit_backend_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    llm.set_backend("claude-cli")
    assert llm.resolve_backend() == "claude-cli"


def test_set_backend_rejects_unknown():
    with pytest.raises(LLMError, match="Unknown LLM backend"):
        llm.set_backend("gpt")


def test_resolve_backend_api_requires_key(monkeypatch):
    # Explicit --llm-backend api (or --anthropic) without a key must fail in
    # preflight, not at the first LLM call mid-run.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm.set_backend("api")
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY is not set"):
        llm.resolve_backend()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert llm.resolve_backend() == "api"


# -- gemini backend ---------------------------------------------------------------


def test_set_backend_accepts_gemini():
    llm.set_backend("gemini")  # must not raise


def test_resolve_backend_gemini_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    llm.set_backend("gemini")
    with pytest.raises(LLMError, match="GEMINI_API_KEY is not set"):
        llm.resolve_backend()
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    assert llm.resolve_backend() == "gemini"


def test_auto_never_picks_gemini(monkeypatch):
    # GEMINI_API_KEY alone must not flip `auto` to gemini — it's opt-in only.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: None)
    with pytest.raises(LLMError, match="No LLM backend available"):
        llm.resolve_backend()


@pytest.mark.parametrize(
    "given, expected",
    [
        ("gemini-3.5-flash", "gemini-3.5-flash"),
        ("gemini-3-flash-preview", "gemini-3-flash-preview"),
        ("gemini/gemini-3.1-pro-preview", "gemini-3.1-pro-preview"),  # litellm prefix stripped
        ("my-tuned-model", "my-tuned-model"),  # explicit non-gemini name passes through
    ],
)
def test_gemini_model_explicit_passthrough(given, expected, monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert llm._provider_model(llm.PROVIDERS["gemini"], given) == expected


@pytest.mark.parametrize("given", ["", None, "claude-sonnet-5"])
def test_gemini_model_env_fallback(given, monkeypatch):
    # Empty / the repo's Claude config default count as "not specified".
    monkeypatch.setenv("GEMINI_MODEL", "gemini-from-env")
    assert llm._provider_model(llm.PROVIDERS["gemini"], given) == "gemini-from-env"


@pytest.mark.parametrize("given", ["", None, "claude-sonnet-5"])
def test_gemini_model_unspecified_raises(given, monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    with pytest.raises(LLMError, match="No Gemini model specified"):
        llm._provider_model(llm.PROVIDERS["gemini"], given)


@pytest.mark.parametrize(
    "given, expected",
    [
        ("gpt-5.1", "gpt-5.1"),
        ("openai/gpt-5-mini", "gpt-5-mini"),  # litellm prefix stripped
        ("my-ft-model", "my-ft-model"),  # explicit name passes through
    ],
)
def test_openai_model_explicit_passthrough(given, expected, monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert llm._provider_model(llm.PROVIDERS["openai"], given) == expected


@pytest.mark.parametrize("given", ["", None, "claude-sonnet-5"])
def test_openai_model_env_fallback_and_unspecified(given, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-from-env")
    assert llm._provider_model(llm.PROVIDERS["openai"], given) == "gpt-from-env"
    monkeypatch.delenv("OPENAI_MODEL")
    with pytest.raises(LLMError, match="No OpenAI model specified"):
        llm._provider_model(llm.PROVIDERS["openai"], given)


def test_anthropic_model_claude_name_is_valid(monkeypatch):
    # For Anthropic, claude* is NOT a config-default leak — it's the model.
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-from-env")
    spec = llm.PROVIDERS["anthropic"]
    assert llm._provider_model(spec, "claude-opus-4-8") == "claude-opus-4-8"
    assert llm._provider_model(spec, "anthropic/claude-opus-4-8") == "claude-opus-4-8"
    assert llm._provider_model(spec, None) == "claude-from-env"
    monkeypatch.delenv("ANTHROPIC_MODEL")
    # Unlike Gemini/OpenAI, the repo-wide config default is an OK fallback.
    assert llm._provider_model(spec, None) == "claude-sonnet-5"


def test_apply_provider_session_bridges_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-secret")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-from-env")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    resolved = llm.apply_provider_session("gemini", None)
    assert resolved == "gemini-from-env"
    import os

    assert os.environ["LLM_API_KEY"] == "gk-secret"
    assert os.environ["LLM_MODEL"] == "gemini/gemini-from-env"


def test_apply_provider_session_openai_bridges_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    resolved = llm.apply_provider_session("openai", "gpt-5.1")
    assert resolved == "gpt-5.1"
    import os

    assert os.environ["LLM_API_KEY"] == "sk-openai-secret"
    assert os.environ["LLM_MODEL"] == "openai/gpt-5.1"


def test_apply_provider_session_anthropic_bridges_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    resolved = llm.apply_provider_session("anthropic", None)
    assert resolved == "claude-sonnet-5"
    import os

    assert os.environ["LLM_API_KEY"] == "sk-ant-secret"
    assert os.environ["LLM_MODEL"] == "anthropic/claude-sonnet-5"


def test_apply_provider_session_explicit_model_beats_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-secret")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-from-env")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    resolved = llm.apply_provider_session("gemini", "gemini-3-flash-preview")
    assert resolved == "gemini-3-flash-preview"
    import os

    assert os.environ["LLM_MODEL"] == "gemini/gemini-3-flash-preview"


def test_apply_provider_session_no_model_anywhere_raises(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-secret")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    with pytest.raises(LLMError, match="No Gemini model specified"):
        llm.apply_provider_session("gemini", None)


def test_apply_provider_session_respects_preset_openhands_env(monkeypatch):
    # A user who set LLM_API_KEY / LLM_MODEL by hand keeps them (setdefault).
    monkeypatch.setenv("GEMINI_API_KEY", "gk-secret")
    monkeypatch.setenv("LLM_API_KEY", "my-own-key")
    monkeypatch.setenv("LLM_MODEL", "gemini/gemini-1.5-flash")
    llm.apply_provider_session("gemini", "gemini-3.5-flash")
    import os

    assert os.environ["LLM_API_KEY"] == "my-own-key"
    assert os.environ["LLM_MODEL"] == "gemini/gemini-1.5-flash"


def _install_fake_genai(monkeypatch, captured):
    """Register a stand-in google.genai in sys.modules (SDK isn't installed)."""
    import sys
    import types as pytypes

    class FakeUsage:
        prompt_token_count = 120
        candidates_token_count = 40

    class FakeResp:
        text = '{"feasible": true}'
        usage_metadata = FakeUsage()

    class FakeModels:
        def generate_content(self, model, contents, config):
            captured["model"] = model
            captured["contents"] = contents
            captured["config"] = config
            return FakeResp()

    class FakeClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key
            self.models = FakeModels()

    class FakeGenConfig:
        def __init__(self, system_instruction=None, max_output_tokens=None):
            self.system_instruction = system_instruction
            self.max_output_tokens = max_output_tokens

    google_mod = pytypes.ModuleType("google")
    genai_mod = pytypes.ModuleType("google.genai")
    genai_types_mod = pytypes.ModuleType("google.genai.types")
    genai_mod.Client = FakeClient
    genai_types_mod.GenerateContentConfig = FakeGenConfig
    genai_mod.types = genai_types_mod
    google_mod.genai = genai_mod
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", genai_types_mod)


def test_gemini_backend_calls_sdk(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    captured: dict = {}
    _install_fake_genai(monkeypatch, captured)

    monkeypatch.setenv("GEMINI_MODEL", "gemini-from-env")
    resp = llm._complete_gemini("be a planner", "plan this", "claude-sonnet-5", 4096)
    assert resp.text == '{"feasible": true}'
    assert captured["api_key"] == "gk-test"
    assert captured["model"] == "gemini-from-env"  # claude config name -> env var
    assert captured["contents"] == "plan this"
    assert captured["config"].system_instruction == "be a planner"
    assert captured["config"].max_output_tokens == 4096
    assert resp.cost_usd > 0  # priced from usage metadata


def test_gemini_backend_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    captured: dict = {}
    _install_fake_genai(monkeypatch, captured)
    with pytest.raises(LLMError, match="GEMINI_API_KEY is not set"):
        llm._complete_gemini("s", "u", "gemini-3.5-flash", 100)


def test_complete_dispatches_to_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-test")
    llm.set_backend("gemini")
    monkeypatch.setattr(
        "readme2demo.llm._complete_gemini",
        lambda system, user, model, max_tokens: llm.LLMResponse(text="hi", cost_usd=0.001),
    )
    resp = llm.complete("s", "u", "gemini-3.5-flash")
    assert resp.text == "hi"


def test_regression_retired_gemini_default_404_in_ingest(monkeypatch):
    """Regression: a --gemini run died in ingest with Google's 404
    "This model models/gemini-2.5-flash is no longer available" because a
    default model name was hardcoded and went stale. There is NO baked-in
    Gemini (or OpenAI) model any more: the name must come from the user
    (--gemini/--model) or the provider's model env var, and its absence is a
    loud, actionable error — never a silent guess at a name the provider may
    retire.
    """
    assert not hasattr(llm, "GEMINI_DEFAULT_MODEL")
    assert llm.PROVIDERS["gemini"].default_model is None
    assert llm.PROVIDERS["openai"].default_model is None
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    with pytest.raises(LLMError, match="GEMINI_MODEL"):
        llm._provider_model(llm.PROVIDERS["gemini"], "")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-99-flash")
    assert llm._provider_model(llm.PROVIDERS["gemini"], "") == "gemini-99-flash"


def test_anthropic_default_model_matches_config():
    # The only hardcoded preset fallback is Anthropic's, and it must stay in
    # lockstep with the repo-wide config default.
    from readme2demo.config import Config

    assert llm.PROVIDERS["anthropic"].default_model == Config().model


# -- openai backend -----------------------------------------------------------------


def test_set_backend_accepts_openai():
    llm.set_backend("openai")  # must not raise


def test_resolve_backend_openai_requires_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm.set_backend("openai")
    with pytest.raises(LLMError, match="OPENAI_API_KEY is not set"):
        llm.resolve_backend()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    assert llm.resolve_backend() == "openai"


def test_auto_never_picks_openai(monkeypatch):
    # OPENAI_API_KEY alone must not flip `auto` to openai — it's opt-in only.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: None)
    with pytest.raises(LLMError, match="No LLM backend available"):
        llm.resolve_backend()


def _install_fake_openai(monkeypatch, captured):
    """Register a stand-in openai module in sys.modules (SDK isn't installed)."""
    import sys
    import types as pytypes

    class FakeUsage:
        prompt_tokens = 120
        completion_tokens = 40

    class FakeMessage:
        content = '{"feasible": true}'

    class FakeChoice:
        message = FakeMessage()

    class FakeResp:
        choices = [FakeChoice()]
        usage = FakeUsage()

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return FakeResp()

    class FakeChat:
        completions = FakeCompletions()

    class FakeOpenAI:
        def __init__(self, api_key):
            captured["api_key"] = api_key
            self.chat = FakeChat()

    openai_mod = pytypes.ModuleType("openai")
    openai_mod.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", openai_mod)


def test_openai_backend_calls_sdk(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    captured: dict = {}
    _install_fake_openai(monkeypatch, captured)

    monkeypatch.setenv("OPENAI_MODEL", "gpt-from-env")
    resp = llm._complete_openai("be a planner", "plan this", "claude-sonnet-5", 4096)
    assert resp.text == '{"feasible": true}'
    assert captured["api_key"] == "sk-openai-test"
    assert captured["model"] == "gpt-from-env"  # claude config name -> env var
    # max_completion_tokens, not the deprecated max_tokens (reasoning models
    # reject the latter).
    assert captured["max_completion_tokens"] == 4096
    assert "max_tokens" not in captured
    assert captured["messages"][0] == {"role": "system", "content": "be a planner"}
    assert captured["messages"][1] == {"role": "user", "content": "plan this"}
    assert resp.cost_usd > 0  # priced from usage


def test_openai_backend_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    captured: dict = {}
    _install_fake_openai(monkeypatch, captured)
    with pytest.raises(LLMError, match="OPENAI_API_KEY is not set"):
        llm._complete_openai("s", "u", "gpt-5.1", 100)


def test_regression_openai_sdk_missing_is_loud_and_actionable(monkeypatch):
    """Regression (run glow-20260710-162012): --openai without the openai
    package installed sailed through preflight and burned the ingest stage on
    an ImportError. check_sdk is the preflight gate: it must raise the
    actionable install hint for backends whose SDK is an optional extra, and
    stay a no-op for backends whose SDK is a core dependency.
    """
    import sys

    monkeypatch.setitem(sys.modules, "openai", None)  # import -> ImportError
    with pytest.raises(LLMError, match=r"readme2demo\[openai\]"):
        llm.check_sdk("openai")
    monkeypatch.setitem(sys.modules, "google", None)
    monkeypatch.setitem(sys.modules, "google.genai", None)
    with pytest.raises(LLMError, match=r"readme2demo\[gemini\]"):
        llm.check_sdk("gemini")
    for backend in ("api", "claude-cli", "auto"):
        llm.check_sdk(backend)  # no optional SDK — must not raise


def test_openai_backend_missing_sdk_same_message(monkeypatch):
    # The call-time path raises the same single-source message as preflight —
    # including the actionable extra hint, not just the first clause.
    import sys

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setitem(sys.modules, "openai", None)
    with pytest.raises(
        LLMError, match=r"openai is not installed.*readme2demo\[openai\]"
    ):
        llm._complete_openai("s", "u", "gpt-5.1", 100)


def test_check_sdk_detects_too_old_sdk(monkeypatch):
    # An openai<1.0 install has no OpenAI client class: importable, unusable.
    # check_sdk must say "upgrade", not pass and die later on a from-import.
    import sys
    import types as pytypes

    monkeypatch.setitem(sys.modules, "openai", pytypes.ModuleType("openai"))
    with pytest.raises(LLMError, match="too old.*pip install -U"):
        llm.check_sdk("openai")


def test_check_sdk_reports_broken_import_distinctly(monkeypatch):
    # An installed SDK whose import dies on a transitive dep (e.g. httpx) is
    # NOT "not installed" — the real error must be quoted, not replaced by a
    # misleading install hint.
    def boom(module):
        raise ModuleNotFoundError("No module named 'httpx'", name="httpx")

    monkeypatch.setattr("readme2demo.llm.importlib.import_module", boom)
    with pytest.raises(LLMError, match="failed to import.*httpx"):
        llm.check_sdk("openai")


def test_check_model_gates_provider_backends(monkeypatch):
    # A provider backend with no model named anywhere must fail preflight,
    # not burn the run dir in ingest ("No Gemini model specified").
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    with pytest.raises(LLMError, match="No Gemini model specified"):
        llm.check_model("gemini", "claude-sonnet-5")  # config default leak
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")
    llm.check_model("gemini", "claude-sonnet-5")  # env names it — ok
    # Backends that resolve through the config default are no-ops.
    llm.check_model("api", "claude-sonnet-5")
    llm.check_model("claude-cli", None)
    llm.check_model("auto", None)


def test_complete_dispatches_to_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    llm.set_backend("openai")
    monkeypatch.setattr(
        "readme2demo.llm._complete_openai",
        lambda system, user, model, max_tokens: llm.LLMResponse(text="hi", cost_usd=0.001),
    )
    resp = llm.complete("s", "u", "gpt-5.1")
    assert resp.text == "hi"


# -- claude-cli backend -----------------------------------------------------------


def _fake_cli_envelope(result_text: str, cost: float = 0.0123, is_error: bool = False):
    return json.dumps(
        {"type": "result", "result": result_text, "total_cost_usd": cost,
         "is_error": is_error, "num_turns": 1}
    )


def test_cli_backend_parses_envelope(monkeypatch):
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: "/bin/claude")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input", "")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_fake_cli_envelope('{"feasible": true}'), stderr=""
        )

    monkeypatch.setattr("readme2demo.llm.subprocess.run", fake_run)
    resp = llm._complete_cli("be a planner", "plan this", "claude-sonnet-5")
    assert resp.text == '{"feasible": true}'
    assert resp.cost_usd == 0.0123
    assert captured["cmd"][:4] == ["claude", "-p", "--output-format", "json"]
    assert "--model" in captured["cmd"]
    assert "be a planner" in captured["input"]  # system prompt travels via stdin
    assert "plan this" in captured["input"]


def test_cli_backend_error_envelope(monkeypatch):
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: "/bin/claude")
    monkeypatch.setattr(
        "readme2demo.llm.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=_fake_cli_envelope("rate limited", is_error=True), stderr=""
        ),
    )
    with pytest.raises(LLMError, match="reported an error"):
        llm._complete_cli("s", "u", "m")


def test_cli_backend_nonzero_exit(monkeypatch):
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: "/bin/claude")
    monkeypatch.setattr(
        "readme2demo.llm.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
    )
    with pytest.raises(LLMError, match="failed"):
        llm._complete_cli("s", "u", "m")


def test_cli_backend_missing_binary(monkeypatch):
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: None)
    with pytest.raises(LLMError, match="claude CLI not found"):
        llm._complete_cli("s", "u", "m")


def test_complete_dispatches_to_cli(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: "/bin/claude")
    monkeypatch.setattr(
        "readme2demo.llm.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout=_fake_cli_envelope("hello"), stderr=""
        ),
    )
    resp = llm.complete("s", "u", "claude-sonnet-5")
    assert resp.text == "hello"


# -- engine auth: API key OR OAuth token --------------------------------------------


def test_engine_env_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-abcdefghijklmnop")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert ClaudeCodeEngine().resolve_env() == {
        "ANTHROPIC_API_KEY": "sk-ant-test-abcdefghijklmnop"
    }


def test_engine_env_oauth_fallback(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-abcdefghijklmnop")
    assert ClaudeCodeEngine().resolve_env() == {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-abcdefghijklmnop"
    }


def test_engine_env_neither_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(EngineError, match="setup-token"):
        ClaudeCodeEngine().resolve_env()


CORRUPTED_TOKEN = (
    "\x1b[?2004hWelcome to Claude Code v2.1.198\n · Opening browser to sign in…"
)


def test_engine_env_rejects_corrupted_token(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CORRUPTED_TOKEN)
    with pytest.raises(EngineError, match="doesn't look like a credential"):
        ClaudeCodeEngine().resolve_env()


def test_engine_env_strips_valid_token(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", " sk-ant-oat01-abcdefghijkl \n")
    env = ClaudeCodeEngine().resolve_env()
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-abcdefghijkl"}


# -- host claude -p env sanitation ------------------------------------------------


def test_sanitized_env_drops_corrupted_credential(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CORRUPTED_TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-valid-key-abcdefgh")
    env = llm._sanitized_env()
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env  # garbage dropped
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-valid-key-abcdefgh"  # valid kept


def test_cli_subprocess_gets_sanitized_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CORRUPTED_TOKEN)
    monkeypatch.setattr("readme2demo.llm.shutil.which", lambda _: "/bin/claude")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=_fake_cli_envelope("ok"), stderr=""
        )

    monkeypatch.setattr("readme2demo.llm.subprocess.run", fake_run)
    llm._complete_cli("s", "u", "m")
    assert captured["env"] is not None
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in captured["env"]


# -- openhands engine: sandbox image + runnable command ----------------------------


def test_regression_openhands_exit_127_no_transcript():
    """Regression (run glow-20260709-202150): --gemini against the standard
    base image died with `exec exit=127` and "stderr tail: (empty)" — the
    image has no OpenHands and no `python` alias, and the engine command
    neither set RUNTIME=local nor captured its output. The command must be
    runnable in the openhands image and diagnosable when it fails.
    """
    from readme2demo.engines.base import Limits
    from readme2demo.engines.openhands import OpenHandsEngine

    cmd = OpenHandsEngine().build_command(Limits(max_turns=42))
    # The image's venv wrapper — not `python3` (no OpenHands; and a PATH
    # prepend would be wiped by `bash -lc` sourcing /etc/profile).
    assert "openhands-python -m openhands.core.main" in cmd
    assert "RUNTIME=local" in cmd  # no nested docker runtime inside the sandbox
    # docker never sets USER; unset, OpenHands does `su openhands -` and dies
    assert 'USER="$(id -un)"' in cmd
    assert "SKIP_DEPENDENCY_CHECK=1" in cmd  # dev-repo layout check doesn't apply
    assert "WORKSPACE_BASE=/work" in cmd  # else the agent runs in an empty temp dir
    assert "AGENT_ENABLE_BROWSING=false" in cmd  # no playwright browsers in image
    assert "--max-iterations 42" in cmd
    # env var, NOT a flag: the pinned 0.48 CLI has no --save-trajectory-path
    assert "SAVE_TRAJECTORY_PATH=/work/.r2d/transcript.ndjson" in cmd
    assert "--save-trajectory-path" not in cmd
    assert ">/work/.r2d/agent.stderr 2>&1" in cmd  # failures become readable


def test_openhands_resolve_env_points_at_presets(monkeypatch):
    # Bare --engine openhands without litellm creds must name the presets
    # that fill them, not just the raw env var names.
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    from readme2demo.engines.openhands import OpenHandsEngine

    with pytest.raises(EngineError, match="--gemini / --openai / --anthropic"):
        OpenHandsEngine().resolve_env()


def test_openhands_resolve_env_forwards_creds(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "some-key")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-5.1")
    from readme2demo.engines.openhands import OpenHandsEngine

    assert OpenHandsEngine().resolve_env() == {
        "LLM_API_KEY": "some-key",
        "LLM_MODEL": "openai/gpt-5.1",
    }


def test_openhands_default_image_and_claude_none():
    from readme2demo.engines import get_engine

    assert get_engine("openhands").default_image == "readme2demo/openhands:latest"
    assert get_engine("claude-code").default_image is None


def test_openhands_check_image_pass(monkeypatch):
    import subprocess as sp

    from readme2demo.engines.openhands import OpenHandsEngine

    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    OpenHandsEngine().check_image("readme2demo/openhands:latest")  # no raise
    assert captured["cmd"][0] == "docker"
    assert "openhands-python" in captured["cmd"][-1]
    assert "import openhands.core.main" in captured["cmd"][-1]


def test_openhands_check_image_missing_runtime(monkeypatch):
    import subprocess as sp

    from readme2demo.engines.openhands import OpenHandsEngine

    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: sp.CompletedProcess(
            cmd, 1, stdout="", stderr="ModuleNotFoundError: No module named 'openhands'"
        ),
    )
    with pytest.raises(EngineError, match="images/openhands"):
        OpenHandsEngine().check_image("readme2demo/base:latest")


def test_openhands_check_image_daemon_down_is_silent(monkeypatch):
    # A dead docker daemon is a docker problem, not a missing-image problem.
    import subprocess as sp

    from readme2demo.engines.openhands import OpenHandsEngine

    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kw: sp.CompletedProcess(
            cmd, 1, stdout="",
            stderr="failed to connect to the docker API at unix:///var/run/docker.sock",
        ),
    )
    OpenHandsEngine().check_image("readme2demo/openhands:latest")  # no raise


def test_openhands_check_image_docker_missing_is_silent(monkeypatch):
    # docker absent/wedged is the docker preflight check's job, not this probe's.
    def raise_fnf(cmd, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr("subprocess.run", raise_fnf)
    from readme2demo.engines.openhands import OpenHandsEngine

    OpenHandsEngine().check_image("readme2demo/base:latest")  # no raise


def test_claude_engine_check_image_is_noop():
    ClaudeCodeEngine().check_image("readme2demo/base:latest")  # base-class no-op


def test_regression_agent_stderr_copied_to_run_dir(tmp_path, monkeypatch):
    """Regression (run glow-20260710-143231): the agent failed with a 2KB
    stderr tail full of client-side traceback while the real server error
    scrolled away inside the destroyed sandbox. The FULL agent log must be
    copied to the run dir before destroy, and the error must point at it.
    """
    from readme2demo import agent as agent_mod
    from readme2demo.agent import AgentRunError
    from readme2demo.config import Config
    from readme2demo.engines import get_engine
    from readme2demo.sandbox import ExecResult, SandboxError
    from readme2demo.types import Plan, SuccessCriteria

    class FakeSandbox:
        def __init__(self, **kw):
            pass

        def start(self):
            pass

        def exec(self, cmd, timeout=None):
            return ExecResult(1, "tail-of-stderr")

        def copy_in(self, src, dst):
            pass

        def copy_out(self, src, dst):
            if src == agent_mod.STDERR_CONTAINER_PATH:
                dst.write_text("the real server error, in full")
            else:
                raise SandboxError("no transcript")  # agent produced none

        def destroy(self):
            pass

    monkeypatch.setattr(agent_mod, "Sandbox", FakeSandbox)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-abcdefghijklmnop")
    (tmp_path / "repo").mkdir()
    plan = Plan(quickstart_summary="q", success_criteria=SuccessCriteria(command="x"))
    with pytest.raises(AgentRunError, match="Full agent log"):
        agent_mod.run_agent(tmp_path, plan, get_engine("claude-code"), Config())
    assert (tmp_path / "agent.stderr").read_text() == "the real server error, in full"


# -- --allow-docker-socket plumbing --------------------------------------------------


def test_agent_prompt_guide_mode_demands_every_step(tmp_path):
    from pathlib import Path as P

    from readme2demo.agent import render_agent_prompt
    from readme2demo.types import Plan, SuccessCriteria

    plan = Plan(
        quickstart_summary="q",
        success_criteria=SuccessCriteria(command="./run.sh"),
        guide_path="step_by_step.md",
    )
    rendered = render_agent_prompt(plan, P("src/readme2demo/prompts/agent.md"))
    assert "execute EVERY step" in rendered
    assert "SKIPPED_STEP:" in rendered
    assert "does NOT finish the run" in rendered


def test_docker_socket_mounted_when_enabled(tmp_path, monkeypatch):
    from readme2demo import agent as agent_mod
    from readme2demo.config import Config
    from readme2demo.types import Plan, SuccessCriteria

    captured: dict = {}

    class FakeSandbox:
        def __init__(self, **kw):
            captured.update(kw)

        def start(self):
            raise RuntimeError("stop here")  # abort after construction

        def destroy(self):
            pass

    monkeypatch.setattr(agent_mod, "Sandbox", FakeSandbox)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-abcdefghijklmnop")
    (tmp_path / "repo").mkdir()
    plan = Plan(quickstart_summary="q", success_criteria=SuccessCriteria(command="x"))
    from readme2demo.engines import get_engine

    for enabled, expected in ((True, True), (False, False)):
        captured.clear()
        cfg = Config(allow_docker_socket=enabled)
        try:
            agent_mod.run_agent(tmp_path, plan, get_engine("claude-code"), cfg)
        except RuntimeError:
            pass
        has_socket = any(
            src == "/var/run/docker.sock" for src, _, _ in captured["mounts"]
        )
        assert has_socket is expected


def test_render_cmd_includes_socket_when_enabled(tmp_path, monkeypatch):
    import subprocess

    from readme2demo import render as render_mod
    from readme2demo.config import Config

    (tmp_path / "demo.tape").write_text("Output demo.mp4\nType \"x\"\nEnter\nSleep 2s\n")
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(render_mod, "check_render_image", lambda img: None)
    monkeypatch.setattr(render_mod, "_generate_gif_preview", lambda *a: None)
    monkeypatch.setattr(render_mod, "validate_outputs", lambda *a, **k: [])

    render_mod.run_render(tmp_path, Config(allow_docker_socket=True))
    assert "/var/run/docker.sock:/var/run/docker.sock" in captured["cmd"]
    render_mod.run_render(tmp_path, Config(allow_docker_socket=False))
    assert "/var/run/docker.sock:/var/run/docker.sock" not in captured["cmd"]


def test_docker_socket_gid_probe(monkeypatch):
    import subprocess

    from readme2demo import sandbox as sandbox_mod

    monkeypatch.setattr(
        sandbox_mod.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="999\n", stderr=""),
    )
    assert sandbox_mod.docker_socket_gid("img") == "999"
    # probe failure falls back to root group
    monkeypatch.setattr(
        sandbox_mod.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="err"),
    )
    assert sandbox_mod.docker_socket_gid("img") == "0"


def test_sandbox_start_includes_group_add(monkeypatch):
    from readme2demo.sandbox import ExecResult, Sandbox

    captured: dict = {}

    def fake_run(cmd, timeout=None, stream_to=None):
        captured["cmd"] = cmd
        return ExecResult(0, "cid")

    sb = Sandbox(image="img", group_add="999")
    monkeypatch.setattr(sb, "_run", fake_run)
    sb.start()
    assert "--group-add" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--group-add") + 1] == "999"
    sb2 = Sandbox(image="img")
    monkeypatch.setattr(sb2, "_run", fake_run)
    sb2.start()
    assert "--group-add" not in captured["cmd"]


def test_render_socket_includes_group_add(tmp_path, monkeypatch):
    import subprocess

    from readme2demo import render as render_mod
    from readme2demo import sandbox as sandbox_mod
    from readme2demo.config import Config

    (tmp_path / "demo.tape").write_text('Output demo.mp4\nType "x"\nEnter\nSleep 2s\n')
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(render_mod, "check_render_image", lambda img: None)
    monkeypatch.setattr(render_mod, "_generate_gif_preview", lambda *a: None)
    monkeypatch.setattr(render_mod, "validate_outputs", lambda *a, **k: [])
    monkeypatch.setattr(sandbox_mod, "docker_socket_gid", lambda img: "999")

    render_mod.run_render(tmp_path, Config(allow_docker_socket=True))
    assert "--group-add" in captured["cmd"]
    assert "999" in captured["cmd"]
