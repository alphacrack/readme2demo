---
name: git-github-operator
description: Git and GitHub operations for maintaining the readme2demo repo — branching, conventional commits, PRs, releases, and the never-commit rules. Use when committing, opening a PR, tagging a release, or doing any git/gh operation on this repo.
---

# Git & GitHub operator for readme2demo

Remote: `https://github.com/alphacrack/readme2demo` (origin). Default branch:
`main`. In non-interactive sessions you can stage/commit locally but the human
runs `git push` (auth lives on their machine).

## Never commit

- `runs/` — every pipeline run (transcripts, videos, cloned repos, and
  potentially the target repo's content). Gitignored; keep it that way.
  Publishable examples are HAND-COPIED into `examples/` after review.
- `.env`, `readme2demo.toml`, any credential. Before every commit:
  `git diff --cached | grep -iE 'sk-ant-[a-z0-9]{8}'` must be empty, and
  `git status --short | grep runs/` must be empty.

## Commit style

Conventional-ish, imperative subject, then a body that lists WHAT changed and
WHY — this repo's history is a design log (read `git log` to see the bar).
When a commit fixes a failure class, name the run that found it and note the
regression test. One concern per commit where practical; batch tightly-coupled
changes (code + test + CLAUDE.md entry) together.

## Pre-push gate

Run the `/ship-check` command (or the ship-check flow): tests green, compile
clean, grounding-auditor on any src change touching the LLM→artifact path,
hygiene greps. If `images/base/Dockerfile` changed, the commit body must tell
users to rebuild (`docker build --no-cache -t readme2demo/base:latest images/base/`).

## Branches & PRs

- Feature work on a branch; PR into main. PR description states the failure
  class or feature, the systemic fix (code first), and the regression test.
- CI (`.github/workflows/ci.yml`) runs the suite on 3.10 and 3.12 — a red CI
  blocks merge. Keep the suite docker/network/API-free so CI stays fast.
- `good first issue` / `help wanted` labels are seeded for contributors (see
  launch/LAUNCH_CHECKLIST.md).

## Releases

- Tag `vX.Y.Z` with honest notes (state the MVP tradeoffs — Docker required,
  README treated as untrusted code, API key enters sandbox until the egress
  proxy lands). The pinned Claude Code version in the base image is coupled to
  the stream-json parser — call out a bump in the release notes.

## Repairing a user's run without git

Run dirs are disposable and gitignored. To salvage a failed run, patch its
`commands.sh`/`plan.json` or regenerate its guide+tape with the pure functions
(see the debug-run skill) and give the `resume --from-stage` command — no
commit involved.
