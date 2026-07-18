"""Unit tests for the hardened Docker sandbox (sandbox.py).

Executable form of the CLAUDE.md rule "never weaken sandbox.py hardening
flags": every hardening flag has a test named for it, so removing a flag from
``Sandbox.start()`` fails a test whose name says which flag went missing. All
docker CLI calls are intercepted by a fake ``subprocess.run`` — no Docker, no
network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import readme2demo.sandbox as sandbox_mod
from readme2demo.sandbox import Sandbox, SandboxError, docker_socket_gid

IMAGE = "readme2demo/base:latest"


class _FakeProc:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeDocker:
    """Records every argv handed to subprocess.run; never touches Docker.

    Results (or exceptions) can be queued FIFO with :meth:`queue`; calls
    beyond the queue succeed with exit code 0.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []
        self._queue: list[object] = []

    def queue(self, *results: object) -> None:
        self._queue.extend(results)

    def __call__(self, cmd: list[str], **kwargs: object) -> _FakeProc:
        self.calls.append(list(cmd))
        self.kwargs.append(kwargs)
        result = self._queue.pop(0) if self._queue else _FakeProc()
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> _FakeDocker:
    fake = _FakeDocker()
    monkeypatch.setattr(sandbox_mod.subprocess, "run", fake)
    return fake


def _pair(argv: list[str], flag: str) -> str | None:
    """Value following ``flag`` in argv, or None when the flag is absent."""
    for i, tok in enumerate(argv[:-1]):
        if tok == flag:
            return argv[i + 1]
    return None


def _pairs(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, tok in enumerate(argv[:-1]) if tok == flag]


def _start_argv(docker: _FakeDocker, **kwargs: object) -> list[str]:
    Sandbox(image=IMAGE, name="r2d-test", **kwargs).start()  # type: ignore[arg-type]
    return docker.calls[0]


# --- Sandbox.start hardening argv ---------------------------------------------


class TestStartHardeningArgv:
    """One test per hardening flag: a removed flag fails the test naming it."""

    def test_exact_default_run_argv(self, docker: _FakeDocker) -> None:
        """The full docker-run argv, pinned. Any drift in the hardening set
        shows up here as an exact diff — weakening it needs an explicit test
        change, which is the discussion CLAUDE.md demands."""
        assert _start_argv(docker) == [
            "docker", "run", "-d",
            "--name", "r2d-test",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", "4g",
            "--cpus", "2",
            "--pids-limit", "512",
            "--network", "bridge",
            "-w", "/work",
            IMAGE, "sleep", "infinity",
        ]

    def test_cap_drop_all(self, docker: _FakeDocker) -> None:
        assert _pair(_start_argv(docker), "--cap-drop") == "ALL"

    def test_security_opt_no_new_privileges(self, docker: _FakeDocker) -> None:
        assert _pair(_start_argv(docker), "--security-opt") == "no-new-privileges"

    def test_memory_limit(self, docker: _FakeDocker) -> None:
        assert _pair(_start_argv(docker), "--memory") == "4g"

    def test_cpu_limit(self, docker: _FakeDocker) -> None:
        assert _pair(_start_argv(docker), "--cpus") == "2"

    def test_pids_limit(self, docker: _FakeDocker) -> None:
        assert _pair(_start_argv(docker), "--pids-limit") == "512"

    def test_network_mode_default_bridge(self, docker: _FakeDocker) -> None:
        assert _pair(_start_argv(docker), "--network") == "bridge"

    def test_network_mode_configurable(self, docker: _FakeDocker) -> None:
        assert _pair(_start_argv(docker, network="none"), "--network") == "none"

    def test_workdir(self, docker: _FakeDocker) -> None:
        assert _pair(_start_argv(docker), "-w") == "/work"

    def test_non_root_no_privilege_escalation_flags(self, docker: _FakeDocker) -> None:
        """The base image bakes in USER demo (uid 1000); start() must not
        override it back to root, and must never grant privileges."""
        argv = _start_argv(docker)
        assert "--user" not in argv  # image's non-root USER stays in effect
        assert "--privileged" not in argv
        assert "--cap-add" not in argv

    def test_user_flag_only_when_requested(self, docker: _FakeDocker) -> None:
        assert _pair(_start_argv(docker, user="1000:1000"), "--user") == "1000:1000"

    def test_group_add_only_when_requested(self, docker: _FakeDocker) -> None:
        assert _pair(_start_argv(docker), "--group-add") is None
        docker.calls.clear()
        assert _pair(_start_argv(docker, group_add="999"), "--group-add") == "999"


# --- Sandbox.start env and mounts ---------------------------------------------


