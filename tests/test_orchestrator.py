"""Tests for the manifest state machine and orchestrator control flow.

No docker, no network, no API — stages are monkeypatched.
"""

from pathlib import Path
import json

import pytest

from readme2demo import ingest as ingest_mod
from readme2demo.config import Config
from readme2demo.manifest import STAGES, Manifest, StageRecord
from readme2demo.orchestrator import Orchestrator, PipelineError, summarize, summarize_markdown
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


def test_regression_stage_fail_records_cost_and_updates_total(tmp_path: Path):
    """Regression (#103): a stage that pays and then fails must not report $0.00.

    The distiller can raise after two paid LLM calls (its grounding retry), so
    stage_fail accounts spend the same way stage_complete does.
    """
    m = Manifest.create(tmp_path / "rf", "https://github.com/x/y", "claude-code", "img")
    m.stage_start("ingest")
    m.stage_complete("ingest", cost_usd=0.01)
    m.stage_start("distill")
    m.stage_fail("distill", "Distiller produced ungrounded commands", cost_usd=0.04)

    loaded = Manifest.load(tmp_path / "rf")
    assert loaded.stages["distill"].status == "failed"
    assert loaded.stages["distill"].cost_usd == pytest.approx(0.04)
    assert loaded.total_cost_usd == pytest.approx(0.05)


def test_stage_fail_without_cost_leaves_total_unchanged(tmp_path: Path):
    """A failure with no recoverable spend must not perturb the total (#103)."""
    m = Manifest.create(tmp_path / "rg", "https://github.com/x/y", "claude-code", "img")
    m.stage_start("ingest")
    m.stage_complete("ingest", cost_usd=0.02)
    m.stage_start("agent")
    m.stage_fail("agent", "engine exited 127")

    assert m.stages["agent"].cost_usd == 0.0
    assert m.total_cost_usd == pytest.approx(0.02)


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


# -- StageRecord.duration_seconds (#200) ----------------------------------------
# A plain @property (not @computed_field) on purpose: pydantic only serializes
# computed fields, so this must never change manifest.json's on-disk shape.


def test_duration_seconds_completed_stage():
    """Regression: a completed stage reports elapsed wall-clock seconds."""
    rec = StageRecord(
        status="completed",
        started_at="2026-07-01T10:00:00+00:00",
        finished_at="2026-07-01T10:00:12.500000+00:00",
    )
    assert rec.duration_seconds == pytest.approx(12.5)


def test_duration_seconds_still_running_is_none():
    """Regression: a stage with started_at but no finished_at (still running,
    or the process died mid-stage) is an unknown duration, not zero."""
    rec = StageRecord(status="running", started_at="2026-07-01T10:00:00+00:00")
    assert rec.duration_seconds is None


def test_duration_seconds_skipped_without_start_is_none():
    """Regression: stage_skip sets finished_at without started_at when a stage
    (e.g. via --skip-video) never actually started. Duration is unknown, not
    a negative or zero number."""
    rec = StageRecord(status="skipped", finished_at="2026-07-01T10:00:00+00:00")
    assert rec.duration_seconds is None


def test_duration_seconds_pending_stage_is_none():
    rec = StageRecord()
    assert rec.duration_seconds is None


def test_duration_seconds_unparseable_timestamp_is_none():
    """Regression: a hand-edited or corrupt manifest shouldn't raise — an
    unparseable timestamp is an unknown duration."""
    rec = StageRecord(status="completed", started_at="not-a-date", finished_at="also-not-a-date")
    assert rec.duration_seconds is None


def test_duration_seconds_negative_span_is_none():
    """Regression: clock skew or a hand-edited manifest where finished_at
    precedes started_at must not report a negative duration."""
    rec = StageRecord(
        status="completed",
        started_at="2026-07-01T10:00:10+00:00",
        finished_at="2026-07-01T10:00:00+00:00",
    )
    assert rec.duration_seconds is None


def test_duration_seconds_zero_length_stage_is_not_none():
    """A genuinely sub-second stage (e.g. `normalize`) is real elapsed time,
    not "unknown" — must not be conflated with the None cases above."""
    rec = StageRecord(
        status="completed",
        started_at="2026-07-01T10:00:00+00:00",
        finished_at="2026-07-01T10:00:00+00:00",
    )
    assert rec.duration_seconds == 0.0


def test_duration_seconds_not_serialized_to_manifest_json(tmp_path: Path):
    """Regression: exposing duration via @computed_field would serialize it
    into every manifest.json write, changing the on-disk schema of a
    crash-safe state file for a cosmetic report feature. Must stay a plain
    property."""
    m = Manifest.create(tmp_path / "r5", "https://github.com/x/y", "claude-code", "img")
    m.stage_start("ingest")
    m.stage_complete("ingest")
    raw = json.loads((tmp_path / "r5" / "manifest.json").read_text())
    assert "duration_seconds" not in raw["stages"]["ingest"]


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


