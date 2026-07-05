# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `-gr`/`--github-repo` flag for the repository URL, and the repo is now
  **optional**. `readme2demo run` accepts a repo, a `-s/--step-by-step` guide,
  or both — at least one is required.
- Guide-only runs: with a self-contained step-by-step guide and no repo, the
  pipeline skips cloning and the fresh-container replay still verifies every
  command (the grounding invariant is unchanged).
- Correctness-focused `ruff` lint gate (`E9`, `F`) wired into CI.

### Changed
- CI now runs a lint job and tests across Python 3.10–3.13.

## [0.1.0] — 2026-07-05

Initial public release.

### Added
- End-to-end pipeline: ingest/plan → agent run in a hardened Docker sandbox →
  normalize → distill → **verify in a fresh container** → tutorial → VHS demo
  video. Nothing is published that a clean-room replay did not execute.
- Crash-safe, resumable runs via `manifest.json`
  (`readme2demo resume <run> [--from-stage <stage>]`).
- Pluggable agent engines (`claude-code` default, `openhands` experimental).
- Multiple LLM backends for the planner/distiller/tutorial passes
  (`auto` | `api` | `claude-cli`).
- Generated artifacts: `tutorial.md`, `step_by_step.md`, `troubleshooting.md`,
  `commands.sh`, `demo.tape`, `demo.mp4`, `demo.gif`, and `howto.jsonld`.
- Verified `examples/toolhive/` reference run committed as proof.

[Unreleased]: https://github.com/alphacrack/readme2demo/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.1.0
