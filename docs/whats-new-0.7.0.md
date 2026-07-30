---
title: "What's new in readme2demo 0.7.0 — step by step"
description: "Walk every user-facing change from 0.6.0 to 0.7.0 with runnable commands: CI-grade exit codes, report --markdown, badge.json, stricter CLI validation, and the GitHub Action that fails your build when your README breaks."
source_repo: "https://github.com/alphacrack/readme2demo"
---

# What's new in 0.7.0 — every change, runnable

This guide walks the user-facing changes between v0.6.0 and v0.7.0. In this
repo's spirit, every command below was actually executed against the verified
example run that ships in `examples/readme2demo/` — you can replay each step
yourself with nothing but a Python install; no Docker daemon or API key needed.

The headline: **0.7.0 is the GitHub Action release.** readme2demo can now sit
in CI and turn "the README quietly broke" into a red X.

## Step 1 — Install from the checkout

readme2demo isn't on PyPI yet (#19) — install from the repo:

```bash
pip install -e ".[dev]"
readme2demo --version
```

## Step 2 — `report` exit codes now mean something (new in 0.7.0)

`report` used to always exit 0. Now the exit code is the verification verdict,
so CI can gate on it directly: **0** verified · **1** completed but unverified
· **2** a stage failed.

```bash
readme2demo report examples/readme2demo --json | head -8
echo "exit: $?"    # 0 — this run's fresh-container replay passed
```

Point it at an unverified run and the same command exits 1 — that single bit
is what lets a workflow fail the build.

## Step 3 — `report --markdown`: a job summary for humans (new in 0.7.0)

Pipe it to `$GITHUB_STEP_SUMMARY` in CI (the Action does this for you), or
just read it in a terminal:

```bash
readme2demo report examples/readme2demo --markdown
```

You get a verified badge line, a stage table with per-stage cost, and the
artifact list — rendered from `manifest.json` alone.

## Step 4 — every run now mints `badge.json` (new in 0.7.0)

A shields.io endpoint file appears in each run dir. Verified runs get
`"verified <date>"` in green; unverified runs get a loud red `"unverified"` —
written before the tutorial LLM pass, so nothing can suppress it.

```bash
cat examples/readme2demo/badge.json
```

Hosting it as a live badge endpoint is documented in [Verification badge](usage.md#verification-badge) (slice of #63).

## Step 5 — the GitHub Action (the 0.7.0 flagship)

Drop ~15 lines of YAML in a repo and its README gets verified in CI — the
Action installs readme2demo, builds the sandbox image, runs the pipeline, and
fails the check if the fresh-container replay doesn't pass. Artifacts and the
job summary come along for free.

```bash
cat .github/workflows/readme-check.yml
```

See the README's *GitHub Action* section for the full snippet and the honest
caveat: url mode verifies the default branch until local-path ingestion (#74)
lands.

## Step 6 — the CLI stopped being polite about your typos (0.6.3)

Two silent footguns became loud, immediate errors:

```bash
readme2demo resume /nonexistent/run     # clear error at argument parsing
readme2demo run <url> --config oops.toml  # missing config = hard error, not silent defaults
```

## Also since 0.6.0 (no commands to run — they just work now)

- VHS render timeout errors name the image that actually ran (0.6.1).
- scp-style URLs, SEO titles, and installer-URL phase tagging fixed by three
  first-time contributors (0.6.2).
- 150+ new tests: the sandbox hardening argv, the clean-room verify stage, and
  config precedence are all pinned; CI gained a coverage ratchet and a
  zero-error mypy gate (0.6.4); the suite stays under a second (0.7.0).

---

*Every command in this guide was executed against the committed example run
before publishing. That's the house rule: nothing ships that didn't run.*
