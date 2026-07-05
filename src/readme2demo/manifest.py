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


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class StageRecord(BaseModel):
    status: StageStatus = "pending"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    cost_usd: float = 0.0
    meta: dict = Field(default_factory=dict)


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
    total_cost_usd: float = 0.0

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
        self.total_cost_usd = round(
            sum(r.cost_usd for r in self.stages.values()), 6
        )
        self.save()

    def stage_fail(self, name: str, error: str, **meta) -> None:
        rec = self.stages[name]
        rec.status = "failed"
        rec.finished_at = utcnow()
        rec.error = error
        rec.meta.update(meta)
        self.save()

    def stage_skip(self, name: str, reason: str = "") -> None:
        rec = self.stages[name]
        rec.status = "skipped"
        rec.finished_at = utcnow()
        if reason:
            rec.meta["reason"] = reason
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
