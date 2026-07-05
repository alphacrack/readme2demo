# readme2demo — verified tutorials & demo videos from your README

**readme2demo** turns a repository's README into a tutorial, a step-by-step
guide, a troubleshooting doc, structured HowTo JSON-LD, and a demo video — but
only after an AI agent has actually *run* the README inside a hardened Docker
sandbox and an independent clean-room replay has passed.

The value is not "AI writes a tutorial." It is that the tutorial **ran, twice**,
in throwaway containers before you ever saw it. Nothing is published that a
fresh container did not independently execute.

[![readme2demo verified demo](https://raw.githubusercontent.com/alphacrack/readme2demo/main/examples/toolhive/demo.gif)](https://github.com/alphacrack/readme2demo/tree/main/examples/toolhive)

## Install

```bash
pip install -e ".[dev]"
docker build -t readme2demo/base:latest images/base/
```

## Quickstart

```bash
# On your Claude subscription (no API key) — self-hosted, single operator
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
readme2demo run https://github.com/example/tool --llm-backend claude-cli

# Or on metered API billing
export ANTHROPIC_API_KEY=sk-ant-...
readme2demo run https://github.com/example/tool
```

The repository is optional — you can run from a self-contained step-by-step
guide alone, or from both a repo and a guide. See [Usage](usage.md).

## Why it exists

AI coding assistants happily produce plausible-but-untested commands. That is
the exact failure mode readme2demo eliminates: a **grounding invariant**,
enforced in code rather than by prompt, guarantees every published command was
observed to succeed in a real sandbox and then reproduced from zero in a fresh
container. See [How it works](how-it-works.md).

## What you get

Every run writes a `runs/<run-id>/` directory containing `tutorial.md`,
`step_by_step.md`, `troubleshooting.md`, `commands.sh`, `demo.tape`,
`demo.mp4`, `demo.gif`, `howto.jsonld`, and a crash-safe `manifest.json` with
per-stage status and total cost. Runs are resumable.

## Project links

- Source & issues: [github.com/alphacrack/readme2demo](https://github.com/alphacrack/readme2demo)
- Verified example output: [examples/toolhive](https://github.com/alphacrack/readme2demo/tree/main/examples/toolhive)
- Roadmap, security policy, and contributing guide live in the repository.

MIT licensed. The CLI and verification pipeline are, and will stay, free and
open source.
