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

One builder ships here: :func:`build_promo` (#170), the adapter this module's
registry was designed around. It is the only place that knows a promo is a
*paid* output — it plans the cut with an LLM (``promo_script.run_promo_script``)
and hands the result to the deterministic compositor (``promo.render_promo``) —
so it is also the only place that has to answer for the money: the spend lands
in ``manifest.format_costs`` on the success AND the failure path (#103/#209).
"""

from __future__ import annotations

import hashlib
import importlib
import json
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
#: format, so declaring a row here costs nothing before its PR lands. This is
#: the seam for a format whose entry point ALREADY has the
#: :data:`FormatBuilder` shape; #170's promo does not (it ships
#: ``render_promo(run_dir, script, cfg)``, whose middle argument is the promo
#: script, not the manifest), so it goes through the adapter at the bottom of
#: this module instead. Either way :func:`produce` is untouched.
_OPTIONAL_BUILDERS: dict[str, tuple[str, str]] = {}

#: The promo cut (#170): registered below, once :func:`build_promo` is defined.
PROMO_FORMAT = "promo"

#: Run artifacts :func:`build_promo` reads. ``step_by_step.md`` is the FINAL
#: guide (tutorial stage) and ``step_timestamps.json`` is derived from the same
#: tape the render stage filmed, so a scene can only cite footage that exists.
PROMO_INPUTS: tuple[str, ...] = (
    "plan.json",
    "command_log.json",
    "step_by_step.md",
    "step_timestamps.json",
)


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


# --- the promo builder (#170) --------------------------------------------------


def _read_artifact(run_dir: Path, name: str) -> str:
    """Text of a run artifact the promo needs, or a naming error.

    A missing input is the single most likely reason a promo cannot be built
    (``--skip-video`` ran, an old run predates ``step_timestamps.json``), so it
    is worth one clear sentence in ``manifest.formats`` instead of whatever
    ``FileNotFoundError`` a deeper call would have produced.
    """
    path = run_dir / name
    if not path.is_file():
        raise FileNotFoundError(
            f"{name} is missing from {run_dir} — the promo cut is derived from "
            "the verified run's own artifacts and cannot be built without it"
        )
    return path.read_text(encoding="utf-8", errors="replace")


def _exception_cost(exc: BaseException) -> float:
    """Spend an exception says it already incurred, or ``0.0``.

    The same ``getattr(e, "cost_usd", 0.0)`` contract ``orchestrator.run`` uses
    for a failed stage, hardened for a failure path: this runs while another
    exception is propagating, so a ``cost_usd`` that is not a number must not
    become the exception the caller sees instead of the real one.
    """
    try:
        return float(getattr(exc, "cost_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def build_promo(run_dir: Path, manifest: Manifest, cfg: Config) -> None:
    """Build ``promo.mp4`` for a verified run — a :data:`FormatBuilder` (#170).

    The adapter between the two halves of the promo epic, and nothing more:

    1. load the run's own artifacts (:data:`PROMO_INPUTS`),
    2. ``promo_script.run_promo_script`` — the ONE paid call: it returns a
       :class:`~readme2demo.types.PromoScript` only after the code-level
       validator confirms every ``demo_segment`` traces to a step that is both
       published in the final ``step_by_step.md`` and grounded in
       ``command_log.json``,
    3. record the spend (below),
    4. write ``promo_script.json`` through the module that owns that artifact,
    5. ``promo.render_promo`` — deterministic, $0, and the only code that
       touches ffmpeg.

    **Cost.** ``run_promo_script`` returns its spend and its ``PromoScriptError``
    carries ``cost_usd``, so the ``finally`` below records it either way. A
    promo that pays for a grounding retry and *then* fails still spent that
    money, and a run that reports $0.00 for it is the bug #103/#209 exist to
    prevent. This is the format-level analogue of ``stage_fail(cost_usd=…)``.

    **The verified gate is already applied.** Both promo modules state that
    their caller must establish ``manifest.verified`` before a cut is planned or
    composited — a promo of an unverified run would be a shareable video of
    commands no fresh container executed. This function is that caller's
    callee: :func:`produce` refuses to dispatch anything on an unverified run,
    which is why the check does not (and must not) live here as a second,
    softer copy.

    **Failure is never a run failure.** Every path out of here is either a
    written ``promo.mp4`` or an exception, which :func:`produce` turns into
    ``skipped: <reason>`` in ``manifest.formats``. Nothing here writes a
    protected artifact (``render_promo``'s ffmpeg argv can only write
    ``/vhs/promo.mp4``, audited by ``promo.assert_only_demo_footage``), and
    ``produce`` fingerprints them around this call regardless.

    **Publication comes from the return value, never from the disk.**
    ``render_promo`` returns ``None`` when its size/duration gate refuses a cut
    it nonetheless left on disk as the evidence ``promo.log`` describes; a
    caller that published by globbing the run dir would ship exactly the
    truncated video the compositor just refused. ``None`` is therefore a skip
    even when ``promo.mp4`` exists.

    Args:
        run_dir: the verified run directory; also where ``promo.mp4`` lands.
        manifest: the run manifest — supplies ``repo_url`` for the title card
            and receives the LLM spend via ``record_format_cost``.
        cfg: pipeline config: ``model`` for the scene-planning call, plus the
            brand kit and container limits the compositor uses.

    Raises:
        Exception: any failure at all, for :func:`produce` to record as a skip.
    """
    # Imported inside the builder on purpose: promo_script pulls in the LLM
    # backends and the distiller, and the dispatch registry must stay importable
    # (and cheap) for the runs that never ask for a promo. An optional
    # dependency that is missing then degrades to a recorded skip, exactly like
    # a builder module that has not landed.
    from . import promo as promo_mod
    from . import promo_script as promo_script_mod
    from .types import CommandLog, Plan

    # Presentation knobs, read defensively through getattr for the same reason
    # requested_formats() does: the --promo-style/--promo-duration surface is a
    # later PR's, and this wiring must not depend on it landing first.
    style = str(getattr(cfg, "promo_style", None) or promo_mod.DEFAULT_STYLE)
    preset = int(getattr(cfg, "promo_duration", None) or promo_mod.DEFAULT_DURATION)
    # Validate the presets BEFORE the paid call: an unknown style is a config
    # typo, and a typo must not cost an LLM call to discover (the same
    # fail-fast-for-pennies rule the ingest infeasibility gate follows).
    promo_mod.style_preset(style)
    promo_mod.duration_preset(preset)

    # Read through PROMO_INPUTS rather than four literals, so the documented
    # input set IS the one that gets loaded — and every one of them is proven
    # present before the paid call, not one at a time as the code reaches it.
    artifacts = {name: _read_artifact(run_dir, name) for name in PROMO_INPUTS}
    plan = Plan.model_validate_json(artifacts["plan.json"])
    log = CommandLog.model_validate_json(artifacts["command_log.json"])
    guide_text = artifacts["step_by_step.md"]
    timestamps = json.loads(artifacts["step_timestamps.json"])
    if not isinstance(timestamps, dict):
        raise promo_mod.PromoError(
            "step_timestamps.json is not a JSON object — without the per-step "
            "windows no scene can be bounds-checked against real footage"
        )

    cost = 0.0
    try:
        script, cost = promo_script_mod.run_promo_script(
            plan,
            log,
            guide_text,
            timestamps,
            cfg.model,
            repo_url=manifest.repo_url,
            # Plan the cut for the same budget the compositor will enforce, so
            # the duration gate in validate_promo is judging the length the
            # model was actually asked for.
            target_duration_s=float(preset),
        )
    except Exception as e:  # noqa: BLE001 — re-raised; this only rescues the cost
        cost = _exception_cost(e)
        raise
    finally:
        manifest.record_format_cost(PROMO_FORMAT, cost)

    promo_script_mod.write_promo_script(script, run_dir)
    produced = promo_mod.render_promo(run_dir, script, cfg, style=style, preset=preset)
    if produced is None:
        raise promo_mod.PromoError(
            f"no {promo_mod.PROMO_ARTIFACT} was produced — see "
            f"{promo_mod.PROMO_LOG} in the run directory. Any file of that name "
            "left on disk is the refused cut, kept as evidence, and is NOT "
            "published"
        )


register_builder(PROMO_FORMAT, build_promo)
