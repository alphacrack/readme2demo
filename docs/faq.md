# FAQ

### What does readme2demo actually do?

It points an AI agent at a repository, runs the README inside a hardened Docker
sandbox, independently replays the distilled result in a fresh container, and
only then publishes a tutorial, a step-by-step guide, a troubleshooting doc,
HowTo JSON-LD, and a demo video that executes every step on camera.

### How is this different from asking an LLM to write a tutorial?

An LLM alone produces plausible-but-untested commands. readme2demo enforces a
**grounding invariant in code**: every published command must have succeeded in
a real sandbox run and then been reproduced from zero in a fresh container. If
the replay fails, the output is labeled `⚠ UNVERIFIED` rather than published as
if it worked.

### Do I need a repository to run it?

No. The repository is optional. You can run from a self-contained
`--step-by-step` guide alone, from a repo, or from both. With a guide alone, no
repo is cloned and the guide must set everything up itself; the fresh-container
replay still verifies every command.

### What do I need installed?

Python 3.10+ and Docker, plus one Claude credential — either a local Claude Code
install (`--llm-backend claude-cli` with `CLAUDE_CODE_OAUTH_TOKEN`) or an
`ANTHROPIC_API_KEY`.

### Can I use my Claude subscription instead of an API key?

Yes, for self-hosted, single-operator runs against your own repos. Create a
token with `claude setup-token`, set `CLAUDE_CODE_OAUTH_TOKEN`, and use
`--llm-backend claude-cli`. Hosting readme2demo as a service for other people
requires `ANTHROPIC_API_KEY` instead.

### Is it safe to run on untrusted repositories?

The agent runs inside a container hardened with dropped capabilities,
no-new-privileges, non-root execution, and resource limits — that container is
the boundary. There is one known MVP tradeoff (the model key enters the
sandbox; use a dedicated low-limit key). See the [Security model](security.md).

### What does a run cost?

The planner, distiller, and tutorial passes plus the agent run consume model
tokens. Set a ceiling with `--budget-usd`; the run aborts if the agent exceeds
it, and `manifest.json` records the total cost of every run.

### Is it free and open source?

Yes. The CLI and verification pipeline are MIT licensed and will stay free and
open source.

### How do I resume a run that failed partway?

`readme2demo resume runs/<run-id> --from-stage <stage>`. Runs are crash-safe;
each stage's state is persisted in `manifest.json`.
