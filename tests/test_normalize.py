"""Unit tests for the M4 transcript normalizer and the Claude Code parser.

Pure fixture-driven tests: no network, no docker, no API calls.
"""

from __future__ import annotations

import json
from pathlib import Path

from readme2demo.engines.claude_code import (
    MAX_OUTPUT_BYTES,
    TRUNCATION_SEPARATOR,
    ClaudeCodeEngine,
)
from readme2demo.normalize import normalize, tag_phases
from readme2demo.types import AgentResult, CommandEntry, CommandLog


def _parse_fixture(fixtures_dir: Path) -> CommandLog:
    return ClaudeCodeEngine().parse_transcript(
        fixtures_dir / "claude_transcript.ndjson"
    )


def _write_ndjson(path: Path, events: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    return path


def _bash_event(tool_id: str, command: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Bash",
                    "input": {"command": command},
                }
            ]
        },
    }


def _result_event(tool_id: str, text: str, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": [{"type": "text", "text": text}],
                    "is_error": is_error,
                }
            ]
        },
    }


# --- ClaudeCodeEngine.parse_transcript ---------------------------------------


class TestClaudeParseFixture:
    def test_bash_entry_count(self, fixtures_dir: Path) -> None:
        log = _parse_fixture(fixtures_dir)
        assert len(log.entries) == 8

    def test_commands_and_exit_codes(self, fixtures_dir: Path) -> None:
        log = _parse_fixture(fixtures_dir)
        assert [e.cmd for e in log.entries] == [
            "ls -la",
            "cat README.md",
            "head -n 20 app.py",
            "which python3",
            "pip install -r requirements.txt",
            "python app.py",
            "pip install requests",
            "python app.py",
        ]
        assert [e.exit_code for e in log.entries] == [0, 0, 0, 0, 0, 1, 0, 0]

    def test_outputs_paired(self, fixtures_dir: Path) -> None:
        log = _parse_fixture(fixtures_dir)
        # tool_result content as plain string is handled too.
        assert log.entries[3].output == "/usr/bin/python3"
        assert "ModuleNotFoundError" in log.entries[5].output
        assert log.entries[7].output == "Hello from sample-cli!"

    def test_fix_marker_parsed(self, fixtures_dir: Path) -> None:
        log = _parse_fixture(fixtures_dir)
        assert len(log.fixes) == 1
        assert log.fixes[0].what == "install missing dep"
        assert log.fixes[0].because == "not in requirements"

    def test_file_edits_collected(self, fixtures_dir: Path) -> None:
        log = _parse_fixture(fixtures_dir)
        assert log.file_edits == ["/work/config.json"]

    def test_outcome_success(self, fixtures_dir: Path) -> None:
        log = _parse_fixture(fixtures_dir)
        assert log.result.outcome == "success"
        assert log.result.blocked_reason is None

    def test_result_metadata_parsed(self, fixtures_dir: Path) -> None:
        log = _parse_fixture(fixtures_dir)
        assert log.result.cost_usd == 0.0421
        assert log.result.num_turns == 12
        assert log.result.duration_s == 95.0

    def test_engine_name(self, fixtures_dir: Path) -> None:
        log = _parse_fixture(fixtures_dir)
        assert log.engine == "claude-code"


class TestClaudeTruncation:
    def test_huge_output_truncated_head_and_tail(self, tmp_path: Path) -> None:
        head_sentinel = "STARTMARK"
        tail_sentinel = "ENDMARK"
        huge = head_sentinel + "x" * 20000 + tail_sentinel
        path = _write_ndjson(
            tmp_path / "t.ndjson",
            [
                _bash_event("t1", "cat big.log"),
                _result_event("t1", huge),
            ],
        )
        log = ClaudeCodeEngine().parse_transcript(path)
        out = log.entries[0].output
        assert TRUNCATION_SEPARATOR in out
        assert out.startswith(head_sentinel)
        assert out.endswith(tail_sentinel)
        assert len(out) <= MAX_OUTPUT_BYTES + len(TRUNCATION_SEPARATOR)

    def test_small_output_untouched(self, tmp_path: Path) -> None:
        path = _write_ndjson(
            tmp_path / "t.ndjson",
            [
                _bash_event("t1", "echo hi"),
                _result_event("t1", "hi"),
            ],
        )
        log = ClaudeCodeEngine().parse_transcript(path)
        assert log.entries[0].output == "hi"


class TestClaudeBlocked:
    def test_blocked_outcome(self, tmp_path: Path) -> None:
        path = _write_ndjson(
            tmp_path / "blocked.ndjson",
            [
                _bash_event("t1", "cat README.md"),
                _result_event("t1", "Set OPENAI_API_KEY before running."),
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "BLOCKED: needs OPENAI_API_KEY"}
                        ]
                    },
                },
            ],
        )
        log = ClaudeCodeEngine().parse_transcript(path)
        assert log.result.outcome == "blocked"
        assert log.result.blocked_reason == "needs OPENAI_API_KEY"

    def test_no_marker_no_result_is_failed(self, tmp_path: Path) -> None:
        path = _write_ndjson(
            tmp_path / "failed.ndjson",
            [
                _bash_event("t1", "python app.py"),
                _result_event("t1", "boom", is_error=True),
            ],
        )
        log = ClaudeCodeEngine().parse_transcript(path)
        assert log.result.outcome == "failed"


# --- tag_phases ---------------------------------------------------------------


def _log(entries: list[CommandEntry]) -> CommandLog:
    return CommandLog(
        engine="claude-code",
        entries=entries,
        result=AgentResult(outcome="success"),
    )


