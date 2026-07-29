"""Tests for the engine interface hooks in ``readme2demo.engines``.

Engine-parser behaviour lives in test_normalize.py; this file covers the
``AgentEngine.credential_env_vars()`` seam (#235).
"""

from pathlib import Path

from readme2demo.engines.base import AgentEngine, Limits
from readme2demo.engines.claude_code import ClaudeCodeEngine
from readme2demo.engines.openhands import OpenHandsEngine
from readme2demo.types import AgentResult, CommandLog


class _DummyEngine(AgentEngine):
    """Minimal concrete engine for exercising the base-class default."""

    name = "dummy"

    def __init__(self, required: list[str]) -> None:
        self._required = required

    def required_env(self) -> list[str]:
        return self._required

    def build_command(self, limits: Limits) -> str:
        return "true"

    def parse_transcript(self, transcript_path: Path) -> CommandLog:
        return CommandLog(engine=self.name, result=AgentResult(outcome="failed"))


def test_claude_code_credentials_include_oauth_token():
    """Regression: claude-code's credential_env_vars() must include BOTH
    ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN.

    This override is REQUIRED, not cosmetic: required_env() returns only
    ["ANTHROPIC_API_KEY"] (the OAuth token lives only in AUTH_ENV_VARS), so
    the fail-closed base default would miss the OAuth token — yet
    resolve_env() forwards whichever of the two is set. A consumer swapping
    credentials for placeholders would then leak the real OAuth token.
    """
    creds = ClaudeCodeEngine().credential_env_vars()
    assert creds == {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
    # The gap this override closes: the OAuth token is NOT in required_env().
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in ClaudeCodeEngine().required_env()


def test_openhands_credentials_exclude_llm_model():
    """Regression: openhands's credential_env_vars() is exactly {"LLM_API_KEY"}
    — LLM_MODEL is plain config and must never be treated as a credential.

    This override is DEFENSIVE: the fail-closed default already yields this
    exact set today (LLM_MODEL matches nothing in the name regex). It pins
    the intent and guards a future rename of the key var.
    """
    engine = OpenHandsEngine()
    assert engine.credential_env_vars() == {"LLM_API_KEY"}
    assert "LLM_MODEL" in engine.required_env()
    assert "LLM_MODEL" not in engine.credential_env_vars()


def test_base_default_is_fail_closed_on_secret_named_vars():
    """Regression: an engine that does not override credential_env_vars()
    still has every required_env() name containing KEY, TOKEN, or SECRET
    treated as a credential; plain config names are left alone."""
    engine = _DummyEngine(
        ["SOME_API_KEY", "OAUTH_TOKEN", "APP_SECRET", "MODEL_NAME", "BASE_URL"]
    )
    assert engine.credential_env_vars() == {"SOME_API_KEY", "OAUTH_TOKEN", "APP_SECRET"}


def test_base_default_matches_names_case_insensitively():
    """Regression: the credential-name match is case-insensitive — lowercase
    or mixed-case secret-named vars are credentials too."""
    engine = _DummyEngine(["my_api_key", "X_Auth_Token", "app_secret", "verbose"])
    assert engine.credential_env_vars() == {"my_api_key", "X_Auth_Token", "app_secret"}


def test_credential_env_vars_has_no_consumer_yet():
    """Regression: credential_env_vars() is a purely additive seam (#235) —
    wiring it into agent.py/sandbox.py is a different issue (#165). Pin that
    nothing outside the engines themselves references it."""
    src = Path(__file__).resolve().parent.parent / "src" / "readme2demo"
    allowed = {
        src / "engines" / "base.py",
        src / "engines" / "claude_code.py",
        src / "engines" / "openhands.py",
    }
    offenders = [
        path
        for path in src.rglob("*.py")
        if "credential_env_vars" in path.read_text(encoding="utf-8")
        and path not in allowed
    ]
    assert offenders == []
