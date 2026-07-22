"""Tests for the manifest state machine and orchestrator control flow.

No docker, no network, no API — stages are monkeypatched.
"""

from pathlib import Path

import pytest

from readme2demo import ingest as ingest_mod
from readme2demo.config import Config
from readme2demo.manifest import STAGES, Manifest, StageRecord, stage_duration
from readme2demo.orchestrator import Orchestrator, PipelineError, summarize_markdown
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


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (StageRecord(started_at="2026-07-21T12:00:00+00:00", finished_at="2026-07-21T12:01:25+00:00"), 85.0),
        (StageRecord(finished_at="2026-07-21T12:01:25+00:00"), None),
        (StageRecord(started_at="2026-07-21T12:00:00+00:00"), None),
        (StageRecord(started_at="not-a-date", finished_at="2026-07-21T12:01:25+00:00"), None),
        (StageRecord(started_at="2026-07-21T12:01:25+00:00", finished_at="2026-07-21T12:00:00+00:00"), None),
    ],
)
def test_regression_stage_duration_handles_unknown_timestamps(record, expected):
    """Regression: report must not invent zero-duration stages from bad timestamps."""
    assert stage_duration(record) == expected


def test_regression_stage_duration_does_not_change_manifest_schema():
    """Regression: cosmetic report timing must never be persisted in manifest.json."""
    manifest = Manifest.model_validate(
        {
            "run_id": "schema-test",
            "stages": {
                "ingest": {
                    "status": "completed",
                    "started_at": "2026-07-21T12:00:00+00:00",
                    "finished_at": "2026-07-21T12:00:01+00:00",
                }
            },
        }
    )
    assert stage_duration(manifest.stages["ingest"]) == 1.0
    assert "duration_seconds" not in manifest.model_dump()["stages"]["ingest"]


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
            "agent": {
                "status": "completed", "cost_usd": 0.098,
                "started_at": "2026-07-21T12:00:00+00:00",
                "finished_at": "2026-07-21T12:01:25+00:00",
            },
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
    assert "| Stage | Status | Duration | Cost (USD) | Notes |" in lines
    assert "| ingest | completed | — | 0.0021 |  |" in lines
    assert "| agent | completed | 1m 25s | 0.0980 |  |" in lines
    assert "| verify | completed | — | 0.0000 |  |" in lines
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
    assert "| agent | failed | — | 0.0000 | exit 127: no engine |" in md.splitlines()


def test_summarize_markdown_skip_reason_in_notes():
    m = make_report_manifest(
        stages={"render": {"status": "skipped", "meta": {"reason": "dry-run stop"}}},
    )
    md = summarize_markdown(m, [])
    assert "| render | skipped | — | 0.0000 | dry-run stop |" in md.splitlines()


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
