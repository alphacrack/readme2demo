"""Post-render dispatch of extra output formats — best-effort, never load-bearing.

The pipeline's seven stages (``manifest.STAGES``) end at ``render``, which
produces the artifacts of record: ``demo.mp4`` and the published guide
(``step_by_step.md``, ``commands.sh``, ``demo.tape``). Anything an operator
additionally asks for through ``--formats`` — a promo cut (#170), a social cut
(#116), a podcast (#111) — is built HERE, after render, by a per-format builder
looked up in a small registry.

Three rules make this seam safe (#230):

1. **Gated on verification.** :func:`produce` dispatches nothing when
   ``manifest.verified`` is False, exactly as ``_stage_render`` refuses to film
   an unverified script — an extra format is a derivative of verified footage,
   and there is no such thing as a promo cut of an unverified run. The refusal
   is recorded per format, with a reason, like every other skip.
2. **A builder failure is never a run failure.** Every builder call is wrapped;
   the exception becomes a ``"skipped: ..."`` string in the returned mapping and
   in ``manifest.formats``. This generalizes the contract
   ``render._generate_gif_preview`` already states: "failure here never fails
   the stage — the mp4 is the artifact of record."
3. **Builders may not touch the protected artifacts.**
   :data:`PROTECTED_ARTIFACTS` is fingerprinted before and after every builder
   call; a builder that mutates one is recorded as skipped so the violation
   lands in the manifest instead of shipping silently. The invariant is that no
   published artifact may be reached by code the fresh-container replay did not
   validate — prompts are suggestions, parsers are law, and so are digests.

Formats are **not stages**: they are absent from ``manifest.STAGES``, they are
not resumable, and they never gate the run. A builder that is not registered —
or whose module has not landed yet — simply yields a skipped format, which is
exactly how a format implemented in a later PR behaves today.
"""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .manifest import Manifest

#: The four artifacts of record. A builder that changes any of these has left
#: the presentation layer and entered the grounding path, which no optional
#: output is allowed to do (see the module docstring, rule 3).
PROTECTED_ARTIFACTS: tuple[str, ...] = (
    "demo.mp4",
    "step_by_step.md",
    "commands.sh",
    "demo.tape",
)

#: Formats the render stage itself owns (VHS writes ``demo.mp4``; the ffmpeg
#: preview writes ``demo.gif``). They are filtered out of the dispatch set:
#: their outcome is the render stage's manifest record, not a format record,
#: and re-running them here would rewrite a protected artifact.
RENDER_STAGE_FORMATS: frozenset[str] = frozenset({"demo", "gif"})

#: Fallback selection for a ``Config`` that predates ``Config.formats`` (#212).
#: Same pair that PR's field defaults to — today's exact output set.
DEFAULT_FORMATS: tuple[str, ...] = ("demo", "gif")

#: Status recorded for a format whose builder ran to completion.
PRODUCED = "produced"

#: Reason recorded for every requested format on an unverified run.
UNVERIFIED_REASON = "replay unverified — no extra formats"

#: Digest stand-in for a protected artifact that is not on disk. It matters as
#: much as the real digests: a builder that CREATES a protected artifact which
#: was not there (a stray ``demo.tape``) violates the invariant just as much as
#: one that rewrites an existing file.
_ABSENT = "absent"

#: A format builder: given the run directory, the manifest and the config, it
#: writes its own artifact into the run directory and returns nothing. It may
#: read the verified artifacts; it may not write them.
FormatBuilder = Callable[[Path, Manifest, Config], None]

#: Explicitly registered builders (see :func:`register_builder`). Checked first.
_BUILDERS: dict[str, FormatBuilder] = {}

#: Lazily-imported builders: format name → ``(module, attribute)``. The module
#: need NOT exist — a failed import resolves to "no builder", i.e. a skipped
#: format, so declaring a row here costs nothing before its PR lands. Wiring
#: #170's promo compositor is one line in this table once ``promo.py`` exposes a
#: callable with the :data:`FormatBuilder` signature; because #170 ships
#: ``render_promo(run_dir, script, cfg)`` (its middle argument is the promo
#: script, not the manifest), that one line is instead a three-line adapter
#: handed to :func:`register_builder`. Either way :func:`produce` is untouched.
_OPTIONAL_BUILDERS: dict[str, tuple[str, str]] = {
    # "promo": ("readme2demo.promo", "render_promo"),  # ← #170
}


def register_builder(name: str, builder: FormatBuilder) -> None:
    """Register ``builder`` as the producer of format ``name``.

    The registration point for format implementations that live outside this
    module (or that need an adapter around a differently-shaped entry point).
    Registering an already-registered name replaces it.
    """
    _BUILDERS[name] = builder


