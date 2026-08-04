"""Tests for produce(): post-render dispatch of extra output formats (#230).

No docker, no network, no API — every stage and every format builder is
monkeypatched (the pattern in tests/test_orchestrator.py).
"""

from pathlib import Path

from readme2demo import produce as produce_mod
from readme2demo.config import Config
from readme2demo.manifest import Manifest
from readme2demo.orchestrator import Orchestrator
from readme2demo.produce import (
    PRODUCED,
    PROTECTED_ARTIFACTS,
    builder_for,
    produce,
    requested_formats,
)

_URL = "https://github.com/x/y"


# -- helpers -------------------------------------------------------------------


def make_manifest(tmp_path: Path, *, verified: bool = True, name: str = "run") -> Manifest:
    m = Manifest.create(tmp_path / name, _URL, "claude-code", "img")
    m.verified = verified
    m.save()
    return m


def cfg_with_formats(*names: str, **kwargs) -> Config:
    """A Config carrying a ``formats`` selection.

    Built with ``model_copy(update=...)`` rather than ``Config(formats=[...])``
    because the field itself arrives with #212 and ``Config`` is
    ``extra="forbid"`` — this helper works both before and after that lands,
    which is exactly the degradation produce() itself has to survive.
    """
    return Config(**kwargs).model_copy(update={"formats": list(names)})


def seed_protected(run_dir: Path, *, skip: tuple[str, ...] = ()) -> dict[str, bytes]:
    """Write the four protected artifacts with known bytes; return them."""
    written: dict[str, bytes] = {}
    for name in PROTECTED_ARTIFACTS:
        if name in skip:
            continue
        data = f"{name} contents\n".encode()
        (run_dir / name).write_bytes(data)
        written[name] = data
    return written


# -- format selection ----------------------------------------------------------


def test_requested_formats_normalizes_case_dedupes_and_keeps_order():
    cfg = cfg_with_formats("GIF", "demo", " gif ", "promo")
    assert requested_formats(cfg) == ["gif", "demo", "promo"]


def test_requested_formats_defaults_to_the_render_stage_set():
    """Regression (#230): a default config dispatches nothing.

    #212 landed ``Config.formats`` defaulting to today's exact output set —
    both formats the render stage owns — so produce() must find nothing left
    to dispatch on an untouched config. (This test previously pinned a
    getattr bridge for "before #212 lands"; the bridge is retired, the
    guarantee it protected is not.)
    """
    assert Config().formats == ["demo", "gif"]
    assert requested_formats(Config()) == ["demo", "gif"]


def test_requested_formats_empty_selection_falls_back(tmp_path: Path):
    """Regression (#230): an explicitly emptied selection must not mean
    "dispatch nothing unknowingly" — it falls back to the render-stage set,
    which produce() then filters out anyway."""
    assert requested_formats(cfg_with_formats()) == ["demo", "gif"]


def test_render_stage_formats_are_never_dispatched(tmp_path: Path):
    """demo/gif belong to the render stage: produce() must not re-produce them.

    Re-running them here would rewrite demo.mp4 — a protected artifact — from
    outside the stage that the verified verdict is attached to.
    """
    m = make_manifest(tmp_path)
    seed_protected(m._run_dir)
    results = produce(m._run_dir, m, cfg_with_formats("demo", "gif"))
    assert results == {}
    assert Manifest.load(m._run_dir).formats == {}


# -- the verified gate ---------------------------------------------------------


def test_unverified_run_skips_every_extra_format_with_a_recorded_reason(tmp_path: Path):
    """Regression (#230): formats are gated on manifest.verified, like render.

    An unverified run gets no promo cut — and, exactly as _stage_render records
    "replay unverified — no video", the refusal is recorded with its reason
    rather than being a silent no-op.
    """
    calls: list[Path] = []

    def builder(run_dir, manifest, cfg):
        calls.append(run_dir)

    m = make_manifest(tmp_path, verified=False)
    seed_protected(m._run_dir)
    produce_mod.register_builder("promo", builder)
    try:
        results = produce(m._run_dir, m, cfg_with_formats("demo", "gif", "promo"))
    finally:
        produce_mod._BUILDERS.pop("promo", None)

    assert results == {"promo": "skipped: replay unverified — no extra formats"}
    assert calls == [], "no builder may run on an unverified run"
    assert Manifest.load(m._run_dir).formats == results


# -- builder lookup ------------------------------------------------------------