def test_regression_failed_distill_bills_its_llm_spend(tmp_path: Path, monkeypatch):
    """Regression (#103): a DistillError after paid calls lands on the manifest.

    The distiller's grounding retry means failure can arrive after two LLM
    calls. Before this fix the orchestrator called stage_fail without a cost,
    so the run that most needs a spend figure reported $0.00.
    """
    from readme2demo import distill as distill_mod
    from readme2demo.distill import DistillError
    from readme2demo.types import AgentResult, CommandLog

    cfg = Config(runs_dir=tmp_path)
    orch = Orchestrator.new_run("https://github.com/x/y", cfg)
    (orch.run_dir / "plan.json").write_text(make_plan().model_dump_json())
    (orch.run_dir / "command_log.json").write_text(
        CommandLog(
            engine="claude-code", result=AgentResult(outcome="success")
        ).model_dump_json()
    )
    for s in ("ingest", "agent", "normalize"):
        orch.manifest.stage_start(s)
        orch.manifest.stage_complete(s, cost_usd=0.01)

    def boom(*args, **kwargs):
        raise DistillError("ungrounded after retry", cost_usd=0.06)

    monkeypatch.setattr(distill_mod, "distill", boom)
    with pytest.raises(DistillError):
        orch.run()

    loaded = Manifest.load(orch.run_dir)
    assert loaded.stages["distill"].status == "failed"
    assert loaded.stages["distill"].cost_usd == pytest.approx(0.06)
    assert loaded.total_cost_usd == pytest.approx(0.09)  # 3 x 0.01 + 0.06


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


def test_dry_run_stops_after_ingest(tmp_path: Path, monkeypatch):
    # --dry-run: feasibility verdict lands in the manifest, every later stage
    # is skipped with a clear reason, and no agent stage ever starts.
    cfg = Config(runs_dir=tmp_path, dry_run=True)
    orch = Orchestrator.new_run("https://github.com/x/y", cfg)

    def fake_ingest(repo_url, run_dir, model, **kwargs):
        plan = make_plan(feasible=True)
        (run_dir / "plan.json").write_text(plan.model_dump_json())
        return plan, "abc1234", 0.005

    monkeypatch.setattr(ingest_mod, "ingest", fake_ingest)
    manifest = orch.run()
    assert manifest.stages["ingest"].status == "completed"
    for s in ("agent", "normalize", "distill", "verify", "render", "tutorial"):
        assert manifest.stages[s].status == "skipped"
        assert manifest.stages[s].meta.get("reason") == "dry-run stop"
    assert manifest.verified is False


def test_badge_written_even_when_tutorial_llm_fails(tmp_path: Path, monkeypatch):
    # badge.json is written before run_tutorial: a TutorialError from the LLM
    # polish pass must not be able to suppress the badge (issue #139) — an
    # unverified run always gets a loud red badge, never a missing file.
    import json

    from readme2demo import tutorial as tutorial_mod
    from readme2demo.tutorial import TutorialError
    from readme2demo.types import AgentResult, CommandLog, TutorialOutline, TutorialStep

    cfg = Config(runs_dir=tmp_path)
    orch = Orchestrator.new_run("https://github.com/x/y", cfg)
    (orch.run_dir / "plan.json").write_text(make_plan().model_dump_json())
    (orch.run_dir / "command_log.json").write_text(
        CommandLog(engine="claude-code", result=AgentResult(outcome="success"))
        .model_dump_json()
    )
    (orch.run_dir / "tutorial_outline.json").write_text(
        TutorialOutline(
            title="T", intro="i",
            steps=[TutorialStep(title="s", command="c", explanation="e")],
        ).model_dump_json()
    )

    def boom(*args, **kwargs):
        raise TutorialError("polish call failed")

    monkeypatch.setattr(tutorial_mod, "run_tutorial", boom)
    with pytest.raises(TutorialError):
        orch._stage_tutorial()
    doc = json.loads((orch.run_dir / "badge.json").read_text())
    assert doc["message"] == "unverified"
    assert doc["color"] == "red"


# -- summarize_markdown (#140) -------------------------------------------------
# Pure renderer for `report --markdown` / $GITHUB_STEP_SUMMARY: no filesystem,
# no LLM — the CLI pre-computes the artifact list via existence checks.


def make_report_manifest(**overrides) -> Manifest:
    data = {
        "run_id": "glow-20260710-162012-33fc72",
        "repo_url": "https://github.com/charmbracelet/glow",
        "commit_sha": "a531d7c9deadbeef",
        "engine": "claude-code",
        "verified": True,
        "total_cost_usd": 0.1234,
        "stages": {
            "ingest": {"status": "completed", "cost_usd": 0.0021},
            "agent": {"status": "completed", "cost_usd": 0.098},
            "verify": {"status": "completed"},
        },
        **overrides,
    }
    return Manifest.model_validate(data)


