"""M6 — Verifier (the moat).

Proves ``commands.sh`` works from zero by replaying it in a fresh container
the agent never touched. Pass = exit code 0 AND the ``R2D_VERIFY_OK`` marker
(printed by the script's success-criteria assertion block) in the replay log.

Nothing is published unverified; on failure the orchestrator feeds
:func:`verify_feedback` back to the distiller for one regeneration loop.
"""

from __future__ import annotations

from pathlib import Path

from readme2demo.config import Config
from readme2demo.sandbox import Sandbox
from readme2demo.types import Plan, VerifyReport

_MARKER = "R2D_VERIFY_OK"
# NOT under /work: the script's harness preamble does `git clone <url> .` in
# /work, which git refuses if anything (like the script itself) is already
# there. /tmp keeps the clone target pristine.
_SCRIPT_DST = "/tmp/commands.sh"


def run_verify(run_dir: Path, plan: Plan, cfg: Config) -> VerifyReport:
    """Replay ``run_dir/commands.sh`` in a fresh sandbox and report the verdict.

    The sandbox is created from ``cfg.base_image`` with NO repo mount and no
    state from the agent's container: ``commands.sh`` must set up everything
    itself, starting from the harness-injected clone preamble that
    ``distill.write_artifacts`` prepends.

    Combined output streams to ``run_dir/verify.log`` (truncated at the start
    of each verify run). Pass = exit code 0 AND ``R2D_VERIFY_OK`` in the
    attempt's log output. On failure, the script is retried once in a NEW
    fresh sandbox (network flakiness happens) when ``cfg.verify_retries >= 1``;
    both attempts are recorded under ``===== ATTEMPT n =====`` separators.
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

    total_attempts = 2 if cfg.verify_retries >= 1 else 1
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


def cwd_hints(script_text: str) -> list[str]:
    """Static cwd analysis of commands.sh for the distiller feedback loop.

    Walks the script tracking the working directory through ``cd`` lines and
    flags commands that use relative paths (``-e .``, ``./x``, bare ``.``)
    while the simulated cwd is somewhere the distiller probably didn't
    intend (tfdrift regression: ``cd /tmp`` for a download, then
    ``pip install -e .`` editable-installed /tmp instead of the repo).
    Hints only — precision for the retry prompt, never a hard gate.
    """
    import re as _re

    rel_re = _re.compile(r"(^|\s)(\./|\-e \.($|\s)|\.($|\s))")
    chain_split = _re.compile(r"\s*(?:&&|;)\s*")
    hints: list[str] = []
    cwd = "/work"
    for raw in script_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # A relative path is only suspicious when the cwd has drifted away
        # from the repo root AND the line doesn't set its own directory.
        if cwd != "/work" and "cd " not in line and rel_re.search(line):
            hints.append(
                f"`{line[:90]}` uses a relative path while the working "
                f"directory is `{cwd}` — is that the intended directory? "
                "(cd persists across lines)"
            )
        # Track cd through every chain segment of the line.
        for seg in chain_split.split(line):
            seg = seg.strip()
            if seg.startswith("cd ") or seg == "cd":
                target = seg[2:].strip().strip("'\"") or "~"
                cwd = target if target.startswith("/") else f"{cwd}/{target}"
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
