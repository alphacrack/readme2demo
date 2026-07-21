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

import sys
from pathlib import Path
import json
from typing import Any, Optional

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape

from readme2demo.config import Config
from readme2demo.manifest import STAGES, Manifest
from readme2demo.orchestrator import (
    Orchestrator,
    PipelineError,
    summarize,
    summarize_markdown,
)
from readme2demo import __version__ as version


# Sentinel value the argv normalizer injects after a bare provider preset flag
# (`--gemini` / `--openai` / `--anthropic`), meaning "no model named here —
# resolve from --model / the provider's model env var".
PRESET_MODEL_UNSET = "__preset-model-unset__"


def _normalize_preset_argv(args: list[str]) -> list[str]:
    """Give the provider preset flags an optional value.

    Click supports optional-value options natively (``flag_value``), but typer
    drops that setting when building the click command, so a plain
    ``--gemini`` (or ``--openai`` / ``--anthropic``) would swallow the next
    token as its value. This pure pre-parser makes both spellings work: after
    a bare preset flag whose next token is missing, another flag, or doesn't
    look like one of that provider's model names (per
    ``ProviderSpec.model_prefixes``), the :data:`PRESET_MODEL_UNSET` sentinel
    is injected. For a model name outside those prefixes (e.g. a tuned model),
    use the explicit ``--gemini=<name>`` form, which is never rewritten.
    """
    from readme2demo.llm import PROVIDERS

    out: list[str] = []
    for i, arg in enumerate(args):
        out.append(arg)
        spec = PROVIDERS.get(arg[2:]) if arg.startswith("--") else None
        if spec is not None:
            nxt = args[i + 1] if i + 1 < len(args) else None
            if nxt is None or not nxt.startswith(spec.model_prefixes):
                out.append(PRESET_MODEL_UNSET)
    return out


class _OptionalPresetValueTyper(typer.Typer):
    """Typer app that normalizes argv so ``--gemini [MODEL]`` etc. work.

    Applies only to the real CLI entry points (console script and
    ``python -m``); typer's CliRunner bypasses ``__call__``, so tests exercise
    :func:`_normalize_preset_argv` directly.
    """

    def __call__(self, *args, **kwargs):
        sys.argv = [sys.argv[0], *_normalize_preset_argv(sys.argv[1:])]
        return super().__call__(*args, **kwargs)


