# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] — 2026-08-07

**Multi-format outputs: the promo cut.** `--formats demo,gif,promo` now
produces `promo.mp4` — a short marketing cut assembled *from* the run's own
`demo.mp4`, so the promo can only ever show footage a fresh container really
produced. The grounding invariant reaches the new artifact intact: every
`demo_segment` must trace to a step that is both published in the final
`step_by_step.md` and grounded in `command_log.json`, at least one segment of
real footage is mandatory, and any command a card puts on screen must be one
the run can back.

Extra formats are deliberately **not** stages. `produce()` dispatches them
after render, gated on `manifest.verified` exactly as render is, and every
builder call is wrapped with the four protected artifacts (`demo.mp4`,
`step_by_step.md`, `commands.sh`, `demo.tape`) fingerprinted around it. A
builder that raises, is unimplemented, or writes into the grounding path is
recorded in `manifest.formats` as `skipped: <reason>` and never fails the run.
No format is load-bearing.

The podcast, `--narrate` and `--social-cut` tracks remain unbuilt and move to
a later milestone; `--formats` accepts them today only as a validated surface.

### Added
- `--formats` output registry: a comma-separated CLI surface validated against
  `formats.py`, defaulting to today's exact output set (#212, thanks
  @MohammedAnasNathani).
- Grounded `promo_script.json` scene plan — one LLM pass behind a pure code
  validator that rejects any scene citing an unverified step (#169).
- Pure-ffmpeg promo compositor: title card, speed-ramped crossfade concat of
  real `demo.mp4` segments, end card, brand kit, 15/30/60s duration presets
  (#170). `assert_only_demo_footage` audits the FINISHED ffmpeg argv, so a cut
  that reached for foreign footage is refused structurally, not by intent.
- `produce()` dispatch for post-render formats, best-effort by contract, with
  per-format outcomes in `manifest.formats` and LLM spend in
  `manifest.format_costs` — including on the failure path (#230, #258).
- Per-stage durations in `readme2demo report` (#204, thanks @kushin25).
- Pure `placeholder_credential()` / `is_placeholder_credential()` helpers
  (#240, thanks @Nitjsefnie).

### Fixed
- **The promo budget is measured in played seconds, not nominal ones.** Demo
  segments are sped up to fit; comparing the raw sum of scene durations
  rejected honest, footage-rich plans that render exactly on target — a plan
  selecting 60s of verified footage for a 30s cut renders at 28.8s and was
  refused, burning both LLM calls (#278).
- **verify asserts the command the sandbox proved, not the planner's guess.**
  `success_criteria.command` is written at ingest, before anything runs. When
  pip installed a console script into `~/.local/bin` — not on `PATH` — the
  agent adapted and the assertion did not, so a run whose criterion passed
  twice was reported UNVERIFIED (#279). The substitution is a one-way partial
  order: only a literal the log proves exited 0, and only one at least as
  *specific* as the plan's own command, since unlike grounding this string is
  executed.
- **ffmpeg drawtext escape depth is per-character.** `,;[]` need one
  backslash, `:` two, `'` three, `\` four — and the failure modes differ:
  too few on `:` makes ffmpeg reject the whole option, too few on `'` renders
  the card blank, too few on `\` silently drops it. `%` was never an escaping
  problem but template expansion, now disabled with `expansion=none`, which
  also stops an LLM-authored `%{...}` from being evaluated into a published
  video (#281).
- OpenHands' native `finish` action is read as a marker source, so an agent
  that reports success through the action rather than a shell is no longer
  recorded as failed after the agent time is already spent (#259).
- GitHub deep links (`/tree/...`) are stripped before `git clone` (#215,
  thanks @MohammedAnasNathani).
- Single-line fenced JSON responses parse correctly (#118, thanks
  @pollychen-lab).
- Missing type hints on plan params (#48, thanks @ulises-jeremias).

### Changed
- CI caches pip and cancels superseded runs (#102, thanks @ulises-jeremias).
- `actions/setup-python` 6 → 7 (#228).
- Stale test counts and missing CHANGELOG footer links corrected (#43, thanks
  @ulises-jeremias).

## [0.7.5] — 2026-07-31

Fifteen commits of fixes and groundwork. The user-facing half is a batch of
"the tool now tells you the truth" fixes; the rest is unwired plumbing the
v0.8 multi-format work builds on.

### Added
- `-o` as a short alias for `--output-dir` (#132, thanks @ulises-jeremias).
- `step_timestamps.json`: per-step `[start, end]` offsets on the tape clock,
  written beside `tape_coverage.json` — YouTube chapter markers today, the
  promo cut's segment source next (#231).
- Brand-kit configuration (`brand_logo`, `brand_color`, `brand_font`) plus
  pure ffmpeg drawtext helpers (#232).
- A declarative `TTS_PROVIDERS` table mirroring `llm.PROVIDERS` — resolution
  and preflight only, no audio generation (#213).
- `classify_url`: a pure classifier telling a git repo URL from a hosted docs
  page, unwired pending docs-site ingestion (#207).
- `docs/dependencies.md` — the pinning policy, including why the sandbox
  image's Claude Code pin is coupled to the stream-json parser (#219).

### Fixed
- `--dry-run` no longer demands the agent credential, the sandbox image, or
  the Docker CLI — none of which it uses. The one command meant to be a cheap
  feasibility check before committing a key was gated behind that key
  (#221, closes #220).
- `verify_retries` is honored as a retry count: `verify_retries=3` now means
  four attempts, not two. Any value ≥ 1 previously collapsed to two
  (#229, closes #45, thanks @Nitjsefnie).
- Python inline regex flags are fully translated for `grep -E`: `(?is)` and
  friends no longer reach the shell, and `(?x)` fails loudly at distill
  instead of silently at verify (#214, closes #108).
- Six CLI/orchestrator/llm errors now say what to do next, not just what
  failed (#203, closes #198, thanks @MohammedAnasNathani).
- Sandbox containers carry a `readme2demo=1` label, so containers leaked by a
  hard kill can be reaped by label (#239, closes #238, thanks @foma-agent).
- Image alt text is descriptive in the README, the examples gallery, and
  generated tutorials (#216, closes #197).

### Changed
- DSL escaping helpers extracted to `escaping.py` — a zero-behavior move that
  establishes the review pattern for splitting `distill.py` (#210, closes #195).
- `SECURITY.md` restructured into a claims-vs-roadmap table with file
  pointers, so no control is claimed that code doesn't enforce
  (#205, closes #168 and #65, thanks @kevinnft).
- `docs/usage.md` documents publishing `badge.json` for shields.io (#208).

## [0.7.4] — 2026-07-24

### Fixed
- Verify no longer fails when the success command is a non-idempotent
  scaffolder (`X init` / `create-next-app` / `terraform init`): the command
  ran as both the last setup step and the assertion, so the assertion's
  re-run failed on the state setup created. It now runs exactly once, in the
  assertion, against a clean state — grounding unchanged (#224, closes #222).

### Changed
- `CITATION.cff` credits the current 15 contributors instead of a placeholder
  author (#226, closes #225).

## [0.7.3] — 2026-07-23

### Fixed
- A stage that pays for LLM calls and then fails is now billed for that
  spend: `stage_fail` accumulates `cost_usd` like `stage_complete`, and
  `DistillError` carries the distiller's grounding-retry cost across the
  raise. Previously a failed distill — the one run you most want a spend
  figure for — reported $0.00 (#209, closes #103).

### Changed
- Docs: `howto.jsonld` added to the README's generated-outputs list and
  `llm_backend` to its config example (#119, thanks @BenjaminAyivoh1);
  the architecture diagram, module map, and the debug-run skill now show
  the real verify → tutorial → render stage order (#211, thanks
  @professor314).

## [0.7.2] — 2026-07-21

### Fixed
- `extract_json` no longer truncates JSON whose string values contain `}` —
  the brace counter is replaced with `raw_decode`, so plan commands like
  `awk '{...}'` parse correctly (#146, closes #49, thanks @Sanjays2402).
- Unknown TOML configuration keys fail fast with a concise CLI error
  instead of being silently ignored; the retired `vhs_image` key is
  accepted as a deprecated, warned, ignored compatibility field (#174,
  closes #84, thanks @cen-zp).
- SEO descriptions collapse embedded whitespace, keeping generated YAML
  front matter single-line (#125, closes #91, thanks @pollychen-lab).
- The README contributors section pointed at a fork — badge, graph link,
  and avatar mosaic now show this repository's contributors, on GitHub
  and PyPI (#180, closes #179, thanks @BenjaminAyivoh1).

### Changed
- CI: bump `actions/upload-artifact` to 7 in release.yml, aligning with
  the pin the composite action already ships (#177).

## [0.7.1] — 2026-07-19

readme2demo is on PyPI: `pip install readme2demo`.

### Added
- The release workflow publishes to PyPI on every tag via Trusted
  Publishing (OIDC) — no tokens or stored secrets anywhere (#176,
  closes #175).

### Fixed
- README renders correctly on the PyPI project page: all 14 relative
  links/images converted to absolute URLs, and the dead
  `IMPLEMENTATION_PLAN.md` link (file removed in v0.6.0) repointed to
  `architecture/README.md` (#176).

## [0.7.0] — 2026-07-19

The GitHub Action release: readme2demo can now sit in CI and turn "the
README quietly broke" into a red X. Runnable walkthrough of everything
below: docs/whats-new-0.7.0.md.

### Added
- **GitHub Action** (#157): a repo-root composite action — install, build the
  sandbox image, run the pipeline, fail the check when the fresh-container
  replay doesn't pass. Artifacts and a job summary included; url mode until
  local-path ingestion (#74) unlocks PR-head verification. Self-test via
  workflow_dispatch.
- `report` exit codes signal the verdict (#158): 0 verified, 1 completed but
  unverified, 2 stage failed — CI can gate on the exit code alone.
- `report --markdown` (#159): a GitHub-flavored job summary (verified badge
  line, stage table with per-stage cost, artifact list) for
  $GITHUB_STEP_SUMMARY.
- Every run mints `badge.json` (#156): a shields.io endpoint file, written
  before the tutorial LLM pass so nothing can suppress an unverified run's
  red badge. Hosting tracked in #63.

### Fixed
- The suite is portable on Windows: the POSIX executable-bit test skips on
  NTFS (assertions untouched on POSIX) and artifact reads pin UTF-8 instead
  of trusting the locale (#154, thanks @innovationty).
- A docker-socket test reached the real `docker run` probe and stalled the
  suite 60s per run whenever Docker Desktop was half-up; the probe is now
  monkeypatched and the suite stays under a second (#155).

## [0.6.4] — 2026-07-18

Internal quality release: no user-facing behavior changes.

### Added
- Trust-boundary test suite (#151): 70 tests covering the modules that are
  the security/verification story. Every sandbox hardening flag now has a
  named test — the "never weaken hardening flags" rule is executable — and
  the clean-room replay is pinned as the only source of "verified"
  (fresh sandbox per attempt, no agent state, marker + exit code both
  required, no marker leakage across attempts).
- CI: coverage ratchet at 80% (measured floor) on the 3.12 leg and a mypy
  type-check job gating at zero errors (#152). The handful of src touches
  required are annotation-only or provably behavior-identical; grounding
  paths audited.

## [0.6.3] — 2026-07-17

### Fixed
- `readme2demo resume` rejects a nonexistent or non-directory run dir at
  argument parsing with a clear message instead of failing mid-stage with a
  raw traceback (#133, thanks @ulises-jeremias).
- `--config` pointing at a missing file is now a hard error instead of being
  silently ignored and falling back to defaults — both the `run` and
  `resume` paths are covered (#127, thanks @pollychen-lab).
- The bug-report issue template lists the real `--llm-backend` values
  (`auto`, `api`, `claude-cli`, `gemini`, `openai`) and engine names
  (`claude-code`, `openhands`), so reports arrive with usable repro fields
  (#131, thanks @ulises-jeremias).

### Changed
- Docstring accuracy: `collect_docs` documents the guide-first ordering it
  actually implements (#130), the VHS GIF-preview docstring no longer
  renders a literal `{GIF_PREVIEW_SECONDS}` (#128) (both thanks
  @ulises-jeremias), and the agent prompt-builder docstring documents the
  `{guide_note}` placeholder (#126, thanks @veronica-foltz).
- The agent stderr container path is a module-level constant shared by both
  derivation sites instead of being built twice inline (#129, thanks
  @ulises-jeremias).

## [0.6.2] — 2026-07-16

### Fixed
- scp-style repository URLs (`git@github.com:owner/repo.git`) normalize
  correctly in SEO titles instead of leaking the `git@host:` prefix
  (#121, thanks @hkJerryLeung).
- `step_by_step.md` now carries a distinct SEO title from `tutorial.md`, so
  the two published pages no longer compete for the same search snippet
  (#134, thanks @cnYui).
- Installer-URL phase tagging is word-anchored: URLs merely containing the
  substring "install" (`installer.tar.gz`, `reinstall-notes.txt`) no longer
  classify as setup steps (#122, thanks @hkJerryLeung).

### Removed
- Dead `Sandbox.commit()` method — zero call sites, no behavior change
  (#82, thanks @professor314).

### Changed
- Docs: the documented pipeline stage order matches `manifest.STAGES`
  (verify → tutorial → render) across README, CONTRIBUTING, ROADMAP, and the
  bug-report template (#120, thanks @cnYui); README gained an auto-updating
  contributors section (#136).
- CI: bump `actions/setup-python` to 6 (#34) and `actions/download-artifact`
  to 8 (#35) — the v4-upload/v8-download artifact pairing in release.yml is
  cross-compatible (verified against the v8 compatibility matrix before
  merge).

## [0.6.1] — 2026-07-14

Recorded retroactively: the `v0.6.1` tag was cut at commit `5ca0eda` without
a version bump or changelog entry, so its GitHub Release shipped placeholder
notes and the built wheel self-reports 0.6.0. Contents:

### Fixed
- VHS render timeout errors now name the image that actually ran instead of
  the never-used stock VHS image, and the dead `vhs_image` config field is
  gone — a stale `vhs_image` key in an existing `readme2demo.toml` is
  silently tolerated (#81, thanks @sapunyangkut).

### Changed
- CI: bump `actions/configure-pages` to 6 (#33).

## [0.6.0] — 2026-07-12

### Added
- `--dry-run` flag on `run`: stop right after the ingest/planning stage with
  the feasibility verdict and blockers, before any agent time is spent; the
  remaining stages are marked skipped with reason "dry-run stop" (#29, #30,
  thanks @Garifallos). Promoted to a first-class `dry_run` config field, so
  it can also be set in `readme2demo.toml`.

### Fixed
- `report --json` crashed with an AttributeError on any run whose manifest
  had recorded stages (the stages dict was iterated as a list of objects),
  and reported `cost` as 0.0 and `commit` as null from nonexistent field
  names — a regression that slipped in alongside the dry-run CLI change
  (the old test only covered an empty manifest). Restored the real manifest
  fields (`total_cost_usd`, `commit_sha`) with a regression test that uses a
  populated manifest.

### Changed
- CI: bump `actions/checkout` 4 → 7 and `actions/deploy-pages` 4 → 5
  (Dependabot).
- Internal go-to-market notes (`launch/`, `RELEASE_PLAN.md`,
  `IMPLEMENTATION_PLAN.md`) removed from the tracked repo.

## [0.5.0] — 2026-07-10

### Added
- Provider presets `--gemini [model]`, `--openai [model]`, and `--anthropic
  [model]` on `run` and `resume`: each runs the whole session on that
  provider via its single API key (`GEMINI_API_KEY` / `OPENAI_API_KEY` /
  `ANTHROPIC_API_KEY`). A preset selects the OpenHands engine for the
  sandboxed agent, the matching LLM backend for the planner/distiller/tutorial
  passes (`gemini` via `google-genai`, a new `openai` backend via the `openai`
  SDK, `api` via the Anthropic SDK), and bridges the key into the engine's
  litellm-style `LLM_API_KEY`/`LLM_MODEL`. No Gemini or OpenAI model name is
  hardcoded (both providers retire model names): name it inline (`--gemini
  gemini-3.5-flash`, `--openai gpt-5.1`), via `--model`, or via the
  `GEMINI_MODEL` / `OPENAI_MODEL` env var — a bare flag with none of those is
  a loud preflight error; `--anthropic` falls back to the config model.
  Install the SDK extras: `pip install 'readme2demo[gemini]'` /
  `'readme2demo[openai]'`. Presets are mutually exclusive, and conflicting
  flags (`--engine claude-code`, a mismatched `--llm-backend`, two differing
  model spellings) are rejected. The default without any flag is unchanged:
  the claude-code engine on your Claude subscription.
- `images/openhands/Dockerfile`: sandbox image with the pinned OpenHands
  0.48.0 runtime — the newest release with wheels for both amd64 and arm64
  (self-contained Python 3.13 via uv, tmux for the local runtime). It
  is the default image for `--engine openhands` and the provider presets when
  no `base_image` is set, and preflight probes the image so a missing runtime
  is a fast, actionable error instead of a mid-run exit 127 with no
  transcript.
- Preflight now fails fast — before a run directory is created — when an
  explicitly chosen LLM backend is missing its API key (`api` without
  `ANTHROPIC_API_KEY`, `openai` without `OPENAI_API_KEY`), when its optional
  SDK is absent, broken (the real ImportError is quoted), or too old
  (`llm.check_sdk`), when no model name is resolvable for a provider backend
  (`llm.check_model` — a bare `--llm-backend gemini` with no model named
  anywhere), or when the configured backend name is unknown.

### Fixed
- OpenHands runs no longer scan the echoed task prompt for outcome markers
  (run glow-20260710-182508): OpenHands records the prompt as a
  `source:"user"` message in the trajectory, and its marker documentation
  (`BLOCKED: <reason>`, `ADJUSTED_SUCCESS: <new command>`, the literal
  R2D_SUCCESS example) was parsed as real markers — a genuinely successful
  run was reported blocked and the plan's success command was overwritten
  with the `<new command>` placeholder. User-sourced messages are now
  skipped, and the shared marker scanners additionally reject captures that
  are un-filled `<...>` template placeholders (protects claude-code when a
  model restates its instructions verbatim).
- A missing optional LLM SDK (`openai` / `google-genai`) is now a preflight
  error (`llm.check_sdk`), caught before a run directory is created — a
  `--openai` run without the package previously passed preflight and burned
  its ingest stage on an ImportError (run glow-20260710-162012).
- Dynamic text printed through Rich (stage errors, run summaries, adjusted
  commands, corrected patterns, guide steps) is now markup-escaped. The
  install hint `pip install 'readme2demo[openai]'` previously rendered as the
  useless `pip install 'readme2demo'` because Rich parsed `[openai]` as a
  markup tag; shell snippets like `[ -f x ]` and regexes like `[0-9]+` were
  equally at risk of being silently swallowed.
- OpenHands engine actually runs inside the sandbox, verified end-to-end up
  to the LLM call: `RUNTIME=local` (no nested Docker runtime),
  `WORKSPACE_BASE=/work` (else the agent runs in an empty temp dir), the
  image's `openhands-python` wrapper (a PATH prepend is erased by `bash -lc`
  sourcing /etc/profile, and a symlink from outside a venv skips pyvenv.cfg),
  `SKIP_DEPENDENCY_CHECK=1` plus a `poetry run` shim (LocalRuntime assumes
  OpenHands' dev-repo layout), missing undeclared deps baked into the image
  (`deprecated`, `memory-profiler`, `jupyter-kernel-gateway`), a browser
  fail-fast patch (browser startup could block the action server past the
  client's 120s connect window), `USER="$(id -un)"` for the agent process
  (docker never sets USER; unset, OpenHands runs bash as the nonexistent
  `openhands` user via `su` — impossible under cap-drop ALL), and
  stdout/stderr captured to `agent.stderr` so failures surface in the error
  message instead of "(empty)". The agent's full log is now always copied to
  `runs/<id>/agent.stderr` before the sandbox is destroyed, and
  no-transcript errors point at it.

## [0.4.1] — 2026-07-09

### Changed
- CI: bump `actions/setup-python` 5 → 6 and `actions/upload-pages-artifact`
  3 → 5 (Dependabot). Workflow-only changes; the published package is
  unchanged from 0.4.0.

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

[Unreleased]: https://github.com/alphacrack/readme2demo/compare/v0.7.5...HEAD
[0.8.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.8.0
[0.7.5]: https://github.com/alphacrack/readme2demo/releases/tag/v0.7.5
[0.7.4]: https://github.com/alphacrack/readme2demo/releases/tag/v0.7.4
[0.7.3]: https://github.com/alphacrack/readme2demo/releases/tag/v0.7.3
[0.7.2]: https://github.com/alphacrack/readme2demo/releases/tag/v0.7.2
[0.7.1]: https://github.com/alphacrack/readme2demo/releases/tag/v0.7.1
[0.7.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.7.0
[0.6.4]: https://github.com/alphacrack/readme2demo/releases/tag/v0.6.4
[0.6.3]: https://github.com/alphacrack/readme2demo/releases/tag/v0.6.3
[0.6.2]: https://github.com/alphacrack/readme2demo/releases/tag/v0.6.2
[0.6.1]: https://github.com/alphacrack/readme2demo/releases/tag/v0.6.1
[0.6.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.6.0
[0.5.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.5.0
[0.4.1]: https://github.com/alphacrack/readme2demo/releases/tag/v0.4.1
[0.4.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.4.0
[0.3.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.3.0
[0.2.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.2.0
[0.1.0]: https://github.com/alphacrack/readme2demo/releases/tag/v0.1.0
