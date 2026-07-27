"""Tests for the TTS provider registry (slice 1 of #113). Pure — no network/audio."""

from __future__ import annotations

import sys
import types

import pytest

from readme2demo import tts


def test_providers_table_shape():
    assert set(tts.TTS_PROVIDERS) == {"openai", "elevenlabs", "local"}
    assert tts.TTS_PROVIDERS["openai"].default_model is None
    assert tts.TTS_PROVIDERS["elevenlabs"].default_model is None
    assert tts.TTS_PROVIDERS["openai"].default_voice is not None
    assert tts.TTS_PROVIDERS["elevenlabs"].default_voice is not None
    assert tts.TTS_PROVIDERS["local"].noncommercial is True
    assert tts.TTS_PROVIDERS["local"].license_note
    assert "CC-BY-NC" in (tts.TTS_PROVIDERS["local"].license_note or "")


def test_resolve_backend_explicit_requires_key(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(tts.TTSError, match="ELEVENLABS_API_KEY"):
        tts.resolve_tts_backend("elevenlabs")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test")
    assert tts.resolve_tts_backend("elevenlabs") == "elevenlabs"


def test_resolve_backend_auto_picks_first_commercial_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(tts.TTSError, match="No TTS backend available"):
        tts.resolve_tts_backend("auto")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-el")
    assert tts.resolve_tts_backend("auto") == "elevenlabs"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa")
    # table order: openai first
    assert tts.resolve_tts_backend("auto") == "openai"


def test_resolve_backend_auto_never_selects_noncommercial(monkeypatch):
    """Regression: #113 CC-BY-NC local backend must never win auto-selection."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    # Even if we invent a key env for local, noncommercial blocks auto.
    # local.key_env is None — auto still fails rather than picking local.
    with pytest.raises(tts.TTSError, match="Non-commercial"):
        tts.resolve_tts_backend("auto")
    # Explicit local is allowed without a cloud key.
    assert tts.resolve_tts_backend("local") == "local"


def test_resolve_model_explicit_env_default_error(monkeypatch):
    monkeypatch.delenv("OPENAI_TTS_MODEL", raising=False)
    with pytest.raises(tts.TTSError, match="No OpenAI TTS model specified.*OPENAI_TTS_MODEL"):
        tts.resolve_tts_model("openai", None)
    monkeypatch.setenv("OPENAI_TTS_MODEL", "tts-1-hd")
    assert tts.resolve_tts_model("openai", None) == "tts-1-hd"
    assert tts.resolve_tts_model("openai", "openai/tts-1") == "tts-1"
    # local has a default_model
    assert tts.resolve_tts_model("local", None) == "oute-default"


def test_resolve_model_cross_provider_leak_guard(monkeypatch):
    """Regression: stale config defaults from another provider must not stick.

    Same class of bug as the --gemini run that died on a Claude config default
    (llm._provider_model leak guard / CLAUDE.md failure history).
    """
    monkeypatch.delenv("OPENAI_TTS_MODEL", raising=False)
    # eleven_ prefix belongs to elevenlabs, not openai
    with pytest.raises(tts.TTSError, match="No OpenAI TTS model specified"):
        tts.resolve_tts_model("openai", "eleven_multilingual_v2")
    monkeypatch.setenv("OPENAI_TTS_MODEL", "tts-1")
    assert tts.resolve_tts_model("openai", "eleven_multilingual_v2") == "tts-1"


def test_resolve_voice_default_and_env(monkeypatch):
    monkeypatch.delenv("OPENAI_TTS_VOICE", raising=False)
    assert tts.resolve_tts_voice("openai", None) == "alloy"
    monkeypatch.setenv("OPENAI_TTS_VOICE", "nova")
    assert tts.resolve_tts_voice("openai", None) == "nova"
    assert tts.resolve_tts_voice("openai", "echo") == "echo"


def test_check_tts_sdk_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "elevenlabs", None)

    def boom(name):
        raise ModuleNotFoundError("No module named 'elevenlabs'", name="elevenlabs")

    monkeypatch.setattr(tts.importlib, "import_module", boom)
    with pytest.raises(tts.TTSError, match=r"elevenlabs is not installed"):
        tts.check_tts_sdk("elevenlabs")


def test_check_tts_sdk_too_old(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", types.ModuleType("openai"))
    with pytest.raises(tts.TTSError, match="too old"):
        tts.check_tts_sdk("openai")


def test_check_tts_sdk_broken_import(monkeypatch):
    def boom(module):
        raise ModuleNotFoundError("No module named 'httpx'", name="httpx")

    monkeypatch.setattr(tts.importlib, "import_module", boom)
    with pytest.raises(tts.TTSError, match="failed to import.*httpx"):
        tts.check_tts_sdk("openai")


def test_check_tts_sdk_noop_for_local():
    tts.check_tts_sdk("local")  # no sdk


def test_check_tts_model_noop_unknown_backend(monkeypatch):
    monkeypatch.delenv("OPENAI_TTS_MODEL", raising=False)
    tts.check_tts_model("not-a-backend", None)  # no-op


def test_check_tts_model_gates_when_unspecified(monkeypatch):
    monkeypatch.delenv("OPENAI_TTS_MODEL", raising=False)
    with pytest.raises(tts.TTSError, match="No OpenAI TTS model specified"):
        tts.check_tts_model("openai", None)
