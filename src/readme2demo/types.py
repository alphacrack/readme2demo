"""Shared data contracts for the readme2demo pipeline.

Every stage communicates through these models, serialized as JSON files in the
run directory. This module is the single source of truth — if a stage needs a
new field, it is added here first.

Run directory layout (see IMPLEMENTATION_PLAN.md):

    runs/<run-id>/
      manifest.json        # stage state machine (see manifest.py)
      plan.json            # Plan
      transcript.ndjson    # raw engine transcript (engine-specific format)
      command_log.json     # CommandLog
      commands.sh          # distilled minimal reproducible script
      demo.tape            # VHS tape
      tutorial_outline.json# TutorialOutline
      verify.log           # replay output from the verifier
      demo.mp4 / demo.gif  # rendered by VHS
      tutorial.md
      troubleshooting.md
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

Phase = Literal["explore", "setup", "demo", "fix", "unknown"]
Outcome = Literal["success", "blocked", "failed"]
SourceKind = Literal["git", "docs", "unsupported"]

# Sentinel markers the agent prompt instructs the agent to print.
SUCCESS_MARKER = "R2D_SUCCESS"
BLOCKED_MARKER = "BLOCKED:"
FIX_MARKER = "FIX:"
# "ADJUSTED_SUCCESS: <command> EXPECT: <regex>" — the agent may swap the
# planned success command for a nearby equivalent when sandbox infrastructure
# makes the original impossible (e.g. `--help` requiring a Docker daemon).
ADJUSTED_MARKER = "ADJUSTED_SUCCESS:"


class UrlVerdict(BaseModel):
    """Result of pure URL classification for future docs-site ingestion (#67).

    ``kind`` is the coarse class; ``repo_url`` is a cloneable repository root when
    ``kind == "git"`` (deep links are reduced to that root). ``reason`` is a short
    human-readable explanation suitable for error messages.
    """

    kind: SourceKind
    repo_url: Optional[str] = None
    reason: str = ""

class SuccessCriteria(BaseModel):
    """Machine-checkable definition of 'the quickstart works'.

    ``command`` is the final demo command; ``expected_pattern`` is a regex that
    must match its combined stdout/stderr. If ``expected_pattern`` is None,
    exit code 0 alone is the criterion.
    """

    command: str
    expected_pattern: Optional[str] = None
    description: str = ""


class Plan(BaseModel):
    """Output of the M1 planner pass (plan.json)."""

    project_type: str = "unknown"
    quickstart_summary: str
    # Repo-relative path of a repo-provided step-by-step guide
    # (step_by_step.md), when one exists. Set by ingest, not the LLM: the
    # guide takes precedence over the README for the planner AND the agent.
    guide_path: Optional[str] = None
    prereqs: list[str] = Field(default_factory=list)
    steps_expected: list[str] = Field(default_factory=list)
    success_criteria: SuccessCriteria
    blockers: list[str] = Field(default_factory=list)
    feasible: bool = True
    reasoning: str = ""


class CommandEntry(BaseModel):
    """One command the agent executed, normalized from any engine."""

    cmd: str
    cwd: Optional[str] = None
    exit_code: Optional[int] = None
    output: str = ""  # truncated: head + tail, ~4KB max
    duration_s: Optional[float] = None
    timestamp: Optional[str] = None  # ISO 8601
    phase: Phase = "unknown"
    # Findings tools (drift detectors, linters, scanners) exit NONZERO when
    # they find what the demo exists to show. Set by normalize when the
    # command matches the plan's success command and its output matches the
    # expected pattern — such entries count as successful everywhere.
    findings_success: bool = False


class FixMarker(BaseModel):
    """A deviation from the README the agent declared via ``FIX: ... BECAUSE: ...``."""

    what: str
    because: str = ""


class AgentResult(BaseModel):
    """Terminal metadata for the agent run."""

    outcome: Outcome
    blocked_reason: Optional[str] = None
    cost_usd: Optional[float] = None
    num_turns: Optional[int] = None
    duration_s: Optional[float] = None


class CommandLog(BaseModel):
    """Normalized transcript (command_log.json) — the pipeline's core contract.

    Produced by M4 from the raw engine transcript; consumed by M5/M6/M8.
    """

    schema_version: int = SCHEMA_VERSION
    engine: str
    entries: list[CommandEntry] = Field(default_factory=list)
    fixes: list[FixMarker] = Field(default_factory=list)
    file_edits: list[str] = Field(default_factory=list)  # paths the agent wrote/edited
    # Set when the agent printed ADJUSTED_SUCCESS; downstream stages must use
    # this command (and pattern, if given) instead of the plan's original.
    adjusted_success_command: Optional[str] = None
    adjusted_success_pattern: Optional[str] = None
    result: AgentResult

    def successful_commands(self) -> list[CommandEntry]:
        return [e for e in self.entries if e.exit_code == 0 or e.findings_success]


class TapeCommand(BaseModel):
    """One command in the VHS tape, chosen by the distiller."""

    cmd: str
    comment: Optional[str] = None  # rendered as a typed `# comment` line
    wait_pattern: Optional[str] = None  # VHS `Wait+Screen /pattern/`; falls back to sleep
    sleep_after_s: float = 1.0  # used when wait_pattern is None
    hide: bool = False  # wrap in Hide/Show (long boring output, e.g. installs)
    # For multi-line commands (heredocs): the individual lines, typed one per
    # Type/Enter so the file creation plays on camera. When set, the template
    # types these lines instead of ``cmd`` (a real shell reads a heredoc until
    # its terminator, so line-by-line typing works).
    lines: list[str] = Field(default_factory=list)


class TutorialStep(BaseModel):
    title: str
    command: str
    explanation: str
    expected_output: Optional[str] = None  # filled from verify.log, not the agent run


class TutorialOutline(BaseModel):
    """Output of the distiller consumed by the tutorial generator (M8)."""

    title: str
    intro: str
    prereqs: list[str] = Field(default_factory=list)
    steps: list[TutorialStep] = Field(default_factory=list)


class DistillOutput(BaseModel):
    """Everything M5 produces in one structured LLM response."""

    commands: list[str]  # becomes commands.sh, in order
    tape: list[TapeCommand]
    outline: TutorialOutline


class VerifyReport(BaseModel):
    """Result of the M6 replay (stored in manifest stage meta + verify.log)."""

    passed: bool
    attempts: int = 1
    exit_code: Optional[int] = None
    criteria_matched: Optional[bool] = None
    log_path: str = "verify.log"
