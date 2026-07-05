"""readme2demo CLI.

    readme2demo run https://github.com/example/tool
    readme2demo run -gr https://github.com/example/tool
    readme2demo run -s ./my_guide.md                    # guide-only, no repo
    readme2demo run -gr https://github.com/example/tool -s ./my_guide.md  # both
    readme2demo resume runs/tool-20260702-... --from-stage render
    readme2demo report runs/tool-20260702-...

The repo is OPTIONAL: pass it positionally or with -gr/--github-repo, supply a
step-by-step guide with -s/--step-by-step, or both. At least one is required;
when both are given, the guide is treated as authoritative and both are used.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from readme2demo.config import Config
from readme2demo.manifest import STAGES, Manifest
from readme2demo.orchestrator import Orchestrator, PipelineError, summarize

app = typer.Typer(
    name="readme2demo",
    help="Verified tutorial + demo video generation from a repo's README.",
    no_args_is_help=True,
)
console = Console()


def _build_config(
    config_file: Optional[Path],
    engine: Optional[str],
    model: Optional[str],
    output_dir: Optional[Path],
    timeout: Optional[int],
    budget_usd: Optional[float],
    max_turns: Optional[int],
    skip_video: Optional[bool],
    base_image: Optional[str],
    llm_backend: Optional[str] = None,
) -> Config:
    return Config.load(
        toml_path=config_file,
        engine=engine,
        model=model,
        runs_dir=output_dir,
        agent_timeout_s=timeout,
        budget_usd=budget_usd,
        max_turns=max_turns,
        skip_video=skip_video,
        base_image=base_image,
        llm_backend=llm_backend,
    )


def _resolve_repo(
    repo_url: Optional[str],
    github_repo: Optional[str],
    step_by_step: Optional[Path],
) -> Optional[str]:
    """Reconcile the positional repo, ``-gr/--github-repo``, and ``-s`` guide.

    The positional argument and ``-gr`` are two spellings of the same input:
    if both are given they must agree. Returns the effective repo URL, or
    ``None`` for a guide-only run. Raises ``typer.BadParameter`` when the two
    repo spellings conflict, or when neither a repo nor a guide is supplied.
    """
    if repo_url and github_repo and repo_url != github_repo:
        raise typer.BadParameter(
            f"Repo given twice and they differ: positional {repo_url!r} vs "
            f"-gr/--github-repo {github_repo!r}. Pass the repo only once."
        )
    repo = github_repo or repo_url
    if not repo and step_by_step is None:
        raise typer.BadParameter(
            "Provide a GitHub/GitLab repo (positional or -gr/--github-repo) "
            "and/or a step-by-step guide (-s/--step-by-step) — at least one "
            "is required."
        )
    return repo


@app.command()
def run(
    repo_url: Optional[str] = typer.Argument(
        None,
        help="GitHub/GitLab https URL of the repo (optional). Alternatively use "
             "-gr/--github-repo, or run from a --step-by-step guide alone.",
    ),
    github_repo: Optional[str] = typer.Option(
        None, "-gr", "--github-repo",
        help="GitHub/GitLab https URL of the repo. OPTIONAL — provide this, a "
             "--step-by-step guide, or both (when both are given, both are used).",
    ),
    engine: Optional[str] = typer.Option(None, help="Agent engine: claude-code | openhands"),
    model: Optional[str] = typer.Option(None, help="Model for planner/distiller/tutorial passes"),
    output_dir: Optional[Path] = typer.Option(None, "--output-dir", help="Runs directory"),
    timeout: Optional[int] = typer.Option(None, help="Agent wall-clock timeout (s)"),
    budget_usd: Optional[float] = typer.Option(None, help="Abort if agent cost exceeds this"),
    max_turns: Optional[int] = typer.Option(None, help="Agent max turns"),
    skip_video: Optional[bool] = typer.Option(None, "--skip-video/--with-video"),
    base_image: Optional[str] = typer.Option(None, help="Sandbox base image"),
    step_by_step: Optional[Path] = typer.Option(
        None, "-s", "--step-by-step",
        help="Your own step_by_step.md: treated as the authoritative guide "
             "(planner + agent follow it) and the demo video is built from it",
        exists=True, dir_okay=False, resolve_path=True,
    ),
    allow_docker_socket: bool = typer.Option(
        False, "--allow-docker-socket",
        help="Mount the host Docker socket into the sandbox (needed for tools "
             "that manage containers, e.g. `thv run`). SECURITY TRADEOFF: the "
             "socket pierces sandbox isolation — only for repos you trust.",
    ),
    llm_backend: Optional[str] = typer.Option(
        None, "--llm-backend",
        help="LLM backend for planner/distiller/tutorial passes: "
             "auto | api | claude-cli (host `claude -p` on your subscription; "
             "self-hosted runs — use api to host for other users)",
    ),
    config_file: Optional[Path] = typer.Option(None, "--config", help="readme2demo.toml path"),
) -> None:
    """Run the full pipeline against a repository, a step-by-step guide, or both."""
    repo_url = _resolve_repo(repo_url, github_repo, step_by_step)
    cfg = _build_config(
        config_file, engine, model, output_dir, timeout,
        budget_usd, max_turns, skip_video, base_image, llm_backend,
    )
    if step_by_step is not None:
        cfg = cfg.model_copy(update={"step_by_step": step_by_step})
    if repo_url is None:
        console.print(
            "[dim]No repo URL — guide-only run: building from your "
            "--step-by-step guide (it must be self-contained).[/]"
        )
    if allow_docker_socket:
        cfg = cfg.model_copy(update={"allow_docker_socket": True})
        console.print(
            "[red]⚠ --allow-docker-socket: the host Docker socket is mounted "
            "into the sandbox. This pierces isolation — a malicious repo "
            "could control your Docker host. Only for repos you trust.[/]"
        )
    _preflight(cfg)
    orch = Orchestrator.new_run(repo_url, cfg)
    _drive(orch)


@app.command()
def resume(
    run_dir: Path = typer.Argument(..., help="Path to an existing runs/<run-id> directory"),
    from_stage: Optional[str] = typer.Option(
        None, "--from-stage", help=f"Re-run from this stage: {', '.join(STAGES)}"
    ),
    llm_backend: Optional[str] = typer.Option(None, "--llm-backend"),
    allow_docker_socket: bool = typer.Option(False, "--allow-docker-socket"),
    config_file: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Resume an interrupted run (optionally re-running from a given stage)."""
    if from_stage and from_stage not in STAGES:
        console.print(f"[red]Unknown stage {from_stage!r}. Stages: {', '.join(STAGES)}[/]")
        raise typer.Exit(2)
    cfg = Config.load(toml_path=config_file, llm_backend=llm_backend)
    if allow_docker_socket:
        cfg = cfg.model_copy(update={"allow_docker_socket": True})
        console.print(
            "[red]⚠ --allow-docker-socket: host Docker socket mounted into "
            "the sandbox — trusted repos only.[/]"
        )
    _preflight(cfg)
    orch = Orchestrator.resume(run_dir, cfg, from_stage=from_stage)
    _drive(orch)