def builder_for(name: str) -> Optional[FormatBuilder]:
    """Resolve the builder for ``name``, or ``None`` when the format has none.

    Explicit registrations win. Otherwise a row in ``_OPTIONAL_BUILDERS`` is
    imported lazily; any failure to resolve it — the module has not landed yet,
    an optional dependency is missing, the attribute is not callable — returns
    ``None`` rather than raising, because a format that cannot be built is a
    skipped format, never a crashed run.
    """
    builder = _BUILDERS.get(name)
    if builder is not None:
        return builder
    target = _OPTIONAL_BUILDERS.get(name)
    if target is None:
        return None
    module_name, attribute = target
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 — a missing/broken optional module is a skip
        return None
    candidate = getattr(module, attribute, None)
    return candidate if callable(candidate) else None


def requested_formats(cfg: Config) -> list[str]:
    """Format names this run asked for: lowercased, de-duplicated, order kept.

    Reads ``cfg.formats`` — the ``--formats`` registry surface landed in #212,
    so the field is always present. An empty list still falls back to
    :data:`DEFAULT_FORMATS`, today's exact output set, so a config that
    explicitly clears the selection doesn't silently dispatch nothing.
    """
    raw = cfg.formats or DEFAULT_FORMATS
    names: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        name = str(entry).strip().lower()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def protected_digests(run_dir: Path) -> dict[str, str]:
    """SHA-256 of every artifact in :data:`PROTECTED_ARTIFACTS` under ``run_dir``.

    Missing files map to the :data:`_ABSENT` sentinel (which cannot collide with
    a hex digest), so both mutation and creation of a protected artifact show up
    as a changed fingerprint. Read in chunks: ``demo.mp4`` is a full-length
    tutorial video.
    """
    digests: dict[str, str] = {}
    for name in PROTECTED_ARTIFACTS:
        path = run_dir / name
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            digests[name] = _ABSENT
            continue
        digests[name] = digest.hexdigest()
    return digests


def _skipped(reason: str) -> str:
    """Format a skip status; the counterpart of :data:`PRODUCED`."""
    return f"skipped: {reason}"


def produce(run_dir: Path, manifest: Manifest, cfg: Config) -> dict[str, str]:
    """Build every requested extra output format, best-effort, after render.

    Dispatches over the run's requested formats minus the ones the render stage
    already owns (:data:`RENDER_STAGE_FORMATS`), running each format's builder
    inside a guard that (a) turns any exception into a recorded skip and (b)
    fingerprints :data:`PROTECTED_ARTIFACTS` before and after the call so a
    builder that writes one is recorded as untrusted instead of quietly
    corrupting the published run.

    Args:
        run_dir: Run directory holding the verified artifacts; builders write
            their own outputs here.
        manifest: Run manifest — supplies the ``verified`` gate and receives the
            per-format outcomes via :meth:`Manifest.record_formats`.
        cfg: Pipeline config; ``cfg.formats`` selects what to build.

    Returns:
        Mapping of format name → ``"produced"`` or ``"skipped: <reason>"``, in
        request order. Empty when nothing beyond the render stage's own formats
        was requested — the default case, which records nothing and prints
        nothing.

    Never raises on behalf of a builder: the return value and
    ``manifest.formats`` are the entire failure channel. The run's verdict,
    exit code and artifacts are identical whether a builder succeeded, failed,
    or was never implemented.
    """
    requested = [n for n in requested_formats(cfg) if n not in RENDER_STAGE_FORMATS]
    results: dict[str, str] = {}
    if not requested:
        return results

    if not manifest.verified:
        # Same gate, same wording shape as _stage_render's "no video": an extra
        # format derived from an unverified run would carry the run's
        # unverified content into a shareable artifact with no badge on it.
        for name in requested:
            results[name] = _skipped(UNVERIFIED_REASON)
        manifest.record_formats(results)
        return results

    before = protected_digests(run_dir)
    for name in requested:
        builder = builder_for(name)
        if builder is None:
            results[name] = _skipped(f"no builder registered for {name!r}")
            continue
        try:
            builder(run_dir, manifest, cfg)
        except Exception as e:  # noqa: BLE001 — an extra format is never load-bearing
            results[name] = _skipped(f"builder failed — {type(e).__name__}: {e}")
        else:
            results[name] = PRODUCED
        # Checked whether the builder returned or raised: a builder that dies
        # halfway through is exactly the one most likely to have left a
        # protected artifact half-written.
        after = protected_digests(run_dir)
        mutated = [n for n in PROTECTED_ARTIFACTS if before[n] != after[n]]
        if mutated:
            results[name] = _skipped(
                f"builder mutated protected artifact(s) {', '.join(mutated)} — "
                "output not trusted"
            )
            # Re-baseline so the next builder is judged on its own damage only.
            before = after

    manifest.record_formats(results)
    return results
