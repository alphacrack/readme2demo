"""M8 — Tutorial Generator: verified data in, human-facing docs out.

Produces ``tutorial.md`` (LLM-polished prose around code-enforced commands and
replay-captured outputs) and ``troubleshooting.md`` (pure Python, from the
agent's ``FIX:`` markers and failed commands).

Grounding rules enforced here in code, never delegated to the prompt:

* Every command block in the tutorial is byte-identical to the distilled
  outline's command (:func:`enforce_commands`).
* Expected-output blocks come from ``verify.log`` (the clean replay), falling
  back to the agent run's log — never from the model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from . import llm
from .types import CommandLog, Plan, TutorialOutline

#: Maximum characters of captured output quoted per step / error block.
OUTPUT_TRUNCATE_CHARS = 800

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_TEMPLATES_DIR = Path(__file__).parent / "templates"


class TutorialError(RuntimeError):
    """Raised when the tutorial stage cannot produce its artifacts."""


# -- verify.log parsing -------------------------------------------------------


def _normalize(cmd: str) -> str:
    """Whitespace-normalize a command for fuzzy matching."""
    return " ".join(cmd.split())


_ANSI_RE = None  # compiled lazily


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences (terraform et al. color their output)."""
    global _ANSI_RE
    if _ANSI_RE is None:
        import re as _re

        _ANSI_RE = _re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\[[0-9;]+m")
    return _ANSI_RE.sub("", text)


def _truncate(text: str, limit: int = OUTPUT_TRUNCATE_CHARS) -> str:
    """Strip ANSI noise, trim whitespace, cap at ``limit`` characters."""
    return _strip_ansi(text).strip()[:limit]


def extract_expected_outputs(verify_log: str, commands: list[str]) -> dict[str, str]:
    """Map each command to the output it produced in a ``bash -x`` style log.

    ``commands.sh`` runs under ``set -euxo pipefail``, so the replay log echoes
    every top-level command as a ``+ cmd`` line followed by its output. This
    splits the log into segments at lines starting with ``"+ "`` (nested
    expansions echo as ``++ ...`` and are treated as output, not commands) and
    matches segments to ``commands`` by whitespace-normalized equality.

    Best-effort: commands not found in the log, or whose captured output is
    empty, are simply absent from the result. When a command runs more than
    once, the first occurrence wins. Outputs are truncated to
    ``OUTPUT_TRUNCATE_CHARS`` characters.

    Returns:
        Dict keyed by the *original* command strings from ``commands``.
    """
    # Parse the log into (normalized command, output) segments.
    segments: dict[str, str] = {}
    current_cmd: str | None = None
    current_lines: list[str] = []

    def _flush() -> None:
        if current_cmd is not None and current_cmd not in segments:
            output = _truncate("\n".join(current_lines))
            if output:
                segments[current_cmd] = output

    for line in verify_log.splitlines():
        if line.startswith("++"):
            continue  # nested-expansion xtrace (e.g. `++ tfdrift scan`) — noise
        if line.startswith("+ "):
            _flush()
            current_cmd = _normalize(line[2:])
            current_lines = []
        elif current_cmd is not None:
            current_lines.append(line)
    _flush()

    result: dict[str, str] = {}
    for cmd in commands:
        output = segments.get(_normalize(cmd))
        if output is not None:
            result[cmd] = output
    return result


# -- grounding guard ----------------------------------------------------------


def enforce_commands(original: TutorialOutline, polished: TutorialOutline) -> TutorialOutline:
    """Return ``polished`` with every verified artifact restored from ``original``.

    The LLM pass may only touch prose (title, intro, prereqs wording, step
    titles and explanations). Commands are verified artifacts and expected
    outputs come from the replay log, so both are restored from ``original``
    regardless of what the model returned. If the model added, removed, or
    reordered steps, the step list is rebuilt to match ``original`` step for
    step, keeping the polished prose where a same-index step exists.
    """
    enforced = polished.model_copy(deep=True)
    steps = []
    for i, orig_step in enumerate(original.steps):
        if i < len(enforced.steps):
            step = enforced.steps[i].model_copy()
            step.command = orig_step.command
            step.expected_output = orig_step.expected_output
        else:
            step = orig_step.model_copy()
        steps.append(step)
    enforced.steps = steps
    return enforced


# -- artifact writers ---------------------------------------------------------


def _verified_line(verified: bool, base_image: str, commit_sha: str | None) -> str:
    """The verification badge — the product's signature line."""
    if not verified:
        return "⚠️ UNVERIFIED — the replay did not pass"
    date = datetime.now(timezone.utc).date().isoformat()
    sha7 = commit_sha[:7] if commit_sha else "unknown"
    return f"✅ Verified on {date} · image {base_image} · commit {sha7}"


def _repo_name(repo_url: str) -> str:
    """`https://github.com/owner/repo` → `owner/repo` (best effort).

    Git also accepts scp-style URLs such as
    ``git@github.com:owner/repo.git``. Normalize the host/path separator
    before extracting the final owner/repository pair so those URLs produce
    the same name as their HTTPS equivalents.
    """
    value = repo_url.rstrip("/").removesuffix(".git")
    if "://" not in value and ":" in value:
        value = value.rsplit(":", 1)[-1]
    parts = value.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else repo_url


def seo_title(repo_url: str, fallback: str) -> str:
    """Query-shaped page title: matches how people actually search."""
    if not repo_url:
        return fallback
    return f"How to install and run {_repo_name(repo_url)} — verified tutorial"


def seo_description(intro: str, max_len: int = 160) -> str:
    """First sentence of the intro, clamped to meta-description length."""
    first = intro.split(". ")[0].strip().replace('"', "'")
    if not first.endswith("."):
        first += "."
    suffix = " Every command verified in a clean container."
    if len(first) + len(suffix) <= max_len:
        first += suffix
    return first[:max_len]


def _render_tutorial_md(
    run_dir: Path,
    outline: TutorialOutline,
    verified: bool,
    base_image: str,
    commit_sha: str | None,
    repo_url: str = "",
) -> Path:
    """Render ``tutorial.md`` from the packaged Jinja2 template.

    SEO/GEO shape: YAML front matter (title/description/date/provenance) for
    static-site pipelines, a query-shaped ``seo_title``, and an explicit
    source + verification provenance footer that generative engines can cite.
    """
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = env.get_template("tutorial.md.j2")
    has_video = (run_dir / "demo.gif").exists() or (run_dir / "demo.mp4").exists()
    text = template.render(
        title=outline.title,
        seo_title=seo_title(repo_url, outline.title),
        description=seo_description(outline.intro),
        verified=verified,
        verified_line=_verified_line(verified, base_image, commit_sha),
        intro=outline.intro,
        prereqs=outline.prereqs,
        repo_url=repo_url,
        commit_sha=commit_sha,
        steps=[
            {
                "index": i + 1,
                "title": step.title,
                "command": step.command,
                "explanation": step.explanation,
                "expected_output": step.expected_output,
            }
            for i, step in enumerate(outline.steps)
        ],
        has_video=has_video,
        generated_at=datetime.now(timezone.utc).date().isoformat(),
    )
    path = run_dir / "tutorial.md"
    path.write_text(text, encoding="utf-8")
    return path


def write_howto_jsonld(
    run_dir: Path,
    outline: TutorialOutline,
    prereqs: list[str],
    repo_url: str,
    verified: bool,
) -> Path:
    """Emit ``howto.jsonld`` — a schema.org HowTo for the tutorial.

    The future docs site embeds this verbatim in each programmatic-SEO page;
    structured data is what makes "how to install X" results eligible for
    rich snippets, and gives generative engines an unambiguous machine-
    readable version of the verified steps.
    """
    doc = {
        "@context": "https://schema.org",
        "@type": "HowTo",
        "name": outline.title,
        "description": seo_description(outline.intro, max_len=300),
        "datePublished": datetime.now(timezone.utc).date().isoformat(),
        "isBasedOn": repo_url or None,
        "tool": [{"@type": "HowToTool", "name": p} for p in prereqs],
        "step": [
            {
                "@type": "HowToStep",
                "position": i + 1,
                "name": s.title,
                "text": s.explanation,
                "itemListElement": [
                    {"@type": "HowToDirection", "text": s.command}
                ],
            }
            for i, s in enumerate(outline.steps)
        ],
        "creativeWorkStatus": "verified" if verified else "unverified",
        "author": {"@type": "SoftwareApplication", "name": "readme2demo"},
    }
    doc = {k: v for k, v in doc.items() if v is not None}
    path = run_dir / "howto.jsonld"
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def write_troubleshooting(run_dir: Path, log: CommandLog) -> Path:
    """Write ``troubleshooting.md`` from the agent's fixes — no LLM involved.

    Each ``FixMarker`` becomes a section pairing the agent's declared fix with
    the error output of a failed command from the run (the i-th failed entry
    for the i-th fix when available, else the last failed entry). If the agent
    declared no fixes, the file simply says the README worked as written.
    """
    date = datetime.now(timezone.utc).date().isoformat()
    lines: list[str] = ["# Troubleshooting", ""]

    if not log.fixes:
        lines += [
            f"No issues encountered — the README worked as written on {date}.",
            "",
        ]
    else:
        lines += [
            "Issues the agent hit while running the README, and how it fixed them.",
            "",
        ]
        failed = [e for e in log.entries if e.exit_code not in (None, 0)]
        for i, fix in enumerate(log.fixes):
            lines.append(f"### {fix.what}")
            lines.append("")
            if fix.because:
                lines.append(f"**Why:** {fix.because}")
                lines.append("")
            entry = failed[i] if i < len(failed) else (failed[-1] if failed else None)
            if entry is not None:
                lines.append("Error seen:")
                lines.append("")
                lines.append("```text")
                lines.append(f"$ {entry.cmd}")
                if entry.output:
                    lines.append(_truncate(entry.output))
                lines.append("```")
                lines.append("")

    lines.append(f"*Generated by readme2demo on {date}.*")
    path = run_dir / "troubleshooting.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# -- main entry point ---------------------------------------------------------


def run_tutorial(
    run_dir: Path,
    plan: Plan,
    log: CommandLog,
    outline: TutorialOutline,
    model: str,
    verified: bool,
    base_image: str,
    commit_sha: str | None,
    generate_step_by_step: bool = True,
    repo_url: str = "",
) -> float:
    """Produce ``tutorial.md`` and ``troubleshooting.md`` in ``run_dir``.

    Flow:

    1. Ground every step's ``expected_output`` in ``verify.log`` (the clean
       replay is the source of truth), falling back to the agent run's output
       for that command.
    2. One LLM pass polishes titles/intro/explanations for a beginner
       audience; :func:`enforce_commands` then restores every command and
       expected output from the grounded outline, so the model cannot alter
       verified artifacts.
    3. Render ``tutorial.md`` from the packaged template, with the
       verification badge reflecting ``verified``.
    4. Write ``troubleshooting.md`` in pure Python from the log's fixes.

    Returns:
        Cost in USD of the LLM pass.

    Raises:
        TutorialError: if the LLM pass fails validation after retries.
    """
    grounded = outline.model_copy(deep=True)

    # (a) expected outputs: verify.log first, agent log as fallback.
    verify_path = run_dir / "verify.log"
    expected: dict[str, str] = {}
    if verify_path.exists():
        verify_text = verify_path.read_text(encoding="utf-8", errors="replace")
        expected = extract_expected_outputs(verify_text, [s.command for s in grounded.steps])
    for step in grounded.steps:
        output = expected.get(step.command)
        if output is None:
            for entry in log.entries:
                if entry.exit_code == 0 and _normalize(entry.cmd) == _normalize(step.command):
                    if entry.output.strip():
                        output = _truncate(entry.output)
        if output:
            step.expected_output = output

    # (b) LLM polish pass, with the grounding guard applied to the result.
    system = (_PROMPTS_DIR / "tutorial.md").read_text(encoding="utf-8")
    user = json.dumps(
        {
            "outline": grounded.model_dump(),
            "expected_outputs": {s.command: s.expected_output for s in grounded.steps},
            "fixes": [f.model_dump() for f in log.fixes],
            "prereqs": plan.prereqs,
        },
        indent=2,
    )
    try:
        polished, cost_usd = llm.complete_json(system, user, model, TutorialOutline)
    except llm.LLMError as e:
        raise TutorialError(f"tutorial LLM pass failed: {e}") from e
    final = enforce_commands(grounded, polished)

    # (c) render tutorial.md + structured data; (d) troubleshooting.md.
    _render_tutorial_md(run_dir, final, verified, base_image, commit_sha, repo_url)
    write_howto_jsonld(run_dir, final, plan.prereqs, repo_url, verified)
    write_troubleshooting(run_dir, log)

    # (e) step_by_step.md — only when the repo didn't provide its own
    # (plan.guide_path unset): a detailed, fully grounded walkthrough built
    # from commands.sh + verified outputs, suitable for contributing back to
    # the repo.
    if generate_step_by_step:
        write_step_by_step(run_dir, plan, final, log, verified, repo_url)

    return cost_usd


def _fallback_step_title(cmd: str) -> str:
    """Readable title for a command the outline has no polished step for."""
    import re as _re

    stripped = cmd.strip()
    # Heredoc: `cat > /path/file <<'EOF' ...` → this step CREATES a file.
    hd = _re.match(r"^\s*(?:cat|tee)\s*>?\s*>?\s*(\S+)\s*<<", stripped)
    if hd:
        return f"Create `{hd.group(1)}`"
    if stripped.startswith("git clone"):
        return "Get the source code"
    if stripped.startswith("cd"):
        return "Move into the working directory"
    if stripped.startswith("export "):
        return "Set up the environment"
    tokens = stripped.split()
    first = tokens[0]
    if first in ("pip", "pip3", "npm", "yarn", "pnpm", "apt", "apt-get"):
        return "Install dependencies"
    if "install" in stripped:
        return "Install prerequisites"
    if any(tok in stripped for tok in ("build", "make", "compile")):
        return "Build the project"
    # Two-token title for subcommand CLIs: `terraform init`, `thv version`.
    if len(tokens) >= 2 and not tokens[1].startswith("-") and tokens[1].isalnum():
        return f"Run `{first} {tokens[1]}`"
    return f"Run `{first}`"


def write_step_by_step(
    run_dir: Path,
    plan: Plan,
    outline: TutorialOutline,
    log: CommandLog,
    verified: bool,
    repo_url: str = "",
) -> Path:
    """Write a detailed ``step_by_step.md`` — every command, in order, grounded.

    Pure Python (no LLM): the step list is ``commands.sh`` itself (minus the
    harness preamble and assertion block), so every step is verified by
    construction. Outline steps contribute polished titles/explanations where
    their command matches; other commands get a plain heading. Expected
    outputs come from verify.log.
    """
    import re as _re

    _heredoc_re = _re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
    script = run_dir / "commands.sh"
    commands: list[str] = []
    if script.is_file():
        in_assertion = False
        heredoc_term: str | None = None
        for line in script.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if heredoc_term is not None:
                # inside a heredoc: content belongs to the previous command
                commands[-1] += "\n" + line.rstrip()
                if stripped == heredoc_term:
                    heredoc_term = None
                continue
            if stripped.startswith("# --- readme2demo success-criteria"):
                in_assertion = True
            if (
                in_assertion
                or not stripped
                or stripped.startswith("#")
                or stripped in ("#!/usr/bin/env bash", "set -euxo pipefail")
                or stripped.startswith("export DEBIAN_FRONTEND")
            ):
                continue
            commands.append(stripped)
            m = _heredoc_re.search(stripped)
            if m:
                heredoc_term = m.group(1)
    else:  # fall back to the outline's commands
        commands = [s.command for s in outline.steps]

    # The success-criteria command is the tutorial's PAYOFF; commands.sh only
    # runs it inside the assertion block, so without this it never appears as
    # a numbered step (tfdrift regression: the guide ended at `--version` and
    # never showed the actual drift scan).
    demo_cmd = plan.success_criteria.command
    demo_norm = _normalize(demo_cmd)
    covered = {_normalize(c) for c in commands}
    covered |= {c.split("<<", 1)[0].strip() for c in covered}
    if demo_norm not in covered and not any(
        c.startswith(demo_norm) or demo_norm.startswith(c.split("|", 1)[0].strip())
        for c in covered
    ):
        commands.append(demo_cmd)

    # Map outline steps by normalized command for titles/explanations.
    by_cmd = {_normalize(s.command): s for s in outline.steps}

    verify_text = ""
    verify_path = run_dir / "verify.log"
    if verify_path.exists():
        verify_text = verify_path.read_text(encoding="utf-8", errors="replace")
    expected = extract_expected_outputs(verify_text, commands) if verify_text else {}

    today = datetime.now(timezone.utc).date().isoformat()
    badge = (
        f"> ✅ Every command below was executed and verified in a clean Linux "
        f"container on {today}."
        if verified
        else "> ⚠️ UNVERIFIED — the clean-container replay did not pass; treat "
        "these steps as a best-effort record of a working session."
    )
    lines = [
        "---",
        f'title: "{seo_title(repo_url, outline.title + " — step by step")}"',
        f'description: "{seo_description(outline.intro)}"',
        f"date: {today}",
        f"verified: {'true' if verified else 'false'}",
        f'source_repo: "{repo_url}"',
        "generator: readme2demo",
        "---",
        "",
        f"# {outline.title} — step by step",
        "",
        badge,
        "",
        outline.intro,
        "",
    ]
    if plan.prereqs:
        lines += ["## Prerequisites", ""]
        lines += [f"- {p}" for p in plan.prereqs]
        lines.append("")
    lines += [
        "## Steps",
        "",
        "Start in an empty working directory.",
        "",
    ]
    def _agent_output_for(cmd: str) -> str | None:
        """Captured output for ``cmd`` from the agent run (pipe/2>&1 aware)."""
        import re as _re

        want = _normalize(cmd).replace(" 2>&1", "")
        for entry in log.entries:
            if (entry.exit_code == 0 or entry.findings_success) and entry.output.strip():
                have = _normalize(entry.cmd).replace(" 2>&1", "")
                last_seg = _re.split(r"\s*(?:&&|;)\s*", have)[-1].strip()
                if want in (have, last_seg, have.split("|", 1)[0].strip(),
                            last_seg.split("|", 1)[0].strip()):
                    return _truncate(entry.output)
        return None

    for i, cmd in enumerate(commands, 1):
        step = by_cmd.get(_normalize(cmd))
        is_demo_step = _normalize(cmd) == demo_norm
        if step:
            title = step.title
        elif is_demo_step:
            title = "The payoff — see it work"
        else:
            title = _fallback_step_title(cmd)
        lines += [f"### Step {i} — {title}", ""]
        if step and step.explanation:
            lines += [step.explanation, ""]
        elif is_demo_step and plan.success_criteria.description:
            lines += [plan.success_criteria.description, ""]
        lines += ["```bash", cmd, "```", ""]
        output = (
            expected.get(cmd)
            or (step.expected_output if step else None)
            or _agent_output_for(cmd)
        )
        if output:
            lines += ["Expected output:", "", "```text", output.strip(), "```", ""]
    has_video = (run_dir / "demo.gif").exists() or (run_dir / "demo.mp4").exists()
    payoff = (
        f"With everything set up, `{plan.success_criteria.command}` demonstrates "
        "the tool doing its job."
    )
    if has_video:
        payoff += " Watch every step execute in demo.mp4 / demo.gif alongside this file."
    lines += [
        "## The payoff",
        "",
        payoff,
        "",
        "---",
        (
            f"Source: [{repo_url}]({repo_url}) · " if repo_url else ""
        )
        + "*Generated by [readme2demo](https://github.com/alphacrack/readme2demo)"
        " — every step above ran before it was written.*",
        "",
    ]
    dest = run_dir / "step_by_step.md"
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest
