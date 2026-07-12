# Security Policy

## The threat model, stated plainly

readme2demo exists to **execute untrusted code**. A README — and any script,
Makefile, or install command it references — is attacker-controllable input.
When you point the tool at a repo, an AI agent runs that repo's instructions.
Treat every run as if you were executing a stranger's shell script, because you
are.

The security design follows from that single fact: the agent runs *inside* a
hardened Docker container, and **that container is the permission boundary** —
not the prompt, not the agent's judgment. The container is configured to
drop all Linux capabilities, forbid privilege escalation
(`--no-new-privileges`), run as a non-root user, and cap memory, CPU, and PID
counts. A malicious README can do whatever it likes to that disposable
container; the goal is that it can't reach your host, your network, or your
other runs.

## Known tradeoffs (MVP, documented on purpose)

These are real limitations of the current release. We would rather name them
than let you discover them.

- **The API key enters the sandbox.** In the current design, the agent needs
  model credentials inside the container — whichever provider you run on
  (`ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN`, or `OPENAI_API_KEY` /
  `GEMINI_API_KEY` via the litellm-style `LLM_API_KEY` with the provider
  presets). A malicious repo that fully
  compromises the agent could read them. **Mitigation: use a dedicated,
  low-limit API key for runs — never your primary key.** A host-side,
  key-injecting egress proxy that keeps credentials out of the sandbox is on
  the roadmap.
- **`--allow-docker-socket` pierces the isolation.** Some tools legitimately
  manage their own containers, so this flag mounts the Docker socket into the
  sandbox. Doing so is effectively root on the host's Docker daemon. **Only
  pass it for repos you trust**, and the CLI treats it as an explicit,
  opt-in security tradeoff.
- **Network egress is unrestricted bridge networking.** Runs need to clone repos
  and pull packages, so the sandbox has outbound network access. A
  key-injecting egress proxy that restricts outbound connections is planned
  (see ROADMAP / egress-proxy epic) but not yet implemented. Data
  exfiltration by a determined, compromised agent is not prevented in
  this release.

Do not run readme2demo against untrusted repos on a machine that holds
secrets you can't afford to rotate. Prefer a throwaway VM or CI runner.

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