app = _OptionalPresetValueTyper(
    name="readme2demo",
    help="Verified tutorial + demo video generation from a repo's README.",
    no_args_is_help=True,
)
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(version)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    pass


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
    return _load_config(
        config_file,
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


def _load_config(config_file: Optional[Path], **overrides: Any) -> Config:
    try:
        return Config.load(toml_path=config_file, **overrides)
    except ValidationError as exc:
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"])
        source = config_file or Path("readme2demo.toml")
        if error["type"] == "extra_forbidden":
            console.print(
                f"[red]Unknown config key '{escape(location)}' in "
                f"{escape(str(source))}.[/]"
            )
        else:
            console.print(
                f"[red]Invalid configuration in {escape(str(source))}: "
                f"{escape(error['msg'])}.[/]"
            )
        raise typer.Exit(2) from None


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


def _select_preset(
    gemini: Optional[str],
    openai: Optional[str],
    anthropic: Optional[str],
) -> Optional[tuple[str, Optional[str]]]:
    """Reconcile the mutually exclusive provider preset flags.

    Returns ``(provider, model)`` for the one preset given (``model`` is None
    for a bare flag — the :data:`PRESET_MODEL_UNSET` sentinel is unwrapped
    here), ``None`` when no preset was given, and raises
    ``typer.BadParameter`` when more than one preset is on the command line.
    """
    chosen = {
        name: value
        for name, value in (
            ("gemini", gemini), ("openai", openai), ("anthropic", anthropic)
        )
        if value is not None
    }
    if len(chosen) > 1:
        flags = ", ".join(f"--{name}" for name in chosen)
        raise typer.BadParameter(
            f"Provider presets are mutually exclusive, but {flags} were all "
            f"given. Pick one."
        )
    if not chosen:
        return None
    ((name, value),) = chosen.items()
    return name, (None if value == PRESET_MODEL_UNSET else value)


def _apply_provider(
    provider: str,
    engine: Optional[str],
    model: Optional[str],
    llm_backend: Optional[str],
    preset_model: Optional[str] = None,
) -> tuple[str, str, str]:
    """Resolve a provider preset: OpenHands engine + that provider everywhere.

    Returns the concrete ``(engine, model, llm_backend)`` for this run and
    bridges the provider's API key into the OpenHands engine's env (see
    :func:`llm.apply_provider_session`). The model comes from ``preset_model``
    (the ``--<provider> <model>`` value), else ``model`` (``--model``), else
    the provider's model env var — hardcoded fallbacks only where the spec
    allows one (Anthropic). Raises ``typer.BadParameter`` for flags that
    contradict the preset, so a stray ``--engine claude-code`` or a mismatched
    ``--llm-backend`` fails loudly instead of being silently overridden.
    """
    from readme2demo import llm

    spec = llm.PROVIDERS[provider]
    flag = f"--{provider}"
    if llm_backend not in (None, spec.backend):
        raise typer.BadParameter(
            f"{flag} implies --llm-backend {spec.backend}, but --llm-backend "
            f"{llm_backend!r} was also given. Drop one of them."
        )
    if engine not in (None, "openhands"):
        raise typer.BadParameter(
            f"{flag} runs the agent on the OpenHands engine, but --engine "
            f"{engine!r} was also given. Drop --engine or drop {flag}."
        )
    if (
        preset_model
        and model
        and model.removeprefix(f"{spec.litellm_prefix}/").startswith(spec.model_prefixes)
        and preset_model != model
    ):
        raise typer.BadParameter(
            f"{spec.title} model given twice and they differ: {flag} "
            f"{preset_model!r} vs --model {model!r}. Pass the model only once."
        )
    try:
        resolved_model = llm.apply_provider_session(provider, preset_model or model)
    except llm.LLMError as e:
        raise typer.BadParameter(str(e)) from None
    return "openhands", resolved_model, spec.backend


def _announce_preset(provider: str, model: str) -> None:
    from readme2demo.llm import PROVIDERS

    spec = PROVIDERS[provider]
    console.print(
        f"[dim]--{provider}: engine=openhands, model={model}, "
        f"LLM backend={spec.backend} (auth via {spec.key_env}).[/]"
    )


def _apply_engine_image(cfg: Config) -> Config:
    """Give the run the engine's own sandbox image when none was chosen.

    OpenHands needs its runtime baked into the image (readme2demo/openhands),
    while the standard base image serves claude-code. Applies only when
    base_image was set nowhere (neither CLI flag nor toml) — an explicit
    choice always wins. Unknown engine names pass through untouched; preflight
    reports them with the full list of valid engines.
    """
    from readme2demo.engines import get_engine
    from readme2demo.engines.base import EngineError

    try:
        engine = get_engine(cfg.engine)
    except EngineError:
        return cfg
    if engine.default_image and "base_image" not in cfg.model_fields_set:
        return cfg.model_copy(update={"base_image": engine.default_image})
    return cfg


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
    output_dir: Optional[Path] = typer.Option(None, "-o", "--output-dir", help="Runs directory"),
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
             "auto | api | claude-cli | gemini | openai (claude-cli = host "
             "`claude -p` on your subscription; self-hosted runs — use api to "
             "host for other users)",
    ),
    gemini: Optional[str] = typer.Option(
        None, "--gemini",
        help="Run this session entirely on Google Gemini: the OpenHands engine "
             "drives the sandboxed agent and the planner/distiller/tutorial "
             "passes use Gemini, all via GEMINI_API_KEY. Takes an optional "
             "model name (`--gemini gemini-3.5-flash`); bare `--gemini` uses "
             "--model if it names a Gemini model, else the GEMINI_MODEL env "
             "var. No model name is built in — one of those must name it. For "
             "a model name not starting with 'gemini', use `--gemini=<name>`.",
    ),
    openai: Optional[str] = typer.Option(
        None, "--openai",
        help="Run this session entirely on OpenAI: the OpenHands engine drives "
             "the sandboxed agent and the planner/distiller/tutorial passes "
             "use OpenAI, all via OPENAI_API_KEY. Takes an optional model name "
             "(`--openai gpt-5.1`); bare `--openai` uses --model if it names "
             "an OpenAI model, else the OPENAI_MODEL env var. No model name is "
             "built in — one of those must name it. For a model name outside "
             "the gpt/o/chatgpt prefixes, use `--openai=<name>`.",
    ),
    anthropic: Optional[str] = typer.Option(
        None, "--anthropic",
        help="Run the sandboxed agent on the OpenHands engine with a Claude "
             "model, and the planner/distiller/tutorial passes on the "
             "Anthropic API, all via ANTHROPIC_API_KEY (metered billing — the "
             "default no-flag setup uses the claude-code engine on your "
             "subscription instead). Takes an optional model name; bare "
             "`--anthropic` uses --model, else the ANTHROPIC_MODEL env var, "
             "else the config default.",
    ),
    config_file: Optional[Path] = typer.Option(
        None, "--config", help="readme2demo.toml path",
        exists=True, dir_okay=False, resolve_path=True,
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Run ingest/plan only and print feasibility/blockers, then stop "
             "— a cheap feasibility check before spending agent time.",
    ),
) -> None:
    """Run the full pipeline against a repository, a step-by-step guide, or both."""
    repo_url = _resolve_repo(repo_url, github_repo, step_by_step)
    preset = _select_preset(gemini, openai, anthropic)
    if preset is not None:
        provider, preset_model = preset
        engine, model, llm_backend = _apply_provider(
            provider, engine, model, llm_backend, preset_model
        )
        _announce_preset(provider, model)
    cfg = _build_config(
        config_file, engine, model, output_dir, timeout,
        budget_usd, max_turns, skip_video, base_image, llm_backend,
    )
    if dry_run:
        cfg = cfg.model_copy(update={"dry_run": True})
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
    cfg = _apply_engine_image(cfg)
    _preflight(cfg)
    orch = Orchestrator.new_run(repo_url, cfg)
    _drive(orch)


