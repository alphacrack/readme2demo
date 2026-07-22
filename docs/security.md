# Security model

READMEs are untrusted code. readme2demo executes them, so isolation is a core
design concern, not an afterthought.

## The sandbox is the boundary

The agent runs *inside* a hardened Docker container:

- `--cap-drop ALL` and `--security-opt no-new-privileges`
- non-root user (`USER demo` in the base image)
- memory, CPU, and PID limits
- the repo is mounted read-only; the agent works on a writable copy

The verify stage then replays the distilled `commands.sh` in a **separate,
fresh container** with no model credentials and no state carried over from the
agent's run.

For a **claims-vs-roadmap table** (enforced today vs planned v0.8 vs known
limitations, each with code pointers), see the root
[`SECURITY.md`](../SECURITY.md). That table is the source of truth; this page
is the short summary.

## Known tradeoffs

- **API key in the sandbox** (MVP): the model credential currently enters the
  sandbox (and the `docker run` argv). Use a dedicated, low-limit key. A
  host-side, key-injecting egress proxy is planned so the credential never
  crosses the boundary ([#64](https://github.com/alphacrack/readme2demo/issues/64);
  argv exposure tracked in
  [#51](https://github.com/alphacrack/readme2demo/issues/51)).
- **`--allow-docker-socket`** is off by default. Enabling it mounts the host
  Docker socket into the sandbox so demos that manage containers work — but the
  socket pierces isolation. Only enable it for repositories you trust.
- **Network egress is open bridge networking today, not allowlisted.** The
  sandbox defaults to Docker `--network bridge` so package installs and git
  clones work. Domain allowlisting is planned for v0.8 (#64). Until then,
  treat outbound network from a compromised agent as possible.

## Reporting a vulnerability

Please report vulnerabilities **privately**, not in public issues. See
[`SECURITY.md`](https://github.com/alphacrack/readme2demo/blob/main/SECURITY.md)
in the repository for the full threat model and the private reporting channel
(GitHub Security Advisories).
