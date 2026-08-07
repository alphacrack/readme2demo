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

# _ASSIGN_RE is imported rather than re-declared for the reason failure class 5
# gives: two copies of a grounding regex drift apart the first time one of them
# learns something. distill does not import normalize, so this is cycle-free.
from readme2demo.distill import _ASSIGN_RE as _ASSIGN_PREFIX_RE
from readme2demo.engines.base import AgentEngine
from readme2demo.types import CommandLog, Phase, Plan

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


def validate_success_pattern(plan: Plan, log: CommandLog) -> tuple[bool, str]:
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


#: Shell metacharacters that change control flow, capture, or word splitting.
#: A reconciled literal may never contain one. The assertion interpolates the
#: command into ``$( ... 2>&1)``, so a single trailing ``;`` makes the null
#: command the last one in the substitution and ``$?`` reports 0 no matter what
#: the real command did — turning a false NEGATIVE fix into a false POSITIVE on
#: the definition of "verified". Chains also reopen segment-selection: a
#: candidate must be one whole simple command, never a piece of a longer one.
_SHELL_CONTROL_RE = re.compile(r"[;&|`\n]|\$\(")


def _exe_token(tokens: list[str]) -> str:
    """The program token: the first token that is not a ``NAME=value`` prefix."""
    for t in tokens:
        if not _ASSIGN_PREFIX_RE.match(t):
            return t
    return ""


def _at_least_as_specific(literal: str, want: str) -> bool:
    """True when ``literal`` names its program at least as precisely as ``want``.

    :func:`distill.normalize_cmd` is a SYMMETRIC equivalence, which is exactly
    right for grounding — both sides get the same lossy transform, so a false
    match needs two independently drifted strings. It is NOT sufficient here,
    because the winner gets EXECUTED: the transform is lossy in the tokens that
    decide *which program runs and what it does* (absolute path → basename,
    ``python3`` → ``python``, dropped sandbox flags). Substituting in that
    direction can silently run a different binary — e.g. a plan naming
    ``/work/.venv/bin/pytest`` replaced by a system ``pytest`` that passes while
    the venv the tutorial teaches is broken.

    So the substitution is a one-way partial order: only accept a literal that
    is equally or MORE specific than the plan's own command.
    """
    lt, wt = literal.split(), want.split()
    l_exe, w_exe = _exe_token(lt), _exe_token(wt)
    if not l_exe or not w_exe:
        return False
    # A plan that named an absolute program may only be replaced by that same
    # absolute program; a plan that named a bare one may gain a path.
    if w_exe.startswith("/") and l_exe != w_exe:
        return False
    # python3 -> python loses the interpreter the plan pinned; the reverse is fine.
    if w_exe == "python3" and l_exe != "python3":
        return False
    # Every flag the plan carried must survive. The literal may ADD flags (the
    # sandbox often requires --break-system-packages); it may never drop one.
    return {t for t in wt if t.startswith("-")} <= {t for t in lt if t.startswith("-")}


def reconcile_success_command(plan, log: CommandLog) -> tuple[bool, str]:
    """Replace the plan's success command with the literal form the run PROVED.

    ``success_criteria.command`` is written by the planner at ingest, BEFORE
    anything has run, so it is a guess about spelling. The agent then discovers
    what the sandbox actually accepts — ``python3`` not ``python``, pip needing
    ``--break-system-packages``, or a console script that pip put in
    ``~/.local/bin`` while that directory is not on ``PATH``, forcing an
    absolute path. The distilled STEPS already carry those proven forms
    (grounding picks them), but the success-criteria assertion emitted the
    planner's nominal string verbatim — so ``commands.sh`` could assert a
    command the fresh container cannot even resolve, and a run whose success
    criterion genuinely passed was reported UNVERIFIED
    (run readme2demo-20260806-180604: ``readme2demo report examples/toolhive``
    → ``command not found``, exit 127, while
    ``/home/demo/.local/bin/readme2demo report examples/toolhive`` had exited 0
    twice in the very same log).

    This edits the command that DEFINES "verified", so every rule below exists
    to make the swap unable to invent a pass:

    - only entries that exited **0** count (``None`` means the result was never
      observed — a killed or truncated run — not success), plus entries
      :func:`mark_findings_success` already proved are findings-successes;
    - the replacement must be equivalent under :func:`distill.normalize_cmd`,
      the same canonicalizer grounding uses;
    - it must be at least as SPECIFIC (:func:`_at_least_as_specific`), because
      unlike grounding this string is executed;
    - neither side may contain shell control characters, so a substitution can
      never alter what ``$?`` reports or smuggle in a chain segment;
    - multi-line (heredoc) commands are left alone entirely — they are
      prefix-matched everywhere else and are not safely substitutable.

    If the planner's own spelling ran successfully anywhere in the log, nothing
    changes. Returns ``(changed, reason)``.
    """
    from readme2demo.distill import normalize_cmd

    want = plan.success_criteria.command or ""
    if not want.strip() or "\n" in want or _SHELL_CONTROL_RE.search(want):
        return False, ""
    want_bare = _strip_stderr_merge(want)
    want_norm = normalize_cmd(want)

    proven: list[str] = []
    for e in log.entries:
        # exit_code None == never observed (unpaired tool_use, killed run).
        if e.exit_code != 0 and not e.findings_success:
            continue
        literal = _strip_stderr_merge(e.cmd)
        if not literal or _SHELL_CONTROL_RE.search(literal):
            continue
        if literal == want_bare:
            # The planner's own spelling ran and worked — nothing to reconcile.
            return False, ""
        if normalize_cmd(literal) != want_norm:
            continue
        if not _at_least_as_specific(literal, want):
            continue
        proven.append(literal)

    if not proven:
        return False, ""
    # The LAST proven form is the one the agent settled on after its retries.
    chosen = proven[-1]
    plan.success_criteria.command = chosen
    return True, (
        f"{want!r} was never run successfully in that exact form; using the "
        f"proven equivalent {chosen!r}"
    )


def _strip_stderr_merge(cmd: str) -> str:
    """Normalized-whitespace command without a ``2>&1`` merge.

    The assertion appends its own ``2>&1`` when it wraps the command, so a
    chosen literal must not carry one too.
    """
    return " ".join(cmd.split()).replace(" 2>&1", "").strip()


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


def mark_findings_success(plan: Plan, log: CommandLog) -> int:
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
