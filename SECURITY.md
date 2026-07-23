# Security Policy

## The threat model, stated plainly

readme2demo exists to **execute untrusted code**. A README — and any script,
Makefile, or install command it references — is attacker-controllable input.
When you point the tool at a repo, an AI agent runs that repo's instructions.
Treat every run as if you were executing a stranger's shell script, because you
are.

The security design follows from that single fact: the agent runs *inside* a
hardened Docker container, and **that container is the permission boundary** —
not the prompt, not the agent's judgment. A malicious README can do whatever it
likes to that disposable container; the goal is that it can't reach your host,
your secrets beyond what you deliberately inject, or your other runs.

## Sandbox claims vs roadmap

Every row marked **enforced today** was re-verified against the cited code on
this checkout. Rows marked **planned** or **known limitation** are intentional
honesty, not marketing. Parent epic for planned egress/credential hardening:
[#64](https://github.com/alphacrack/readme2demo/issues/64).

| Claim (agent + verify containers) | Status | Where (verify on your checkout — line numbers drift) |
|---|---|---|
| All Linux capabilities dropped | **enforced today** | `src/readme2demo/sandbox.py` `start()` — `"--cap-drop", "ALL"` |
| No privilege escalation | **enforced today** | `sandbox.py` `start()` — `"--security-opt", "no-new-privileges"` |
| Memory / CPU / PID caps | **enforced today** | `sandbox.py` `Sandbox` defaults `memory="4g"`, `cpus="2"`, `pids_limit=512`; applied in `start()` |
| Non-root user in image | **enforced today** | `images/base/Dockerfile` — `useradd … demo` then `USER demo` |
| Target repo mounted read-only | **enforced today** | `agent.py` — mount `(…/repo, "/repo", "ro")`; agent copies to writable `/work` |
| Verify replays in a fresh container without model credentials | **enforced today** | `verify.py` — `Sandbox(...)` built with **no** `env=` argument |
| Docker socket only on explicit opt-in | **enforced today** | `agent.py` / `verify.py` — socket mounted only when `cfg.allow_docker_socket` |
| Network egress domain allowlist | **planned (v0.8)** — today: plain Docker network | `sandbox.py` default `network: str = "bridge"`; `start()` passes `"--network", self.network`. No allowlist in tree. See [#64](https://github.com/alphacrack/readme2demo/issues/64) |
| Host-side key-injecting proxy (credentials never enter the sandbox) | **planned (v0.8)** | [#64](https://github.com/alphacrack/readme2demo/issues/64) |
| Disk quotas on the work volume | **planned (v0.8)** | [#64](https://github.com/alphacrack/readme2demo/issues/64) |
| Red-team acceptance harness (hostile-README integration tests) | **planned (v0.8)** | [#64](https://github.com/alphacrack/readme2demo/issues/64) |
| API key visible on the docker argv (`-e KEY=VALUE`) | **known limitation** | `sandbox.py` `start()` env loop (`-e f"{k}={v}"`); agent passes `env=env`. Tracked in [#51](https://github.com/alphacrack/readme2demo/issues/51) |
| Render-stage container hardening (cap-drop / no-new-privileges / pids) | **known limitation** | `render.py` builds its own `docker run` with `--memory`/`--cpus`/`--network` only (no `--cap-drop ALL`, no `no-new-privileges`, no `--pids-limit`). Untrusted-execution surface separate from `Sandbox.start()`. See architecture/README.md |

### Known tradeoffs (narrative)

These match the table; keep them named so adopters are not surprised.

- **The API key enters the sandbox (MVP).** The agent stage constructs
  `Sandbox(..., env=env)` so the model credential is present inside the
  container and also appears on the `docker run` argv — whichever provider you
  run on (`ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`, or `OPENAI_API_KEY` /
  `GEMINI_API_KEY` via the litellm-style `LLM_API_KEY` with the provider
  presets). A fully compromised agent can read it. **Mitigation: use a
  dedicated, low-limit key — never your primary key.** Host-side key injection
  is planned in [#64](https://github.com/alphacrack/readme2demo/issues/64);
  argv exposure is tracked in
  [#51](https://github.com/alphacrack/readme2demo/issues/51). Related:
  [#52](https://github.com/alphacrack/readme2demo/issues/52).
- **`--allow-docker-socket` pierces isolation.** When set, the Docker socket is
  mounted read-write into the sandbox (agent and verify; render can also mount
  it when enabled). That is effectively root on the host Docker daemon. **Only
  pass it for repos you trust.**
- **Network egress is open bridge networking today, not allowlisted.** Runs need
  to clone repos and pull packages, so the default is `--network bridge` with
  no domain filter. Data exfiltration by a determined, compromised agent is not
  fully prevented in this release. Domain allowlisting is planned for v0.8
  ([#64](https://github.com/alphacrack/readme2demo/issues/64)).
- **Render container is less hardened than agent/verify.** The render stage does
  not go through `Sandbox.start()`; see the table row above.

Do not run readme2demo against untrusted repos on a machine that holds
secrets you can't afford to rotate. Prefer a throwaway VM or CI runner.

A shorter operator-facing summary lives in
[`docs/security.md`](docs/security.md) and links back here.

## Supported versions

readme2demo is pre-1.0 and moves fast. Security fixes land on `main` and in
the latest tagged release only. Pin a version for reproducibility, but track
`main` for security updates until 1.0.

| Version | Supported |
|---|---|
| `main` / latest `0.x` tag | ✅ |
| older `0.x` tags | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Report it privately through GitHub's private vulnerability reporting:

1. Go to the repository's **Security** tab → **Report a vulnerability**
   (this opens a private advisory only maintainers can see), or use the direct
   link: `https://github.com/alphacrack/readme2demo/security/advisories/new`.
2. Include: affected version or commit, a description of the issue, and — if
   you can — a minimal repo or README that reproduces it. A run's
   `manifest.json` and the tail of `verify.log` are ideal.

What to expect:

- An acknowledgement of your report as soon as it's triaged.
- An honest assessment of severity and a fix timeline, kept in the private
  advisory thread.
- Credit in the release notes when the fix ships, unless you'd rather stay
  anonymous.

We support coordinated disclosure: please give us a reasonable window to ship
a fix before any public write-up.

## Scope

**In scope:** sandbox escapes, isolation-flag bypasses, credential leakage
paths beyond the documented tradeoffs above, grounding bypasses that let an
unverified command reach published output (`tutorial.md`, `step_by_step.md`,
`commands.sh`, or the demo tape), and anything that turns a target repo into
host code execution.

**Out of scope:** the documented MVP tradeoffs listed above, vulnerabilities
in target repos themselves (that's the point — we run them in a box), and
issues that require the operator to have already disabled the sandbox flags.
