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