def test_format_without_a_builder_is_skipped_not_failed(tmp_path: Path):
    m = make_manifest(tmp_path)
    seed_protected(m._run_dir)
    results = produce(m._run_dir, m, cfg_with_formats("podcast"))
    assert results == {"podcast": "skipped: no builder registered for 'podcast'"}


def test_optional_builder_whose_module_has_not_landed_is_a_skip(tmp_path: Path, monkeypatch):
    """Regression (#230): a declared-but-absent builder module never crashes.

    This is the state of #170's promo compositor today — the registry row can
    point at ``readme2demo.promo`` before that module exists, and the format
    simply reports as skipped.
    """
    monkeypatch.setitem(
        produce_mod._OPTIONAL_BUILDERS, "promo", ("readme2demo.promo", "render_promo")
    )
    assert builder_for("promo") is None
    m = make_manifest(tmp_path)
    seed_protected(m._run_dir)
    results = produce(m._run_dir, m, cfg_with_formats("promo"))
    assert results["promo"].startswith("skipped: no builder registered")


def test_optional_builder_is_resolved_lazily_once_its_module_exists(
    tmp_path: Path, monkeypatch
):
    """The lookup imports on demand, so a later PR's module wires itself in."""
    import sys
    import types

    module = types.ModuleType("fake_format_builders")
    seen: list[Path] = []
    module.build = lambda run_dir, manifest, cfg: seen.append(run_dir)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_format_builders", module)
    monkeypatch.setitem(
        produce_mod._OPTIONAL_BUILDERS, "promo", ("fake_format_builders", "build")
    )

    m = make_manifest(tmp_path)
    seed_protected(m._run_dir)
    assert produce(m._run_dir, m, cfg_with_formats("promo")) == {"promo": PRODUCED}
    assert seen == [m._run_dir]


def test_non_callable_attribute_resolves_to_no_builder(monkeypatch):
    import sys
    import types

    module = types.ModuleType("fake_format_builders")
    module.build = "not callable"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fake_format_builders", module)
    monkeypatch.setitem(
        produce_mod._OPTIONAL_BUILDERS, "promo", ("fake_format_builders", "build")
    )
    assert builder_for("promo") is None


# -- best-effort execution -----------------------------------------------------


def test_registered_builder_runs_and_is_recorded_produced(tmp_path: Path, monkeypatch):
    def builder(run_dir, manifest, cfg):
        (run_dir / "promo.mp4").write_bytes(b"promo")

    monkeypatch.setitem(produce_mod._BUILDERS, "promo", builder)
    m = make_manifest(tmp_path)
    before = seed_protected(m._run_dir)

    results = produce(m._run_dir, m, cfg_with_formats("demo", "gif", "promo"))

    assert results == {"promo": PRODUCED}
    assert (m._run_dir / "promo.mp4").read_bytes() == b"promo"
    assert Manifest.load(m._run_dir).formats == {"promo": PRODUCED}
    for name, data in before.items():
        assert (m._run_dir / name).read_bytes() == data


def test_regression_raising_builder_is_recorded_and_never_propagates(
    tmp_path: Path, monkeypatch
):
    """Regression (#230): a builder exception is data, not a run failure.

    Generalizes render._generate_gif_preview's contract — "failure here never
    fails the stage" — to every optional output.
    """
    def builder(run_dir, manifest, cfg):
        raise RuntimeError("compositor died mid-frame")

    monkeypatch.setitem(produce_mod._BUILDERS, "promo", builder)
    m = make_manifest(tmp_path)
    before = seed_protected(m._run_dir)

    results = produce(m._run_dir, m, cfg_with_formats("promo"))

    assert results["promo"] == (
        "skipped: builder failed — RuntimeError: compositor died mid-frame"
    )
    assert Manifest.load(m._run_dir).formats == results
    for name, data in before.items():
        assert (m._run_dir / name).read_bytes() == data


def test_one_bad_builder_does_not_stop_the_next_format(tmp_path: Path, monkeypatch):
    def boom(run_dir, manifest, cfg):
        raise ValueError("nope")

    def ok(run_dir, manifest, cfg):
        (run_dir / "social.mp4").write_bytes(b"social")

    monkeypatch.setitem(produce_mod._BUILDERS, "promo", boom)
    monkeypatch.setitem(produce_mod._BUILDERS, "social", ok)
    m = make_manifest(tmp_path)
    seed_protected(m._run_dir)

    results = produce(m._run_dir, m, cfg_with_formats("promo", "social"))

    assert results["promo"].startswith("skipped: builder failed — ValueError")
    assert results["social"] == PRODUCED


