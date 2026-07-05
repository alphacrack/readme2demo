# Roadmap

Directional, not a promise. Priorities shift with real usage and contributor
interest — if something here matters to you, open an issue or a PR and it moves
up. The one thing that never changes is the invariant: **nothing gets published
that a fresh container didn't independently execute.**

## Now (0.x, the MVP that shipped)

- Core pipeline: `ingest → agent → normalize → distill → verify → render → tutorial`, crash-safe and resumable per stage.
- Claude Code as the default in-sandbox agent engine; OpenHands as an experimental opt-in.
- Hardened Docker sandbox as the security boundary (cap-drop, no-new-privileges, non-root, resource caps).
- Verified `tutorial.md`, `step_by_step.md`, `troubleshooting.md`, `commands.sh`, `howto.jsonld`, and a VHS `demo.mp4` / `demo.gif` — every run reproducible from its `manifest.json`.
- SEO/GEO-shaped output (front matter, provenance footers, schema.org HowTo).

## Next (hardening the core)

- **Egress proxy with key injection** — keep model credentials *out* of the sandbox entirely (closes the biggest documented MVP tradeoff; see [SECURITY.md](SECURITY.md)).
- **OpenHands engine hardening** — bring the experimental engine to parity and reliability with the default.
- **GitHub Action wrapper** — run readme2demo in CI and open a PR with the verified guide + demo when a README changes.
- **Docs-site / URL ingestion** — point it at a hosted docs page, not just a repo.
- Broader base-image toolchains so more ecosystems verify out of the box.

## Later (breadth)

- More agent engines behind the same `command_log.json` contract.
- A published examples gallery across ecosystems (Go, Python, Rust, Terraform, MCP servers, security scanners).
- Richer troubleshooting synthesis and coverage reporting.
- Multi-README / monorepo awareness.

## Hosted / SaaS (exploratory — gated on feedback)

If the open-source tool earns real traction, the natural next step is a **hosted
service** so people can get a verified tutorial and demo video without running
Docker locally:

- A "paste a repo URL, get verified docs + a demo video" web app.
- A GitHub App that re-verifies and refreshes docs automatically on every release.
- Managed run infrastructure (the sandbox + verification as a service), with an egress proxy so no customer keys ever touch the run.
- Team features: private repos, run history, badges you can embed.

**Auth model:** the self-hosted CLI can run on a Claude subscription (`claude -p`)
— Pro/Max plans include an Agent SDK credit that covers it. A *hosted, multi-tenant*
offering is different: per Anthropic's current terms, subscription / claude.ai-login
auth may not power a product served to other end users, so the service tier must run
on **metered API keys**. That's a clean split — subscription for individuals running
the tool themselves, API billing for the hosted service.

**Intended model: open-core.** The CLI and the verification pipeline in this
repo stay free and MIT-licensed — that's the part the community builds on and
trusts. Any commercial offering would be *hosting, scale, and convenience*
around that same open core, not a paywall bolted onto it. This section is
deliberately non-binding; it exists so the direction is transparent, and it
only happens if the community says the tool is worth it.

Have thoughts on the hosted direction? That's exactly the kind of feedback
[Discussions](https://github.com/alphacrack/readme2demo/discussions) is for.
