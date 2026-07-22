"""M5 — Distiller.

Turns the messy successful agent run (``command_log.json``) into the minimal
clean reproduction path: ``commands.sh``, ``demo.tape``, and
``tutorial_outline.json``.

The single most important correctness rule in the system lives here — the
**grounding rule**, enforced in code, not by prompt: every command the
distiller emits must fuzzy-match a command that *actually succeeded* in the
agent's log. An LLM writing plausible-but-untested commands is exactly the
failure mode this project exists to eliminate.

Fuzzy matching is deliberately conservative (see :func:`is_grounded`):
whitespace normalization, env-var-assignment prefixes, segments of chained
commands, plus unconditional acceptance of ``cd`` navigation and
comment/blank lines. Anything else is a violation and triggers one retry,
then :class:`DistillError`.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from textwrap import indent
from typing import Optional

from jinja2 import Environment, FileSystemLoader

from readme2demo import llm
from readme2demo.types import CommandLog, DistillOutput, Plan, TapeCommand

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_README_MAX_BYTES = 8_192
_LOG_OUTPUT_SNIPPET_CHARS = 700

# A leading `NAME=value ` env-var assignment (value may be quoted).
_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S*)\s+")

# Chain separators for segment matching. Naive split (does not respect
# quoting) — acceptable for grounding, where a false negative just costs one
# distiller retry and a false positive is practically impossible for real
# commands.
_CHAIN_SPLIT_RE = re.compile(r"\s*(?:&&|;)\s*")

# Heredoc operator: `cat > file <<'EOF'` / `<<-EOF` / `<< "END"`.
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


def heredoc_prefix(cmd: str) -> Optional[str]:
    """The command text up to (excluding) a heredoc operator, or None.

    Grounding compares heredoc commands by PREFIX only (`cat >
    /tmp/demo/main.tf`): exact-matching a multi-line file body is hopeless —
    any whitespace drift in 15 lines of file content fails the match — and
    the verify replay still gates the actual content (wrong file body →
    demo assertion fails → distiller feedback loop).
    """
    m = _HEREDOC_RE.search(cmd)
    if not m:
        return None
    return " ".join(cmd[: m.start()].split()).strip()


class DistillError(RuntimeError):
    """Raised when the distiller cannot produce a fully grounded output.

    Carries ``cost_usd`` so spend already incurred before the failure is not
    lost: the grounding retry means this error can arrive *after* two paid
    LLM calls, and the orchestrator records it against the failed stage.
    """

    def __init__(self, *args, cost_usd: float = 0.0) -> None:
        super().__init__(*args)
        self.cost_usd = cost_usd


# -- grounding ----------------------------------------------------------------


# The agent frequently runs a guide's command in a sandbox-drifted but
# equivalent form: by absolute path (`/home/demo/.local/bin/tool` vs `tool`),
# via `python3` (vs the guide's `python`), or with `--break-system-packages`
# (Debian's externally-managed Python forces it on `pip install`; the guide
# omits it). Canonicalizing these makes a PROVEN command still ground.
_ABS_EXE_RE = re.compile(r"^(?:/[\w.+-]+)+/([\w.+-]+)$")  # /a/b/bin/exe -> exe
_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")               # NAME=val env prefix
_DROP_FLAGS = frozenset({"--break-system-packages"})


def _canonicalize_segment(seg: str) -> str:
    """Canonicalize one command/segment so sandbox/syntax drift grounds.

    Drops sandbox-only flags and normalizes the EXECUTABLE token only: an
    absolute path collapses to its basename and ``python3`` maps to ``python``.
    Argument paths are left untouched (``cat /etc/hosts`` stays as-is). Applied
    symmetrically via :func:`normalize_cmd`, so it can never ground a command
    the log didn't run — only the same command spelled differently.
    """
    toks = [t for t in seg.split(" ") if t not in _DROP_FLAGS]
    i = 0
    while i < len(toks) and _ASSIGN_RE.match(toks[i]):  # skip NAME=val prefixes
        i += 1
    if i < len(toks) and toks[i]:
        exe = toks[i]
        m = _ABS_EXE_RE.match(exe)
        if m:
            exe = m.group(1)
        if exe == "python3":
            exe = "python"
        toks[i] = exe
    return " ".join(toks)


def normalize_cmd(cmd: str) -> str:
    """Normalize a command string for grounding comparison.

    Collapses internal whitespace runs to single spaces, strips leading and
    trailing whitespace, drops trailing ``;``, and removes ``2>&1`` stderr
    merges (``cmd`` and ``cmd 2>&1`` are the same command for grounding —
    the agent frequently adds the merge, the distiller frequently drops it).
    Each chain segment is then canonicalized (see :func:`_canonicalize_segment`)
    so a proven-but-drifted command — absolute exe path, ``python3``, or a
    ``--break-system-packages`` pip flag — still grounds. Case is preserved —
    commands are case-sensitive. Applied symmetrically to both the candidate
    set and the queried command, so every transform here is safe.
    """
    s = re.sub(r"\s+", " ", cmd).strip()
    s = s.replace(" 2>&1", "")
    while s.endswith(";"):
        s = s[:-1].rstrip()
    # Preserve the && / ; separators so chained commands still split the same.
    parts = re.split(f"({_CHAIN_SPLIT_RE.pattern})", s)
    return "".join(
        p if _CHAIN_SPLIT_RE.fullmatch(p) else _canonicalize_segment(p)
        for p in parts
    )


def _strip_env_prefix(cmd: str) -> str:
    """Remove leading ``NAME=value`` assignment tokens from a normalized command."""
    while True:
        m = _ENV_PREFIX_RE.match(cmd)
        if not m:
            return cmd
        cmd = cmd[m.end() :]


def _grounded_candidates(log: CommandLog) -> set[str]:
    """Every normalized form a distilled command may legitimately match.

    For each *successful* log entry: the whole normalized command, each
    segment of it when split on ``&&`` / ``;``, and the env-prefix-stripped
    variant of each of those.
    """
    candidates: set[str] = set()
    for entry in log.successful_commands():
        norm = normalize_cmd(entry.cmd)
        candidates.add(norm)  # the whole command — chains included
        candidates.add(_strip_env_prefix(norm))
        hd = heredoc_prefix(norm)
        if hd:
            candidates.add(hd)
            candidates.add(_strip_env_prefix(hd))
        for segment in _CHAIN_SPLIT_RE.split(norm):
            segment = segment.strip()
            if not segment:
                continue
            candidates.add(segment)
            candidates.add(_strip_env_prefix(segment))
            seg_hd = heredoc_prefix(segment)
            if seg_hd:
                candidates.add(seg_hd)
    return candidates


def _segment_grounded(segment: str, candidates: set[str]) -> bool:
    """One chain segment: grounded via candidates, env-prefix, heredoc-prefix,
    cd, or comment."""
    if not segment or segment.startswith("#"):
        return True
    if segment == "cd" or segment.startswith("cd "):
        return True
    if segment in candidates or _strip_env_prefix(segment) in candidates:
        return True
    hd = heredoc_prefix(segment)
    return hd is not None and (hd in candidates or _strip_env_prefix(hd) in candidates)


def is_grounded(cmd: str, log: CommandLog) -> bool:
    """True if ``cmd`` is backed by commands that actually succeeded.

    Accepted:

    - exact match (after :func:`normalize_cmd`) with any successful entry,
      including full chained commands;
    - match with any segment of a successful chained command (``&&`` / ``;``);
    - a chained command whose EVERY segment is individually grounded (the
      distiller may recombine steps the agent ran separately);
    - match differing only in an env-var-assignment prefix
      (``FOO=1 python x.py`` vs ``python x.py``, either direction);
    - ``cd <dir>`` — navigation is always safe;
    - comment lines and blank lines.
    """
    norm = normalize_cmd(cmd)
    if not norm or norm.startswith("#"):
        return True
    if norm == "cd" or norm.startswith("cd "):
        return True
    candidates = _grounded_candidates(log)
    if norm in candidates or _strip_env_prefix(norm) in candidates:
        return True
    hd = heredoc_prefix(norm)
    if hd is not None:
        # Never chain-split past the heredoc operator — the body may contain
        # && / ; that are file content, not command separators. Ground the
        # pre-heredoc chain segment-wise; the final segment (`cat > file`)
        # matches the heredoc-prefix candidates harvested from the log.
        if hd in candidates or _strip_env_prefix(hd) in candidates:
            return True
        segments = [s.strip() for s in _CHAIN_SPLIT_RE.split(hd) if s.strip()]
        return bool(segments) and all(
            _segment_grounded(s, candidates) for s in segments
        )
    segments = [s.strip() for s in _CHAIN_SPLIT_RE.split(norm) if s.strip()]
    if len(segments) > 1:
        return all(_segment_grounded(s, candidates) for s in segments)
    return False


def validate_grounding(commands: list[str], log: CommandLog) -> list[str]:
    """Return the subset of ``commands`` that are NOT grounded (empty = valid)."""
    return [c for c in commands if not is_grounded(c, log)]


def _collect_violations(out: DistillOutput, log: CommandLog) -> list[str]:
    """All grounding violations in a distiller response: script + tape.

    Tape commands are additionally allowed to match a command already present
    in ``out.commands`` (the tape is a subset/re-presentation of the script).
    """
    violations = validate_grounding(out.commands, log)
    script_cmds = {normalize_cmd(c) for c in out.commands}
    for tc in out.tape:
        if normalize_cmd(tc.cmd) in script_cmds or is_grounded(tc.cmd, log):
            continue
        violations.append(f"(tape) {tc.cmd}")
    return violations


# -- LLM pass -----------------------------------------------------------------


def _format_log(log: CommandLog) -> str:
    """Render successful log entries (with phase tags + output snippets) and fixes."""
    lines: list[str] = []
    for entry in log.successful_commands():
        lines.append(f"[{entry.phase}] $ {entry.cmd}")
        out = entry.output.strip()
        if out:
            if len(out) > _LOG_OUTPUT_SNIPPET_CHARS:
                half = _LOG_OUTPUT_SNIPPET_CHARS // 2
                out = f"{out[:half]}\n...[truncated]...\n{out[-half:]}"
            lines.append(indent(out, "    "))
    if log.fixes:
        lines.append("")
        lines.append("Deviations the agent declared while making the run work:")
        for fix in log.fixes:
            lines.append(f"- FIX: {fix.what} BECAUSE: {fix.because}")
    return "\n".join(lines)


def _build_user_message(
    plan: Plan, log: CommandLog, readme_text: str, feedback: str
) -> str:
    """Assemble the distiller user message: plan + log + truncated README (+ feedback)."""
    readme = readme_text.encode("utf-8")[:_README_MAX_BYTES].decode(
        "utf-8", errors="ignore"
    )
    parts = [
        "## Plan (plan.json)",
        f"```json\n{plan.model_dump_json(indent=2)}\n```",
        "## Command log — commands that SUCCEEDED in the agent run",
        f"```\n{_format_log(log)}\n```",
        "## README (truncated to first 8KB)",
        f"```markdown\n{readme}\n```",
    ]
    if feedback:
        parts += [
            "## Verifier feedback — the previous distilled script FAILED in a clean container",
            feedback,
        ]
    parts.append(
        "Respond with ONLY the JSON object matching the DistillOutput schema."
    )
    return "\n\n".join(parts)


def run_distiller(
    plan: Plan,
    log: CommandLog,
    readme_text: str,
    model: str,
    feedback: str = "",
) -> tuple[DistillOutput, float]:
    """Run the distiller LLM pass and enforce the grounding rule in code.

    If the first response contains ungrounded commands (in ``commands`` or in
    the tape), retry ONCE with the violations listed explicitly; if the retry
    still violates, raise :class:`DistillError`. Returns
    ``(validated output, total llm cost in USD)``.
    """
    system = (_PROMPTS_DIR / "distill.md").read_text(encoding="utf-8")
    user = _build_user_message(plan, log, readme_text, feedback)
    total_cost = 0.0

    out, cost = llm.complete_json(
        system=system, user=user, model=model, schema=DistillOutput
    )
    total_cost += cost

    violations = _collect_violations(out, log)
    if violations:
        retry_user = (
            f"{user}\n\n"
            "## GROUNDING VIOLATIONS — your previous response was rejected\n\n"
            "These commands never succeeded in the command log:\n"
            + "\n".join(f"- {v}" for v in violations)
            + "\n\nUse ONLY commands that appear as successful entries in the "
            "command log above (verbatim where possible). Respond again with "
            "the complete JSON object."
        )
        out, cost = llm.complete_json(
            system=system, user=retry_user, model=model, schema=DistillOutput
        )
        total_cost += cost
        violations = _collect_violations(out, log)
        if violations:
            raise DistillError(
                "Distiller produced ungrounded commands after retry: "
                + "; ".join(repr(v) for v in violations),
                cost_usd=total_cost,
            )
    return out, total_cost


# -- artifact writing ---------------------------------------------------------


def _grep_flags_and_pattern(pattern: str) -> tuple[str, str]:
    """Translate a Python-style regex to grep -E usage.

    GNU grep -E does not understand inline flags like ``(?i)``; the planner
    (an LLM) writes Python-style patterns. Handle the common case by
    stripping a leading ``(?i)`` and adding grep's ``-i`` flag.
    """
    if pattern.startswith("(?i)"):
        return "-qiE", pattern[4:]
    return "-qE", pattern


def _tolerate_findings_steps(commands: list[str], log: CommandLog | None) -> list[str]:
    """Append ``|| true`` to step commands that legitimately exit nonzero.

    ``commands.sh`` runs under ``set -e``, but findings tools (drift detectors,
    linters, scanners) exit nonzero ON SUCCESS. When such a command appears as
    a normal step (not just the assertion), its nonzero exit would abort the
    script before the assertion is reached. We know which commands are
    findings-successful from the log entries `normalize.mark_findings_success`
    flagged; make exactly those tolerant. Real failures still abort.
    """
    if log is None:
        return commands
    findings = {
        normalize_cmd(e.cmd) for e in log.entries if e.findings_success
    }
    findings |= {c.split("|", 1)[0].strip() for c in findings}
    if not findings:
        return commands
    out_lines: list[str] = []
    for cmd in commands:
        norm = normalize_cmd(cmd)
        last_seg = _CHAIN_SPLIT_RE.split(norm)[-1].strip()
        if (norm in findings or last_seg in findings) and "|| true" not in cmd:
            out_lines.append(f"{cmd} || true")
        else:
            out_lines.append(cmd)
    return out_lines


def _render_commands_sh(out: DistillOutput, plan: Plan, repo_url: str, log: "CommandLog | None" = None) -> str:
    """Build the commands.sh text: header, clone preamble, commands, assertion.

    The clone preamble is harness-injected (not distilled): the agent never
    runs ``git clone`` itself (the harness pre-copies the repo into /work), so
    the grounding rule would forbid the distiller from writing one — yet the
    script must be self-contained in a fresh container. Cloning into ``.``
    makes any distilled ``cd /work`` a harmless no-op.

    For a *guide-only* run (empty ``repo_url``) there is no repo to clone: the
    preamble is just ``cd /work`` and the distilled commands (from a
    self-contained step-by-step guide) must set everything up themselves. The
    fresh-container grounding moat is unchanged — verify still replays this
    script from zero.
    """
    lines: list[str] = [
        "#!/usr/bin/env bash",
        "set -euxo pipefail",
        "export DEBIAN_FRONTEND=noninteractive",
        "",
        "# --- readme2demo preamble (harness-injected): fresh-container setup ---",
        "cd /work",
    ]
    if repo_url:
        lines.append(f"git clone --depth 1 {shlex.quote(repo_url)} .")
    lines.append("")
    lines.extend(_tolerate_findings_steps(out.commands, log))
    criteria = plan.success_criteria
    # The criteria command's exit code must NOT abort the script under set -e:
    # findings tools (drift detectors, linters, scanners) exit nonzero when
    # they find what the demo exists to show. With an expected_pattern, the
    # pattern is the criterion; without one, exit code 0 is.
    lines += [
        "",
        "# --- readme2demo success-criteria assertion ---",
        "set +e",
        f'r2d_output="$({criteria.command} 2>&1)"',
        "r2d_exit=$?",
        "set -e",
        "printf '%s\\n' \"$r2d_output\"",
    ]
    if criteria.expected_pattern:
        flags, pattern = _grep_flags_and_pattern(criteria.expected_pattern)
        fail_msg = (
            "readme2demo: success-criteria pattern not matched: "
            + criteria.expected_pattern
        )
        lines += [
            f"if ! printf '%s\\n' \"$r2d_output\" | grep {flags} {shlex.quote(pattern)}; then",
            f"    echo {shlex.quote(fail_msg)} >&2",
            "    exit 1",
            "fi",
        ]
    else:
        lines += [
            'if [ "$r2d_exit" -ne 0 ]; then',
            '    echo "readme2demo: success command exited $r2d_exit" >&2',
            "    exit 1",
            "fi",
        ]
    lines.append('echo "R2D_VERIFY_OK"')
    return "\n".join(lines) + "\n"


# Go-regexp metacharacters (VHS Wait+Screen patterns), plus the / delimiter.
_VHS_REGEX_METAS = set("\\.+*?()|[]{}^$/")


def vhs_wait_pattern(s: str, max_len: int = 40) -> str:
    """Make a Wait+Screen pattern safe: treat it as a LITERAL substring.

    The distiller is told to use plain substrings, but an LLM instruction is
    not enforcement — e.g. "ToolHive (thv) is a lightweight" silently becomes
    a regex with a capture group that never matches the on-screen parens and
    times the render out. Escape every metacharacter, and truncate so the
    pattern can't span a wrapped terminal line.
    """
    s = s[:max_len]
    return "".join("\\" + ch if ch in _VHS_REGEX_METAS else ch for ch in s)


def vhs_quote(s: str) -> str:
    """Quote a string for a VHS ``Type`` argument.

    VHS string literals do not support backslash escapes; instead VHS accepts
    three delimiters. Pick one the string doesn't contain: double quotes,
    then backticks, then single quotes.
    """
    if '"' not in s:
        return f'"{s}"'
    if "`" not in s:
        return f"`{s}`"
    if "'" not in s:
        return f"'{s}'"
    raise DistillError(
        f"Command cannot be quoted for VHS (contains \", ` and '): {s!r}"
    )


def write_commands_sh(out: DistillOutput, run_dir: Path, plan: Plan, repo_url: str, log: "CommandLog | None" = None) -> Path:
    """Write the executable commands.sh (header + clone preamble + assertion)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    script_path = run_dir / "commands.sh"
    script_path.write_text(_render_commands_sh(out, plan, repo_url, log), encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


def write_tape(tape: list[TapeCommand], run_dir: Path, seed_worktree: bool = False) -> Path:
    """Render demo.tape from TapeCommands.

    The render runs in the base image with run_dir mounted at /vhs, and the
    tape session starts in an empty /work. A guide that fetches the code
    (``git clone`` / ``curl … | tar``) populates /work on camera. But a guide
    that assumes the checkout is already present — e.g. a repo's OWN
    step_by_step.md that opens with ``pip install -e .`` — would run in an empty
    /work and fail ("not a Python project" → later steps "command not found").
    ``seed_worktree=True`` makes the hidden preamble copy the verified worktree
    (mounted at /vhs/worktree) into /work first, so those steps work.

    Sleeps are the pacing mechanism (see template note on Wait+Screen) and are
    clamped to a floor so viewers can read each command's output.
    """
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )
    env.filters["vhs_quote"] = vhs_quote
    env.filters["vhs_wait"] = vhs_wait_pattern
    clamped = [
        tc.model_copy(update={"sleep_after_s": max(tc.sleep_after_s, 2.0)})
        for tc in tape
    ]
    tape_text = env.get_template("demo.tape.j2").render(
        tape=clamped, seed_worktree=seed_worktree
    )
    dest = run_dir / "demo.tape"
    dest.write_text(tape_text, encoding="utf-8")
    return dest