@app.command()
def report(
    run_dir: Path = typer.Argument(..., help="Path to a runs/<run-id> directory"),
) -> None:
    """Print a summary of a run: stage statuses, verification, cost."""
    manifest = Manifest.load(run_dir)
    console.print(summarize(manifest))


def _preflight(cfg: Config) -> None:
    """Fail fast, before creating a run dir, if the environment can't work."""
    import shutil

    from readme2demo import llm
    from readme2demo.engines import get_engine
    from readme2demo.engines.base import EngineError
    from readme2demo.llm import LLMError

    problems: list[str] = []

    # LLM backend for the planner/distiller/tutorial passes.
    llm.set_backend(cfg.llm_backend)
    try:
        backend = llm.resolve_backend()
        console.print(f"[dim]LLM backend: {backend}[/]")
        if backend == "claude-cli":
            console.print(
                "[dim]claude-cli backend: running on your Claude Code "
                "subscription — supported for self-hosted runs (slower, and "
                "subject to your plan's usage caps). For a hosted/multi-tenant "
                "service use --llm-backend api; per Anthropic's terms, "
                "subscription auth may not power a product for other users.[/]"
            )
    except LLMError as e:
        problems.append(str(e))

    # Agent engine auth (forwarded into the sandbox).
    try:
        get_engine(cfg.engine).resolve_env()
    except EngineError as e:
        problems.append(str(e))

    if shutil.which("docker") is None:
        problems.append("docker CLI not found on PATH — install Docker Desktop and retry.")

    if problems:
        for p in problems:
            console.print(f"[red]✗[/] {p}")
        raise typer.Exit(2)


def _drive(orch: Orchestrator) -> None:
    try:
        manifest = orch.run()
    except PipelineError as e:
        console.print(f"[red]Pipeline stopped:[/] {e}")
        console.print(summarize(orch.manifest))
        raise typer.Exit(1)
    except Exception as e:  # noqa: BLE001 — stage errors are already in the manifest
        console.print(f"[red]{type(e).__name__}:[/] {e}")
        console.print(summarize(orch.manifest))
        console.print(
            f"[dim]Fix the cause, then: readme2demo resume {orch.run_dir}[/]"
        )
        raise typer.Exit(1)
    console.print()
    console.print(summarize(manifest))
    if manifest.verified:
        console.print(
            f"\n[bold green]✅ Verified.[/] Artifacts in [bold]{orch.run_dir}[/]: "
            "tutorial.md, commands.sh, demo.tape"
            + ("" if orch.cfg.skip_video else ", demo.mp4, demo.gif")
        )
    else:
        console.print(
            f"\n[bold yellow]⚠ Completed UNVERIFIED.[/] See {orch.run_dir}/verify.log"
        )


if __name__ == "__main__":
    app()
