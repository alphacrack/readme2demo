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

## Outputs

Artifacts land in `runs/<run-id>/`: `tutorial.md`, `step_by_step.md`,
`troubleshooting.md`, `commands.sh`, `demo.tape`, `demo.mp4`, `demo.gif`,
`howto.jsonld`, `manifest.json`, and `badge.json`.

## Verification badge

Every completed run mints a shields.io-compatible `badge.json` in the run
directory (written at the start of the tutorial stage, so an unverified run
still gets a **loud red** badge rather than a missing file). From the GitHub
Action, the path is `${{ steps.<id>.outputs.run-dir }}/badge.json`.

### What the file looks like

Verified (green) — shape from `render_badge` on a verified manifest:

```json
{
  "schemaVersion": 1,
  "label": "readme2demo",
  "message": "verified 2026-07-05",
  "color": "green",
  "commit": "f677e21"
}
```

Unverified (red) — same shape when `manifest.verified` is false:

```json
{
  "schemaVersion": 1,
  "label": "readme2demo",
  "message": "unverified",
  "color": "red",
  "commit": "f677e21"
}
```

The extra `commit` key is human provenance; shields.io ignores unknown keys.

### Route A (recommended): orphan branch endpoint

Publish `badge.json` to a dedicated `readme2demo-badge` branch so the stable
URL is:

`https://raw.githubusercontent.com/OWNER/REPO/readme2demo-badge/badge.json`

Copy-paste workflow (consumer repo — not part of this project's own CI):

```yaml
name: README check + badge

on:
  push:
    branches: [main]  # default branch only — not pull_request

jobs:
  verify:
    runs-on: ubuntu-latest
    # Scoped write so the publish job can update the badge branch.
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4

      - name: Run readme2demo
        id: r2d
        # Use the published Action; wire secrets/API keys per its docs.
        uses: alphacrack/readme2demo@v0
        with:
          # repo / guide / engine inputs per action.yml
          repo: ${{ github.repository }}

      # MUST be always(): the Action exits 1 when verified != true
      # (action.yml gate). Without always(), a red/unverified run never
      # publishes — and a stale green badge is worse than a red one.
      - name: Publish badge.json
        if: always()
        env:
          RUN_DIR: ${{ steps.r2d.outputs.run-dir }}
        run: |
          set -euo pipefail
          # No run-dir / no badge.json → leave the previous badge untouched.
          # (Pipeline died before tutorial; "no badge" ≠ "red badge".)
          if [ -z "${RUN_DIR}" ] || [ ! -f "${RUN_DIR}/badge.json" ]; then
            echo "No badge.json to publish; keeping previous endpoint."
            exit 0
          fi
          git fetch origin readme2demo-badge:readme2demo-badge 2>/dev/null || true
          git checkout --orphan readme2demo-badge 2>/dev/null || git checkout readme2demo-badge
          git rm -rf . >/dev/null 2>&1 || true
          cp "${RUN_DIR}/badge.json" ./badge.json
          git add badge.json
          git -c user.name='github-actions[bot]' -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
            commit -m "chore: update readme2demo badge" || echo "No badge change"
          git push -f origin HEAD:readme2demo-badge
```

Embed in your README (replace `OWNER` / `REPO`):

```markdown
[![README verified](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/OWNER/REPO/readme2demo-badge/badge.json)](https://github.com/OWNER/REPO/actions/workflows/readme-check.yml)
```

### Route B: GitHub Pages — one deployment source only

You *can* host `badge.json` via Pages (`actions/upload-pages-artifact` +
`actions/deploy-pages`), following the pattern in this repo's
`.github/workflows/docs.yml`. **A repository has a single Pages deployment
source.** A second Pages-deploying workflow replaces the whole site — if you
already publish docs via Pages, fold `badge.json` into that existing build as
an extra file; do not deploy a separate Pages job just for the badge.

### Caching caveat

Both shields.io and `raw.githubusercontent.com` cache responses, so a fresh
run can take a while to show up in the rendered badge. Expect lag; do not
assume an instant update.

### Red badge vs no badge

| Situation | `badge.json` | What to publish |
|---|---|---|
| Completed, verified | green | Publish |
| Completed, unverified | red | Publish (`if: always()`) |
| Failed before tutorial | missing | **Do not** invent a file; leave previous badge |

For Action wiring, secrets, and cost notes, see the Action guide (tracked in
#149); this section only covers publishing the file the pipeline already
writes.