def write_outline(out: DistillOutput, run_dir: Path) -> Path:
    dest = run_dir / "tutorial_outline.json"
    dest.write_text(out.outline.model_dump_json(indent=2), encoding="utf-8")
    return dest


def write_artifacts(out: DistillOutput, run_dir: Path, plan: Plan, repo_url: str, log: "CommandLog | None" = None) -> None:
    """Composite writer: commands.sh + demo.tape (from out.tape) + outline."""
    write_commands_sh(out, run_dir, plan, repo_url, log)
    write_tape(out.tape, run_dir)
    write_outline(out, run_dir)


# -- step_by_step.md as the video source -----------------------------------------


def parse_guide_steps(guide_text: str) -> list[tuple[str, str]]:
    """Extract ``(step_title, command)`` pairs from a step_by_step.md.

    A step title is the most recent markdown heading; commands are the
    non-comment lines of fenced ```bash / ```sh / ``` blocks beneath it.
    A heredoc (``cat > file <<'EOF' ... EOF``) is accumulated into ONE
    multi-line command, terminator line included.
    """
    steps: list[tuple[str, str]] = []
    title = ""
    in_fence = False
    fence_is_shell = False
    heredoc_term: Optional[str] = None
    heredoc_lines: list[str] = []
    for line in guide_text.splitlines():
        stripped = line.strip()
        if heredoc_term is not None:
            heredoc_lines.append(line.rstrip())
            if stripped == heredoc_term:
                steps.append((title, "\n".join(heredoc_lines)))
                heredoc_term = None
                heredoc_lines = []
            continue
        if stripped.startswith("```"):
            if not in_fence:
                lang = stripped[3:].strip().lower()
                fence_is_shell = lang in ("bash", "sh", "shell", "console", "")
            in_fence = not in_fence
            continue
        if in_fence:
            if fence_is_shell and stripped and not stripped.startswith("#"):
                cmd = stripped.removeprefix("$ ").strip()
                if not cmd:
                    continue
                m = _HEREDOC_RE.search(cmd)
                if m:
                    heredoc_term = m.group(1)
                    heredoc_lines = [cmd]
                else:
                    steps.append((title, cmd))
            continue
        if stripped.startswith("#"):
            title = re.sub(
                r"^#+\s*(?:Step\s*\d+\s*[—:–-]+\s*)?", "", stripped
            ).strip()
    if heredoc_term is not None and heredoc_lines:  # unterminated — keep as one
        steps.append((title, "\n".join(heredoc_lines)))
    return steps


