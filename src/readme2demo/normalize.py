"""M4 — Transcript Normalizer.

Pure-Python, deterministic stage: delegates engine-specific parsing to the
engine's ``parse_transcript``, applies phase-tagging heuristics, and writes
the normalized ``command_log.json`` — the internal contract every downstream
stage (distill, verify, tutorial) consumes.

Phase tagging is heuristic, not LLM-based, and the tags are *hints* for the
distiller, not truth (see IMPLEMENTATION_PLAN.md M4).
"""

from __future__ import annotations

import re
from pathlib import Path

from readme2demo.engines.base import AgentEngine
from readme2demo.types import CommandLog, Phase

COMMAND_LOG_FILENAME = "command_log.json"

# Read-only inspection commands — dropped from demos by the distiller.
_EXPLORE_CMDS = frozenset(
    {"ls", "cat", "head", "tail", "grep", "rg", "find", "pwd", "which",
     "file", "tree", "wc"}
)

# Single-word setup commands (installers, environment prep).
_SETUP_CMDS = frozenset(
    {"apt", "apt-get", "pip", "pip3", "npm", "npx", "yarn", "pnpm", "cargo",
     "mkdir", "chmod", "export", "source"}
)

# Two-word setup prefixes checked against (first, second) tokens.
_SETUP_PAIRS = frozenset({("go", "install"), ("git", "clone")})

# Splits command chains; the LAST segment decides the tag.
_CHAIN_SPLIT_RE = re.compile(r"&&|;")

# curl/wget count as setup only when piping into a shell or fetching installers.
_DOWNLOAD_INSTALL_RE = re.compile(r"\|\s*(?:sudo\s+)?(?:ba|z)?sh\b|\binstall\b")

# Characters that make an `echo` non-trivial (redirects, pipes, substitution).
_NONTRIVIAL_ECHO_RE = re.compile(r"[|>`]|\$\(")


def validate_success_pattern(plan, log: CommandLog) -> tuple[bool, str]:
    """Reality-check ``plan.success_criteria.expected_pattern`` against the log.

    Patterns are LLM-authored (planner, or the agent's ``ADJUSTED_SUCCESS …
    EXPECT:``) and are sometimes wrong even about output the model just saw —
    e.g. ``\\brun\\b`` for output that only contains "running". A wrong
    pattern makes the verifier fail a genuinely working build.

    If the success command appears in the log with successful non-empty
    output and the pattern matches none of those outputs (or the pattern is
    invalid regex), the pattern is dropped — exit code 0 becomes the sole
    criterion. When the command can't be found in the log, the pattern is
    left alone (nothing to judge against).

    Returns ``(changed, reason)``.
    """
    pattern = plan.success_criteria.expected_pattern
    if not pattern:
        return False, ""

    def _norm(s: str) -> str:
        return " ".join(s.split())

    cmd = _norm(plan.success_criteria.command)
    outputs = [
        e.output
        for e in log.entries
        if e.exit_code == 0
        and e.output
        and (cmd == _norm(e.cmd) or _norm(e.cmd).endswith(cmd) or cmd in _norm(e.cmd))
    ]
    if not outputs:
        return False, ""
    try:
        rx = re.compile(pattern)
    except re.error:
        plan.success_criteria.expected_pattern = None
        return True, f"invalid regex {pattern!r} dropped"
    if any(rx.search(o) for o in outputs):
        return False, ""
    plan.success_criteria.expected_pattern = None
    return True, (
        f"pattern {pattern!r} never matched the command's real captured output"
        " — dropped (exit code 0 is now the criterion)"
    )


def repo_files_edited(log: CommandLog, repo_dir: Path) -> list[str]:
    """Repo source files the agent wrote/edited during its run.

    The agent works on a copy of the repo at /work; edits to files that exist
    in the pristine clone mean the agent patched the project to make a
    command pass — behavior that cannot survive the clean-clone verification
    replay and is forbidden by agent prompt rule 6. New files (venvs, config
    files the docs describe) are fine and not reported.
    """
    hits: list[str] = []
    for p in log.file_edits:
        if not p.startswith("/work"):
            continue
        rel = p[len("/work"):].lstrip("/")
        if rel and (repo_dir / rel).is_file():
            hits.append(rel)
    return sorted(set(hits))