@app.command()
def resume(
    run_dir: Path = typer.Argument(
        ..., exists=True, file_okay=False,
        help="Path to an existing runs/<run-id> directory",
    ),
    from_stage: Optional[str] = typer.Option(
        None, "--from-stage", help=f"Re-run from this stage: {', '.join(STAGES)}"
    ),
    llm_backend: Optional[str] = typer.Option(None, "--llm-backend"),
    gemini: Optional[str] = typer.Option(
        None, "--gemini",
        help="Resume on Google Gemini (OpenHands engine + Gemini passes, via "
             "GEMINI_API_KEY). Takes an optional model name; bare `--gemini` "
             "uses the config model if it names a Gemini model, else the "
             "GEMINI_MODEL env var. See `readme2demo run --help`.",
    ),
    openai: Optional[str] = typer.Option(
        None, "--openai",
        help="Resume on OpenAI (OpenHands engine + OpenAI passes, via "
             "OPENAI_API_KEY). Takes an optional model name; bare `--openai` "
             "uses the config model if it names an OpenAI model, else the "
             "OPENAI_MODEL env var. See `readme2demo run --help`.",
    ),
    anthropic: Optional[str] = typer.Option(
        None, "--anthropic",
        help="Resume on the Anthropic API (OpenHands engine + api passes, via "
             "ANTHROPIC_API_KEY). Takes an optional model name; bare "
             "`--anthropic` uses the config model, else ANTHROPIC_MODEL, else "
             "the config default. See `readme2demo run --help`.",
    ),
    allow_docker_socket: bool = typer.Option(False, "--allow-docker-socket"),
    config_file: Optional[Path] = typer.Option(
        None, "--config", exists=True, dir_okay=False, resolve_path=True
    ),
) -> None:
    """Resume an interrupted run (optionally re-running from a given stage)."""
    if from_stage and from_stage not in STAGES:
        # escape AFTER repr — the other order re-breaks the markup (repr
        # doubles escape()'s backslashes, reviving the swallowed tag).
        console.print(
            f"[red]Unknown stage {escape(repr(from_stage))}. "
            f"Stages: {', '.join(STAGES)}[/]"
        )
        raise typer.Exit(2)
    preset = _select_preset(gemini, openai, anthropic)
    cfg = _load_config(config_file, llm_backend=llm_backend)
    if preset is not None:
        provider, preset_model = preset
        # Precedence: --<provider> <model> > config model (CLI beats toml) >
        # the provider's model env var — passed as one value so the run-style
        # two-flag conflict check doesn't fire against the toml model.
        engine, resolved, backend = _apply_provider(
            provider, None, preset_model or cfg.model, llm_backend
        )
        cfg = cfg.model_copy(
            update={"engine": engine, "model": resolved, "llm_backend": backend}
        )
        _announce_preset(provider, resolved)
    if allow_docker_socket:
        cfg = cfg.model_copy(update={"allow_docker_socket": True})
        console.print(
            "[red]⚠ --allow-docker-socket: host Docker socket mounted into "
            "the sandbox — trusted repos only.[/]"
        )
    cfg = _apply_engine_image(cfg)
    _preflight(cfg)
    orch = Orchestrator.resume(run_dir, cfg, from_stage=from_stage)
    _drive(orch)