class TestTagPhases:
    def test_pip_install_is_setup(self) -> None:
        log = tag_phases(_log([CommandEntry(cmd="pip install requests", exit_code=0)]))
        assert log.entries[0].phase == "setup"

    def test_ls_is_explore(self) -> None:
        log = tag_phases(_log([CommandEntry(cmd="ls -la", exit_code=0)]))
        assert log.entries[0].phase == "explore"

    def test_command_after_failure_is_fix(self) -> None:
        log = tag_phases(
            _log(
                [
                    CommandEntry(cmd="python app.py", exit_code=1),
                    CommandEntry(cmd="pip install requests", exit_code=0),
                ]
            )
        )
        assert log.entries[1].phase == "fix"

    def test_explore_never_becomes_fix(self) -> None:
        log = tag_phases(
            _log(
                [
                    CommandEntry(cmd="python app.py", exit_code=1),
                    CommandEntry(cmd="cat error.log", exit_code=0),
                ]
            )
        )
        assert log.entries[1].phase == "explore"

    def test_python_run_is_demo(self) -> None:
        log = tag_phases(_log([CommandEntry(cmd="python app.py", exit_code=0)]))
        assert log.entries[0].phase == "demo"

    def test_chain_tagged_by_last_segment(self) -> None:
        log = tag_phases(
            _log([CommandEntry(cmd="cd x && python app.py", exit_code=0)])
        )
        assert log.entries[0].phase == "demo"

    def test_git_clone_is_setup(self) -> None:
        log = tag_phases(
            _log([CommandEntry(cmd="git clone https://github.com/x/y", exit_code=0)])
        )
        assert log.entries[0].phase == "setup"

    def test_venv_creation_is_setup(self) -> None:
        log = tag_phases(_log([CommandEntry(cmd="python -m venv .venv", exit_code=0)]))
        assert log.entries[0].phase == "setup"

    def test_env_prefixed_apt_is_setup(self) -> None:
        log = tag_phases(
            _log(
                [
                    CommandEntry(
                        cmd="DEBIAN_FRONTEND=noninteractive apt-get install -y jq",
                        exit_code=0,
                    )
                ]
            )
        )
        assert log.entries[0].phase == "setup"

    def test_download_url_with_install_substring_is_demo(self) -> None:
        log = tag_phases(
            _log(
                [
                    CommandEntry(
                        cmd="curl -O https://example.com/installer.tar.gz",
                        exit_code=0,
                    )
                ]
            )
        )
        assert log.entries[0].phase == "demo"

    def test_shell_piped_installer_download_is_setup(self) -> None:
        log = tag_phases(
            _log(
                [
                    CommandEntry(
                        cmd="curl -fsSL https://example.com/install.sh | bash",
                        exit_code=0,
                    )
                ]
            )
        )
        assert log.entries[0].phase == "setup"


# --- normalize() end to end ----------------------------------------------------


class TestNormalize:
    def test_writes_command_log_json(
        self, fixtures_dir: Path, tmp_run_dir: Path
    ) -> None:
        log = normalize(
            fixtures_dir / "claude_transcript.ndjson",
            ClaudeCodeEngine(),
            tmp_run_dir,
        )
        out_file = tmp_run_dir / "command_log.json"
        assert out_file.exists()
        on_disk = json.loads(out_file.read_text(encoding="utf-8"))
        assert on_disk["engine"] == "claude-code"
        assert len(on_disk["entries"]) == len(log.entries)
        # Phases were applied before writing.
        phases = [e["phase"] for e in on_disk["entries"]]
        assert "unknown" not in phases
        assert phases[0] == "explore"  # ls -la
        assert phases[4] == "setup"  # pip install -r requirements.txt
        assert phases[6] == "fix"  # pip install requests, after the failure
        assert phases[7] == "demo"  # python app.py


# -- ADJUSTED_SUCCESS marker -------------------------------------------------------


def _transcript_with_text(text: str) -> str:
    """Minimal one-event transcript embedding an assistant text block."""
    import json as _json

    return "\n".join(
        [
            _json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": text}]},
                }
            ),
            _json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "total_cost_usd": 0.01,
                    "num_turns": 2,
                    "duration_ms": 1000,
                    "result": "R2D_SUCCESS",
                }
            ),
        ]
    )


def test_adjusted_success_parsed(tmp_path):
    from readme2demo.engines.claude_code import ClaudeCodeEngine

    t = tmp_path / "t.ndjson"
    t.write_text(
        _transcript_with_text(
            "ADJUSTED_SUCCESS: ./bin/thv version EXPECT: (?i)version\nR2D_SUCCESS"
        )
    )
    log = ClaudeCodeEngine().parse_transcript(t)
    assert log.adjusted_success_command == "./bin/thv version"
    assert log.adjusted_success_pattern == "(?i)version"
    assert log.result.outcome == "success"


def test_adjusted_success_without_expect(tmp_path):
    from readme2demo.engines.claude_code import ClaudeCodeEngine

    t = tmp_path / "t.ndjson"
    t.write_text(_transcript_with_text("ADJUSTED_SUCCESS: `./bin/thv help`"))
    log = ClaudeCodeEngine().parse_transcript(t)
    assert log.adjusted_success_command == "./bin/thv help"
    assert log.adjusted_success_pattern is None


def test_no_adjusted_marker(fixtures_dir):
    from readme2demo.engines.claude_code import ClaudeCodeEngine

    log = ClaudeCodeEngine().parse_transcript(fixtures_dir / "claude_transcript.ndjson")
    assert log.adjusted_success_command is None


# -- validate_success_pattern -------------------------------------------------------


def _pattern_fixture(pattern):
    from readme2demo.types import (
        AgentResult, CommandEntry, CommandLog, Plan, SuccessCriteria,
    )

    plan = Plan(
        quickstart_summary="q",
        success_criteria=SuccessCriteria(
            command="./bin/thv version", expected_pattern=pattern
        ),
    )
    log = CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(
                cmd="./bin/thv version",
                exit_code=0,
                output="You are running a local build of ToolHive\nBuilt: today",
            )
        ],
        result=AgentResult(outcome="success"),
    )
    return plan, log


def test_pattern_kept_when_it_matches():
    from readme2demo.normalize import validate_success_pattern

    plan, log = _pattern_fixture("local build of ToolHive")
    changed, _ = validate_success_pattern(plan, log)
    assert not changed
    assert plan.success_criteria.expected_pattern == "local build of ToolHive"


