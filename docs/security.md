# Security model

READMEs are untrusted code. readme2demo executes them, so isolation is a core
design concern, not an afterthought.

## The sandbox is the boundary

The agent runs *inside* a hardened Docker container:

- `--cap-drop ALL` and `--security-opt no-new-privileges`
- non-root user
- memory, CPU, and PID limits
- the repo is mounted read-only; the agent works on a writable copy

The verify stage then replays the distilled `commands.sh` in a **separate,
fresh container** with no state carried over from the agent's run.

## Known tradeoffs

- **API key in the sandbox** (MVP): the model credential currently enters the
  sandbox. Use a dedicated, low-limit key. A host-side, key-injecting egress
  proxy is planned so the credential never crosses the boundary.
- **`--allow-docker-socket`** is off by default. Enabling it mounts the host
  Docker socket into the sandbox so demos that manage containers work — but the
  socket pierces isolation. Only enable it for repositories you trust.

## Reporting a vulnerability

Please report vulnerabilities **privately**, not in public issues. See
[`SECURITY.md`](https://github.com/alphacrack/readme2demo/blob/main/SECURITY.md)
in the repository for the full threat model and the private reporting channel
(GitHub Security Advisories).
