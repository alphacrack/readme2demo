"""M6 — Verifier (the moat).

Proves ``commands.sh`` works from zero by replaying it in a fresh container
the agent never touched. Pass = exit code 0 AND the ``R2D_VERIFY_OK`` marker
(printed by the script's success-criteria assertion block) in the replay log.

Nothing is published unverified; on failure the orchestrator feeds
:func:`verify_feedback` back to the distiller for one regeneration loop.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from pathlib import Path
from typing import Optional

from readme2demo.config import Config
from readme2demo.sandbox import Sandbox
from readme2demo.types import Plan, VerifyReport

_MARKER = "R2D_VERIFY_OK"
# NOT under /work: the script's harness preamble does `git clone <url> .` in
# /work, which git refuses if anything (like the script itself) is already
# there. /tmp keeps the clone target pristine.
_SCRIPT_DST = "/tmp/commands.sh"
# images/base/Dockerfile creates this home for USER demo; keep the contract
# covered by test_issue_96_home_matches_base_image when the image user changes.
_SANDBOX_HOME = "/home/demo"


def run_verify(run_dir: Path, plan: Plan, cfg: Config) -> VerifyReport:
    """Replay ``run_dir/commands.sh`` in a fresh sandbox and report the verdict.

    The sandbox is created from ``cfg.base_image`` with NO repo mount and no
    state from the agent's container: ``commands.sh`` must set up everything
    itself, starting from the harness-injected clone preamble that
    ``distill.write_artifacts`` prepends.

    Combined output streams to ``run_dir/verify.log`` (truncated at the start
    of each verify run). Pass = exit code 0 AND ``R2D_VERIFY_OK`` in the
    attempt's log output. On failure, the script is retried in a NEW fresh
    sandbox (network flakiness happens) up to ``cfg.verify_retries`` times
    (so ``verify_retries=1`` means one retry, i.e. two attempts total); every
    attempt is recorded under ``===== ATTEMPT n =====`` separators.
    Sandboxes are always destroyed, pass or fail.
    """
    script = run_dir / "commands.sh"
    if not script.is_file():
        raise FileNotFoundError(f"commands.sh not found in run dir: {script}")

    log_path = run_dir / "verify.log"
    criteria = plan.success_criteria
    header = f"# readme2demo verify — image={cfg.base_image}\n# success criteria: {criteria.command}"
    if criteria.expected_pattern:
        header += f" =~ /{criteria.expected_pattern}/"
    log_path.write_text(header + "\n", encoding="utf-8")

    total_attempts = 1 + max(0, cfg.verify_retries)
    attempt = 0
    exit_code: int | None = None
    criteria_matched: bool | None = None
    passed = False

    for attempt in range(1, total_attempts + 1):
        with open(log_path, "a", encoding="utf-8") as sink:
            sink.write(f"===== ATTEMPT {attempt} =====\n")
        offset = log_path.stat().st_size

        mounts: list[tuple[str, str, str]] = []
        group_add = None
        if cfg.allow_docker_socket:
            # Same opt-in as the agent stage: the replay must be able to run
            # the same container-managing commands the agent proved.
            from readme2demo.sandbox import docker_socket_gid

            mounts.append(("/var/run/docker.sock", "/var/run/docker.sock", "rw"))
            group_add = docker_socket_gid(cfg.base_image)
        sandbox = Sandbox(
            image=cfg.base_image,
            mounts=mounts,
            group_add=group_add,
            network=cfg.network,
            memory=cfg.memory,
            cpus=cfg.cpus,
            pids_limit=cfg.pids_limit,
            workdir="/work",
        )
        try:
            sandbox.start()
            sandbox.copy_in(script, _SCRIPT_DST)
            result = sandbox.exec(
                f"bash {_SCRIPT_DST}",
                timeout=cfg.verify_timeout_s,
                stream_to=log_path,
            )

            with open(log_path, "rb") as f:
                f.seek(offset)
                attempt_text = f.read().decode("utf-8", errors="replace")

            exit_code = result.exit_code
            criteria_matched = _MARKER in attempt_text
            passed = result.exit_code == 0 and criteria_matched
            if passed:
                # Export the verified worktree (built artifacts included) for
                # the renderer: the VHS tape replays demo commands against it.
                _export_worktree(sandbox, run_dir)
        finally:
            sandbox.destroy()

        if passed:
            break

    return VerifyReport(
        passed=passed,
        attempts=attempt,
        exit_code=exit_code,
        criteria_matched=criteria_matched,
        log_path="verify.log",
    )


def _export_worktree(sandbox: Sandbox, run_dir: Path) -> None:
    """Copy the verified /work out to run_dir/worktree for the render stage.

    The VHS render runs in the stock VHS image with run_dir mounted at /vhs;
    the tape opens with ``cd worktree`` and demo commands run against the
    built artifacts in there. Best effort: a failed export only degrades the
    video, never the verification verdict.
    """
    import shutil

    dest = run_dir / "worktree"
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    try:
        sandbox.copy_out("/work/.", dest)
    except Exception:  # noqa: BLE001 — degrade gracefully, keep the verdict
        shutil.rmtree(dest, ignore_errors=True)


def _cwd_segments(line: str) -> Optional[tuple[list[list[str]], bool]]:
    """Split simple chains while retaining quotes for cd's tilde handling.

    Pipes and redirects make a line opaque, but need not end later analysis.
    Structural syntax still stops it so a shell body cannot masquerade as cd.
    """
    tokens = re.finditer(
        r"""\s+|(?P<comment>\#.*)|(?P<heredoc><<)|(?P<chain>&&|;)|"""
        r"(?P<pipe>\|[|&]?)|(?P<redirect><>|>\||[<>]&|&>>?|>>?|<)|"
        r"""(?P<word>(?:[^\s'"\\;&|()<>]+|'[^']*'|"(?:\\.|[^"\\])*"|\\.)+)"""
        r"|(?P<unsupported>.)",
        line,
    )
    segments: list[list[str]] = [[]]
    opaque = False
    redirect_target = False
    segment_has_redirect = False
    trailing_semicolon = False
    word_end = -1
    for token in tokens:
        if token.lastgroup == "comment":
            break
        if token.lastgroup in {"unsupported", "heredoc"}:
            return None
        if token.lastgroup in {"chain", "pipe", "redirect"} and redirect_target:
            return None
        if token.lastgroup in {"chain", "pipe"}:
            if not segments[-1] and not segment_has_redirect:
                return None
            segments.append([])
            segment_has_redirect = False
            opaque |= token.lastgroup == "pipe"
            trailing_semicolon = token.group() == ";"
        elif token.lastgroup == "redirect":
            opaque = redirect_target = True
            trailing_semicolon = False
            if segments[-1] and word_end == token.start() and segments[-1][-1].isdigit():
                segments[-1].pop()  # adjacent file descriptor, e.g. 2>log
        elif token.lastgroup == "word":
            trailing_semicolon = False
            if redirect_target:
                redirect_target = False  # file/descriptor operand, not a command
                segment_has_redirect = True
            else:
                segments[-1].append(token.group())
                word_end = token.end()
    if redirect_target or (
        not segments[-1] and not segment_has_redirect and not trailing_semicolon and len(segments) > 1
    ):
        return None  # a trailing pipe/redirect may continue on the next line
    for segment in segments:
        if not segment:
            continue
        # Do not mistake conditional/function bodies for executed commands,
        # or guess the environment after assignments or shell builtins.
        if segment[0] in {
            "if", "then", "elif", "else", "fi", "for", "while", "until",
            "do", "done", "case", "esac", "select", "function", "{", "}",
        }:
            return None
        command = shlex.split(segment[0])[0]
        if (
            command in {"source", ".", "eval", "command", "builtin", "pushd", "popd"}
            or re.match(r"\w+=", command)
            or any(char in command for char in "$`")
        ):
            return None
        if command in {"export", "unset", "readonly", "declare", "typeset"} and any(
            shlex.split(word)[0].split("=")[0] in {"HOME", "CDPATH", "OLDPWD", "PWD"}
            or any(char in word for char in "$`")
            for word in segment[1:]
        ):
            return None
        segment[0] = command
    return segments, opaque


