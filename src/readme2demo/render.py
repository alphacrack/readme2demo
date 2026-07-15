"""M7 — Renderer: VHS turns ``demo.tape`` into ``demo.mp4`` + ``demo.gif``.

VHS actually *types and executes* the tape's commands inside its container, so
the rendered video is a genuine run — but that also means the render container
must contain everything the tape needs. See :func:`run_render` for the exact
contract.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .config import Config

_SLEEP_RE = re.compile(r"^Sleep\s+([\d.]+)(ms|s)\s*$", re.MULTILINE)
_TYPE_RE = re.compile(r"^Type\s+(.+)$", re.MULTILINE)
_TYPING_SPEED_RE = re.compile(r"^Set\s+TypingSpeed\s+([\d.]+)ms", re.MULTILINE)

#: Hard wall-clock limit for one VHS render (seconds). Generous: the tape
#: executes the FULL step_by_step.md — clones, installs, and builds included.
RENDER_TIMEOUT_S = 1800

#: VHS renders only demo.mp4 (rendering a multi-minute GIF at full size once
#: filled the Docker VM disk — every frame is a PNG). demo.gif is a short
#: downscaled PREVIEW generated from the mp4 afterwards with ffmpeg.
PRIMARY_ARTIFACT = "demo.mp4"
GIF_PREVIEW = "demo.gif"
GIF_PREVIEW_SECONDS = 30
GIF_PREVIEW_WIDTH = 800
GIF_PREVIEW_FPS = 10

#: Minimum plausible artifact size — anything smaller is a broken render.
MIN_ARTIFACT_BYTES = 10 * 1024

#: Sane mp4 duration bounds (seconds), checked when ffprobe is available.
#: Full-tutorial videos legitimately run many minutes when builds are on camera.
MIN_DURATION_S = 5.0
MAX_DURATION_S = 1500.0


class RenderError(RuntimeError):
    """Raised when VHS fails or produces no valid artifacts."""


def expected_min_duration_s(tape_text: str) -> float:
    """Lower bound on how long a faithful render of this tape must be.

    Sum of every ``Sleep`` plus the typing time of every ``Type`` line at the
    tape's TypingSpeed. Command *execution* time (the ``Wait``s) comes on top
    in reality, so a video shorter than this bound means steps did not play —
    an aborted tape, a broken image, or a stale tape file. That failure mode
    previously shipped silently as a few-second "demo".
    """
    total = 0.0
    for value, unit in _SLEEP_RE.findall(tape_text):
        total += float(value) / (1000.0 if unit == "ms" else 1.0)
    speed_match = _TYPING_SPEED_RE.search(tape_text)
    per_char_s = (float(speed_match.group(1)) if speed_match else 50.0) / 1000.0
    for typed in _TYPE_RE.findall(tape_text):
        total += max(0, len(typed) - 2) * per_char_s  # -2 for the quote delimiters
    return total


def check_render_image(image: str) -> None:
    """Fail fast if the render image can't produce a full-tutorial video.

    The tape executes real toolchain commands, so the image must be the
    readme2demo base image (VHS + toolchains), not the stock VHS image or a
    stale pre-VHS build.
    """
    probe = "command -v vhs && command -v ttyd && command -v ffmpeg && command -v git"
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "sh", image, "-c", probe],
            capture_output=True, text=True, errors="replace", timeout=120,
        )
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"probe of render image {image} timed out") from e
    except FileNotFoundError as e:
        raise RenderError("docker CLI not found — install Docker and ensure it is on PATH") from e
    if proc.returncode != 0:
        raise RenderError(
            f"Render image {image!r} is missing vhs/ttyd/ffmpeg/git — it is "
            "stale or not built from images/base/. Rebuild it:\n"
            "  docker build --no-cache -t readme2demo/base:latest images/base/"
        )


def run_render(run_dir: Path, cfg: Config, image: str | None = None) -> list[Path]:
    """Render ``run_dir/demo.tape`` with VHS in a container; return artifact paths.

    Runs in ``cfg.base_image`` by default — it is built FROM the official VHS
    image (vhs/ttyd/ffmpeg) *plus* the project toolchains, so the tape can
    execute the complete ``step_by_step.md`` for real: clone, install, build,
    demo, each captured on camera. Resource limits mirror the sandbox
    hardening; network is required (the tape clones and installs).

    Args:
        run_dir: Run directory containing ``demo.tape``; artifacts land here.
        cfg: Pipeline config (supplies ``base_image``, limits, network).
        image: Optional image override (must contain a ``vhs`` binary).

    Returns:
        Paths of the valid rendered artifacts (see :func:`validate_outputs`).

    Raises:
        RenderError: if the tape is missing, VHS fails or times out, or no
            valid artifact was produced.
    """
    run_dir = run_dir.resolve()
    tape = run_dir / "demo.tape"
    if not tape.exists():
        raise RenderError(f"demo.tape not found in {run_dir}")

    render_image = image or cfg.base_image
    check_render_image(render_image)
    min_duration = expected_min_duration_s(tape.read_text(encoding="utf-8"))

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{run_dir}:/vhs",
        "-w", "/vhs",
        "--memory", cfg.memory,
        "--cpus", cfg.cpus,
        "--network", cfg.network,
    ]
    if cfg.allow_docker_socket:
        # The tape replays container-managing commands on camera. group-add
        # gives the container permission to actually use the mounted socket.
        from readme2demo.sandbox import docker_socket_gid

        cmd += [
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "--group-add", docker_socket_gid(render_image),
        ]
    cmd += [
        "--entrypoint", "vhs",
        render_image,
        "demo.tape",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=RENDER_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise RenderError(
            f"VHS render timed out after {RENDER_TIMEOUT_S}s (image {image or cfg.base_image})"
        ) from e
    except FileNotFoundError as e:
        raise RenderError("docker CLI not found — install Docker and ensure it is on PATH") from e

    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
        raise RenderError(f"VHS exited with {proc.returncode}:\n{tail}")

    _generate_gif_preview(run_dir, render_image, cfg)
    return validate_outputs(run_dir, min_duration_s=min_duration)


def _generate_gif_preview(run_dir: Path, image: str, cfg: Config) -> None:
    """Best-effort short GIF preview from the rendered mp4 (for README embeds).

    A full-length GIF of a multi-minute tutorial is enormous and rendering it
    frame-by-frame once exhausted the Docker VM disk; a {GIF_PREVIEW_SECONDS}s
    downscaled preview does the README-embed job at ~1% of the size. Failure
    here never fails the stage — the mp4 is the artifact of record.
    """
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{run_dir}:/vhs",
        "--entrypoint", "ffmpeg",
        image,
        "-y", "-loglevel", "error",
        "-t", str(GIF_PREVIEW_SECONDS),
        "-i", f"/vhs/{PRIMARY_ARTIFACT}",
        "-vf", f"fps={GIF_PREVIEW_FPS},scale={GIF_PREVIEW_WIDTH}:-1:flags=lanczos",
        f"/vhs/{GIF_PREVIEW}",
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=300)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _mp4_duration_s(path: Path, ffprobe: str) -> float | None:
    """Return the mp4 duration via ffprobe, or None if it can't be determined."""
    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def validate_outputs(run_dir: Path, min_duration_s: float = 0.0) -> list[Path]:
    """Check rendered artifacts in ``run_dir`` and return the valid ones.

    A file is valid when it exists and is larger than ``MIN_ARTIFACT_BYTES``.
    Additionally, when ``ffprobe`` is available on the host, ``demo.mp4`` must
    be at least ``max(MIN_DURATION_S, min_duration_s * 0.8)`` seconds long —
    ``min_duration_s`` is the tape's computed lower bound, so a shorter video
    means steps did not play (the "few-second incomplete demo" failure) — and
    at most ``MAX_DURATION_S``. Without ffprobe the duration check is skipped.

    Raises:
        RenderError: with per-file specifics if *no* artifact is valid, or if
            the mp4 exists but is shorter than the tape's lower bound.
    """
    ffprobe = shutil.which("ffprobe")
    floor = max(MIN_DURATION_S, min_duration_s * 0.8)

    mp4 = run_dir / PRIMARY_ARTIFACT
    if not mp4.exists():
        raise RenderError(f"{PRIMARY_ARTIFACT}: missing after render")
    size = mp4.stat().st_size
    if size <= MIN_ARTIFACT_BYTES:
        raise RenderError(
            f"{PRIMARY_ARTIFACT}: too small ({size} bytes, need > {MIN_ARTIFACT_BYTES})"
        )
    if ffprobe is not None:
        duration = _mp4_duration_s(mp4, ffprobe)
        if duration is None:
            raise RenderError(f"{PRIMARY_ARTIFACT}: ffprobe could not read duration")
        if duration < floor:
            # An incomplete video is worse than no video: fail the stage.
            raise RenderError(
                f"{PRIMARY_ARTIFACT} is {duration:.1f}s but the tape requires at "
                f"least {floor:.1f}s — the render did not play every step. "
                "Check that the base image is current "
                "(docker build --no-cache -t readme2demo/base:latest images/base/) "
                "and inspect the tape/VHS output."
            )
        if duration > MAX_DURATION_S:
            raise RenderError(
                f"{PRIMARY_ARTIFACT}: duration {duration:.1f}s exceeds "
                f"{MAX_DURATION_S:.0f}s"
            )

    valid = [mp4]
    gif = run_dir / GIF_PREVIEW
    if gif.exists() and gif.stat().st_size > MIN_ARTIFACT_BYTES:
        valid.append(gif)  # preview is best-effort, never required
    return valid