def test_pattern_dropped_when_never_matches():
    """Regression (toolhive run): agent declared EXPECT \\brun\\b but the real
    output only contains 'running' — verify failed a working build."""
    from readme2demo.normalize import validate_success_pattern

    plan, log = _pattern_fixture(r"\brun\b")
    changed, reason = validate_success_pattern(plan, log)
    assert changed
    assert plan.success_criteria.expected_pattern is None
    assert "never matched" in reason


def test_pattern_dropped_when_invalid_regex():
    from readme2demo.normalize import validate_success_pattern

    plan, log = _pattern_fixture("([unclosed")
    changed, reason = validate_success_pattern(plan, log)
    assert changed
    assert "invalid regex" in reason


def test_pattern_left_alone_when_command_not_in_log():
    from readme2demo.normalize import validate_success_pattern
    from readme2demo.types import AgentResult, CommandLog

    plan, _ = _pattern_fixture("anything")
    empty_log = CommandLog(engine="claude-code", result=AgentResult(outcome="success"))
    changed, _ = validate_success_pattern(plan, empty_log)
    assert not changed
    assert plan.success_criteria.expected_pattern == "anything"


def test_pattern_matches_command_run_via_chain():
    from readme2demo.normalize import validate_success_pattern
    from readme2demo.types import (
        AgentResult, CommandEntry, CommandLog, Plan, SuccessCriteria,
    )

    plan = Plan(
        quickstart_summary="q",
        success_criteria=SuccessCriteria(
            command="./bin/thv version", expected_pattern="local build"
        ),
    )
    log = CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(
                cmd="cd /work && ./bin/thv version",
                exit_code=0,
                output="a local build here",
            )
        ],
        result=AgentResult(outcome="success"),
    )
    changed, _ = validate_success_pattern(plan, log)
    assert not changed


# -- repo_files_edited (agent-cheat detection) ---------------------------------------


def test_repo_files_edited_flags_source_patches(tmp_path):
    """Regression (toolhive run 3): agent patched repo source to bypass the
    container-runtime check; success then can't survive the pristine-clone
    replay — must be flagged at normalize time."""
    from readme2demo.normalize import repo_files_edited
    from readme2demo.types import AgentResult, CommandLog

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "factory.go").write_text("package pkg")
    log = CommandLog(
        engine="claude-code",
        file_edits=[
            "/work/pkg/factory.go",      # exists in pristine repo -> flagged
            "/work/.env",                # new file -> fine
            "/work/config.yaml",         # new file -> fine
            "/tmp/scratch.txt",          # outside /work -> ignored
        ],
        result=AgentResult(outcome="success"),
    )
    assert repo_files_edited(log, repo) == ["pkg/factory.go"]


def test_repo_files_edited_empty_when_no_source_touched(tmp_path):
    from readme2demo.normalize import repo_files_edited
    from readme2demo.types import AgentResult, CommandLog

    repo = tmp_path / "repo"
    repo.mkdir()
    log = CommandLog(
        engine="claude-code",
        file_edits=["/work/.venv/pyvenv.cfg"],
        result=AgentResult(outcome="success"),
    )
    assert repo_files_edited(log, repo) == []


# -- mark_findings_success (tfdrift regression) --------------------------------------


def test_findings_tool_nonzero_exit_reclassified():
    """Regression: `tfdrift scan` exits 1 when it FINDS drift — that entry is
    the successful demo and must count for grounding, tape, and outputs."""
    from readme2demo.normalize import mark_findings_success
    from readme2demo.types import (
        AgentResult, CommandEntry, CommandLog, Plan, SuccessCriteria,
    )

    plan = Plan(
        quickstart_summary="q",
        success_criteria=SuccessCriteria(
            command="tfdrift scan --path /tmp/tfdrift-demo",
            expected_pattern="[Dd]rift detected",
        ),
    )
    log = CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(
                cmd='export PATH="/x:$PATH" && tfdrift scan --path /tmp/tfdrift-demo',
                exit_code=1,
                output="Drift detected: 2 resource(s)",
            ),
            CommandEntry(  # unrelated failure: NOT reclassified
                cmd="pip install tfdrift", exit_code=1,
                output="error: externally-managed-environment",
            ),
            CommandEntry(  # matching cmd but pattern absent: NOT reclassified
                cmd="tfdrift scan --path /tmp/tfdrift-demo", exit_code=1,
                output="some unrelated error",
            ),
        ],
        result=AgentResult(outcome="success"),
    )
    assert mark_findings_success(plan, log) == 1
    assert log.entries[0].findings_success is True
    assert log.entries[1].findings_success is False
    assert log.entries[2].findings_success is False
    assert log.entries[0] in log.successful_commands()


def test_findings_marking_requires_pattern():
    from readme2demo.normalize import mark_findings_success
    from readme2demo.types import (
        AgentResult, CommandEntry, CommandLog, Plan, SuccessCriteria,
    )

    plan = Plan(
        quickstart_summary="q",
        success_criteria=SuccessCriteria(command="tool scan", expected_pattern=None),
    )
    log = CommandLog(
        engine="claude-code",
        entries=[CommandEntry(cmd="tool scan", exit_code=2, output="found stuff")],
        result=AgentResult(outcome="success"),
    )
    assert mark_findings_success(plan, log) == 0


def test_cwd_hints_flags_relative_after_cd_away():
    """Regression (tfdrift run 2): `cd /tmp` for a download, then
    `pip install -e .` editable-installed /tmp instead of the repo."""
    from readme2demo.verify import cwd_hints

    script = (
        "#!/usr/bin/env bash\n"
        "cd /work\n"
        "git clone --depth 1 https://github.com/x/y .\n"
        "cd /tmp\n"
        "curl -fsSL https://example.com/tool.zip -o tool.zip\n"
        "pip install --break-system-packages -e .\n"
    )
    hints = cwd_hints(script)
    assert len(hints) == 1
    assert "/tmp" in hints[0]
    assert "pip install" in hints[0]


def test_cwd_hints_quiet_when_cd_in_same_line():
    from readme2demo.verify import cwd_hints

    script = "cd /tmp\ncd /work && pip install -e .\n"
    assert cwd_hints(script) == []


# --- OpenHandsEngine.parse_transcript: prompt-echo marker poisoning -----------