def mark_findings_success(plan, log: CommandLog) -> int:
    """Mark nonzero-exit entries that ARE the successful demo (findings tools).

    Drift detectors, linters, and scanners exit nonzero when they find what
    the tutorial exists to demonstrate. An entry counts when its command
    matches the plan's success command (ignoring env prefixes, ``2>&1``, and
    leading chain segments) AND its output matches the expected pattern.
    Marked entries flow through ``successful_commands()`` into grounding,
    the tape, and output lookups. Returns the number of entries marked.
    """
    pattern = plan.success_criteria.expected_pattern
    if not pattern:
        return 0
    try:
        rx = re.compile(pattern)
    except re.error:
        return 0

    def _norm(s: str) -> str:
        return " ".join(s.split()).replace(" 2>&1", "")

    want = _norm(plan.success_criteria.command)
    marked = 0
    for e in log.entries:
        if e.exit_code in (0, None) or e.findings_success or not e.output:
            continue
        have = _norm(e.cmd)
        last_segment = _CHAIN_SPLIT_RE.split(have)[-1].strip()
        first_pipe = have.split("|", 1)[0].strip()
        if want in (have, last_segment, first_pipe) or last_segment.split(
            "|", 1
        )[0].strip() == want:
            if rx.search(e.output):
                e.findings_success = True
                marked += 1
    return marked


def normalize(transcript_path: Path, engine: AgentEngine, run_dir: Path) -> CommandLog:
    """Parse the raw transcript, tag phases, and write command_log.json.

    Returns the normalized :class:`CommandLog` (also persisted to
    ``run_dir/command_log.json``).
    """
    log = engine.parse_transcript(transcript_path)
    log = tag_phases(log)
    out_path = run_dir / COMMAND_LOG_FILENAME
    out_path.write_text(log.model_dump_json(indent=2), encoding="utf-8")
    return log


def tag_phases(log: CommandLog) -> CommandLog:
    """Tag each entry's phase with deterministic heuristics.

    Rules (applied to the LAST segment of ``a && b`` chains):

    * ``explore`` — ls/cat/head/tail/grep/rg/find/pwd/which/file/tree/wc,
      plus trivial ``echo`` (no pipes, redirects, or substitution).
    * ``setup`` — package managers and env prep (apt, pip, npm, cargo,
      git clone, python -m venv, source, mkdir, chmod, export, and
      curl/wget piped into an installer).
    * ``fix`` — any command whose *previous* entry exited nonzero
      (overrides setup/demo, but never explore).
    * ``demo`` — everything else (the actual run).
    """
    prev_exit: int | None = None
    for entry in log.entries:
        phase = _classify(entry.cmd)
        if prev_exit not in (None, 0) and phase != "explore":
            phase = "fix"
        entry.phase = phase
        prev_exit = entry.exit_code
    return log


def _classify(cmd: str) -> Phase:
    """Classify one command string (ignoring the fix rule) as a phase."""
    segments = [s.strip() for s in _CHAIN_SPLIT_RE.split(cmd) if s.strip()]
    if not segments:
        return "unknown"
    return _classify_segment(segments[-1])


def _classify_segment(segment: str) -> Phase:
    """Classify a single (chain-free) command segment."""
    tokens = _strip_prefixes(segment.split())
    if not tokens:
        return "unknown"
    first = tokens[0]
    second = tokens[1] if len(tokens) > 1 else ""

    if first in _EXPLORE_CMDS:
        return "explore"
    if first == "echo":
        return "explore" if not _NONTRIVIAL_ECHO_RE.search(segment) else "demo"
    if first in _SETUP_CMDS:
        return "setup"
    if (first, second) in _SETUP_PAIRS:
        return "setup"
    if first in ("python", "python3") and tokens[1:3] == ["-m", "venv"]:
        return "setup"
    if first in ("curl", "wget") and _DOWNLOAD_INSTALL_RE.search(segment):
        return "setup"
    return "demo"


def _strip_prefixes(tokens: list[str]) -> list[str]:
    """Drop leading ``sudo``, ``env``, and ``VAR=value`` assignments.

    So ``DEBIAN_FRONTEND=noninteractive apt-get install -y jq`` and
    ``sudo apt-get update`` both classify by ``apt-get``.
    """
    i = 0
    while i < len(tokens) and (
        tokens[i] in ("sudo", "env") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i])
    ):
        i += 1
    return tokens[i:]
