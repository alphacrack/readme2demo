"""Pipeline orchestrator: explicit state machine over the stages in manifest.STAGES.

Each stage is idempotent, reads/writes the run directory, and records its
transition in manifest.json — so ``resume`` picks up exactly where a run
stopped, and ``resume --from-stage`` re-runs only downstream stages.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console
from rich.markup import escape

from readme2demo import distill as distill_mod
from readme2demo import ingest as ingest_mod
from readme2demo import normalize as normalize_mod
from readme2demo import render as render_mod
from readme2demo import tutorial as tutorial_mod
from readme2demo import verify as verify_mod
from readme2demo.agent import run_agent
from readme2demo.config import Config
from readme2demo.engines import get_engine
from readme2demo.manifest import Manifest, new_run_id
from readme2demo.types import CommandLog, DistillOutput, Plan, TutorialOutline

console = Console()


class PipelineError(RuntimeError):
    """A stage failed in a way the orchestrator cannot recover from."""


class Orchestrator:
    def __init__(self, run_dir: Path, cfg: Config, manifest: Manifest):
        self.run_dir = run_dir
        self.cfg = cfg
        self.manifest = manifest
        self._distill_output: Optional[DistillOutput] = None

    # -- constructors ---------------------------------------------------------

    @classmethod
    def new_run(cls, repo_url: Optional[str], cfg: Config) -> "Orchestrator":
        # repo_url is optional: a guide-only run (``-s`` with no repo) passes
        # None/"". The run-id slug then falls back to the guide's file stem.
        repo_url = repo_url or ""
        fallback = cfg.step_by_step.stem if cfg.step_by_step else "guide"
        # Absolute: docker -v mounts and VHS renders need real host paths.
        run_dir = (cfg.runs_dir / new_run_id(repo_url, fallback=fallback)).resolve()
        manifest = Manifest.create(run_dir, repo_url, cfg.engine, cfg.base_image)
        return cls(run_dir, cfg, manifest)

    @classmethod
    def resume(cls, run_dir: Path, cfg: Config,
               from_stage: Optional[str] = None) -> "Orchestrator":
        run_dir = run_dir.resolve()
        manifest = Manifest.load(run_dir)
        if from_stage:
            manifest.reset_from(from_stage)
        return cls(run_dir, cfg, manifest)

    # -- artifact accessors (lazy-loaded from the run dir) --------------------

    def _plan(self) -> Plan:
        return Plan.model_validate_json((self.run_dir / "plan.json").read_text())

    def _log(self) -> CommandLog:
        return CommandLog.model_validate_json(
            (self.run_dir / "command_log.json").read_text()
        )

    def _outline(self) -> TutorialOutline:
        return TutorialOutline.model_validate_json(
            (self.run_dir / "tutorial_outline.json").read_text()
        )

    def _readme_text(self) -> str:
        repo = self.run_dir / "repo"
        for candidate in sorted(repo.glob("README*")):
            try:
                return candidate.read_text(errors="replace")[:8192]
            except OSError:
                continue
        return ""

    # -- stages ----------------------------------------------------------------

    def _stage_ingest(self) -> None:
        plan, sha, cost = ingest_mod.ingest(
            self.manifest.repo_url, self.run_dir, self.cfg.model,
            guide_file=self.cfg.step_by_step,
        )
        self.manifest.commit_sha = sha
        self.manifest.stage_complete("ingest", cost_usd=cost, feasible=plan.feasible)
        if not plan.feasible:
            for s in ("agent", "normalize", "distill", "verify", "render", "tutorial"):
                self.manifest.stage_skip(s, reason="plan marked infeasible")
            raise PipelineError(
                "Planner marked this repo infeasible: "
                + ("; ".join(plan.blockers) or plan.reasoning or "no reason given")
            )

    def _stage_agent(self) -> None:
        engine = get_engine(self.cfg.engine)
        run_agent(self.run_dir, self._plan(), engine, self.cfg)
        self.manifest.stage_complete("agent")

    def _stage_normalize(self) -> None:
        engine = get_engine(self.cfg.engine)
        log = normalize_mod.normalize(
            self.run_dir / "transcript.ndjson", engine, self.run_dir
        )
        adjusted = None
        plan = self._plan()
        plan_dirty = False
        if log.adjusted_success_command:
            # The agent swapped the planned success command for one that works
            # without infrastructure missing in the sandbox (ADJUSTED_SUCCESS
            # marker). Persist it into plan.json so distill/verify/tutorial
            # all use the command that was actually proven.
            plan.success_criteria.command = log.adjusted_success_command
            plan.success_criteria.expected_pattern = log.adjusted_success_pattern
            plan.success_criteria.description += " (adjusted by agent — see FIX notes)"
            adjusted = log.adjusted_success_command
            plan_dirty = True
            # escape(): shell commands often contain [ -f x ]-style brackets
            # that Rich would parse as markup and swallow.
            console.print(
                f"[yellow]Success command adjusted by agent:[/] {escape(adjusted)}"
            )
        # Reality-check the (LLM-authored) expected pattern against captured
        # output — a wrong pattern would fail the verify of a working build.
        pattern_changed, reason = normalize_mod.validate_success_pattern(plan, log)
        if pattern_changed:
            plan_dirty = True
            # escape(): the reason quotes regexes ([0-9]+ etc.) — see above.
            console.print(f"[yellow]Success pattern corrected:[/] {escape(reason)}")
        if plan_dirty:
            (self.run_dir / "plan.json").write_text(plan.model_dump_json(indent=2))
        # Findings tools: a nonzero-exit demo command whose output matches the
        # expected pattern IS the success — mark it and persist so grounding,
        # the tape, and output lookups all see it.
        marked = normalize_mod.mark_findings_success(plan, log)
        if marked:
            (self.run_dir / "command_log.json").write_text(
                log.model_dump_json(indent=2)
            )
            console.print(
                f"[dim]{marked} findings-tool command(s) reclassified as "
                "successful (nonzero exit + expected pattern matched)[/]"
            )
        # Agent-cheat detection: edits to repo source can't survive the
        # pristine-clone replay (prompt rule 6). Surface it NOW, not as a
        # mysterious verify failure later.
        edited = normalize_mod.repo_files_edited(log, self.run_dir / "repo")
        if edited:
            console.print(
                "[red]⚠ Agent modified repo source files (verification will "
                "not reproduce this):[/] " + escape(", ".join(edited[:5]))
            )
        # Guide mode: every step must at least have been ATTEMPTED. An
        # unattempted step can never be grounded, so it silently vanishes from
        # the video — surface the gap here instead.
        unattempted: list[str] = []
        if plan.guide_path:
            guide_file = self.run_dir / "repo" / plan.guide_path
            if guide_file.is_file():
                from readme2demo.distill import normalize_cmd, parse_guide_steps

                attempted = {normalize_cmd(e.cmd) for e in log.entries}
                attempted |= {
                    a.split("|", 1)[0].strip() for a in attempted if "|" in a
                }
                for _, cmd in parse_guide_steps(
                    guide_file.read_text(encoding="utf-8", errors="replace")
                ):
                    norm = normalize_cmd(cmd)
                    if norm not in attempted and not norm.startswith(
                        ("cd ", "git clone")
                    ):
                        unattempted.append(cmd)
                if unattempted:
                    console.print(
                        f"[red]⚠ Agent never attempted {len(unattempted)} guide "
                        "step(s) — they cannot appear in the video:[/]"
                    )
                    for c in unattempted:
                        console.print(f"[red]    {escape(c[:100])}[/]")
        cost = log.result.cost_usd or 0.0
        self.manifest.stage_complete(
            "normalize",
            cost_usd=cost,
            outcome=log.result.outcome,
            commands=len(log.entries),
            adjusted_success=adjusted,
            source_modified=edited or None,
            guide_steps_unattempted=unattempted or None,
        )
        if log.result.outcome == "blocked":
            for s in ("distill", "verify", "render", "tutorial"):
                self.manifest.stage_skip(s, reason=log.result.blocked_reason or "blocked")
            raise PipelineError(f"Agent blocked: {log.result.blocked_reason}")
        if log.result.outcome == "failed":
            raise PipelineError(
                "Agent run failed (max turns / timeout). "
                "Inspect transcript.ndjson; retry with a higher --max-turns."
            )
        if self.cfg.budget_usd and cost > self.cfg.budget_usd:
            raise PipelineError(
                f"Agent cost ${cost:.2f} exceeded budget ${self.cfg.budget_usd:.2f}"
            )

    def _stage_distill(self, feedback: str = "") -> None:
        out, cost = distill_mod.distill(
            self._plan(), self._log(), self._readme_text(),
            self.run_dir, self.cfg.model, self.manifest.repo_url,
            feedback=feedback,
        )
        self._distill_output = out
        self.manifest.stage_complete("distill", cost_usd=cost)

    def _stage_verify(self) -> None:
        plan = self._plan()
        report = verify_mod.run_verify(self.run_dir, plan, self.cfg)
        attempts_left = self.cfg.distill_retries
        if not report.passed and self.manifest.stages["normalize"].meta.get(
            "source_modified"
        ):
            # The distiller cannot fix this class of failure: the agent's
            # success depended on source patches that don't exist in the
            # pristine clone. Don't burn an LLM call pretending otherwise.
            attempts_left = 0
            console.print(
                "[red]Verify failed and the agent had modified repo source — "
                "skipping the distiller retry (it cannot help). The success "
                "path must work on the repo AS PUBLISHED; re-run the agent "
                "stage with: readme2demo resume <run> --from-stage agent[/]"
            )
        while not report.passed and attempts_left > 0:
            attempts_left -= 1
            console.print("[yellow]Verify failed — feeding log back to distiller[/]")
            feedback = verify_mod.verify_feedback(self.run_dir)
            self.manifest.stages["distill"].status = "pending"
            self.manifest.save()
            self.manifest.stage_start("distill")
            self._stage_distill(feedback=feedback)
            report = verify_mod.run_verify(self.run_dir, plan, self.cfg)
        self.manifest.verified = report.passed
        if report.passed:
            self.manifest.stage_complete(
                "verify", attempts=report.attempts, exit_code=report.exit_code
            )
        else:
            # Not fatal: artifacts ship clearly labeled UNVERIFIED.
            self.manifest.stage_complete(
                "verify", attempts=report.attempts,
                exit_code=report.exit_code, passed=False,
            )
            console.print(
                "[red]Replay did not pass — outputs will be labeled UNVERIFIED[/]"
            )

    def _stage_render(self) -> None:
        if self.cfg.skip_video:
            self.manifest.stage_skip("render", reason="--skip-video")
            return
        if not self.manifest.verified:
            # A demo video of an unverified script would be misleading (and
            # its commands likely fail on camera). Tutorial still ships,
            # loudly labeled UNVERIFIED.
            self.manifest.stage_skip("render", reason="replay unverified — no video")
            return
        # Build the tape from the FINALIZED step_by_step.md (written by the
        # tutorial stage) so the video provably follows the published guide.
        out = self._distill_output
        fallback = out.tape if out else []
        coverage = distill_mod.build_tape_from_step_by_step(
            self.run_dir, self._log(), self.manifest.repo_url, fallback
        )
        render_mod.run_render(self.run_dir, self.cfg)
        artifacts = render_mod.validate_outputs(self.run_dir)
        self.manifest.stage_complete(
            "render",
            artifacts=[p.name for p in artifacts],
            tape_coverage=f"{coverage['tape_steps']}/{coverage['guide_steps']} guide steps",
        )

    def _stage_tutorial(self) -> None:
        plan = self._plan()
        cost = tutorial_mod.run_tutorial(
            self.run_dir,
            plan,
            self._log(),
            self._outline(),
            self.cfg.model,
            verified=self.manifest.verified,
            base_image=self.cfg.base_image,
            commit_sha=self.manifest.commit_sha,
            # Repo already ships its own guide → don't generate a rival one.
            generate_step_by_step=plan.guide_path is None,
            repo_url=self.manifest.repo_url,
        )
        self.manifest.stage_complete(
            "tutorial",
            cost_usd=cost,
            step_by_step="generated" if plan.guide_path is None else "repo-provided",
        )

    # -- driver ----------------------------------------------------------------

    def run(self) -> Manifest:
        handlers: dict[str, Callable[[], None]] = {
            "ingest": self._stage_ingest,
            "agent": self._stage_agent,
            "normalize": self._stage_normalize,
            "distill": self._stage_distill,
            "verify": self._stage_verify,
            "render": self._stage_render,
            "tutorial": self._stage_tutorial,
        }
        while (stage := self.manifest.next_stage()) is not None:
            console.print(f"[bold cyan]▶ {stage}[/] ({escape(self.run_dir.name)})")
            self.manifest.stage_start(stage)
            try:
                handlers[stage]()
            except PipelineError as e:
                self.manifest.stage_fail(stage, str(e))
                raise
            except Exception as e:  # noqa: BLE001 — record, then re-raise
                self.manifest.stage_fail(stage, f"{type(e).__name__}: {e}")
                raise

            # Έλεγχος για dry-run αμέσως μετά την επιτυχrunning του ingest στάδιου
            if stage == "ingest" and getattr(self.cfg, "dry_run", False):
                console.print("\n[bold yellow]ℹ Dry-run mode active. Stopping after ingest and planning.[/]")
                # Μαρκάρουμε τα επόμενα στάδια ως skipped με σαφή αιτιολογία
                for s in ("agent", "normalize", "distill", "verify", "render", "tutorial"):
                    if self.manifest.stages[s].status != "completed":
                        self.manifest.stage_skip(s, reason="dry-run stop")
                break

        return self.manifest


def summarize(manifest: Manifest) -> str:
    """Human-readable run report for ``readme2demo report``."""
    repo_line = (
        f"{manifest.repo_url} @ {(manifest.commit_sha or '?')[:7]}"
        if manifest.repo_url
        else "(guide-only run — no repository)"
    )
    lines = [
        f"run:      {manifest.run_id}",
        f"repo:     {repo_line}",
        f"engine:   {manifest.engine}",
        f"verified: {'yes' if manifest.verified else 'NO'}",
        f"cost:     ${manifest.total_cost_usd:.4f}",
        "stages:",
    ]
    for name, rec in manifest.stages.items():
        extra = f" — {rec.error}" if rec.error else ""
        meta = ""
        if rec.meta:
            meta = " " + json.dumps(rec.meta, default=str)
        lines.append(f"  {name:<10} {rec.status:<10}{meta}{extra}")
    return "\n".join(lines)
