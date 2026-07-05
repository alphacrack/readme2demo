---
name: docker-sandbox
description: Docker usage in readme2demo — the base image, hardened sandbox lifecycle, the Docker-socket opt-in, and diagnosing container/daemon failures. Use when touching sandbox.py, images/base/Dockerfile, render.py, verify.py, or when a run fails with a docker/daemon/permission/disk error.
---

# Docker & the sandbox

The container IS the security boundary. READMEs are untrusted code; the agent
runs INSIDE the container so `--dangerously-skip-permissions` is safe.

## The base image (`images/base/Dockerfile`)

- Built FROM `ghcr.io/charmbracelet/vhs:latest` so ONE image serves the agent
  run, the verify replay, AND the video render (vhs + ttyd + ffmpeg from the
  base; git/Go/Python/Node + Claude Code added on top). This is why the video
  can execute real toolchain steps on camera.
- `ENTRYPOINT []` + `CMD ["bash"]`: the VHS image ships `ENTRYPOINT ["vhs"]`;
  reset it so `sandbox.py` can run arbitrary commands. The renderer re-invokes
  vhs via `--entrypoint vhs`.
- Go comes from the official tarball at build time (distro packages lag and
  strand agents on "go >= 1.2x required"). Rebuild periodically to stay current.
- The Claude Code npm pin is COUPLED to the stream-json parser
  (`engines/claude_code.py`) — bump them together.
- Rebuild after ANY Dockerfile change:
  `docker build --no-cache -t readme2demo/base:latest images/base/`. The render
  stage preflights the image (`render.check_render_image`) and errors clearly
  if it's stale (missing vhs/ffmpeg/git).

## Hardening (`sandbox.py` — never weaken without discussion)

Every container: `--cap-drop ALL`, `--security-opt no-new-privileges`,
`--memory/--cpus/--pids-limit`, non-root user `demo` (uid 1000), wall-clock
timeout, destroyed after the stage. Run dirs are mounted with ABSOLUTE paths
(docker -v treats relative as a volume name).

## Docker-socket opt-in (`--allow-docker-socket`)

For tools that manage containers themselves (toolhive `thv run`,
testcontainers, compose repos). Mounts `/var/run/docker.sock` into agent +
verify + render. The non-root user needs `--group-add $(socket gid)` —
`sandbox.docker_socket_gid` probes it (`stat -c %g`) or every socket call is
EACCES and the tool reports "no container runtime available" despite the mount.
Pierces isolation: trusted repos only; the CLI prints the warning in red.

## Diagnosing docker failures

- `input/output error` / `containerd meta.db` → Docker VM disk full (VHS writes
  a PNG per frame). `docker system prune -af --volumes`; restart Docker
  Desktop; raise the VM disk. The render mitigations (24fps, mp4-only, short
  ffmpeg gif preview) exist because full-length GIFs once filled the disk.
- exit 125 from `docker run` → bad flags / relative mount path — check the
  Sandbox args.
- "no container runtime available" WITH `--allow-docker-socket` → group-add gid
  problem (above).
- verify passes but render image errors → stale base image; rebuild.