def _cd_target(words: list[str], cwd: Optional[str], previous: Optional[str]) -> Optional[str]:
    """Resolve a literal cd operand without evaluating shell expansions."""
    if words[:1] == ["--"]:
        words = words[1:]
    if not words:
        return _SANDBOX_HOME
    if len(words) != 1:
        return None
    raw = words[0]
    # Be conservative even when these characters are quoted: shell expansion
    # and filesystem-dependent globbing are outside this hints-only analysis.
    if any(char in raw for char in "$`*?[]{}"):
        return None
    target = shlex.split(raw)[0]
    if target == "-":
        return previous
    if target.startswith("-") or not target:
        return None
    if raw == "~" or raw.startswith("~/"):
        target = _SANDBOX_HOME + target[1:]
    elif raw.startswith("~"):
        return None  # another user's home is unknown
    if posixpath.isabs(target):
        return posixpath.normpath(target)
    return posixpath.normpath(posixpath.join(cwd, target)) if cwd is not None else None


def cwd_hints(script_text: str) -> list[str]:
    """Static cwd analysis of commands.sh for the distiller feedback loop.

    Walks the script tracking the working directory through ``cd`` lines and
    flags commands that use relative paths (``-e .``, ``./x``, bare ``.``)
    while the simulated cwd is somewhere the distiller probably didn't
    intend (tfdrift regression: ``cd /tmp`` for a download, then
    ``pip install -e .`` editable-installed /tmp instead of the repo).
    Hints only — precision for the retry prompt, never a hard gate.

    Tracks literal cd operands in simple &&/; chains, assuming they succeed.
    Unknown targets suppress hints until a known directory is restored;
    ordinary pipes/redirects preserve cwd, but an obscured cd makes it unknown.
    Unsupported structural syntax stops analysis. No commands are executed.
    """
    rel_re = re.compile(r"(^|\s)(\./|\-e \.($|\s)|\.($|\s))")
    hints: list[str] = []
    cwd: Optional[str] = "/work"
    previous: Optional[str] = None
    for raw in script_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _cwd_segments(line)
        if parsed is None:
            break
        segments, opaque = parsed
        cd_segments = [seg for seg in segments if seg[:1] == ["cd"]]
        if opaque and cd_segments:
            # Do not infer whether a piped/conditional/redirected cd ran in the
            # current shell, or retain OLDPWD after an untracked directory change.
            cwd = previous = None
            continue
        # A relative path is only suspicious when the cwd has drifted away
        # from the repo root AND the line doesn't set its own directory.
        if cwd not in (None, "/work") and not cd_segments and rel_re.search(line):
            hints.append(
                f"`{line[:90]}` uses a relative path while the working "
                f"directory is `{cwd}` — is that the intended directory? "
                "(cd persists across lines)"
            )
        # Track cd through every chain segment of the line.
        for seg in cd_segments:
            cwd, previous = _cd_target(seg[1:], cwd, previous), cwd
    return hints


def verify_feedback(run_dir: Path, max_bytes: int = 6000) -> str:
    """Tail of verify.log plus cwd analysis, for the distiller feedback loop.

    Returns an empty string when no verify.log exists yet.
    """
    log_path = run_dir / "verify.log"
    if not log_path.is_file():
        return ""
    data = log_path.read_bytes()
    tail = data[-max_bytes:].decode("utf-8", errors="replace")
    truncated = "...[log truncated]...\n" if len(data) > max_bytes else ""
    hint_text = ""
    script = run_dir / "commands.sh"
    if script.is_file():
        hints = cwd_hints(script.read_text(encoding="utf-8", errors="replace"))
        if hints:
            hint_text = (
                "\n\nWorking-directory analysis of the failing script:\n- "
                + "\n- ".join(hints)
                + "\nRemember: cd persists across lines; the repo is at /work."
            )
    return (
        "commands.sh FAILED when replayed in a fresh clean container (it "
        "worked during the agent run, so a setup step is probably missing or "
        "out of order). Tail of verify.log:\n\n"
        f"{truncated}{tail}{hint_text}"
    )