# -- the protected-artifact guard ----------------------------------------------


def test_regression_builder_mutating_a_protected_artifact_is_recorded_untrusted(
    tmp_path: Path, monkeypatch
):
    """Regression (#230): an optional output may not reach the grounding path.

    demo.tape / step_by_step.md / commands.sh / demo.mp4 are the artifacts a
    fresh container validated. A builder that writes one has left the
    presentation layer, so its output is marked untrusted in the manifest
    instead of shipping as a success.
    """
    def vandal(run_dir, manifest, cfg):
        (run_dir / "demo.tape").write_text("Type 'rm -rf /'\n")

    monkeypatch.setitem(produce_mod._BUILDERS, "promo", vandal)
    m = make_manifest(tmp_path)
    seed_protected(m._run_dir)

    results = produce(m._run_dir, m, cfg_with_formats("promo"))

    assert results["promo"].startswith("skipped: builder mutated protected artifact(s)")
    assert "demo.tape" in results["promo"]
    assert Manifest.load(m._run_dir).formats == results


def test_builder_creating_an_absent_protected_artifact_is_caught(
    tmp_path: Path, monkeypatch
):
    """The absent-file sentinel: creating demo.mp4 counts as mutating it."""
    def creator(run_dir, manifest, cfg):
        (run_dir / "demo.mp4").write_bytes(b"a video nobody verified")

    monkeypatch.setitem(produce_mod._BUILDERS, "promo", creator)
    m = make_manifest(tmp_path)
    seed_protected(m._run_dir, skip=("demo.mp4",))

    results = produce(m._run_dir, m, cfg_with_formats("promo"))

    assert "demo.mp4" in results["promo"]
    assert results["promo"].startswith("skipped: builder mutated")


def test_mutation_by_one_builder_is_not_blamed_on_the_next(tmp_path: Path, monkeypatch):
    def vandal(run_dir, manifest, cfg):
        (run_dir / "commands.sh").write_text("# rewritten\n")

    def innocent(run_dir, manifest, cfg):
        (run_dir / "social.mp4").write_bytes(b"social")

    monkeypatch.setitem(produce_mod._BUILDERS, "promo", vandal)
    monkeypatch.setitem(produce_mod._BUILDERS, "social", innocent)
    m = make_manifest(tmp_path)
    seed_protected(m._run_dir)

    results = produce(m._run_dir, m, cfg_with_formats("promo", "social"))

    assert results["promo"].startswith("skipped: builder mutated")
    assert results["social"] == PRODUCED


def test_raising_builder_that_also_damaged_an_artifact_reports_the_mutation(
    tmp_path: Path, monkeypatch
):
    """A builder that dies halfway is the one most likely to leave damage."""
    def half_written(run_dir, manifest, cfg):
        (run_dir / "step_by_step.md").write_text("truncated")
        raise OSError("disk full")

    monkeypatch.setitem(produce_mod._BUILDERS, "promo", half_written)
    m = make_manifest(tmp_path)
    seed_protected(m._run_dir)

    results = produce(m._run_dir, m, cfg_with_formats("promo"))

    assert results["promo"].startswith("skipped: builder mutated")
    assert "step_by_step.md" in results["promo"]


# -- manifest field ------------------------------------------------------------


def test_record_formats_merges_and_persists(tmp_path: Path):
    m = make_manifest(tmp_path)
    m.record_formats({"promo": PRODUCED})
    m.record_formats({"social": "skipped: no builder registered for 'social'"})
    loaded = Manifest.load(m._run_dir)
    assert loaded.formats == {
        "promo": PRODUCED,
        "social": "skipped: no builder registered for 'social'",
    }


def test_manifests_written_before_the_formats_field_still_load(tmp_path: Path):
    """Regression (#230): the new field is additive — old runs keep resuming."""
    m = Manifest.create(tmp_path / "old", _URL, "claude-code", "img")
    raw = m.model_dump()
    raw.pop("formats")
    assert Manifest.model_validate(raw).formats == {}


