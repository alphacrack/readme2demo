"""Tests for the manifest state machine and orchestrator control flow.

No docker, no network, no API — stages are monkeypatched.
"""

from pathlib import Path

import pytest

from readme2demo import ingest as ingest_mod
from readme2demo.config import Config
from readme2demo.manifest import STAGES, Manifest
from readme2demo.orchestrator import Orchestrator, PipelineError
from readme2demo.types import Plan, SuccessCriteria


def make_plan(feasible: bool = True) -> Plan:
    return Plan(
        quickstart_summary="run the hello example",
        success_criteria=SuccessCriteria(
            command="python examples/hello.py", expected_pattern="Hello"
        ),
        feasible=feasible,
        blockers=[] if feasible else ["requires OPENAI_API_KEY"],
    )


# -- manifest ------------------------------------------------------------------


def test_manifest_roundtrip(tmp_path: Path):
    m = Manifest.create(tmp_path / "r1", "https://github.com/x/y", "claude-code", "img")
    m.stage_start("ingest")
    m.stage_complete("ingest", cost_usd=0.01, feasible=True)
    loaded = Manifest.load(tmp_path / "r1")
    assert loaded.stages["ingest"].status == "completed"
    assert loaded.stages["ingest"].cost_usd == 0.01
    assert loaded.total_cost_usd == 0.01
    assert loaded.next_stage() == "agent"


def test_manifest_next_stage_order(tmp_path: Path):
    m = Manifest.create(tmp_path / "r2", "https://github.com/x/y", "claude-code", "img")
    for s in STAGES:
        assert m.next_stage() == s
        m.stage_start(s)
        m.stage_complete(s)
    assert m.next_stage() is None


def test_manifest_reset_from(tmp_path: Path):
    m = Manifest.create(tmp_path / "r3", "https://github.com/x/y", "claude-code", "img")
    for s in STAGES:
        m.stage_start(s)
        m.stage_complete(s)
    m.verified = True
    m.reset_from("verify")
    assert m.next_stage() == "verify"
    assert m.verified is False
    assert m.stages["distill"].status == "completed"
    assert m.stages["render"].status == "pending"


def test_manifest_skip_counts_as_done(tmp_path: Path):
    m = Manifest.create(tmp_path / "r4", "https://github.com/x/y", "claude-code", "img")
    for s in STAGES[:-2]:
        m.stage_start(s)
        m.stage_complete(s)
    m.stage_skip("render", reason="--skip-video")
    assert m.next_stage() == "tutorial"


# -- orchestrator control flow ---------------------------------------------------


def test_infeasible_plan_stops_pipeline(tmp_path: Path, monkeypatch):
    cfg = Config(runs_dir=tmp_path)
    orch = Orchestrator.new_run("https://github.com/x/y", cfg)

    def fake_ingest(repo_url, run_dir, model, **kwargs):
        plan = make_plan(feasible=False)
        (run_dir / "plan.json").write_text(plan.model_dump_json())
        return plan, "abc1234", 0.005

    monkeypatch.setattr(ingest_mod, "ingest", fake_ingest)
    with pytest.raises(PipelineError, match="infeasible"):
        orch.run()
    assert orch.manifest.stages["ingest"].status == "failed"
    assert orch.manifest.stages["agent"].status == "skipped"
    assert orch.manifest.stages["tutorial"].status == "skipped"


def test_blocked_agent_skips_downstream(tmp_path: Path, monkeypatch):
    from readme2demo import normalize as normalize_mod
    from readme2demo.types import AgentResult, CommandLog

    cfg = Config(runs_dir=tmp_path)
    orch = Orchestrator.new_run("https://github.com/x/y", cfg)

    def fake_ingest(repo_url, run_dir, model, **kwargs):
        plan = make_plan()
        (run_dir / "plan.json").write_text(plan.model_dump_json())
        return plan, "abc1234", 0.005

    def fake_run_agent(run_dir, plan, engine, c):
        (run_dir / "transcript.ndjson").write_text("")
        return run_dir / "transcript.ndjson"

    def fake_normalize(path, engine, run_dir):
        log = CommandLog(
            engine="claude-code",
            result=AgentResult(outcome="blocked", blocked_reason="needs GPU"),
        )
        (run_dir / "command_log.json").write_text(log.model_dump_json())
        return log

    monkeypatch.setattr(ingest_mod, "ingest", fake_ingest)
    monkeypatch.setattr("readme2demo.orchestrator.run_agent", fake_run_agent)
    monkeypatch.setattr(normalize_mod, "normalize", fake_normalize)

    with pytest.raises(PipelineError, match="blocked"):
        orch.run()
    assert orch.manifest.stages["distill"].status == "skipped"
    assert orch.manifest.stages["render"].status == "skipped"


def test_resume_skips_completed_stages(tmp_path: Path, monkeypatch):
    cfg = Config(runs_dir=tmp_path)
    run_dir = tmp_path / "resume-run"
    m = Manifest.create(run_dir, "https://github.com/x/y", "claude-code", "img")
    for s in STAGES:
        m.stage_start(s)
        m.stage_complete(s)
    m.verified = True
    m.save()

    orch = Orchestrator.resume(run_dir, cfg)
    result = orch.run()  # nothing left to do — must be a no-op
    assert result.next_stage() is None


def test_new_run_guide_only_uses_guide_stem_and_empty_repo(tmp_path: Path):
    """Repo made optional: new_run(None) with a -s guide yields an empty
    manifest.repo_url and a run-id slugged from the guide's filename."""
    cfg = Config(runs_dir=tmp_path, step_by_step=tmp_path / "my_guide.md")
    orch = Orchestrator.new_run(None, cfg)
    assert orch.manifest.repo_url == ""
    assert orch.run_dir.name.startswith("my_guide-")


def test_new_run_with_repo_slugs_from_repo(tmp_path: Path):
    cfg = Config(runs_dir=tmp_path)
    orch = Orchestrator.new_run("https://github.com/owner/coolproj", cfg)
    assert orch.manifest.repo_url == "https://github.com/owner/coolproj"
    assert orch.run_dir.name.startswith("coolproj-")


def test_reset_from_after_verify_keeps_verdict(tmp_path: Path):
    """Regression: resume --from-stage render must not demote verified=True."""
    m = Manifest.create(tmp_path / "r5", "https://github.com/x/y", "claude-code", "img")
    for s in STAGES:
        m.stage_start(s)
        m.stage_complete(s)
    m.verified = True
    m.save()
    m.reset_from("render")
    assert m.verified is True
    m.reset_from("verify")
    assert m.verified is False