def tape_from_guide(
    guide_text: str,
    log: CommandLog,
    script_commands: list[str],
    repo_url: str = "",
) -> list[TapeCommand]:
    """Derive the demo tape FROM step_by_step.md — the guide is the video's source.

    EVERY guide step becomes a typed command in the video (the render runs in
    the base image, which has vhs AND the toolchains, so clones, installs, and
    builds execute for real on camera), provided the command is grounded:
    it succeeded in the agent log, is part of the verified script, or is the
    harness-injected clone of this run's repo. Ungrounded guide commands are
    dropped — a hand-written ``-s`` guide cannot put unverified commands on
    camera. Returns [] when nothing qualifies — callers fall back to the
    distiller's own tape.
    """
    script_set = {normalize_cmd(c) for c in script_commands}
    # The agent frequently runs a guide's command in a sandbox-drifted but
    # equivalent form: `python3` (not `python`), an absolute exe path, or
    # `pip install ... --break-system-packages`. The clean guide string FAILS
    # on camera (e.g. bare `pip install` hits Debian's externally-managed
    # error, so nothing installs and later steps report "packages not found");
    # the drifted form is what actually worked. Map the normalized form -> the
    # PROVEN command so the video types the variant that runs.
    # Two passes so a WHOLE-command match wins over a segment match. The second
    # pass maps each chain segment to its FULL proven command, so a guide step
    # that the agent only ran as part of a chain (e.g. `export PATH=... &&
    # readme2demo --help`) is typed WITH that chain — otherwise the bare command
    # isn't on PATH on camera and records "command not found".
    proven_by_norm: dict[str, str] = {}
    for entry in log.successful_commands():
        proven_by_norm.setdefault(normalize_cmd(entry.cmd), entry.cmd)
    for entry in log.successful_commands():
        for seg in _CHAIN_SPLIT_RE.split(normalize_cmd(entry.cmd)):
            seg = seg.strip()
            if seg:
                proven_by_norm.setdefault(seg, entry.cmd)
    # Agents often wrap long-output commands in a capping pipe
    # (`cmd 2>&1 | head -20`). The guide says `cmd`; the log proves the piped
    # variant. Map first-pipe-segment -> full proven command so the step still
    # makes the video — using the LOG's variant, which is also guaranteed to
    # terminate on camera (it terminated for the agent).
    pipe_variants: dict[str, str] = {}
    for entry in log.successful_commands():
        norm_entry = normalize_cmd(entry.cmd)
        if "|" in norm_entry:
            first = norm_entry.split("|", 1)[0].strip()
            pipe_variants.setdefault(first, norm_entry)

    tape: list[TapeCommand] = []
    last_title = None
    for title, cmd in parse_guide_steps(guide_text):
        norm = normalize_cmd(cmd)
        is_repo_clone = bool(
            repo_url and norm.startswith("git clone") and repo_url in norm
        )
        if is_repo_clone:
            chosen = cmd  # harness clone — type the guide's own clone line
        elif is_grounded(cmd, log) or norm in script_set:
            # Prefer the exact command the agent proved; fall back to the
            # guide's text when nothing drifted (they're then identical).
            chosen = proven_by_norm.get(norm, cmd)
        elif norm in pipe_variants:
            chosen = pipe_variants[norm]
        else:
            continue
        comment = title if title and title != last_title else None
        if "\n" in chosen:
            # Heredoc / multi-line: type it line by line so the file creation
            # plays on camera (a shell reads a heredoc until its terminator).
            # Every line must be typeable; a line mixing all three quote
            # delimiters can't be — skip the whole step then (rare).
            lines = chosen.split("\n")
            try:
                for ln in lines:
                    vhs_quote(ln)
            except DistillError:
                continue
            tape.append(
                TapeCommand(cmd=chosen, comment=comment, lines=lines, sleep_after_s=1.0)
            )
        else:
            try:
                vhs_quote(chosen)
            except DistillError:
                continue
            tape.append(TapeCommand(cmd=chosen, comment=comment, sleep_after_s=3.0))
        last_title = title or last_title
    return tape