def test_formats_are_not_stages(tmp_path: Path):
    """STAGES is untouched: a format never gates or resumes the pipeline."""
    from readme2demo.manifest import STAGES

    assert STAGES == [
        "ingest", "agent", "normalize", "distill", "verify", "tutorial", "render",
    ]
    m = make_manifest(tmp_path)
    m.record_formats({"promo": "skipped: builder failed — RuntimeError: boom"})
    assert set(m.stages) == set(STAGES)
    assert "promo" not in m.stages


# -- orchestrator wiring -------------------------------------------------------

# Full-pipeline fakes: deterministic bytes, no timestamps or paths in any
# artifact, so two runs of the same pipeline are byte-identical by construction
# and the only variable under test is the extra format.

_COMMANDS_SH = "#!/usr/bin/env bash\nset -e\npython hello.py\n"
_GUIDE_DRAFT = "# Guide (draft)\n\n```bash\npython hello.py\n```\n"
_GUIDE_FINAL = "# Guide\n\n```bash\npython hello.py\n```\n\nHello, world!\n"
_TAPE = 'Set TypingSpeed 50ms\nType "python hello.py"\nEnter\n'
_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"deterministic frames" * 8


def install_fake_stages(monkeypatch, *, verified: bool = True) -> None:
    """Monkeypatch every stage so the pipeline runs offline and deterministically."""
    from readme2demo import distill as distill_mod
    from readme2demo import ingest as ingest_mod
    from readme2demo import normalize as normalize_mod
    from readme2demo import render as render_mod
    from readme2demo import tutorial as tutorial_mod
    from readme2demo import verify as verify_mod
    from readme2demo.types import (
        AgentResult,
        CommandLog,
        DistillOutput,
        Plan,
        SuccessCriteria,
        TutorialOutline,
        TutorialStep,
        VerifyReport,
    )

    plan = Plan(
        quickstart_summary="run the hello example",
        success_criteria=SuccessCriteria(command="python hello.py"),
    )
    outline = TutorialOutline(
        title="T", intro="i",
        steps=[TutorialStep(title="s", command="python hello.py", explanation="e")],
    )

    def fake_ingest(repo_url, run_dir, model, **kwargs):
        (run_dir / "plan.json").write_text(plan.model_dump_json())
        return plan, "abc1234", 0.0

    def fake_run_agent(run_dir, p, engine, c):
        (run_dir / "transcript.ndjson").write_text("")
        return run_dir / "transcript.ndjson"

    def fake_normalize(path, engine, run_dir):
        log = CommandLog(engine="claude-code", result=AgentResult(outcome="success"))
        (run_dir / "command_log.json").write_text(log.model_dump_json())
        return log

    def fake_distill(p, log, readme, run_dir, model, repo_url, feedback=""):
        (run_dir / "commands.sh").write_text(_COMMANDS_SH)
        (run_dir / "step_by_step.md").write_text(_GUIDE_DRAFT)
        (run_dir / "tutorial_outline.json").write_text(outline.model_dump_json())
        return DistillOutput(commands=["python hello.py"], tape=[], outline=outline), 0.0

    def fake_verify(run_dir, p, c):
        return VerifyReport(passed=verified, attempts=1, exit_code=0 if verified else 1)

    def fake_run_tutorial(run_dir, *args, **kwargs):
        (run_dir / "step_by_step.md").write_text(_GUIDE_FINAL)
        (run_dir / "tutorial.md").write_text("# tutorial\n")
        return 0.0

    def fake_build_tape(run_dir, log, repo_url, fallback):
        (run_dir / "demo.tape").write_text(_TAPE)
        return {"guide_steps": 1, "tape_steps": 1}

    def fake_render(run_dir, c, image=None):
        (run_dir / "demo.mp4").write_bytes(_MP4)
        return [run_dir / "demo.mp4"]

    monkeypatch.setattr(ingest_mod, "ingest", fake_ingest)
    monkeypatch.setattr("readme2demo.orchestrator.run_agent", fake_run_agent)
    monkeypatch.setattr(normalize_mod, "normalize", fake_normalize)
    monkeypatch.setattr(distill_mod, "distill", fake_distill)
    monkeypatch.setattr(verify_mod, "run_verify", fake_verify)
    monkeypatch.setattr(tutorial_mod, "run_tutorial", fake_run_tutorial)
    monkeypatch.setattr(distill_mod, "build_tape_from_step_by_step", fake_build_tape)
    monkeypatch.setattr(render_mod, "run_render", fake_render)
    monkeypatch.setattr(
        render_mod, "validate_outputs", lambda run_dir, **kw: [run_dir / "demo.mp4"]
    )