class TestStartEnvAndMounts:
    def test_env_passed_as_pairs_in_order(self, docker: _FakeDocker) -> None:
        argv = _start_argv(docker, env={"API_KEY": "sk-1", "MODE": "fast"})
        assert _pairs(argv, "-e") == ["API_KEY=sk-1", "MODE=fast"]

    def test_relative_mount_src_forced_absolute(self, docker: _FakeDocker) -> None:
        """docker -v treats a relative src as a NAMED VOLUME, silently mounting
        an empty volume instead of the intended directory — start() must
        resolve every mount src to an absolute path."""
        argv = _start_argv(docker, mounts=[("rel/dir", "/dst", "ro")])
        assert _pairs(argv, "-v") == [f"{Path('rel/dir').resolve()}:/dst:ro"]

    def test_absolute_mount_with_mode(self, docker: _FakeDocker, tmp_path: Path) -> None:
        argv = _start_argv(docker, mounts=[(str(tmp_path), "/repo", "ro")])
        assert _pairs(argv, "-v") == [f"{tmp_path.resolve()}:/repo:ro"]

    def test_image_and_idle_command_are_last(self, docker: _FakeDocker) -> None:
        argv = _start_argv(docker, env={"K": "V"}, mounts=[("/a", "/b", "rw")])
        assert argv[-3:] == [IMAGE, "sleep", "infinity"]


# --- Sandbox.start / _run error paths -----------------------------------------


class TestStartFailure:
    def test_docker_run_failure_raises_with_exit_and_output(self, docker: _FakeDocker) -> None:
        docker.queue(_FakeProc(returncode=125, stderr="daemon down"))
        sb = Sandbox(image=IMAGE, name="r2d-test")
        with pytest.raises(SandboxError, match=r"docker run failed \(125\).*daemon down"):
            sb.start()
        # Never marked started, so destroy() must not issue a docker rm.
        sb.destroy()
        assert ["docker", "rm", "-f", "r2d-test"] not in docker.calls

    def test_docker_cli_missing_raises_actionable_error(self, docker: _FakeDocker) -> None:
        docker.queue(FileNotFoundError("docker"))
        with pytest.raises(SandboxError, match="docker CLI not found"):
            Sandbox(image=IMAGE, name="r2d-test").start()


# --- Sandbox.exec -------------------------------------------------------------


class TestExec:
    def test_exec_before_start_raises(self, docker: _FakeDocker) -> None:
        with pytest.raises(SandboxError, match="not started"):
            Sandbox(image=IMAGE, name="r2d-test").exec("echo hi")
        assert docker.calls == []

    def test_exec_argv(self, docker: _FakeDocker) -> None:
        sb = Sandbox(image=IMAGE, name="r2d-test").start()
        sb.exec("echo hi")
        assert docker.calls[1] == [
            "docker", "exec", "-w", "/work", "r2d-test", "bash", "-lc", "echo hi",
        ]

    def test_exec_workdir_and_env_overrides(self, docker: _FakeDocker) -> None:
        sb = Sandbox(image=IMAGE, name="r2d-test").start()
        sb.exec("run", workdir="/elsewhere", env={"K": "V"})
        assert docker.calls[1] == [
            "docker", "exec", "-w", "/elsewhere", "-e", "K=V",
            "r2d-test", "bash", "-lc", "run",
        ]

    def test_exec_combines_stdout_and_stderr(self, docker: _FakeDocker) -> None:
        sb = Sandbox(image=IMAGE, name="r2d-test").start()
        docker.queue(_FakeProc(returncode=0, stdout="out", stderr="err"))
        res = sb.exec("noisy")
        assert res.ok
        assert res.output == "outerr"

    def test_exec_timeout_returns_124_with_partial_output(self, docker: _FakeDocker) -> None:
        sb = Sandbox(image=IMAGE, name="r2d-test").start()
        docker.queue(
            subprocess.TimeoutExpired(cmd=["docker"], timeout=9, output="partial output")
        )
        res = sb.exec("sleep 999", timeout=9)
        assert res.exit_code == 124
        assert not res.ok
        assert "partial output" in res.output
        assert "TIMEOUT" in res.output

    def test_exec_stream_to_appends_to_sink(self, docker: _FakeDocker, tmp_path: Path) -> None:
        log = tmp_path / "verify.log"
        sb = Sandbox(image=IMAGE, name="r2d-test").start()
        res = sb.exec("bash /tmp/commands.sh", stream_to=log)
        kwargs = docker.kwargs[-1]
        assert kwargs["stderr"] is subprocess.STDOUT  # interleaved, like a terminal
        assert hasattr(kwargs["stdout"], "write")  # an (append-mode) file sink
        assert res.exit_code == 0
        assert str(log) in res.output  # placeholder, not the streamed bytes