# Artifact filenames the pipeline writes to the run-dir root, in pipeline
# order. `report --markdown` lists whichever of these exist — existence checks
# only, so the summary keeps working on partial and failed runs.
REPORT_ARTIFACTS = (
    "commands.sh",
    "demo.tape",
    "step_by_step.md",
    "tutorial.md",
    "troubleshooting.md",
    "howto.jsonld",
    "demo.mp4",
    "demo.gif",
)


def _report_exit_code(manifest: Manifest) -> int:
    """Exit code for ``report``, mirroring ``_drive``'s outcome handling.

    2 — a stage failed (the run itself broke);
    1 — no stage failed, but the fresh-container replay did not pass
        (completed UNVERIFIED);
    0 — verified.

    A failed stage outranks ``verified`` so a stale verdict from an earlier
    pass can never mask a later failure.
    """
    if any(rec.status == "failed" for rec in manifest.stages.values()):
        return 2
    return 0 if manifest.verified else 1


@app.command()
def report(
    run_dir: Path = typer.Argument(..., help="Path to a runs/<run-id> directory"),
    json_output: bool = typer.Option(False, "--json", help="Emit summary as JSON"),
    markdown_output: bool = typer.Option(
        False,
        "--markdown",
        help="Emit summary as GitHub-flavored Markdown "
        "(pipe into $GITHUB_STEP_SUMMARY)",
    ),
) -> None:
    """Print a summary of a run: stage statuses, verification, cost.

    Exit codes signal the run's state so CI can gate on this command:
    0 = verified, 1 = completed but UNVERIFIED, 2 = a stage failed.
    """
    if json_output and markdown_output:
        raise typer.BadParameter("--json and --markdown are mutually exclusive")
    manifest = Manifest.load(run_dir)
    if markdown_output:
        # The renderer is pure; the CLI owns the filesystem side. Existence
        # checks only — never parse other run files, so partial runs report.
        artifacts = [n for n in REPORT_ARTIFACTS if (run_dir / n).exists()]
        # plain print(), like --json: console.print wraps at terminal width
        # and would mangle tables piped into $GITHUB_STEP_SUMMARY.
        print(summarize_markdown(manifest, artifacts))
        raise typer.Exit(_report_exit_code(manifest))
    if json_output:
        output_data = {
            "stages": [
                {"name": name, "status": rec.status}
                for name, rec in manifest.stages.items()
            ],
            "verified": manifest.verified,
            "cost": manifest.total_cost_usd,
            "commit": manifest.commit_sha,
        }
        print(json.dumps(output_data, indent=2))
        raise typer.Exit(_report_exit_code(manifest))
    # escape(): stage errors may contain [bracketed] text Rich would swallow.
    console.print(escape(summarize(manifest)))
    raise typer.Exit(_report_exit_code(manifest))


