# Contributing to readme2demo

Thanks for considering it. This project has one non-negotiable design rule,
and understanding it will make every review go smoothly:

> **The LLM never publishes anything a fresh container didn't independently
> execute.** Grounding is enforced in code, not prompts. If your change lets
> an unverified command reach `tutorial.md`, `step_by_step.md`,
> `commands.sh`, or the demo tape, it will be rejected regardless of how
> clever it is.

## Getting set up

```bash
git clone https://github.com/alphacrack/readme2demo && cd readme2demo
pip install -e ".[dev]"
python -m pytest tests/ -q        # fast: no docker, network, or API keys needed
docker build -t readme2demo/base:latest images/base/   # needed for real runs
```

A real end-to-end run needs Docker plus either `ANTHROPIC_API_KEY` or a local
Claude Code install (`--llm-backend claude-cli` + `CLAUDE_CODE_OAUTH_TOKEN`).

## Layout

Pipeline stages live in `src/readme2demo/`, one module per stage:
`ingest → agent → normalize → distill → verify → render → tutorial`,
orchestrated by `orchestrator.py` over a crash-safe manifest. Agent backends
are plugins under `engines/`. Prompts are markdown files under `prompts/`;
tape/tutorial layouts are jinja templates under `templates/`.

## Rules of the road

- **Tests are cheap here — write them.** The suite runs in under a second
  with no external dependencies; every bug fixed so far carries a regression
  test named after the run that found it. Match that.
- **Engine transcript parsing must stay pure and deterministic** (no LLM
  calls in `parse_transcript`/`normalize`) so it's testable against fixtures.
- **Never weaken the sandbox flags** in `sandbox.py` without discussion —
  READMEs are untrusted code, that's a security boundary.
- **Prompt changes need evidence.** If you edit `prompts/*.md`, include a
  before/after transcript (or run report) from a real repo in the PR.
- Keep PRs to one concern. Type hints and docstrings on public functions.

## Good first issues

Look for the `good first issue` label. Bigger swings (`help wanted`):
egress proxy with key injection, OpenHands engine hardening, GitHub Action
wrapper, docs-site URL ingestion.

## Reporting a broken run

Open an issue with the repo URL, your `manifest.json`, and the tail of
`verify.log` or `transcript.ndjson`. The manifest deliberately records
everything needed to reproduce a run — cost included.