def test_summarize_markdown_verified_run_full_shape():
    md = summarize_markdown(make_report_manifest(), ["tutorial.md", "demo.mp4"])
    lines = md.splitlines()
    assert lines[0] == "## readme2demo — glow-20260710-162012-33fc72"
    # Badge line: verified verdict, repo @ 7-char commit, engine, total cost.
    badge = md.splitlines()[2]
    assert "**Verified: yes**" in badge
    assert "`https://github.com/charmbracelet/glow` @ `a531d7c`" in badge
    assert "engine `claude-code`" in badge
    assert "total cost $0.1234" in badge
    # Stages table: header + one row per recorded stage, with per-stage cost.
    assert "| Stage | Status | Cost (USD) | Duration | Notes |" in lines
    assert "| ingest | completed | 0.0021 | — |  |" in lines
    assert "| agent | completed | 0.0980 | — |  |" in lines
    assert "| verify | completed | 0.0000 | — |  |" in lines
    # Artifact list renders exactly what the caller passed.
    assert "**Artifacts**" in lines
    assert "- tutorial.md" in lines
    assert "- demo.mp4" in lines


def test_summarize_markdown_unverified_badge_is_loud():
    md = summarize_markdown(make_report_manifest(verified=False), [])
    assert "**Verified: NO**" in md
    assert "Verified: yes" not in md


def test_summarize_markdown_guide_only_run():
    # repo_url == "" must not render an empty ``@ `?``` fragment.
    md = summarize_markdown(
        make_report_manifest(repo_url="", commit_sha=None), []
    )
    assert "(guide-only run — no repository)" in md
    assert "@ `?`" not in md


def test_summarize_markdown_failed_stage_error_in_notes():
    m = make_report_manifest(
        verified=False,
        stages={"agent": {"status": "failed", "error": "exit 127: no engine"}},
    )
    md = summarize_markdown(m, [])
    assert "| agent | failed | 0.0000 | — | exit 127: no engine |" in md.splitlines()


def test_summarize_markdown_skip_reason_in_notes():
    m = make_report_manifest(
        stages={"render": {"status": "skipped", "meta": {"reason": "dry-run stop"}}},
    )
    md = summarize_markdown(m, [])
    assert "| render | skipped | 0.0000 | — | dry-run stop |" in md.splitlines()


def test_summarize_markdown_escapes_table_breaking_error_text():
    # Stage errors carry arbitrary shell output: pipes would split the cell,
    # newlines would end the row, and [brackets] must survive verbatim
    # (the Rich-markup cousin of the summarize regression).
    m = make_report_manifest(
        verified=False,
        stages={
            "agent": {
                "status": "failed",
                "error": "cmd | head failed\nsee [openai] extra\r\nline3",
            }
        },
    )
    md = summarize_markdown(m, [])
    rows = [ln for ln in md.splitlines() if ln.startswith("| agent |")]
    assert len(rows) == 1  # newlines collapsed — still one table row
    assert "\\|" in rows[0]  # pipe escaped, cell not split
    assert "[openai]" in rows[0]  # brackets survive verbatim
    assert rows[0].count(" | ") == 4  # exactly 5 cells


def test_summarize_markdown_no_artifacts_omits_section():
    md = summarize_markdown(make_report_manifest(verified=False, stages={}), [])
    assert "**Artifacts**" not in md


def test_summarize_markdown_known_duration_rendered():
    """Regression: a stage with real timestamps shows a real Duration cell,
    not the "—" unknown placeholder."""
    m = make_report_manifest(
        stages={
            "ingest": {
                "status": "completed",
                "cost_usd": 0.0021,
                "started_at": "2026-07-10T16:20:12+00:00",
                "finished_at": "2026-07-10T16:20:23.700000+00:00",
            },
        },
    )
    md = summarize_markdown(m, [])
    assert "| ingest | completed | 0.0021 | 11.7s |  |" in md.splitlines()


def test_summarize_human_report_shows_duration_column():
    m = make_report_manifest(
        stages={
            "ingest": {
                "status": "completed",
                "started_at": "2026-07-10T16:20:12+00:00",
                "finished_at": "2026-07-10T16:20:24+00:00",
            },
            "agent": {"status": "pending"},
        },
    )
    text = summarize(m)
    lines = text.splitlines()
    ingest_line = next(ln for ln in lines if ln.strip().startswith("ingest"))
    agent_line = next(ln for ln in lines if ln.strip().startswith("agent"))
    assert "12.0s" in ingest_line
    # A pending stage (no timestamps at all) is unknown, shown as "—".
    assert "—" in agent_line