def test_regression_prompt_echo_markers_poison_openhands_outcome(tmp_path):
    """Regression (run glow-20260710-182508): OpenHands echoes the TASK PROMPT
    into the trajectory as a source="user" message action. The prompt
    documents the markers (`BLOCKED: <reason>`, `ADJUSTED_SUCCESS: <new
    command> EXPECT: <regex the output matches>`, `FIX: ...`, and a literal
    R2D_SUCCESS example), so scanning it like agent output harvested the
    un-filled templates as real markers: a run whose agent genuinely printed
    R2D_SUCCESS was reported blocked with reason '<reason>', and plan.json's
    success command was overwritten with the literal '<new command>'.
    User-sourced messages must never be marker-scanned.
    """
    from readme2demo.engines.openhands import OpenHandsEngine

    prompt_echo = (
        "# Task: make the quickstart work\n"
        "Declare deviations like this:\n"
        "FIX: <what you are changing> BECAUSE: <why the README's version fails>\n"
        "If truly impossible print:\n"
        "BLOCKED: <reason>\n"
        "If infrastructure is missing declare:\n"
        "ADJUSTED_SUCCESS: <new command> EXPECT: <regex the output matches>\n"
        "On success print exactly:\n"
        "R2D_SUCCESS\n"
    )
    events = [
        {"action": "message", "source": "user", "args": {"content": prompt_echo}},
        {"action": "run", "source": "agent", "args": {"command": "go build -o glow ."}},
        {
            "observation": "run", "source": "agent",
            "content": "built", "extras": {"exit_code": 0},
        },
        {"action": "message", "source": "user", "args": {"content": "Please continue."}},
        {"action": "message", "source": "agent", "args": {"content": "R2D_SUCCESS"}},
    ]
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(events), encoding="utf-8")
    log = OpenHandsEngine().parse_transcript(path)
    assert log.result.outcome == "success"  # the agent's own marker counts
    assert log.result.blocked_reason is None  # '<reason>' must not
    assert log.adjusted_success_command is None
    assert log.adjusted_success_pattern is None
    assert log.fixes == []
    assert [e.cmd for e in log.entries] == ["go build -o glow ."]


def test_openhands_agent_markers_still_parse(tmp_path):
    # Source filtering must not silence REAL agent-emitted markers.
    from readme2demo.engines.openhands import OpenHandsEngine

    events = [
        {
            "action": "message", "source": "agent",
            "args": {"content": "FIX: pin go 1.22 BECAUSE: build needs it\n"
                                "BLOCKED: needs a GPU"},
        },
    ]
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(events), encoding="utf-8")
    log = OpenHandsEngine().parse_transcript(path)
    assert log.result.outcome == "blocked"
    assert log.result.blocked_reason == "needs a GPU"
    assert log.fixes[0].what == "pin go 1.22"


# --- scan_markers / scan_adjusted: template-placeholder guard ------------------


def test_regression_marker_scanners_ignore_template_placeholders():
    """Regression (run glow-20260710-182508, second defense): a model that
    restates its marker instructions verbatim in assistant text must not
    produce markers whose values are the un-filled `<...>` templates — this
    guard protects claude-code too, where the poison would come from the
    model quoting its prompt rather than from a trajectory echo.
    """
    from readme2demo.engines.claude_code import scan_adjusted, scan_markers

    fixes: list = []
    reason = scan_markers(
        "FIX: <what you are changing> BECAUSE: <why the README's version fails>\n"
        "BLOCKED: <reason>",
        fixes,
    )
    assert reason is None
    assert fixes == []
    assert scan_adjusted(
        "ADJUSTED_SUCCESS: <new command> EXPECT: <regex the output matches>"
    ) is None
    # A real command with a placeholder pattern degrades to exit-code-only.
    assert scan_adjusted(
        "ADJUSTED_SUCCESS: ./glow --help EXPECT: <regex the output matches>"
    ) == ("./glow --help", None)
    # Real values still parse — including ones merely CONTAINING angle
    # brackets: only a value that is one whole <...> token is a placeholder.
    assert scan_markers("BLOCKED: needs docker <socket unavailable>", []) == (
        "needs docker <socket unavailable>"
    )
    assert scan_markers("BLOCKED: <tool> needs a GPU here", []) == (
        "<tool> needs a GPU here"
    )
    assert scan_adjusted("ADJUSTED_SUCCESS: ./tool version EXPECT: v[0-9]+") == (
        "./tool version", "v[0-9]+"
    )


def test_prompt_echo_success_marker_alone_is_not_success(tmp_path):
    """Pins the user-source skip independently of the placeholder guard: the
    SUCCESS_MARKER check is an unanchored substring match with no placeholder
    protection, so the source filter is its ONLY defense. A trajectory whose
    only R2D_SUCCESS sits in the echoed prompt (no agent success message)
    must parse as failed, never success.
    """
    from readme2demo.engines.openhands import OpenHandsEngine

    events = [
        {
            "action": "message", "source": "user",
            "args": {"content": "On success print exactly:\nR2D_SUCCESS\n"},
        },
        {"action": "run", "source": "agent", "args": {"command": "ls"}},
        {
            "observation": "run", "source": "agent",
            "content": "README.md", "extras": {"exit_code": 0},
        },
    ]
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(events), encoding="utf-8")
    log = OpenHandsEngine().parse_transcript(path)
    assert log.result.outcome == "failed"


# -- reconcile_success_command -------------------------------------------------


def _success_cmd_fixture(plan_cmd, entries):
    from readme2demo.types import (
        AgentResult, CommandEntry, CommandLog, Plan, SuccessCriteria,
    )

    plan = Plan(
        quickstart_summary="q",
        success_criteria=SuccessCriteria(command=plan_cmd, expected_pattern="verified"),
    )
    log = CommandLog(
        engine="claude-code",
        entries=[CommandEntry(**e) for e in entries],
        result=AgentResult(outcome="success"),
    )
    return plan, log


def _reconcile(plan_cmd, entries):
    from readme2demo.normalize import reconcile_success_command

    plan, log = _success_cmd_fixture(plan_cmd, entries)
    changed, reason = reconcile_success_command(plan, log)
    return changed, plan.success_criteria.command, reason


