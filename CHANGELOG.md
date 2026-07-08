# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] — 2026-07-08

### Added
- `--json` flag on the `report` command — emit the run summary as machine-readable JSON.

## [0.3.0] — 2026-07-06

### Added
- `readme2demo --version` flag that prints the installed version and exits
  (#10, thanks @adjenk).

## [0.2.0] — 2026-07-05

### Added
- `-gr`/`--github-repo` flag for the repository URL, and the repo is now
  **optional**. `readme2demo run` accepts a repo, a `-s/--step-by-step` guide,
  or both — at least one is required.
- Guide-only runs: with a self-contained step-by-step guide and no repo, the
  pipeline skips cloning and the fresh-container replay still verifies every
  command (the grounding invariant is unchanged).
- Documentation site (mkdocs-material) with a GitHub Pages workflow and
  `docs/llms.txt`, plus `CITATION.cff`, `.github/dependabot.yml`, `CODEOWNERS`,
  and config templates (`readme2demo.toml.example`, `.env.example`).
- The verified self-run committed as `examples/readme2demo/` and featured as
  the README/website hero demo.

### Fixed
- Grounding no longer drops a guide step the agent proved in a sandbox-drifted
  form (`python3` vs `python`, an absolute executable path, or a trailing
  `--break-system-packages`) — those steps were silently missing from the video.
- The demo video now types the exact proven command (including any
  `export PATH=… &&` chain) instead of the guide's clean text, so on-camera
  steps don't fail with "command not found".
- The render seeds `/work` from the verified worktree when a guide has no clone
  step of its own, so a repo's own guide (e.g. one starting at
  `pip install -e .`) doesn't run in an empty directory.

### Changed
- CI runs a correctness-focused `ruff` lint gate and tests across Python
  3.10–3.13.
- One canonical one-line description across the repo, docs, `llms.txt`, and
  `CITATION.cff`.

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

[Unreleased]: https://github.com/alphacrack/readme2demo/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.4.0
[0.3.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.3.0
[0.2.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.2.0
[0.1.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.1.0