# --- copy_in / copy_out / destroy / context manager ----------------------------


class TestCopyAndDestroy:
    def test_copy_in_argv(self, docker: _FakeDocker, tmp_path: Path) -> None:
        sb = Sandbox(image=IMAGE, name="r2d-test").start()
        src = tmp_path / "commands.sh"
        src.write_text("echo hi\n", encoding="utf-8")
        sb.copy_in(src, "/tmp/commands.sh")
        assert docker.calls[1] == ["docker", "cp", str(src), "r2d-test:/tmp/commands.sh"]

    def test_copy_in_failure_raises(self, docker: _FakeDocker, tmp_path: Path) -> None:
        sb = Sandbox(image=IMAGE, name="r2d-test").start()
        docker.queue(_FakeProc(returncode=1, stderr="no such container"))
        with pytest.raises(SandboxError, match="docker cp in failed.*no such container"):
            sb.copy_in(tmp_path / "x", "/x")

    def test_copy_out_argv_and_parent_creation(
        self, docker: _FakeDocker, tmp_path: Path
    ) -> None:
        sb = Sandbox(image=IMAGE, name="r2d-test").start()
        dst = tmp_path / "nested" / "deep" / "worktree"
        sb.copy_out("/work/.", dst)
        assert dst.parent.is_dir()  # created for docker cp to land in
        assert docker.calls[1] == ["docker", "cp", "r2d-test:/work/.", str(dst)]

    def test_copy_out_failure_raises(self, docker: _FakeDocker, tmp_path: Path) -> None:
        sb = Sandbox(image=IMAGE, name="r2d-test").start()
        docker.queue(_FakeProc(returncode=1, stderr="path does not exist"))
        with pytest.raises(SandboxError, match="docker cp out failed"):
            sb.copy_out("/work/.", tmp_path / "out")

    def test_destroy_runs_rm_f_once(self, docker: _FakeDocker) -> None:
        sb = Sandbox(image=IMAGE, name="r2d-test").start()
        sb.destroy()
        sb.destroy()  # idempotent: only one rm
        rm_calls = [c for c in docker.calls if c[:3] == ["docker", "rm", "-f"]]
        assert rm_calls == [["docker", "rm", "-f", "r2d-test"]]

    def test_destroy_without_start_is_noop(self, docker: _FakeDocker) -> None:
        Sandbox(image=IMAGE, name="r2d-test").destroy()
        assert docker.calls == []

    def test_context_manager_starts_and_destroys(self, docker: _FakeDocker) -> None:
        with Sandbox(image=IMAGE, name="r2d-ctx") as sb:
            sb.exec("echo hi")  # started: exec must not raise
        assert docker.calls[-1] == ["docker", "rm", "-f", "r2d-ctx"]

    def test_context_manager_destroys_on_body_exception(self, docker: _FakeDocker) -> None:
        """Containers are cattle: even a crashing stage must not leak one."""
        with pytest.raises(RuntimeError, match="boom"):
            with Sandbox(image=IMAGE, name="r2d-ctx"):
                raise RuntimeError("boom")
        assert docker.calls[-1] == ["docker", "rm", "-f", "r2d-ctx"]


# --- docker_socket_gid ---------------------------------------------------------


class TestDockerSocketGid:
    def test_returns_probed_gid(self, docker: _FakeDocker) -> None:
        docker.queue(_FakeProc(returncode=0, stdout="999\n"))
        assert docker_socket_gid(IMAGE) == "999"
        argv = docker.calls[0]
        # The probe stats the socket from INSIDE a container of the same image.
        assert _pair(argv, "-v") == "/var/run/docker.sock:/var/run/docker.sock"
        assert _pair(argv, "--entrypoint") == "stat"

    def test_nonzero_exit_falls_back_to_root_gid(self, docker: _FakeDocker) -> None:
        docker.queue(_FakeProc(returncode=1, stderr="cannot connect"))
        assert docker_socket_gid(IMAGE) == "0"

    def test_non_numeric_output_falls_back_to_root_gid(self, docker: _FakeDocker) -> None:
        docker.queue(_FakeProc(returncode=0, stdout="stat: garbage"))
        assert docker_socket_gid(IMAGE) == "0"

    def test_probe_timeout_falls_back_to_root_gid(self, docker: _FakeDocker) -> None:
        docker.queue(subprocess.TimeoutExpired(cmd=["docker"], timeout=60))
        assert docker_socket_gid(IMAGE) == "0"