def test_regression_failing_format_leaves_the_run_byte_identical(
    tmp_path: Path, monkeypatch
):
    """Regression (#230): the acceptance criterion of the issue.

    Run the pipeline twice — once plain, once with an extra format whose builder
    raises — and demand that demo.mp4, step_by_step.md, commands.sh and
    demo.tape come out byte-identical, and that the run with the failing format
    still succeeds (every stage completed, verified verdict intact).
    """
    install_fake_stages(monkeypatch)

    baseline = Orchestrator.new_run(_URL, Config(runs_dir=tmp_path))
    baseline_manifest = baseline.run()

    dispatched: list[Path] = []

    def exploding_builder(run_dir, manifest, cfg):
        dispatched.append(run_dir)
        raise RuntimeError("compositor died mid-frame")

    monkeypatch.setitem(produce_mod._BUILDERS, "promo", exploding_builder)
    with_format = Orchestrator.new_run(
        _URL, cfg_with_formats("demo", "gif", "promo", runs_dir=tmp_path)
    )
    format_manifest = with_format.run()

    assert dispatched == [with_format.run_dir], "the builder must really have run"
    for name in PROTECTED_ARTIFACTS:
        assert (baseline.run_dir / name).read_bytes() == (
            with_format.run_dir / name
        ).read_bytes(), f"{name} differs when an extra format fails"

    # ... and the run itself is unaffected: nothing left to do, nothing failed.
    assert format_manifest.next_stage() is None
    assert format_manifest.verified is True
    assert all(
        rec.status == "completed" for rec in format_manifest.stages.values()
    )
    assert format_manifest.stages["render"].error is None
    # The failure is recorded as data, and only on the run that asked for it.
    assert format_manifest.formats["promo"].startswith(
        "skipped: builder failed — RuntimeError"
    )
    assert baseline_manifest.formats == {}


def test_produce_runs_after_render_on_the_unverified_path(tmp_path: Path, monkeypatch):
    """Regression (#230): the gate is reachable — render skips, produce records.

    _stage_render returns early on an unverified run, so the dispatch hook has
    to hang off the stage in run(), not off the render work itself; otherwise an
    unverified run would silently record nothing about its extra formats.
    """
    install_fake_stages(monkeypatch, verified=False)

    def builder(run_dir, manifest, cfg):
        raise AssertionError("must not be dispatched on an unverified run")

    monkeypatch.setitem(produce_mod._BUILDERS, "promo", builder)
    cfg = cfg_with_formats("demo", "gif", "promo", runs_dir=tmp_path, distill_retries=0)
    orch = Orchestrator.new_run(_URL, cfg)
    manifest = orch.run()

    assert manifest.verified is False
    assert manifest.stages["render"].status == "skipped"
    assert manifest.formats == {"promo": "skipped: replay unverified — no extra formats"}
    assert Manifest.load(orch.run_dir).formats == manifest.formats


def test_produce_failure_outside_a_builder_still_cannot_fail_the_run(
    tmp_path: Path, monkeypatch
):
    """Belt and braces: even a broken produce() only costs a warning line."""
    install_fake_stages(monkeypatch)

    def broken_produce(run_dir, manifest, cfg):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(produce_mod, "produce", broken_produce)
    orch = Orchestrator.new_run(_URL, Config(runs_dir=tmp_path))
    manifest = orch.run()

    assert manifest.next_stage() is None
    assert manifest.stages["render"].status == "completed"
    assert (orch.run_dir / "demo.mp4").read_bytes() == _MP4


def test_default_run_dispatches_and_records_nothing(tmp_path: Path, monkeypatch):
    """No --formats: produce() is inert, so today's runs are unchanged."""
    install_fake_stages(monkeypatch)
    orch = Orchestrator.new_run(_URL, Config(runs_dir=tmp_path))
    manifest = orch.run()
    assert manifest.formats == {}
    assert Manifest.load(orch.run_dir).formats == {}


def test_protected_artifact_list_is_the_grounding_set():
    """The four artifacts CLAUDE.md names as the grounding path, pinned.

    Widening this set is fine; silently narrowing it would quietly un-guard a
    published artifact, so the list is asserted exactly.
    """
    assert PROTECTED_ARTIFACTS == (
        "demo.mp4", "step_by_step.md", "commands.sh", "demo.tape",
    )