def test_regression_success_command_uses_the_proven_absolute_path():
    """Regression (run readme2demo-20260806-180604-f1d363): the planner wrote a
    bare console-script name, but pip had installed it into ~/.local/bin, which
    is not on PATH. The agent proved the absolute-path form (exit 0, twice);
    commands.sh asserted the bare form and died with `command not found`
    (exit 127), reporting a run whose criterion genuinely passed as UNVERIFIED.
    """
    changed, cmd, reason = _reconcile(
        "readme2demo report examples/toolhive",
        [
            {"cmd": "readme2demo --help", "exit_code": 1},
            {"cmd": "/home/demo/.local/bin/readme2demo --help", "exit_code": 0},
            {
                "cmd": "/home/demo/.local/bin/readme2demo report examples/toolhive",
                "exit_code": 0,
                "output": "verified: yes",
            },
        ],
    )
    assert changed
    assert cmd == "/home/demo/.local/bin/readme2demo report examples/toolhive"
    assert "proven equivalent" in reason


def test_success_command_left_alone_when_its_own_spelling_worked():
    """The planner's spelling ran and exited 0 — nothing to reconcile, and
    rewriting it to another equivalent would be gratuitous churn."""
    changed, cmd, _ = _reconcile(
        "pytest tests/ -q",
        [
            {"cmd": "/usr/local/bin/pytest tests/ -q", "exit_code": 0},
            {"cmd": "pytest tests/ -q", "exit_code": 0, "output": "ok"},
        ],
    )
    assert not changed
    assert cmd == "pytest tests/ -q"


def test_success_command_never_swaps_to_a_failed_or_different_command():
    """The swap may only land on a literal the log proves succeeded AND that
    normalize_cmd calls the same command — never a failed run of it, and never
    some other command that merely looks similar."""
    changed, cmd, _ = _reconcile(
        "acme build --release",
        [
            {"cmd": "/opt/acme/bin/acme build --release", "exit_code": 2},
            {"cmd": "/opt/acme/bin/acme build --debug", "exit_code": 0},
        ],
    )
    assert not changed
    assert cmd == "acme build --release"


def test_regression_exit_code_none_is_not_proof(monkeypatch):
    """Regression (#278-style audit of this very function): exit_code None means
    the result was NEVER OBSERVED — an unpaired tool_use, a killed or truncated
    run — not success. Accepting it made the reconciler strictly more permissive
    than the grounding rule it claims to reuse, so a command that never
    demonstrably ran could be written into the verify assertion."""
    changed, cmd, _ = _reconcile(
        "acme verify",
        [
            {"cmd": "acme verify", "exit_code": 127},
            {"cmd": "/opt/acme/bin/acme verify", "exit_code": None},
        ],
    )
    assert not changed
    assert cmd == "acme verify"


def test_regression_chosen_literal_may_not_smuggle_shell_control_characters():
    """Regression (audit): the assertion interpolates the command into
    `$( ... 2>&1)`. A literal with a trailing `;` makes the null command the
    last one in the substitution, so `$?` is 0 whatever the real command did —
    `$(false ; 2>&1)` exits 0. That converts this false-NEGATIVE fix into a
    false POSITIVE on the definition of "verified"."""
    changed, cmd, _ = _reconcile(
        "acme verify",
        [{"cmd": "/opt/acme/bin/acme verify ;", "exit_code": 0, "output": "ok"}],
    )
    assert not changed
    assert cmd == "acme verify"


def test_regression_chain_segments_are_never_selected():
    """Regression (audit): a candidate must be one whole simple command. A
    chained entry whose LAST segment resembles the success command must not be
    harvested — that is segment-selection, the hazard failure class 5 warns
    about, and here the result would be executed."""
    for chained in (
        "cd /tmp && /opt/acme/bin/acme verify",
        "/opt/acme/bin/acme verify | head",
        "/opt/acme/bin/acme verify; echo done",
    ):
        changed, cmd, _ = _reconcile(
            "acme verify", [{"cmd": chained, "exit_code": 0, "output": "ok"}]
        )
        assert not changed, chained
        assert cmd == "acme verify"


def test_regression_substitution_is_directional_never_less_specific():
    """Regression (audit): normalize_cmd is a SYMMETRIC equivalence, correct for
    grounding because both sides get the same lossy transform. This string is
    EXECUTED, so the same equivalence is unsound in the losing direction — it is
    lossy in exactly the tokens that decide which program runs and what it does.
    """
    # a pinned venv interpreter must not be replaced by whatever is on PATH
    changed, cmd, _ = _reconcile(
        "/work/.venv/bin/pytest tests/",
        [{"cmd": "pytest tests/", "exit_code": 0, "output": "ok"}],
    )
    assert not changed and cmd == "/work/.venv/bin/pytest tests/"

    # a flag the plan carried may not be dropped from an executed assertion
    changed, cmd, _ = _reconcile(
        "pip install --break-system-packages -e .",
        [{"cmd": "pip install -e .", "exit_code": 0, "output": "ok"}],
    )
    assert not changed and cmd == "pip install --break-system-packages -e ."

    # python3 -> python loses the interpreter the plan pinned
    changed, cmd, _ = _reconcile(
        "python3 -m mytool verify",
        [{"cmd": "python -m mytool verify", "exit_code": 0, "output": "ok"}],
    )
    assert not changed and cmd == "python3 -m mytool verify"

    # ...but gaining specificity is exactly the point and must still work
    changed, cmd, _ = _reconcile(
        "python -m mytool verify",
        [{"cmd": "python3 -m mytool verify", "exit_code": 0, "output": "ok"}],
    )
    assert changed and cmd == "python3 -m mytool verify"

    # ...as is a sandbox-required flag the agent had to ADD
    changed, cmd, _ = _reconcile(
        "pip install -e .",
        [{"cmd": "pip install -e . --break-system-packages", "exit_code": 0}],
    )
    assert changed and cmd == "pip install -e . --break-system-packages"


