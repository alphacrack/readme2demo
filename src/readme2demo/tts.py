"""TTS provider registry: resolution and preflight validation (no audio).

First slice of #113. Pure data + env/SDK checks — nothing synthesizes audio,
nothing hits the network, and nothing is wired into CLI/config yet.
Mirrors the shape of ``llm.ProviderSpec`` / ``check_sdk`` / ``_provider_model``.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from typing import Optional


class TTSError(RuntimeError):
    """Raised when a TTS backend/model/voice cannot be resolved or preflighted."""


@dataclass(frozen=True)
class TTSProviderSpec:
    """One TTS backend (OpenAI / ElevenLabs / local).

    ``sdk`` is ``(import_module, pip_name, sentinel_attr)`` for preflight, or
    ``None`` when no Python SDK is required (e.g. a local binary path later).
    """

    name: str
    title: str
    key_env: Optional[str]
    model_env: str
    voice_env: str
    model_prefixes: tuple[str, ...]
    sdk: Optional[tuple[str, str, str]]  # import name, pip name, sentinel
    extra: Optional[str]
    default_model: Optional[str]
    default_voice: Optional[str]
    models_url: Optional[str] = None
    # When True, "auto" must never select this backend (e.g. CC-BY-NC local).
    noncommercial: bool = False
    license_note: Optional[str] = None


# Deterministic insertion order: first commercial key-bearing backend wins auto.
TTS_PROVIDERS: dict[str, TTSProviderSpec] = {
    "openai": TTSProviderSpec(
        name="openai",
        title="OpenAI TTS",
        key_env="OPENAI_API_KEY",
        model_env="OPENAI_TTS_MODEL",
        voice_env="OPENAI_TTS_VOICE",
        model_prefixes=("tts-", "gpt-4o-mini-tts"),
        sdk=("openai", "openai", "OpenAI"),
        extra="openai",
        default_model=None,  # never guess — cloud models get retired with hard 404s
        default_voice="alloy",
        models_url="https://platform.openai.com/docs/guides/text-to-speech",
    ),
    "elevenlabs": TTSProviderSpec(
        name="elevenlabs",
        title="ElevenLabs",
        key_env="ELEVENLABS_API_KEY",
        model_env="ELEVENLABS_TTS_MODEL",
        voice_env="ELEVENLABS_TTS_VOICE",
        model_prefixes=("eleven_", "eleven-"),
        sdk=("elevenlabs", "elevenlabs", "ElevenLabs"),
        extra=None,  # install hint falls back to pip name
        default_model=None,
        default_voice="Rachel",
        models_url="https://elevenlabs.io/docs/api-reference/text-to-speech",
    ),
    "local": TTSProviderSpec(
        name="local",
        title="Local TTS (OuteTTS)",
        key_env=None,  # no cloud key; explicit name required
        model_env="LOCAL_TTS_MODEL",
        voice_env="LOCAL_TTS_VOICE",
        model_prefixes=("oute", "local"),
        sdk=None,
        extra=None,
        default_model="oute-default",
        default_voice="default",
        noncommercial=True,
        license_note="CC-BY-NC (OuteTTS 0.2) — commercial use requires explicit opt-in",
    ),
}


def resolve_tts_backend(name: Optional[str] = None) -> str:
    """Resolve a TTS backend name, including ``auto``.

    Explicit names whose ``key_env`` is unset raise naming the variable.
    ``auto`` picks the first *commercial* provider (table order) whose key is
    set. Non-commercial backends are never auto-selected (#113 CC-BY-NC note).
    """
    b = (name or "auto").strip().lower() or "auto"
    if b != "auto":
        if b not in TTS_PROVIDERS:
            raise TTSError(
                f"Unknown TTS backend {b!r}. Available: "
                f"{', '.join(sorted(TTS_PROVIDERS))}"
            )
        spec = TTS_PROVIDERS[b]
        if spec.key_env and not os.environ.get(spec.key_env):
            raise TTSError(
                f"{spec.key_env} is not set. Export it to use the {spec.title} "
                f"backend (or pass another --tts-backend)."
            )
        return b

    for key, spec in TTS_PROVIDERS.items():
        if spec.noncommercial:
            continue
        if spec.key_env and os.environ.get(spec.key_env):
            return key
    options = ", ".join(
        f"{s.key_env} ({s.title})"
        for s in TTS_PROVIDERS.values()
        if s.key_env and not s.noncommercial
    )
    raise TTSError(
        "No TTS backend available: set one of "
        f"{options}. Non-commercial local backends are never auto-selected — "
        "name them explicitly if you intend to use them."
    )


def _matches_other_provider_prefix(spec: TTSProviderSpec, name: str) -> bool:
    """True when *name* looks like it belongs to a *different* TTS provider."""
    lowered = name.lower()
    for other in TTS_PROVIDERS.values():
        if other.name == spec.name:
            continue
        if any(lowered.startswith(p.lower()) for p in other.model_prefixes):
            return True
    return False


def resolve_tts_model(backend: str, model: Optional[str] = None) -> str:
    """Resolve the model name for a TTS backend.

    Order: explicit (with optional ``<provider>/`` prefix stripped) → env →
    spec default → loud error. A name matching another provider's prefixes
    counts as unspecified (cross-provider config-default leak guard).
    """
    if backend not in TTS_PROVIDERS:
        raise TTSError(f"Unknown TTS backend {backend!r}")
    spec = TTS_PROVIDERS[backend]
    name = (model or "").strip()
    if name.startswith(f"{spec.name}/"):
        name = name.split("/", 1)[1]
    if not name or _matches_other_provider_prefix(spec, name):
        name = os.environ.get(spec.model_env, "").strip() or (spec.default_model or "")
    if not name:
        url_note = f" Current names: {spec.models_url}" if spec.models_url else ""
        raise TTSError(
            f"No {spec.title} model specified. Pass one with --tts-model "
            f"<model>, or export {spec.model_env}.{url_note}"
        )
    return name


def resolve_tts_voice(backend: str, voice: Optional[str] = None) -> str:
    """Resolve the voice name for a TTS backend (explicit → env → default)."""
    if backend not in TTS_PROVIDERS:
        raise TTSError(f"Unknown TTS backend {backend!r}")
    spec = TTS_PROVIDERS[backend]
    name = (voice or "").strip()
    if not name:
        name = os.environ.get(spec.voice_env, "").strip() or (spec.default_voice or "")
    if not name:
        raise TTSError(
            f"No {spec.title} voice specified. Pass one with --tts-voice "
            f"<voice>, or export {spec.voice_env}."
        )
    return name


def check_tts_sdk(backend: str) -> None:
    """Fail fast when *backend* needs an SDK that cannot serve it.

    Three cases, each with its own actionable message: absent → install hint
    from ``spec.extra``; importable but raises → quote the real error; imports
    but missing sentinel → upgrade hint. No-op when ``spec.sdk`` is None.
    """
    spec = TTS_PROVIDERS.get(backend)
    if spec is None or spec.sdk is None:
        return
    module, pip_name, sentinel = spec.sdk
    try:
        mod = importlib.import_module(module)
    except ImportError as e:
        failed = (getattr(e, "name", None) or "").split(".")[0]
        extra_hint = (
            f"pip install 'readme2demo[{spec.extra}]' (or pip install {pip_name})"
            if spec.extra
            else f"pip install {pip_name}"
        )
        if failed == module.split(".")[0]:
            raise TTSError(
                f"{pip_name} is not installed. Install it: {extra_hint}."
            ) from e
        raise TTSError(
            f"{pip_name} is installed but failed to import "
            f"({type(e).__name__}: {e}). Reinstall: "
            f"pip install -U {pip_name}."
        ) from e
    if not hasattr(mod, sentinel):
        raise TTSError(
            f"{pip_name} is installed but too old for readme2demo (no "
            f"{module}.{sentinel}). Upgrade: pip install -U {pip_name}."
        )


def check_tts_model(backend: str, model: Optional[str]) -> None:
    """Preflight: prove a model name is resolvable for known TTS backends.

    No-op for unknown backends (mirrors ``llm.check_model``).
    """
    if backend not in TTS_PROVIDERS:
        return
    resolve_tts_model(backend, model)
