"""Run manifest: the crash-safe state machine over pipeline stages.

Every stage transition is persisted with an atomic write (tmp + rename), so
``readme2demo resume`` can pick up exactly where a run stopped.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

# Order matters: the orchestrator executes stages in this sequence.
# tutorial runs BEFORE render: step_by_step.md is finalized (with verified
# outputs) first, then the demo video is built to follow that published guide.
STAGES = ["ingest", "agent", "normalize", "distill", "verify", "tutorial", "render"]

StageStatus = Literal["pending", "running", "completed", "failed", "skipped"]

MANIFEST_FILENAME = "manifest.json"

# Filenames that only a fresh-container replay may produce. A derived record
# must never claim one of these even by typo.
PROTECTED_VERIFIED_ARTIFACTS = frozenset(
    {"commands.sh", "step_by_step.md", "demo.tape", "demo.mp4"}
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StageRecord(BaseModel):
    status: StageStatus = "pending"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    cost_usd: float = 0.0
    meta: dict = Field(default_factory=dict)


class DerivedArtifact(BaseModel):
    """A run-dir file produced by PARSING source, not by executing anything.

    Never "verified": nothing ran. The fields exist so a reader can reproduce
    the artifact — tool + tool_version against source_commit_sha.
    """

    path: str
    stage: str
    tool: str
    tool_version: str
    source_commit_sha: str
    produced_at: str
    note: str = ""


def stage_duration(record: StageRecord) -> Optional[float]:
    """Return a stage's final-attempt duration in seconds, if it is knowable.

    Missing, malformed, or clock-skewed timestamps are deliberately unknown
    rather than zero: a skipped or still-running stage did not take zero time.
    This remains a plain helper so duration never changes the manifest schema.
    """
    if not record.started_at or not record.finished_at:
        return None
    try:
        seconds = (
            datetime.fromisoformat(record.finished_at)
            - datetime.fromisoformat(record.started_at)
        ).total_seconds()
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


class Manifest(BaseModel):
    run_id: str
    # Empty string == a guide-only run (no repository; the -s/--step-by-step
    # guide is the sole source). Kept as a plain str (not Optional) so existing
    # manifests and the summarize/report paths need no None-handling.
    repo_url: str = ""
    commit_sha: Optional[str] = None
    engine: str = "claude-code"
    base_image: str = ""
    created_at: str = Field(default_factory=utcnow)
    stages: dict[str, StageRecord] = Field(
        default_factory=lambda: {s: StageRecord() for s in STAGES}
    )
    verified: bool = False
    derived: list[DerivedArtifact] = Field(default_factory=list)
    total_cost_usd: float = 0.0
    # Extra output formats dispatched AFTER render by produce.produce (#230):
    # name -> "produced" | "skipped: <reason>". Formats are not stages — they
    # are absent from STAGES, are not resumable, and never gate the run — so
    # they get their own field instead of a StageRecord. Defaults to empty, so
    # manifests written before this field load unchanged.
    formats: dict[str, str] = Field(default_factory=dict)
    # LLM spend a format builder incurred, name -> USD (#170: the promo cut pays
    # for a grounded scene plan). A format is not a stage and has no StageRecord
    # to carry cost_usd, so the money gets its own map — and it is summed into
    # total_cost_usd exactly like a stage's, because a paid failure that reports
    # $0.00 is the bug #103/#209 exist to prevent. Additive: old manifests load.
    format_costs: dict[str, float] = Field(default_factory=dict)

    # -- persistence ---------------------------------------------------------

    _run_dir: Optional[Path] = None  # set by load/create; excluded from dump

    model_config = {"ignored_types": ()}

    @classmethod
    def create(cls, run_dir: Path, repo_url: str = "", engine: str = "claude-code",
               base_image: str = "") -> "Manifest":
        m = cls(
            run_id=run_dir.name,
            repo_url=repo_url,
            engine=engine,
            base_image=base_image,
        )
        m._run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        m.save()
        return m

    @classmethod
    def load(cls, run_dir: Path) -> "Manifest":
        raw = json.loads((run_dir / MANIFEST_FILENAME).read_text())
        m = cls.model_validate(raw)
        m._run_dir = run_dir
        return m

    def save(self) -> None:
        assert self._run_dir is not None, "Manifest not bound to a run dir"
        target = self._run_dir / MANIFEST_FILENAME
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(self.model_dump_json(indent=2))
        os.replace(tmp, target)

    # -- stage transitions ---------------------------------------------------

    def _recompute_total(self) -> None:
        """Re-derive ``total_cost_usd`` from every source of spend in the run.

        Stage records AND :attr:`format_costs`: the total is recomputed (never
        incremented) on each transition, so a source left out of this sum is a
        source of money that silently disappears the next time any stage
        completes.
        """
        self.total_cost_usd = round(
            sum(r.cost_usd for r in self.stages.values())
            + sum(self.format_costs.values()),
            6,
        )

    def stage_start(self, name: str) -> None:
        rec = self.stages[name]
        rec.status = "running"
        rec.started_at = utcnow()
        rec.error = None
        self.save()

    def stage_complete(self, name: str, cost_usd: float = 0.0, **meta) -> None:
        rec = self.stages[name]
        rec.status = "completed"
        rec.finished_at = utcnow()
        rec.cost_usd += cost_usd
        rec.meta.update(meta)
        self._recompute_total()
        self.save()

    def stage_fail(self, name: str, error: str, cost_usd: float = 0.0, **meta) -> None:
        """Mark a stage failed, accounting any spend it incurred before failing.

        Mirrors :meth:`stage_complete`'s cost handling: a stage that pays for
        LLM calls and *then* raises still spent that money, and the run's
        total must say so. Callers pass what they know; the default of 0.0
        keeps failures with no recoverable cost unchanged.
        """
        rec = self.stages[name]
        rec.status = "failed"
        rec.finished_at = utcnow()
        rec.error = error
        rec.cost_usd += cost_usd
        rec.meta.update(meta)
        self._recompute_total()
        self.save()

    def stage_skip(self, name: str, reason: str = "") -> None:
        rec = self.stages[name]
        rec.status = "skipped"
        rec.finished_at = utcnow()
        if reason:
            rec.meta["reason"] = reason
        self.save()

    # -- post-render outputs ---------------------------------------------------

    def record_formats(self, results: dict[str, str]) -> None:
        """Merge post-render format outcomes into the manifest and persist.

        Merges rather than replaces so a ``resume`` that re-dispatches one
        format does not erase what an earlier pass recorded for the others.
        """
        self.formats.update(results)
        self.save()

    def record_derived(
        self,
        *,
        path: str,
        stage: str,
        tool: str,
        tool_version: str,
        source_commit_sha: str,
        note: str = "",
    ) -> None:
        """Record a source-derived artifact and persist it atomically.

        Raises ``ValueError`` when ``path`` is a protected verified artifact
        or when ``stage`` is not a pipeline stage. Parsed artifacts are a
        weaker claim than a fresh-container replay, so the contract refuses
        to let them inherit verified filenames.
        """
        if path in PROTECTED_VERIFIED_ARTIFACTS:
            raise ValueError(
                f"{path} is a verified artifact; derived artifacts may not use it"
            )
        if stage not in STAGES:
            raise ValueError(f"Unknown derived-artifact stage: {stage}")
        artifact = DerivedArtifact(
            path=path,
            stage=stage,
            tool=tool,
            tool_version=tool_version,
            source_commit_sha=source_commit_sha,
            produced_at=utcnow(),
            note=note,
        )
        self.derived.append(artifact)
        self.save()

    def derived_provenance_line(self, artifact: DerivedArtifact) -> str:
        """Return the shared provenance sentence for one derived artifact."""
        repo = self.repo_url or "<repo>"
        sha7 = artifact.source_commit_sha[:7]
        date = artifact.produced_at[:10]
        return (
            f"Derived from {repo} @ {sha7} by {artifact.tool} "
            f"{artifact.tool_version} on {date} — parsed, not executed."
        )

    def record_format_cost(self, name: str, cost_usd: float) -> None:
        """Account LLM spend a format builder incurred, and persist.

        The format counterpart of :meth:`stage_complete` /
        :meth:`stage_fail`'s ``cost_usd``, and it exists for the same reason:
        the promo builder pays for a scene plan (and possibly a grounding
        retry) BEFORE it can fail, so recording money only on the success path
        would report $0.00 for a run that really spent it (#103/#209).

        Accumulates rather than replaces — a format re-dispatched by a
        ``resume`` pays again, exactly as a re-run stage does — and a builder
        that spent nothing records a plain ``0.0`` rather than nothing at all,
        so "cost not recorded" and "cost was zero" stay distinguishable.
        """
        self.format_costs[name] = round(self.format_costs.get(name, 0.0) + cost_usd, 6)
        self._recompute_total()
        self.save()

    def next_stage(self) -> Optional[str]:
        """First stage that is not completed/skipped, or None if done."""
        for s in STAGES:
            if self.stages[s].status not in ("completed", "skipped"):
                return s
        return None

    def reset_from(self, stage: str) -> None:
        """Mark ``stage`` and everything after it pending (for resume --from-stage).

        The ``verified`` verdict is cleared only when the verify stage itself
        is being re-run — resetting from render/tutorial must not demote a
        passing verification.
        """
        idx = STAGES.index(stage)
        for s in STAGES[idx:]:
            self.stages[s] = StageRecord()
        if idx <= STAGES.index("verify"):
            self.verified = False
        self.derived = [
            artifact
            for artifact in self.derived
            if STAGES.index(artifact.stage) < idx
        ]
        self.save()


def new_run_id(repo_url: str, fallback: str = "run") -> str:
    """Build a ``<slug>-<timestamp>-<rand>`` run id.

    The slug is the repo name when a URL is given; for a guide-only run
    (empty ``repo_url``) it falls back to ``fallback`` (e.g. the guide's file
    stem), then to ``"run"``.
    """
    slug = ""
    if repo_url:
        slug = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")[:30]
    slug = slug or fallback[:30] or "run"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{slug}-{stamp}-{uuid.uuid4().hex[:6]}"