def materialize_guide(
    run_dir: Path, plan: Plan, out: DistillOutput, log: CommandLog,
    repo_url: str = "",
) -> str:
    """Ensure run_dir/step_by_step.md exists and return its text.

    Repo-provided (or ``-s``-injected) guide wins verbatim; otherwise a
    grounded guide is generated from the distilled script (badge says
    unverified at this point — the tutorial stage regenerates it with
    verified outputs after the replay passes).
    """
    dest = run_dir / "step_by_step.md"
    if plan.guide_path:
        src = run_dir / "repo" / plan.guide_path
        if src.is_file():
            text = src.read_text(encoding="utf-8", errors="replace")
            dest.write_text(text, encoding="utf-8")
            return text
    from readme2demo.tutorial import write_step_by_step  # no import cycle: tutorial ⊄ distill

    write_step_by_step(run_dir, plan, out.outline, log, verified=False, repo_url=repo_url)
    return dest.read_text(encoding="utf-8", errors="replace")


def distill(
    plan: Plan,
    log: CommandLog,
    readme_text: str,
    run_dir: Path,
    model: str,
    repo_url: str,
    feedback: str = "",
) -> tuple[DistillOutput, float]:
    """Full M5 stage: grounded LLM distillation + artifact writing.

    step_by_step.md is the canonical intermediate: it is materialized here
    (copied from the repo / ``-s`` file, or generated from the distilled
    script) and the demo tape is derived FROM it. The distiller's own tape is
    only a fallback when the guide yields no demo-safe grounded commands.

    Returns ``(distill output, llm cost in USD)``.
    """
    out, cost = run_distiller(plan, log, readme_text, model, feedback)
    write_commands_sh(out, run_dir, plan, repo_url, log)
    materialize_guide(run_dir, plan, out, log, repo_url)
    write_outline(out, run_dir)
    # The AUTHORITATIVE tape is (re)built from the FINALIZED step_by_step.md in
    # the render stage, AFTER the tutorial stage fills in verified outputs — so
    # the video provably follows the published guide. Here we only stash the
    # distiller's own tape as a fallback for guides that yield nothing.
    write_tape(out.tape, run_dir)
    return out, cost


