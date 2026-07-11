"""M3 — Agent Runner.

Runs the chosen agent engine inside a hardened sandbox until the quickstart
works (or the agent blocks / runs out of turns), and pulls the raw transcript
back into the run directory. Everything engine-specific is delegated to the
:class:`~readme2demo.engines.base.AgentEngine`; this module owns the sandbox
lifecycle and the prompt rendering.
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from readme2demo.config import Config
from readme2demo.engines.base import (
    PROMPT_CONTAINER_PATH,
    TRANSCRIPT_CONTAINER_PATH,
    AgentEngine,
    Limits,
)
from readme2demo.sandbox import Sandbox, SandboxError
from readme2demo.types import Plan

# How much of the agent's stderr to include in error messages.
_STDERR_TAIL_BYTES = 2000


class AgentRunError(RuntimeError):
    """The agent run produced no usable transcript."""


def render_agent_prompt(plan: Plan, template_path: Path) -> str:
    """Render prompts/agent.md from a Plan via simple placeholder replacement.

    Placeholders: ``{{quickstart_summary}}``, ``{{success_command}}``,
    ``{{expected_pattern}}``, ``{{prereqs}}``, ``{{steps_expected}}``.
    Plain ``str.replace`` — the template is trusted, no jinja needed here.
    """
    template = template_path.read_text(encoding="utf-8")
    expected = plan.success_criteria.expected_pattern or "(exit code 0 is sufficient)"
    prereqs = ", ".join(plan.prereqs) if plan.prereqs else "none specified"
    steps = (
        "; ".join(plan.steps_expected) if plan.steps_expected else "not specified"
    )
    if plan.guide_path:
        guide_note = (
            f"**This run follows an authoritative step-by-step guide at "
            f"`{plan.guide_path}`. Read it FIRST. Your job is to execute EVERY "
            f"step of the guide, in order — not merely to reach the success "
            f"command. Reaching the success command early does NOT finish the "
            f"run; continue until every guide step has either succeeded or "
            f"been explicitly skipped with a printed line in exactly this "
            f"format:**\n\n"
            f"```\nSKIPPED_STEP: <command> BECAUSE: <reason>\n```\n\n"
            f"**Steps that only fail because of missing sandbox infrastructure "
            f"should still be attempted once so the failure is recorded. All "
            f"rules below still apply (FIX/BLOCKED/ADJUSTED_SUCCESS markers, "
            f"non-interactive commands, no source patching).**"
        )
    else:
        guide_note = ""
    return (
        template.replace("{{quickstart_summary}}", plan.quickstart_summary)
        .replace("{{success_command}}", plan.success_criteria.command)
        .replace("{{expected_pattern}}", expected)
        .replace("{{prereqs}}", prereqs)
        .replace("{{steps_expected}}", steps)
        .replace("{{guide_note}}", guide_note)
    )


def run_agent(run_dir: Path, plan: Plan, engine: AgentEngine, cfg: Config) -> Path:
    """Run the agent inside a fresh sandbox; return the host transcript path.

    Flow: verify required env vars → start sandbox with the repo mounted
    read-only at /repo → copy the repo into /work → inject the rendered
    prompt → run the engine command with the wall-clock timeout → copy the
    raw transcript out to ``run_dir/transcript.ndjson``. The sandbox is
    always destroyed, even on failure.

    Raises:
        EngineError: if any of the engine's required env vars is missing.
        AgentRunError: if no (or an empty) transcript came back.
    """
    env = engine.resolve_env()  # raises EngineError with a clear message

    prompt_path = run_dir / "agent_prompt.md"
    template_path = Path(__file__).parent / "prompts" / "agent.md"
    prompt_path.write_text(
        render_agent_prompt(plan, template_path), encoding="utf-8"
    )

    transcript_path = run_dir / "transcript.ndjson"
    limits = Limits(
        max_turns=cfg.max_turns,
        timeout_s=cfg.agent_timeout_s,
        budget_usd=cfg.budget_usd,
    )
    mounts: list[tuple[str, str, str]] = [(str(run_dir / "repo"), "/repo", "ro")]
    group_add = None
    if cfg.allow_docker_socket:
        # Opt-in (--allow-docker-socket): lets tools that manage containers
        # (e.g. `thv run`) actually work in the sandbox. Pierces isolation.
        # group-add gives the non-root sandbox user permission to use the
        # socket — without it every call fails with EACCES and tools report
        # "no container runtime available" despite the mount.
        from readme2demo.sandbox import docker_socket_gid

        mounts.append(("/var/run/docker.sock", "/var/run/docker.sock", "rw"))
        group_add = docker_socket_gid(cfg.base_image)
    sandbox = Sandbox(
        image=cfg.base_image,
        env=env,
        mounts=mounts,
        group_add=group_add,
        network=cfg.network,
        memory=cfg.memory,
        cpus=cfg.cpus,
        pids_limit=cfg.pids_limit,
    )
    try:
        sandbox.start()
        # The agent works on a writable copy; /repo stays pristine.
        sandbox.exec("cp -r /repo/. /work/ 2>/dev/null || true")
        sandbox.exec(f"mkdir -p {posixpath.dirname(PROMPT_CONTAINER_PATH)}")
        sandbox.copy_in(prompt_path, PROMPT_CONTAINER_PATH)

        agent_res = sandbox.exec(
            engine.build_command(limits), timeout=cfg.agent_timeout_s
        )

        # Always pull the agent's full log to the host BEFORE the sandbox is
        # destroyed — a 2KB stderr tail once hid the real server error behind
        # a client-side traceback, leaving nothing to diagnose with.
        stderr_container = posixpath.join(
            posixpath.dirname(TRANSCRIPT_CONTAINER_PATH), "agent.stderr"
        )
        host_stderr = run_dir / "agent.stderr"
        try:
            sandbox.copy_out(stderr_container, host_stderr)
        except SandboxError:
            pass  # best-effort; the tail read below still works

        try:
            sandbox.copy_out(TRANSCRIPT_CONTAINER_PATH, transcript_path)
        except SandboxError:
            pass  # handled below via the empty/missing check

        if not transcript_path.exists() or transcript_path.stat().st_size == 0:
            stderr_tail = _read_stderr_tail(sandbox)
            log_hint = (
                f" Full agent log: {host_stderr}." if host_stderr.exists() else ""
            )
            raise AgentRunError(
                f"Agent produced no transcript (engine={engine.name}, "
                f"exec exit={agent_res.exit_code}).{log_hint} "
                f"stderr tail:\n{stderr_tail}"
            )
    finally:
        sandbox.destroy()

    return transcript_path


def _read_stderr_tail(sandbox: Sandbox) -> str:
    """Best-effort tail of the agent's stderr file for error reporting."""
    stderr_container = posixpath.join(
        posixpath.dirname(TRANSCRIPT_CONTAINER_PATH), "agent.stderr"
    )
    try:
        res = sandbox.exec(
            f"tail -c {_STDERR_TAIL_BYTES} {stderr_container} 2>/dev/null || true",
            timeout=30,
        )
        return res.output.strip() or "(empty)"
    except SandboxError:
        return "(unavailable)"
