# Usage

## The `run` command

```bash
readme2demo run https://github.com/example/tool
readme2demo run -gr https://github.com/example/tool             # same, via the flag
readme2demo run -s my_guide.md                                  # guide-only: no repo
readme2demo run -gr https://github.com/example/tool -s my_guide.md   # both
readme2demo run https://github.com/example/tool --skip-video --budget-usd 3
```

The repository is **optional**. Provide it positionally or with
`-gr/--github-repo`, provide a step-by-step guide with `-s/--step-by-step`, or
provide both. At least one is required.

- **Repo only** — the agent reads the README (and any `step_by_step.md` the repo
  ships) and runs it.
- **Guide only** — no repository is cloned. Your guide must be self-contained
  (install a published package, or clone whatever it needs as an explicit
  step). The fresh-container replay still verifies every command.
- **Both** — the guide is treated as authoritative; the planner and agent
  follow it, and the repo provides the code. Both are taken into account.

## Common flags

| Flag | Purpose |
|---|---|
| `-gr`, `--github-repo` | Repo URL (optional; same as the positional argument). |
| `-s`, `--step-by-step` | Your own guide; becomes the authoritative source and the video's script. |
| `--llm-backend` | `auto` \| `api` \| `claude-cli` \| `gemini` \| `openai` for the planner/distiller/tutorial passes. |
| `--engine` | `claude-code` (default) or `openhands` (experimental). |
| `--openai [model]` | Run the whole session on OpenAI via `OPENAI_API_KEY` (OpenHands agent + OpenAI passes). Bare flag reads `--model` / `OPENAI_MODEL`. |
| `--gemini [model]` | Run the whole session on Google Gemini via `GEMINI_API_KEY` (OpenHands agent + Gemini passes). Bare flag reads `--model` / `GEMINI_MODEL`. |
| `--anthropic [model]` | Run the sandboxed agent on OpenHands with a Claude model via `ANTHROPIC_API_KEY`. |
| `--budget-usd` | Abort the run if the agent's cost exceeds this. |
| `--dry-run` | Stop after ingest/planning with the feasibility verdict and blockers — no agent time spent. |
| `--skip-video` / `--with-video` | Skip or force VHS rendering. |
| `--allow-docker-socket` | Mount the host Docker socket into the sandbox. Security tradeoff — trusted repos only. |

## Resuming and inspecting runs

Runs are crash-safe and resumable. Every stage records its transition in
`manifest.json`, so you can pick up exactly where a run stopped:

```bash
readme2demo resume runs/tool-20260702-... --from-stage render
readme2demo report runs/tool-20260702-...
```

`report` exits with a code that signals the run's state, so CI can gate on it
directly instead of parsing output:

| Exit code | Meaning |
|---|---|
| `0` | Verified — the fresh-container replay passed. |
| `1` | Completed but **UNVERIFIED** — no stage failed, but the replay did not pass. |
| `2` | A stage failed. |

```bash
readme2demo report runs/tool-20260702-... --json && echo "verified" || echo "gate failed"
```

The human and `--markdown` reports show the final attempt's duration for each
stage. `--json` includes the same value as numeric `duration_seconds` per
stage; it is `null` when either timestamp is unavailable or invalid.

## Outputs

Artifacts land in `runs/<run-id>/`: `tutorial.md`, `step_by_step.md`,
`troubleshooting.md`, `commands.sh`, `demo.tape`, `demo.mp4`, `demo.gif`,
`howto.jsonld`, and `manifest.json`.
