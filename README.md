# readme2demo — verified tutorials & demo videos from your README

[![tests](https://github.com/alphacrack/readme2demo/actions/workflows/ci.yml/badge.svg)](https://github.com/alphacrack/readme2demo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/alphacrack/readme2demo/blob/main/LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://github.com/alphacrack/readme2demo/blob/main/pyproject.toml)

<!-- HERO:START — scripts/self-demo.sh rewrites this block with a fresh self-run.
     Run it locally (needs Docker + VHS + a model credential):
       ./scripts/self-demo.sh
     It runs readme2demo on this very repo, following docs/step-by-step.md,
     and drops the verified demo.gif in right here. -->
[![readme2demo running on its own repo — verified demo](https://raw.githubusercontent.com/alphacrack/readme2demo/main/docs/demo.gif)](https://github.com/alphacrack/readme2demo/tree/main/examples/readme2demo)

<sub>▶ readme2demo generating its own tutorial: an AI agent runs this repo's README in a sandbox, a fresh container replays every step, then the demo is rendered. Full self-run output in <a href="examples/readme2demo/">examples/readme2demo</a> · run against another project in <a href="examples/toolhive/">examples/toolhive</a>.</sub>
<!-- HERO:END -->

**AI-verified tutorial and demo video generator.** Point it at a repo. An AI agent reads the README and actually runs it inside a hardened Docker sandbox. Only after a clean-room replay passes does it render a demo video (VHS) and publish the tutorial, step-by-step guide, and troubleshooting doc.

The value is not "AI writes a tutorial" — it's that the tutorial **ran, twice**, before you saw it.

**See it in action:** browse [verified example runs](https://github.com/alphacrack/readme2demo/tree/main/examples) — real tutorials, step-by-step guides, and demo videos, each independently replayed in a clean container before publishing.

## How it works

```
repo URL → ingest/plan → agent run (in Docker) → normalize transcript
        → distill minimal path → VERIFY replay in fresh container
        → generate tutorial.md + troubleshooting.md → render VHS video
```

See [architecture/README.md](https://github.com/alphacrack/readme2demo/blob/main/architecture/README.md) for the full architecture.

## Requirements

- Python ≥ 3.10, Docker
- Auth, one of:
  - **Your Claude subscription (no API key):** a local Claude Code install. The planner/distiller/tutorial passes run on your subscription via `--llm-backend claude-cli` (`claude -p`), and the in-sandbox agent authenticates with `CLAUDE_CODE_OAUTH_TOKEN` (create one: `claude setup-token`). Fully supported for **self-hosted, single-operator** runs against your own repos — Pro/Max plans include a monthly Agent SDK credit that covers `claude -p`.
  - `ANTHROPIC_API_KEY` — metered API billing; best for scale and concurrency, and **required if you host readme2demo as a service for others** (per Anthropic's terms, subscription auth may not power a multi-tenant product — see [ROADMAP.md](https://github.com/alphacrack/readme2demo/blob/main/ROADMAP.md)). Add `--anthropic [model]` to run the sandboxed agent on the OpenHands engine with a Claude model instead of claude-code.
  - **Google Gemini (`--gemini [model]`):** a single `GEMINI_API_KEY` runs the whole session off Claude — the planner/distiller/tutorial passes use Gemini and the sandboxed agent runs on the OpenHands engine (also on Gemini). No model name is built in (Google retires old ones with a hard 404): name it per run (`--gemini gemini-3.5-flash`) or export `GEMINI_MODEL` once. Install the extra: `pip install 'readme2demo[gemini]'`.
  - **OpenAI (`--openai [model]`):** same shape as Gemini — a single `OPENAI_API_KEY` powers the passes and the OpenHands agent, no model name is built in (`--openai gpt-5.1` or export `OPENAI_MODEL`). Install the extra: `pip install 'readme2demo[openai]'`.
- Optional: `LLM_API_KEY` + `LLM_MODEL` for `--engine openhands` (experimental) with any other litellm provider — the presets above fill them automatically

```bash
# run on your Claude subscription (no API key) — supported for self-hosted runs
claude setup-token        # interactive: approve in browser, then COPY the
                          # sk-ant-oat01-... token it prints (do NOT use $(...))
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
readme2demo run <repo-url> --llm-backend claude-cli

# run on metered API billing (scale, concurrency, or hosting for others)
export ANTHROPIC_API_KEY=sk-ant-...
readme2demo run <repo-url>              # --llm-backend auto picks api

# run the whole session on Google Gemini (OpenHands agent + Gemini passes)
pip install 'readme2demo[gemini]'
docker build -t readme2demo/openhands:latest images/openhands   # one-time: OpenHands sandbox image
export GEMINI_API_KEY=...
readme2demo run <repo-url> --gemini gemini-3.5-flash   # model named per run
export GEMINI_MODEL=gemini-3.5-flash                   # ...or set once, then:
readme2demo run <repo-url> --gemini                    # bare flag reads GEMINI_MODEL

# run the whole session on OpenAI (OpenHands agent + OpenAI passes)
pip install 'readme2demo[openai]'
export OPENAI_API_KEY=sk-...
readme2demo run <repo-url> --openai gpt-5.1            # or export OPENAI_MODEL once

# run the OpenHands agent with a Claude model on API billing
export ANTHROPIC_API_KEY=sk-ant-...
readme2demo run <repo-url> --anthropic                 # uses the config model by default
```

## Install

```bash
pip install -e ".[dev]"
docker build -t readme2demo/base:latest images/base/
docker build -t readme2demo/openhands:latest images/openhands/   # only for --engine openhands / --gemini / --openai / --anthropic
```

## Usage

```bash
readme2demo run https://github.com/example/tool
readme2demo run -gr https://github.com/example/tool             # same, via the flag
readme2demo run -s my_guide.md                                  # guide-only: no repo, your guide is self-contained
readme2demo run -gr https://github.com/example/tool -s my_guide.md   # both: your guide drives everything
readme2demo run https://github.com/example/tool --gemini gemini-3.5-flash  # run on Google Gemini (needs GEMINI_API_KEY; uses the OpenHands agent; bare --gemini reads GEMINI_MODEL)
readme2demo run https://github.com/example/tool --openai gpt-5.1           # run on OpenAI (needs OPENAI_API_KEY; uses the OpenHands agent; bare --openai reads OPENAI_MODEL)
readme2demo run https://github.com/example/tool --anthropic                # OpenHands agent with a Claude model on ANTHROPIC_API_KEY
readme2demo run https://github.com/example/tool --allow-docker-socket  # for tools that manage containers (SECURITY TRADEOFF: pierces sandbox isolation — trusted repos only)
readme2demo run https://github.com/example/tool --skip-video --budget-usd 3
readme2demo resume runs/tool-20260702-... --from-stage render
readme2demo report runs/tool-20260702-...
```

The repo is **optional**: pass it positionally or with `-gr/--github-repo`, supply a guide with `-s/--step-by-step`, or both. At least one is required. With a guide alone, no repo is cloned — the guide must be self-contained (install a published package, or clone what it needs as an explicit step); the fresh-container replay still verifies every command.

Outputs land in `runs/<run-id>/`: `tutorial.md`, `step_by_step.md`, `troubleshooting.md`, `commands.sh`, `demo.tape`, `demo.mp4`, `demo.gif`, plus `manifest.json` with stage statuses and total cost.

## GitHub Action — verify your README in CI

Get a red X when your README stops working. The repo-root composite action installs readme2demo from its own pinned checkout, builds the sandbox image, runs the full pipeline against your repo's URL, and **fails the check when the fresh-container replay does not pass**:

```yaml
name: readme-check
on:
  push:
    branches: [main]        # url mode tests the default branch HEAD — see the caveat below
    paths: ["README.md"]
  schedule:
    - cron: "0 6 * * 1"     # weekly: catch the world changing under an unchanged README

permissions:
  contents: read

jobs:
  verify-readme:
    runs-on: ubuntu-latest
    steps:
      - uses: alphacrack/readme2demo@main   # pin a tag or SHA once released
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          skip-video: "true"
```

> **⚠ URL mode only — this does NOT verify PR heads yet.** The action clones the **remote default branch HEAD** of `repo-url` (default: the repository running the workflow); ingestion accepts only https URLs, `--depth 1`, no ref pinning. On `pull_request` it would test the *base* branch's README — not the PR's — so don't wire it to PRs expecting a pre-merge verdict. Until [#74](https://github.com/alphacrack/readme2demo/issues/74) (local-path ingestion) lands, `on: push` to the default branch and a cron are the honest triggers; a `repo-path` input for real PR-head verification arrives with it.

**Cost:** every run spends real agent money on your `ANTHROPIC_API_KEY` — typically a few dollars, hard-capped by `budget-usd` (default `"5"`; the run aborts if exceeded). The `paths:` filter plus a cron keeps spend proportional to README churn, and `skip-video: "true"` cuts wall-clock time (the render costs no API money either way).

The check fails in two distinguishable ways, named in the step log: **README broken** (pipeline completed, clean-room replay failed — detected via `readme2demo report --json`, because `readme2demo run` deliberately exits 0 on a completed-but-unverified run) and **action infra broke** (nonzero pipeline exit: preflight, budget, Docker). Outputs: `verified` (`"true"`/`"false"`) and `run-dir`; `tutorial.md`, `step_by_step.md`, `verify.log` (and `demo.gif` when video is on) upload as the `readme2demo-run` artifact.

## step_by_step.md — the video's source

The demo video is always built **from** `step_by_step.md`: its steps are parsed, and every demo-safe, grounded command becomes a typed command in the video with the step title shown as an on-screen comment. Three ways it comes to exist, in priority order:

1. **You pass one**: `readme2demo run <url> -s my_guide.md` — injected into the clone as the authoritative guide; planner and agent follow it, video plays it. The `<url>` is optional here: `readme2demo run -s my_guide.md` runs guide-only against an empty sandbox.
2. **The repo ships one** (`step_by_step.md` / `step-by-step.md` at root or `docs/`, any case): same treatment, automatically.
3. **Neither exists**: the pipeline *generates* a detailed `step_by_step.md` — every command from the verified `commands.sh` as a numbered step with real captured outputs — then builds the video from it. Ready to contribute back to the repo.

Setup steps (clones, installs, builds) are documented in the guide but kept out of the video — it plays against the verified, already-built worktree, showing the payoff.

Every tutorial carries a verification badge: `✅ Verified on <date> · image <digest> · commit <sha>` — or a loud `⚠ UNVERIFIED` if the replay didn't pass. Unverified output is never silently published.

## Configuration

CLI flags > `readme2demo.toml` > defaults:

```toml
engine = "claude-code"      # or "openhands"
model = "claude-sonnet-5"   # planner/distiller/tutorial passes
max_turns = 60
budget_usd = 5.0
base_image = "readme2demo/base:latest"
skip_video = false
```

## Development

```bash
python -m pytest tests/ -q            # 175 unit tests, no docker/network needed
ruff check src/ tests/               # correctness lint (matches CI)
python -m pytest -m integration      # requires docker + API keys (none yet)
```

## Security model

READMEs are untrusted code. The agent runs *inside* a hardened container (cap-drop ALL, no-new-privileges, memory/cpu/pids limits, non-root) — that container is the permission boundary. Known MVP tradeoff: the API key enters the sandbox; use a dedicated low-limit key. A host-side key-injecting egress proxy is planned (Milestone 4).

Full threat model and private vulnerability reporting: [SECURITY.md](https://github.com/alphacrack/readme2demo/blob/main/SECURITY.md).

## Project & community

- [Examples](https://github.com/alphacrack/readme2demo/tree/main/examples) — verified output committed as proof
- [Roadmap](https://github.com/alphacrack/readme2demo/blob/main/ROADMAP.md) — where this is headed (including the exploratory hosted/SaaS direction)
- [Contributing](https://github.com/alphacrack/readme2demo/blob/main/CONTRIBUTING.md) — the one non-negotiable rule, and how to get set up
- [Security policy](https://github.com/alphacrack/readme2demo/blob/main/SECURITY.md) · [Code of Conduct](https://github.com/alphacrack/readme2demo/blob/main/CODE_OF_CONDUCT.md)
- [Architecture](https://github.com/alphacrack/readme2demo/blob/main/architecture/README.md) — stage boundaries and diagrams

MIT licensed. The CLI and verification pipeline are, and will stay, free and open source.

## Contributors

A huge thank you to everyone who has contributed to readme2demo!

[![Contributors](https://img.shields.io/github/contributors/Rahul-pamula/readme2demo?style=flat-square)](https://github.com/Rahul-pamula/readme2demo/graphs/contributors)

<a href="https://github.com/Rahul-pamula/readme2demo/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=Rahul-pamula/readme2demo" alt="Contributors" />
</a>

