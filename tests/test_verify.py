"""Unit tests for the M6 verifier (verify.py) — the clean-room replay.

``run_verify`` is the ONLY source of "verified": a brand-new sandbox with zero
agent state (no agent env, no repo mount) replays commands.sh, and only exit
code 0 plus the R2D_VERIFY_OK marker in that attempt's own log segment passes.
The Sandbox class is faked — no Docker, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import pytest

import readme2demo.sandbox as sandbox_mod
import readme2demo.verify as verify_mod
from readme2demo.config import Config
from readme2demo.sandbox import ExecResult, SandboxError
from readme2demo.types import Plan, SuccessCriteria
from readme2demo.verify import _export_worktree, run_verify, verify_feedback

MARKER = "R2D_VERIFY_OK"


def _plan(pattern: Optional[str] = None) -> Plan:
    return Plan(
        quickstart_summary="q",
        success_criteria=SuccessCriteria(command="./tool version", expected_pattern=pattern),
    )


def _write_script(run_dir: Path, text: str = "#!/usr/bin/env bash\necho ok\n") -> Path:
    script = run_dir / "commands.sh"
    script.write_text(text, encoding="utf-8")
    return script


@pytest.fixture
def fake_sandboxes(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[list[tuple[int, str]]], list]:
    """Install a fake Sandbox class in verify's namespace.

    Call with one ``(exit_code, replay_output)`` per expected attempt; returns
    the list that collects every instance verify constructs, in order. The
    fake streams its scripted output into ``stream_to`` exactly like the real
    ``docker exec`` streaming path, so the marker check reads the same bytes
    it would in production.
    """

    created: list = []

    def install(attempts: list[tuple[int, str]]) -> list:
        queue = list(attempts)

        class FakeSandbox:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs
                self.started = False
                self.destroyed = False
                self.copy_ins: list[tuple[Path, str]] = []
                self.copy_outs: list[tuple[str, Path]] = []
                self.exec_calls: list[dict] = []
                self.exit_code, self.output = (
                    queue.pop(0) if queue else (1, "no scripted attempt left")
                )
                created.append(self)

            def start(self) -> "FakeSandbox":
                self.started = True
                return self

            def copy_in(self, src: Path, dst: str) -> None:
                self.copy_ins.append((Path(src), dst))

            def copy_out(self, src: str, dst: Path) -> None:
                self.copy_outs.append((src, Path(dst)))

            def exec(self, command, timeout=None, env=None, stream_to=None, workdir=None):
                self.exec_calls.append(
                    {"command": command, "timeout": timeout, "stream_to": stream_to}
                )
                if stream_to is not None:
                    with open(stream_to, "a", encoding="utf-8") as f:
                        f.write(self.output)
                return ExecResult(exit_code=self.exit_code, output=self.output)

            def destroy(self) -> None:
                self.destroyed = True

        monkeypatch.setattr(verify_mod, "Sandbox", FakeSandbox)
        return created

    return install


# --- run_verify ---------------------------------------------------------------


class TestRunVerify:
    def test_success_path_stamps_verified(self, tmp_run_dir: Path, fake_sandboxes) -> None:
        """Exit 0 + marker => passed=True, the report the orchestrator stamps
        the manifest's 'verified' from. Nothing else produces it."""
        _write_script(tmp_run_dir)
        created = fake_sandboxes([(0, f"setting up\n{MARKER}\n")])
        report = run_verify(tmp_run_dir, _plan(), Config())
        assert report.passed is True
        assert report.attempts == 1
        assert report.exit_code == 0
        assert report.criteria_matched is True
        assert report.log_path == "verify.log"
        assert len(created) == 1  # success on attempt 1: no retry sandbox

    def test_failing_replay_is_not_verified(self, tmp_run_dir: Path, fake_sandboxes) -> None:
        _write_script(tmp_run_dir)
        created = fake_sandboxes([(1, "boom"), (1, "boom again")])
        report = run_verify(tmp_run_dir, _plan(), Config())
        assert report.passed is False
        assert report.attempts == 2  # default verify_retries=1 => one retry
        assert report.exit_code == 1
        assert report.criteria_matched is False
        assert len(created) == 2

    def test_exit_zero_without_marker_fails(self, tmp_run_dir: Path, fake_sandboxes) -> None:
        """A script that exits 0 but never reaches the assertion block (which
        prints the marker) must NOT verify — exit code alone is not proof."""
        _write_script(tmp_run_dir)
        fake_sandboxes([(0, "ran fine, no assertion"), (0, "again")])
        report = run_verify(tmp_run_dir, _plan(), Config())
        assert report.passed is False
        assert report.exit_code == 0
        assert report.criteria_matched is False

    def test_marker_with_nonzero_exit_fails(self, tmp_run_dir: Path, fake_sandboxes) -> None:
        _write_script(tmp_run_dir)
        fake_sandboxes([(3, f"{MARKER}\nthen a step failed"), (3, MARKER)])
        report = run_verify(tmp_run_dir, _plan(), Config())
        assert report.passed is False
        assert report.criteria_matched is True  # marker seen, exit code failed it

    def test_marker_from_previous_attempt_does_not_leak(
        self, tmp_run_dir: Path, fake_sandboxes
    ) -> None:
        """The marker check reads only the current attempt's log segment: a
        marker printed by failed attempt 1 must not verify attempt 2."""
        _write_script(tmp_run_dir)
        fake_sandboxes([(1, f"{MARKER}\nbut exit 1\n"), (0, "clean run, no marker\n")])
        report = run_verify(tmp_run_dir, _plan(), Config())
        assert report.passed is False
        assert report.attempts == 2
        assert report.criteria_matched is False  # attempt 2's own segment only

    def test_fresh_sandbox_per_attempt_with_zero_agent_state(
        self, tmp_run_dir: Path, fake_sandboxes
    ) -> None:
        """The clean-room property itself: every attempt constructs a NEW
        Sandbox from cfg (never reusing the agent's), with no repo mount and
        no agent env — the script must set up everything from zero."""
        _write_script(tmp_run_dir)
        created = fake_sandboxes([(1, "fail"), (1, "fail")])
        cfg = Config(
            base_image="custom/img:1", network="none", memory="8g", cpus="4", pids_limit=128
        )
        run_verify(tmp_run_dir, _plan(), cfg)
        assert len(created) == 2
        assert created[0] is not created[1]
        for sb in created:
            assert sb.kwargs["image"] == "custom/img:1"
            assert sb.kwargs["mounts"] == []  # no repo mount: nothing from the agent
            assert sb.kwargs["group_add"] is None
            assert sb.kwargs["workdir"] == "/work"
            assert sb.kwargs["network"] == "none"
            assert sb.kwargs["memory"] == "8g"
            assert sb.kwargs["cpus"] == "4"
            assert sb.kwargs["pids_limit"] == 128
            assert "env" not in sb.kwargs  # agent credentials never leak in

    def test_retries_disabled_runs_single_attempt(
        self, tmp_run_dir: Path, fake_sandboxes
    ) -> None:
        _write_script(tmp_run_dir)
        created = fake_sandboxes([(1, "fail")])
        report = run_verify(tmp_run_dir, _plan(), Config(verify_retries=0))
        assert report.passed is False
        assert report.attempts == 1
        assert len(created) == 1

    def test_script_replayed_from_tmp_copy(self, tmp_run_dir: Path, fake_sandboxes) -> None:
        """The script is copied to /tmp (NOT /work) so its own `git clone .`
        preamble finds /work empty, then replayed with the verify timeout."""
        script = _write_script(tmp_run_dir)
        created = fake_sandboxes([(0, MARKER)])
        run_verify(tmp_run_dir, _plan(), Config(verify_timeout_s=123))
        sb = created[0]
        assert sb.copy_ins == [(script, "/tmp/commands.sh")]
        assert sb.exec_calls[0]["command"] == "bash /tmp/commands.sh"
        assert sb.exec_calls[0]["timeout"] == 123

    def test_sandboxes_always_destroyed(self, tmp_run_dir: Path, fake_sandboxes) -> None:
        _write_script(tmp_run_dir)
        created = fake_sandboxes([(1, "fail"), (0, MARKER)])
        run_verify(tmp_run_dir, _plan(), Config())
        assert [sb.destroyed for sb in created] == [True, True]
        assert [sb.started for sb in created] == [True, True]

    def test_missing_commands_sh_raises(self, tmp_run_dir: Path, fake_sandboxes) -> None:
        fake_sandboxes([])
        with pytest.raises(FileNotFoundError, match="commands.sh"):
            run_verify(tmp_run_dir, _plan(), Config())

    def test_log_header_separators_and_truncation(
        self, tmp_run_dir: Path, fake_sandboxes
    ) -> None:
        _write_script(tmp_run_dir)
        (tmp_run_dir / "verify.log").write_text("stale previous run\n", encoding="utf-8")
        fake_sandboxes([(0, f"replay output\n{MARKER}\n")])
        run_verify(tmp_run_dir, _plan(pattern="v[0-9]+"), Config())
        log = (tmp_run_dir / "verify.log").read_text(encoding="utf-8")
        assert "stale previous run" not in log  # truncated at the start of each run
        assert "image=readme2demo/base:latest" in log
        assert "./tool version =~ /v[0-9]+/" in log
        assert "===== ATTEMPT 1 =====" in log
        assert "replay output" in log

    def test_docker_socket_opt_in_mounts_socket_with_gid(
        self, tmp_run_dir: Path, fake_sandboxes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--allow-docker-socket: the replay gets the same socket + group-add
        the agent proved with, or the replay can't repeat container-managing
        commands (failure class 9)."""
        _write_script(tmp_run_dir)
        probed: list[str] = []

        def fake_gid(image: str) -> str:
            probed.append(image)
            return "999"

        monkeypatch.setattr(sandbox_mod, "docker_socket_gid", fake_gid)
        created = fake_sandboxes([(0, MARKER)])
        run_verify(tmp_run_dir, _plan(), Config(allow_docker_socket=True))
        assert probed == ["readme2demo/base:latest"]
        assert created[0].kwargs["mounts"] == [
            ("/var/run/docker.sock", "/var/run/docker.sock", "rw")
        ]
        assert created[0].kwargs["group_add"] == "999"

    def test_success_exports_worktree_for_render(
        self, tmp_run_dir: Path, fake_sandboxes
    ) -> None:
        _write_script(tmp_run_dir)
        created = fake_sandboxes([(0, MARKER)])
        run_verify(tmp_run_dir, _plan(), Config())
        assert created[0].copy_outs == [("/work/.", tmp_run_dir / "worktree")]

    def test_failure_does_not_export_worktree(self, tmp_run_dir: Path, fake_sandboxes) -> None:
        """Only the VERIFIED /work may reach the renderer — an unverified
        worktree on the tape would break the grounding invariant."""
        _write_script(tmp_run_dir)
        created = fake_sandboxes([(1, "fail"), (1, "fail")])
        run_verify(tmp_run_dir, _plan(), Config())
        assert all(sb.copy_outs == [] for sb in created)


# --- _export_worktree ----------------------------------------------------------


class _StubSandbox:
    """copy_out-only stand-in for _export_worktree tests."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def copy_out(self, src: str, dst: Path) -> None:
        if self.fail:
            raise SandboxError("docker cp out failed: container is gone")
        Path(dst).mkdir(parents=True)
        (Path(dst) / "fresh.txt").write_text("built artifact", encoding="utf-8")


class TestExportWorktree:
    def test_replaces_stale_worktree(self, tmp_run_dir: Path) -> None:
        stale = tmp_run_dir / "worktree" / "stale.txt"
        stale.parent.mkdir(parents=True)
        stale.write_text("from an older run", encoding="utf-8")
        _export_worktree(_StubSandbox(), tmp_run_dir)
        assert (tmp_run_dir / "worktree" / "fresh.txt").is_file()
        assert not stale.exists()

    def test_failed_export_degrades_gracefully(self, tmp_run_dir: Path) -> None:
        """Best effort: a failed export removes the partial dir and must NOT
        raise — it degrades the video, never the verification verdict."""
        (tmp_run_dir / "worktree").mkdir()
        (tmp_run_dir / "worktree" / "partial.txt").write_text("junk", encoding="utf-8")
        _export_worktree(_StubSandbox(fail=True), tmp_run_dir)  # must not raise
        assert not (tmp_run_dir / "worktree").exists()


# --- verify_feedback (cwd_hints integration) ------------------------------------


class TestVerifyFeedback:
    def test_empty_without_log(self, tmp_run_dir: Path) -> None:
        assert verify_feedback(tmp_run_dir) == ""

    def test_contains_log_tail_and_cwd_hints(self, tmp_run_dir: Path) -> None:
        """Regression-adjacent (tfdrift run 2): the feedback that reaches the
        distiller retry must carry BOTH the replay log tail and the cwd_hints
        static analysis of the failing script."""
        (tmp_run_dir / "verify.log").write_text(
            "ERROR: no module named tool\n", encoding="utf-8"
        )
        _write_script(tmp_run_dir, "cd /tmp\npip install -e .\n")
        fb = verify_feedback(tmp_run_dir)
        assert "FAILED when replayed in a fresh clean container" in fb
        assert "ERROR: no module named tool" in fb
        assert "pip install -e ." in fb  # the flagged line
        assert "/tmp" in fb  # the drifted cwd
        assert "cd persists across lines" in fb

    def test_no_hints_section_when_script_is_clean(self, tmp_run_dir: Path) -> None:
        (tmp_run_dir / "verify.log").write_text("some failure\n", encoding="utf-8")
        _write_script(tmp_run_dir, "cd /work\npip install -e .\n")
        fb = verify_feedback(tmp_run_dir)
        assert "some failure" in fb
        assert "Working-directory analysis" not in fb

    def test_long_log_truncated_to_tail(self, tmp_run_dir: Path) -> None:
        (tmp_run_dir / "verify.log").write_text(
            "HEAD-SENTINEL " + "x" * 7000 + " TAIL-SENTINEL", encoding="utf-8"
        )
        _write_script(tmp_run_dir, "cd /work\necho ok\n")
        fb = verify_feedback(tmp_run_dir, max_bytes=6000)
        assert "...[log truncated]..." in fb
        assert "TAIL-SENTINEL" in fb
        assert "HEAD-SENTINEL" not in fb  # head is dropped, tail is kept