def _tape_fetches_code(tape: list[TapeCommand]) -> bool:
    """True if the tape itself puts the repo in the working dir.

    A guide that clones or downloads+extracts the code populates the empty
    /work on camera; a guide that assumes the checkout is already there (a
    repo's own step_by_step.md) does not — the render seeds /work from the
    verified worktree for the latter (see ``write_tape(seed_worktree=...)``).
    """
    for tc in tape:
        c = tc.cmd.lower()
        if "git clone" in c:
            return True
        if ("curl" in c or "wget" in c) and "tar" in c:
            return True
    return False


def build_tape_from_step_by_step(
    run_dir: Path, log: CommandLog, repo_url: str, fallback: list[TapeCommand]
) -> dict:
    """Derive demo.tape from the CURRENT run_dir/step_by_step.md and record coverage.

    Called by the render stage so the video is built from the final, published
    guide (the video follows step_by_step.md, not the other way round). Writes
    ``demo.tape`` and ``tape_coverage.json``; returns the coverage dict.
    """
    guide_path = run_dir / "step_by_step.md"
    guide_text = guide_path.read_text(encoding="utf-8", errors="replace") if guide_path.is_file() else ""
    tape = tape_from_guide(guide_text, log, [], repo_url)
    guide_steps = parse_guide_steps(guide_text)
    n_guide = len(guide_steps)
    kept = {normalize_cmd(tc.cmd) for tc in tape}
    kept_firsts = {k.split("|", 1)[0].strip() for k in kept}
    dropped = [
        c for _, c in guide_steps
        if normalize_cmd(c) not in kept and normalize_cmd(c) not in kept_firsts
    ]
    coverage = {"guide_steps": n_guide, "tape_steps": len(tape), "dropped": dropped}
    (run_dir / "tape_coverage.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    if tape and dropped:
        from rich.console import Console
        from rich.markup import escape

        console = Console()
        console.print(
            f"[yellow]⚠ {len(dropped)}/{n_guide} guide steps excluded from the "
            f"video (not proven in the agent run):[/]"
        )
        for c in dropped:
            # escape(): guide commands routinely contain [bracketed] tokens
            # (pip extras, sed/grep classes, [ -f x ]) that Rich would parse
            # as markup and silently swallow from this diagnostic.
            console.print(f"[yellow]    {escape(c.splitlines()[0][:100])}[/]")
    final_tape = tape if tape else fallback
    # A repo's own guide (no clone step) assumes the checkout is present; seed
    # /work from the verified worktree so its `pip install -e .`/build steps
    # aren't run in an empty directory.
    write_tape(final_tape, run_dir, seed_worktree=not _tape_fetches_code(final_tape))
    return coverage