def test_success_command_swap_drops_a_stderr_merge():
    """The assertion wraps the command and appends its own 2>&1, so a chosen
    literal must not carry one or the emitted line doubles it."""
    changed, cmd, _ = _reconcile(
        "acme verify",
        [{"cmd": "/opt/acme/bin/acme verify 2>&1", "exit_code": 0, "output": "ok"}],
    )
    assert changed
    assert cmd == "/opt/acme/bin/acme verify"


def test_regression_last_proven_form_wins():
    """Regression (audit): the docstring promises the LAST proven form — what the
    agent settled on after its retries. Unpinned, `proven[0]` passed the suite."""
    changed, cmd, _ = _reconcile(
        "acme verify",
        [
            {"cmd": "/opt/a/acme verify", "exit_code": 0},
            {"cmd": "/opt/b/acme verify", "exit_code": 0},
        ],
    )
    assert changed
    assert cmd == "/opt/b/acme verify"


def test_multiline_heredoc_success_command_is_left_alone():
    """Heredocs are one multi-line command matched by PREFIX everywhere else
    (failure class 7); collapsing one into a single line destroys it.

    The proven form here is deliberately DIFFERENT from the plan's (an absolute
    ``/bin/cat``) so the multi-line guard is what rejects it — not the
    already-ran early return, which would hide the guard's absence.
    """
    heredoc = "cat > app.py <<'EOF'\nprint('hi')\nEOF"
    proven = "/bin/cat > app.py <<'EOF'\nprint('hi')\nEOF"
    changed, cmd, _ = _reconcile(heredoc, [{"cmd": proven, "exit_code": 0}])
    assert not changed
    assert cmd == heredoc
    assert "\n" in cmd  # not flattened


def test_regression_a_longer_command_is_not_an_equivalent_one():
    """Regression (audit mutation M4): the equivalence must be EXACT, not
    "contains". ``acme verify --deep`` is a different command from
    ``acme verify`` — it only adds flags, so the specificity rule alone would
    wave it through, and substituting it would silently change what the
    assertion asserts."""
    changed, cmd, _ = _reconcile(
        "acme verify",
        [{"cmd": "acme verify --deep", "exit_code": 0, "output": "ok"}],
    )
    assert not changed
    assert cmd == "acme verify"


def test_regression_findings_tool_success_command_is_reconcilable():
    """A findings tool exits nonzero ON SUCCESS (failure class 4) and is proof
    only once mark_findings_success has marked it — which is why the orchestrator
    runs this reconciler AFTER that pass, not before."""
    from readme2demo.normalize import reconcile_success_command

    plan, log = _success_cmd_fixture(
        "drift-detect ./src",
        [{"cmd": "/opt/tools/drift-detect ./src", "exit_code": 3, "output": "2 found"}],
    )
    log.entries[0].findings_success = True
    changed, _ = reconcile_success_command(plan, log)
    assert changed
    assert plan.success_criteria.command == "/opt/tools/drift-detect ./src"


def test_known_gap_findings_marking_does_not_yet_see_drifted_spellings():
    """Documents a REAL gap found while auditing this change, so nobody assumes
    classes 4 and 17 already compose.

    ``mark_findings_success`` matches the plan command with a literal
    whitespace-only ``_norm``, not with ``normalize_cmd``. So a findings tool
    invoked by absolute path is never marked, and the reconciler's
    ``findings_success`` branch stays unreachable in production no matter how
    the two passes are ordered. Teaching ``mark_findings_success`` the same
    symmetric equivalence changes which entries enter the grounding candidate
    set, so it is deliberately NOT bundled into this fix.

    When that is fixed, this test flips to the composing assertion below it.
    """
    from readme2demo.normalize import mark_findings_success

    plan, log = _success_cmd_fixture(
        "drift-detect ./src",
        [{"cmd": "/opt/tools/drift-detect ./src", "exit_code": 3, "output": "2 found"}],
    )
    plan.success_criteria.expected_pattern = "found"
    assert mark_findings_success(plan, log) == 0  # <- the gap; not the ideal
    assert not any(e.findings_success for e in log.entries)


def test_reconciler_is_ordered_after_findings_marking():
    """The orchestrator must call the reconciler AFTER mark_findings_success.

    ``normalize()`` re-parses the transcript, so every ``findings_success`` is
    False until that pass runs; reconciling first would make the branch dead by
    construction. Pins the call-site ORDER in source, since the two functions
    are pure and a unit test cannot observe the sequence.
    """
    import inspect

    from readme2demo import orchestrator

    src = inspect.getsource(orchestrator.Orchestrator._stage_normalize)
    assert src.index("mark_findings_success") < src.index("reconcile_success_command")


# --- OpenHandsEngine.parse_transcript: the native `finish` action (#259) ------


def _openhands_log(tmp_path, events: list[dict]):
    from readme2demo.engines.openhands import OpenHandsEngine

    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(events), encoding="utf-8")
    return OpenHandsEngine().parse_transcript(path)


def test_regression_259_finish_action_success_from_real_trajectory(fixtures_dir: Path):
    """Regression (#259, run glow-20260804-203454-2e5c91): the agent built glow
    and rendered the README, then signalled success through OpenHands' NATIVE
    `finish` action instead of printing R2D_SUCCESS to a shell. The parser
    scanned agent MESSAGE actions only, so the marker never reached the scanner
    and a working run was recorded as outcome="failed", killing the pipeline at
    normalize. This golden fixture is that trajectory (event 18 — the finish
    action — verbatim); it parses as "failed" without the fix.
    """
    from readme2demo.engines.openhands import OpenHandsEngine

    log = OpenHandsEngine().parse_transcript(
        fixtures_dir / "openhands_glow_trajectory.json"
    )
    assert log.result.outcome == "success"
    assert log.result.blocked_reason is None
    # The same fixture carries the class-16 poison: a source="user" echo of the
    # task prompt documenting `BLOCKED: <reason>` / `ADJUSTED_SUCCESS: <new
    # command>`. Reading the finish action must not have re-opened that door.
    assert log.adjusted_success_command is None
    assert log.adjusted_success_pattern is None
    assert log.fixes == []
    # The real work is still parsed out of the run actions.
    assert len(log.entries) == 7
    assert log.entries[-1].cmd.strip() == "cd /work && ./glow README.md"
    assert "Render markdown on the CLI" in log.entries[-1].output


