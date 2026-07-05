"""Hardened Docker sandbox, driven via the docker CLI (no docker-py dependency).

Containers are cattle: one per stage that needs one, always destroyed. The
container is the security boundary for the agent, so hardening flags are not
optional — see IMPLEMENTATION_PLAN.md M2.
"""

from __future__ import annotations

import shlex
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class SandboxError(RuntimeError):
    pass


@dataclass
class ExecResult:
    exit_code: int
    output: str  # combined stdout+stderr

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def docker_socket_gid(image: str) -> str:
    """GID owning /var/run/docker.sock, probed from inside a container.

    The sandbox runs as non-root ``demo``; a mounted Docker socket is
    typically root-owned, so without group access every socket call fails
    with EACCES and tools report "no container runtime available" even
    though the socket is right there. Passing ``--group-add <this gid>``
    fixes it. Falls back to "0" (root group) when the probe fails.
    """
    try:
        proc = subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "--entrypoint", "stat",
                image, "-c", "%g", "/var/run/docker.sock",
            ],
            capture_output=True, text=True, timeout=60, errors="replace",
        )
        gid = proc.stdout.strip()
        if proc.returncode == 0 and gid.isdigit():
            return gid
    except (subprocess.TimeoutExpired, OSError):
        pass
    return "0"


@dataclass
class Sandbox:
    """Lifecycle wrapper around one hardened container.

    Usage::

        with Sandbox(image="readme2demo/base:latest", env={"K": "V"}) as sb:
            res = sb.exec("echo hello")
    """

    image: str
    env: dict[str, str] = field(default_factory=dict)
    mounts: list[tuple[str, str, str]] = field(default_factory=list)  # (src, dst, mode)
    workdir: str = "/work"
    network: str = "bridge"
    memory: str = "4g"
    cpus: str = "2"
    pids_limit: int = 512
    user: Optional[str] = None
    group_add: Optional[str] = None  # extra group (e.g. docker socket gid)
    name: str = field(default_factory=lambda: f"r2d-{uuid.uuid4().hex[:10]}")
    _started: bool = field(default=False, init=False)

    # -- low-level -----------------------------------------------------------

    @staticmethod
    def _run(cmd: list[str], timeout: Optional[int] = None,
             stream_to: Optional[Path] = None) -> ExecResult:
        """Run a docker CLI command on the host."""
        try:
            if stream_to is not None:
                with open(stream_to, "ab") as sink:
                    proc = subprocess.run(
                        cmd, stdout=sink, stderr=subprocess.STDOUT, timeout=timeout
                    )
                return ExecResult(proc.returncode, f"(streamed to {stream_to})")
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, errors="replace"
            )
            return ExecResult(proc.returncode, (proc.stdout or "") + (proc.stderr or ""))
        except subprocess.TimeoutExpired as e:
            partial = ""
            if e.stdout:
                partial = e.stdout if isinstance(e.stdout, str) else e.stdout.decode(errors="replace")
            return ExecResult(124, partial + "\n[readme2demo] TIMEOUT")
        except FileNotFoundError as e:  # docker not installed
            raise SandboxError(
                "docker CLI not found — install Docker and ensure it is on PATH"
            ) from e

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "Sandbox":
        cmd = [
            "docker", "run", "-d",
            "--name", self.name,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            "--network", self.network,
            "-w", self.workdir,
        ]
        if self.user:
            cmd += ["--user", self.user]
        if self.group_add:
            cmd += ["--group-add", self.group_add]
        for k, v in self.env.items():
            cmd += ["-e", f"{k}={v}"]
        for src, dst, mode in self.mounts:
            # docker -v treats relative paths as volume names; force absolute.
            cmd += ["-v", f"{Path(src).resolve()}:{dst}:{mode}"]
        cmd += [self.image, "sleep", "infinity"]
        res = self._run(cmd, timeout=120)
        if not res.ok:
            raise SandboxError(f"docker run failed ({res.exit_code}): {res.output.strip()}")
        self._started = True
        return self

    def exec(
        self,
        command: str,
        timeout: Optional[int] = None,
        env: Optional[dict[str, str]] = None,
        stream_to: Optional[Path] = None,
        workdir: Optional[str] = None,
    ) -> ExecResult:
        """Execute a shell command inside the container via ``bash -lc``."""
        if not self._started:
            raise SandboxError("Sandbox not started")
        cmd = ["docker", "exec"]
        if workdir or self.workdir:
            cmd += ["-w", workdir or self.workdir]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [self.name, "bash", "-lc", command]
        return self._run(cmd, timeout=timeout, stream_to=stream_to)

    def copy_in(self, src: Path, dst: str) -> None:
        res = self._run(["docker", "cp", str(src), f"{self.name}:{dst}"], timeout=300)
        if not res.ok:
            raise SandboxError(f"docker cp in failed: {res.output.strip()}")

    def copy_out(self, src: str, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        res = self._run(["docker", "cp", f"{self.name}:{src}", str(dst)], timeout=300)
        if not res.ok:
            raise SandboxError(f"docker cp out failed: {res.output.strip()}")

    def commit(self, tag: str) -> str:
        """Snapshot the container (used for the post-setup render checkpoint)."""
        res = self._run(["docker", "commit", self.name, tag], timeout=300)
        if not res.ok:
            raise SandboxError(f"docker commit failed: {res.output.strip()}")
        return tag

    def destroy(self) -> None:
        if self._started:
            self._run(["docker", "rm", "-f", self.name], timeout=60)
            self._started = False

    # -- context manager -----------------------------------------------------

    def __enter__(self) -> "Sandbox":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.destroy()


def shell_quote(s: str) -> str:
    return shlex.quote(s)