def _preflight(cfg: Config) -> None:
    """Fail fast, before creating a run dir, if the environment can't work."""
    import shutil

    from readme2demo import llm
    from readme2demo.engines import get_engine
    from readme2demo.engines.base import EngineError
    from readme2demo.llm import LLMError

    problems: list[str] = []

    # LLM backend for the planner/distiller/tutorial passes. check_sdk and
    # check_model make a missing/broken optional SDK or an unresolvable model
    # a preflight error — a --openai run once burned its ingest stage on an
    # ImportError instead. set_backend sits inside the try so a bad toml
    # llm_backend is a clean ✗, not a traceback.
    try:
        llm.set_backend(cfg.llm_backend)
        backend = llm.resolve_backend()
        llm.check_sdk(backend)
        llm.check_model(backend, cfg.model)
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

    # Agent engine auth (forwarded into the sandbox) + sandbox image probe:
    # an image without the engine's runtime dies mid-run with a bare exit 127
    # and no transcript, so it must be caught here, before agent time is spent.
    try:
        engine = get_engine(cfg.engine)
        engine.resolve_env()
        engine.check_image(cfg.base_image)
    except EngineError as e:
        problems.append(str(e))

    if shutil.which("docker") is None:
        problems.append("docker CLI not found on PATH — install Docker Desktop and retry.")

    if problems:
        for p in problems:
            # escape(): problem text may contain [bracketed] content (e.g.
            # "pip install 'readme2demo[openai]'") that Rich would otherwise
            # parse as markup and silently swallow.
            console.print(f"[red]✗[/] {escape(p)}")
        raise typer.Exit(2)


def _drive(orch: Orchestrator) -> None:
    # escape(): error and summary text carries arbitrary content ("pip install
    # 'readme2demo[openai]'", shell snippets like `[ -f x ]`) that Rich would
    # otherwise parse as markup and silently swallow.
    try:
        manifest = orch.run()
    except PipelineError as e:
        console.print(f"[red]Pipeline stopped:[/] {escape(str(e))}")
        console.print(escape(summarize(orch.manifest)))
        raise typer.Exit(1)
    except Exception as e:  # noqa: BLE001 — stage errors are already in the manifest
        console.print(f"[red]{type(e).__name__}:[/] {escape(str(e))}")
        console.print(escape(summarize(orch.manifest)))
        console.print(
            f"[dim]Fix the cause, then: readme2demo resume {escape(str(orch.run_dir))}[/]"
        )
        raise typer.Exit(1)
    console.print()
    console.print(escape(summarize(manifest)))
    if manifest.verified:
        console.print(
            f"\n[bold green]✅ Verified.[/] Artifacts in "
            f"[bold]{escape(str(orch.run_dir))}[/]: "
            "tutorial.md, commands.sh, demo.tape"
            + ("" if orch.cfg.skip_video else ", demo.mp4, demo.gif")
        )
    else:
        console.print(
            f"\n[bold yellow]⚠ Completed UNVERIFIED.[/] "
            f"See {escape(str(orch.run_dir))}/verify.log"
        )


if __name__ == "__main__":
    app()