def test_regression_261_openhands_metadata_exit_codes_do_not_ground(
    fixtures_dir: Path,
):
    """Regression (#261): real OpenHands 0.48 exit codes live in metadata.

    Before the adapter read ``extras.metadata.exit_code``, failed fixture
    commands were coerced to zero and entered the grounding candidate set.
    """
    from readme2demo.distill import is_grounded
    from readme2demo.engines.openhands import OpenHandsEngine

    log = OpenHandsEngine().parse_transcript(
        fixtures_dir / "openhands_glow_trajectory.json"
    )

    assert [entry.exit_code for entry in log.entries] == [2, 0, -1, -1, 127, 0, 0]
    failed = [log.entries[index] for index in (0, 2, 3, 4)]
    assert all(entry not in log.successful_commands() for entry in failed)
    # The failed `go build ./...` segment must not be accepted as grounded.
    assert not is_grounded("go build ./...", log)
    # A real zero remains a successful grounding candidate.
    assert log.entries[1] in log.successful_commands()


def test_regression_261_openhands_exit_code_fallbacks(tmp_path):
    """Regression (#261): prefer metadata, tolerate the old path, and fail closed."""
    from readme2demo.engines.openhands import OpenHandsEngine

    events = [
        {"action": "run", "args": {"command": "legacy-zero"}},
        {"observation": "run", "content": "ok", "extras": {"exit_code": 0}},
        {"action": "run", "args": {"command": "metadata-zero"}},
        {
            "observation": "run",
            "content": "ok",
            "extras": {"exit_code": 9, "metadata": {"exit_code": 0}},
        },
        {"action": "run", "args": {"command": "metadata-failed"}},
        {
            "observation": "run",
            "content": "failed",
            "extras": {"metadata": {"exit_code": 3}},
        },
        {"action": "run", "args": {"command": "legacy-failed"}},
        {
            "observation": "run",
            "content": "failed",
            "extras": {"exit_code": 5, "metadata": {"exit_code": "bad"}},
        },
        {"action": "run", "args": {"command": "missing"}},
        {"observation": "run", "content": "unknown", "extras": {}},
        {"action": "run", "args": {"command": "malformed"}},
        {
            "observation": "run",
            "content": "unknown",
            "extras": {"exit_code": "bad", "metadata": {"exit_code": "bad"}},
        },
    ]
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(events), encoding="utf-8")

    log = OpenHandsEngine().parse_transcript(path)

    assert [entry.exit_code for entry in log.entries] == [0, 0, 3, 5, None, None]
    assert [entry.cmd for entry in log.successful_commands()] == [
        "legacy-zero",
        "metadata-zero",
    ]


def test_regression_259_finish_action_marker_shapes(tmp_path):
    """Regression (#259): the finish text lands in a different field in almost
    every OpenHands/provider combination — on the event (`message`), in the
    action args (`final_thought`), or inside the `finish` tool call's
    `arguments`, which arrive either as a JSON string or an already-parsed
    dict, at the top of the event or nested under `tool_call_metadata`. Every
    shape must ground; none may explode.
    """
    shapes: list[dict] = [
        # 1. event-level message only
        {"action": "finish", "source": "agent", "message": "R2D_SUCCESS"},
        # 2. args.final_thought (what OpenHands 0.48 writes)
        {
            "action": "finish", "source": "agent",
            "message": "All done! What's next on the agenda?",
            "args": {"final_thought": "R2D_SUCCESS", "task_completed": "true",
                     "outputs": {}, "thought": ""},
        },
        # 3. top-level tool call, arguments as a JSON *string*
        {
            "action": "finish", "source": "agent",
            "tool_calls": [{"function": {
                "name": "finish",
                "arguments": '{"message": "R2D_SUCCESS", "task_completed": "true"}',
            }}],
        },
        # 4. same, arguments already parsed into a dict
        {
            "action": "finish", "source": "agent",
            "tool_calls": [{"function": {
                "name": "finish",
                "arguments": {"message": "R2D_SUCCESS", "task_completed": True},
            }}],
        },
        # 5. flat tool call (no "function" wrapper), unparseable arguments
        {
            "action": "finish", "source": "agent",
            "tool_calls": [{"name": "finish", "arguments": '{"message": "R2D_SUCCESS'}],
        },
        # 6. buried in tool_call_metadata.model_response — the real 0.48 shape
        {
            "action": "finish", "source": "agent",
            "tool_call_metadata": {"model_response": {"choices": [{"message": {
                "tool_calls": [{"function": {
                    "name": "finish",
                    "arguments": '{"message": "R2D_SUCCESS", "task_completed": "true"}',
                }}],
            }}]}},
        },
    ]
    for i, event in enumerate(shapes):
        log = _openhands_log(tmp_path, [event])
        assert log.result.outcome == "success", f"shape {i + 1} did not ground"

    # Shapes that carry no marker must stay failed rather than raise.
    for junk in (
        {"action": "finish", "source": "agent"},
        {"action": "finish", "source": "agent", "args": None, "message": None},
        {"action": "finish", "source": "agent", "args": {"final_thought": 17}},
        {"action": "finish", "source": "agent", "tool_calls": "not-a-list"},
        {"action": "finish", "source": "agent",
         "tool_calls": [{"function": {"name": "finish", "arguments": None}}]},
    ):
        assert _openhands_log(tmp_path, [junk]).result.outcome == "failed"


def test_regression_259_finish_does_not_reopen_prompt_echo(tmp_path):
    """Regression (#259, mirror image of class 16): reading the finish action
    must not weaken the source filter. A user-sourced `finish` — the shape a
    prompt echo or a replayed instruction would take — is still skipped, so
    neither its R2D_SUCCESS nor its marker templates count.
    """
    log = _openhands_log(tmp_path, [
        {
            "action": "finish", "source": "user",
            "message": "On success print exactly:\nR2D_SUCCESS",
            "args": {"final_thought": "BLOCKED: <reason>", "task_completed": "true"},
        },
        {"action": "run", "source": "agent", "args": {"command": "ls"}},
    ])
    assert log.result.outcome == "failed"
    assert log.result.blocked_reason is None
    assert log.fixes == []


