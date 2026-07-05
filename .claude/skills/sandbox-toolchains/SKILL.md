---
name: sandbox-toolchains
description: How readme2demo handles TARGET-repo languages and toolchains (Go, Node, Python, Rust, Terraform, etc.) inside the sandbox — version currency, install patterns, and the agent rules that keep runs honest. Use when a run fails on a toolchain install/version, or when extending language support in the base image or agent prompt.
---

# Target-repo toolchains in the sandbox

readme2demo runs OTHER projects' quickstarts. The base image ships common
toolchains; the agent installs anything else. Two recurring hazards, both with
agent-prompt rules AND code defenses.

## Version currency (failure class 2)

The agent's training data is STALE — languages/tools newer than it remembers
almost certainly exist. `prompts/agent.md` rule 4: never declare a version
"unreleased"; CHECK the official source
(`curl -sL 'https://go.dev/VERSION?m=text'`, `pyenv install --list`,
`npm view <pkg> version`) then download it. Keep the base image's Go current
(built from the official tarball) so the common case needs no download.

## Install patterns the agent uses (and that must survive verify)

- Python externally-managed sandbox → `pip install --break-system-packages`
  (and `--no-input`). Editable installs (`pip install -e .`) are cwd-sensitive
  — see below.
- Go: `go build` with a current toolchain; GOTOOLCHAIN pitfalls handled by
  shipping a new-enough Go.
- Binaries fetched by curl+unzip go to `~/.local/bin` or `~/bin` on PATH.

## cwd drift (failure class 6)

`cd` persists across script lines. The classic bug: `cd /tmp` to unpack a
binary, then `pip install -e .` editable-installs /tmp instead of the repo.
`prompts/distill.md` rule 5b requires a `cd` on/before any relative-path line;
`verify.cwd_hints` runs a static cwd simulation of a failing script and injects
precise hints into the distiller retry. The repo is always at `/work`.

## Findings tools (failure class 4)

Linters, drift detectors, scanners (tfdrift, etc.) exit NONZERO when they find
what the demo exists to show. The assertion block uses `set +e` +
pattern-as-criterion; `normalize.mark_findings_success` reclassifies such
entries as successful so they count for grounding, the tape, and the payoff
step. When adding language support, remember success ≠ exit 0 for this class.

## Extending support

Add a toolchain to `images/base/Dockerfile` only if it's common across target
repos (keep the image lean; the agent can install niche tools). Per-ecosystem
slim images are a known future optimization, not the MVP.