def test_regression_259_finish_placeholders_are_rejected(tmp_path):
    """Regression (#259): `_is_placeholder` still governs everything harvested
    from a finish action. An agent that restates its instructions in the final
    message ships un-filled `<...>` templates; they must not become a blocked
    reason, a FIX, or an adjusted success command.
    """
    log = _openhands_log(tmp_path, [
        {
            "action": "finish", "source": "agent",
            "message": "Here is how I would report problems:",
            "args": {
                "final_thought": (
                    "FIX: <what you are changing> BECAUSE: <why it fails>\n"
                    "BLOCKED: <reason>\n"
                    "ADJUSTED_SUCCESS: <new command> EXPECT: <regex the output matches>"
                ),
                "task_completed": "false",
            },
        },
    ])
    assert log.result.outcome == "failed"
    assert log.result.blocked_reason is None
    assert log.adjusted_success_command is None
    assert log.fixes == []

    # Real values delivered through finish still parse.
    log = _openhands_log(tmp_path, [
        {
            "action": "finish", "source": "agent",
            "args": {"final_thought": "BLOCKED: the demo needs a GPU",
                     "task_completed": "false"},
        },
    ])
    assert log.result.outcome == "blocked"
    assert log.result.blocked_reason == "the demo needs a GPU"


def test_regression_259_task_completed_alone_is_not_success(tmp_path):
    """Regression (#259): `task_completed: true` is the agent's own unverified
    self-report — corroborating evidence, never proof. An agent can call finish
    having failed, so a finish action with no R2D_SUCCESS anywhere must stay
    "failed" no matter how confidently it claims completion.
    """
    for completed in ("true", True):
        log = _openhands_log(tmp_path, [
            {"action": "run", "source": "agent", "args": {"command": "go build ./..."}},
            {"observation": "run", "source": "agent", "content": "build failed",
             "extras": {"exit_code": 1}},
            {
                "action": "finish", "source": "agent",
                "message": "All done! What's next on the agenda?",
                "args": {"final_thought": "I could not get the build working.",
                         "task_completed": completed},
                "tool_calls": [{"function": {
                    "name": "finish",
                    "arguments": json.dumps(
                        {"message": "Wrapping up.", "task_completed": completed}
                    ),
                }}],
            },
        ])
        assert log.result.outcome == "failed"


def test_regression_259_finish_ignores_non_finish_tool_arguments(tmp_path):
    """Regression (#259): only the `finish` tool call's arguments are read. A
    command the agent merely *intended* to run (`echo R2D_SUCCESS`) is a plan,
    not an outcome — grounding never comes from a tool call's input.

    Tool identification is default-DENY, so an UNNAMED call (the shape a
    streamed tool call takes when the name arrived in an earlier delta) is
    read only when the event itself is anchored to the finish tool via
    `tool_call_metadata.function_name`.
    """
    named_other = _openhands_log(tmp_path, [
        {
            "action": "finish", "source": "agent",
            "message": "Wrapping up without success.",
            "tool_calls": [{"function": {
                "name": "execute_bash",
                "arguments": '{"command": "echo R2D_SUCCESS"}',
            }}],
        },
    ])
    assert named_other.result.outcome == "failed"

    # Unnamed, unanchored: an anonymous payload never grounds on its own.
    for anonymous in (
        {"arguments": '{"command": "echo R2D_SUCCESS"}'},
        {"name": None, "arguments": '{"command": "echo R2D_SUCCESS"}'},
    ):
        log = _openhands_log(tmp_path, [
            {
                "action": "finish", "source": "agent",
                "message": "Wrapping up, the build never worked.",
                "tool_call_metadata": {"model_response": {"choices": [
                    {"message": {"tool_calls": [anonymous]}}
                ]}},
            },
        ])
        assert log.result.outcome == "failed"

    # Same payload, but the event names the finish tool: now it is the
    # agent's completion signal and the marker counts.
    anchored = _openhands_log(tmp_path, [
        {
            "action": "finish", "source": "agent",
            "message": "All done!",
            "tool_call_metadata": {
                "function_name": "finish",
                "model_response": {"choices": [{"message": {"tool_calls": [
                    {"arguments": '{"message": "R2D_SUCCESS"}'}
                ]}}]},
            },
        },
    ])
    assert anchored.result.outcome == "success"


def test_regression_259_adjusted_success_through_finish(tmp_path):
    """Regression (#259): ADJUSTED_SUCCESS is the one extraction with teeth —
    it rewrites plan.json's success command, which becomes the commands.sh
    assertion and the guide's payoff step. Delivered through a finish action it
    must parse exactly as it does from a message, and only when it is real.
    """
    log = _openhands_log(tmp_path, [
        {"action": "run", "source": "agent", "args": {"command": "go build -o glow ."}},
        {"observation": "run", "source": "agent", "content": "ok",
         "extras": {"exit_code": 0}},
        {
            "action": "finish", "source": "agent",
            "args": {
                "final_thought": (
                    "ADJUSTED_SUCCESS: ./glow --help EXPECT: Render markdown\n"
                    "R2D_SUCCESS"
                ),
                "task_completed": "true",
            },
        },
    ])
    assert log.result.outcome == "success"
    assert log.adjusted_success_command == "./glow --help"
    assert log.adjusted_success_pattern == "Render markdown"


def test_regression_259_finish_without_source_is_still_scanned(tmp_path):
    """Regression (#259): the class-16 gate is a deny-list (`source == "user"`),
    deliberately — a trajectory that drops or renames `source` must not silently
    stop grounding, which is the very failure this issue fixed. Only OpenHands
    itself emits `finish`, and it emits it for the agent, so a source-less
    finish is scanned. Pinned so the choice stays deliberate.
    """
    log = _openhands_log(tmp_path, [
        {"action": "finish", "args": {"final_thought": "R2D_SUCCESS"}},
    ])
    assert log.result.outcome == "success"
