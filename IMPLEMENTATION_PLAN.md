# README2Demo — Implementation Plan

**One-liner:** Point it at a repo → an AI agent actually runs the README inside a hardened Docker sandbox → you get a *verified* tutorial, a minimal `commands.sh`, and a clean VHS-rendered demo video.

**Stack decisions (locked):** Python CLI · pluggable agent engine — Claude Code headless (`claude -p`) as default, OpenHands as opt-in when an LLM API key is provided · hardened Docker · VHS for rendering.

**Core value:** not "AI writes a tutorial" — it's "AI *ran* the tutorial and it worked, twice." Verification is the moat.

---

## Architecture overview

```
readme2demo <repo-url>
        │
        ▼
┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ M1 Ingest    │──▶│ M2 Sandbox   │──▶│ M3 Agent Run │──▶│ M4 Transcript │
│ clone, parse │   │ hardened     │   │ claude -p    │   │ NDJSON →      │
│ README, plan │   │ container    │   │ inside ctr   │   │ CommandLog    │
└─────────────┘   └──────────────┘   └──────────────┘   └──────┬───────┘
                                                                │
        ┌───────────────────────────────────────────────────────┘
        ▼
┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ M5 Distill   │──▶│ M6 Verify    │──▶│ M7 Render    │──▶│ M8 Tutorial   │
│ commands.sh  │   │ replay in    │   │ VHS tape →   │   │ tutorial.md + │
│ + demo.tape  │   │ FRESH ctr    │   │ mp4/gif      │   │ troubleshoot  │
└─────────────┘   └──────────────┘   └──────────────┘   └──────────────┘

M9 Orchestrator/CLI wraps all stages; every stage reads/writes a shared run directory.
```

### Key design decisions (and why)

1. **No terminal recorder during the agent run.** `claude -p --output-format stream-json --verbose` emits every tool call — each Bash command, its output, and exit status — as structured NDJSON. That's strictly better data than scraping a pty. asciinema/pty capture is dropped from the MVP entirely.
2. **The agent runs *inside* the container.** The container is the permission boundary, which makes `--dangerously-skip-permissions` acceptable. A malicious README can never touch the host. (Anthropic's own devcontainer pattern.)
3. **Record last, on the verified path.** VHS types and executes commands for real, so the demo video is a genuine run — but of the distilled minimal path, not the agent's exploration. No cleanup editing needed.
4. **Verification replay is a hard gate.** `commands.sh` must exit 0 in a *fresh* container before anything is published. If replay fails, the run fails (or loops back to the agent with the failure).
5. **Every stage is resumable.** A `manifest.json` state machine in the run directory means you can re-run just the render or just the distill without paying for another agent run.

### Run directory contract

```
runs/<run-id>/
  manifest.json        # state machine: stage statuses, timings, cost, model, image digest
  plan.json            # M1 output: chosen quickstart, prereqs, success criteria
  transcript.ndjson    # M3 raw stream-json from claude -p
  command_log.json     # M4 normalized: [{cmd, cwd, exit_code, output, duration, phase}]
  commands.sh          # M5 minimal reproducible script
  demo.tape            # M5 VHS tape
  verify.log           # M6 replay output
  demo.mp4 / demo.gif  # M7
  tutorial.md          # M8
  troubleshooting.md   # M8
```

---

## M1 — Ingest & Planner

**Job:** turn a repo URL into a structured `plan.json` before any agent spins up.

- `git clone --depth 1` into the run dir (host side; read-only mount into container later). Validate URL against `https://github.com/...` / `https://gitlab.com/...` patterns; reject local paths in MVP.
- Collect candidate docs: `README*`, `docs/**/*.md` (capped, e.g. 50KB total), `examples/` listing, top-level file inventory (detect `package.json`, `pyproject.toml`, `go.mod`, `Dockerfile`, `Makefile`).
- **Planner LLM pass** (single API call, no agent): given README + inventory, emit `plan.json`:
  ```json
  {
    "project_type": "python-cli",
    "quickstart": "pip install then run examples/hello.py",
    "prereqs": ["python>=3.10"],
    "steps_expected": ["clone", "install", "run example"],
    "success_criteria": "examples/hello.py prints greeting and exits 0",
    "blockers": [],          // e.g. "requires OPENAI_API_KEY", "needs GPU"
    "feasible": true
  }
  ```
- **Feasibility gate:** if the quickstart needs credentials, paid services, GPUs, or a GUI, bail *now* with a clear report instead of burning an agent run. This is the cheapest place to fail.
- Explicit, machine-checkable `success_criteria` is critical — it becomes the agent's stop condition (M3) and the verifier's assertion (M6).

**Interfaces:** `ingest(repo_url) -> RunDir with plan.json`
**Out of scope for MVP:** docs-site URLs (crawling), monorepos with multiple quickstarts (pick first / flag).

---

## M2 — Sandbox

**Job:** produce a hardened, disposable container the agent lives in.

- **One "kitchen sink" base image** for MVP: Ubuntu 24.04 + git, curl, build-essential, Python 3.12 + venv, Node 22 + npm, Go — plus Claude Code CLI preinstalled. The agent can `apt-get`/`pip`/`npm install` anything else itself. Per-ecosystem images are a v2 optimization. Pin the image by digest and record it in `manifest.json` for reproducibility.
- Non-root user (`demo`), repo mounted read-only at `/repo`, agent works in a copy at `/work`.
- **Hardening flags:**
  ```
  --cap-drop ALL --security-opt no-new-privileges
  --memory 4g --cpus 2 --pids-limit 512
  --read-only is NOT used (agent needs to install), but /work is the only writable host-visible volume
  --network bridge (MVP)  →  egress proxy allowlist (Milestone 4)
  ```
- Hard wall-clock kill: `docker run` wrapped with a timeout (default 25 min), plus disk quota on the volume.
- **API key handling — the honest MVP tradeoff:** the key must reach the Claude Code process inside the container, so a malicious README could in principle exfiltrate it. MVP mitigations: use a dedicated low-limit key, pass via env at exec-time (never baked into image or written to disk), monitor spend. Real fix (Milestone 4): host-side HTTPS proxy that injects the key, so it never enters the sandbox, and doubles as the egress allowlist (`api.anthropic.com`, `github.com`, `pypi.org`, `registry.npmjs.org`, `deb.ubuntu.com`, …).
- Python side: use `docker` SDK (docker-py) for lifecycle; one class `Sandbox` with `create() / exec(cmd) / copy_out(path) / destroy()`. **Always destroy** — containers are cattle; fresh container per stage that needs one (agent run gets one, verify gets a new one, render gets a new one).

---

## M3 — Agent Runner

**Job:** run an AI agent inside the sandbox until the quickstart works, streaming a structured transcript out.

### Engine abstraction (pluggable backends)

```python
class AgentEngine(Protocol):
    name: str
    def build_command(self, prompt: str, limits: Limits) -> list[str]   # exec'd inside container
    def parse_transcript(self, raw_path: Path) -> CommandLog            # engine-specific → normalized
    def required_env(self) -> list[str]                                 # e.g. ["ANTHROPIC_API_KEY"]
```

- **`claude-code` (default):** Claude Code headless as below. Used when `ANTHROPIC_API_KEY` is set.
- **`openhands` (optional):** selected via `--engine openhands` or config, only if `LLM_API_KEY` (+ `LLM_MODEL`, litellm-style, so it can point at OpenAI/Anthropic/local) is provided. Runs OpenHands headless mode (`python -m openhands.core.main -t "<task>"`) inside the sandbox; its event-stream/trajectory JSON is parsed by an `OpenHandsAdapter` into the same `CommandLog` schema. OpenHands ships its own runtime-sandbox logic — we bypass it and treat it like any process in *our* container (one sandbox model, not two).
- The engine choice affects **only** M3 invocation + the M4 adapter. Everything downstream (distill, verify, render, tutorial) consumes `command_log.json` and never knows which agent ran. Distiller/planner/tutorial LLM calls stay on the Anthropic API regardless of engine in MVP (config can route them later).
- Engine selection logic: explicit flag > config > auto-detect from available env vars; error clearly if the chosen engine's key is missing.
- MVP builds `claude-code` fully; `openhands` ships as the adapter interface + stub in Milestone 0–2, real implementation in Milestone 3.

### Claude Code engine (default)

- Invocation (inside container):
  ```bash
  claude -p "$(cat /task/prompt.md)" \
    --output-format stream-json --verbose \
    --dangerously-skip-permissions \
    --max-turns 60 \
    > /work/.r2d/transcript.ndjson
  ```
  Host tails the file (or reads the docker attach stream) for live progress display.
- **Prompt template** (rendered from `plan.json`):
  - Goal: make the quickstart in plan.json succeed; success = `success_criteria`.
  - Rules: work only in `/work`; never ask for credentials — if truly required, print `BLOCKED: <reason>` and stop; prefer the README's own commands, deviate only to fix prereqs/versions; when a deviation is needed, state `FIX: <what> BECAUSE: <why>` before running it (these markers become troubleshooting.md gold); finish by printing `SUCCESS_MARKER` + re-running the final demo command cleanly.
  - Do **not** ask the agent to write the tutorial or tape — that's M5/M8's job with better information. Single responsibility keeps the agent run short and cheap.
- **Budget guards:** `--max-turns`, wall-clock timeout, and cost cap read from the final `result` event (`total_cost_usd`); abort and mark run `over_budget` if exceeded.
- **Outcome classification:** `success` (marker printed + criteria met), `blocked` (credentials/GPU), `failed` (max turns / timeout). All three produce useful artifacts — a `blocked` report is still a publishable "what this repo actually needs" doc.
- One retry policy: on `failed`, optionally resume the session (`--resume`) once with the failure context. Off by default in MVP.

---

## M4 — Transcript Normalizer

**Job:** pure-Python parse of `transcript.ndjson` → `command_log.json`. No LLM, fully deterministic, unit-testable.

- Per-engine adapters behind one interface: `ClaudeCodeAdapter` (NDJSON stream-json) and `OpenHandsAdapter` (event-stream/trajectory JSON). Both emit the identical `command_log.json` schema below.
- Claude Code path: walk NDJSON events; extract each `tool_use` (name=Bash) paired with its `tool_result`: command string, output (truncate to ~4KB, keep head+tail), exit status, timestamp, duration. OpenHands path: walk `CmdRunAction`/`CmdOutputObservation` event pairs for the same fields.
- Also extract: file edits (Write/Edit tool calls — these matter when the agent had to patch something), assistant text containing `FIX:`/`BLOCKED:` markers, and the final `result` event (cost, turns, duration).
- **Phase tagging (heuristic, not LLM):** classify each command as
  `explore` (ls, cat, head, grep, find — drop from demo), `setup` (apt, pip, npm, git clone, venv), `demo` (the actual run), `fix` (commands following a nonzero exit). The distiller gets these as hints, not truth.
- Schema is *the* internal contract — M5, M6, M8 all consume `command_log.json`. Version it (`"schema": 1`). If Claude Code's stream format shifts (it has before), only this module changes.

---

## M5 — Distiller

**Job:** LLM pass that turns the messy successful run into the minimal clean path.

- Input: `command_log.json` + `plan.json` + README. Output (single structured response): `commands.sh`, `demo.tape`, `tutorial_outline.json` (step → command → expected-output snippets → explanation notes).
- **Grounding rule (enforced in code, not by prompt):** every command in `commands.sh` must fuzzy-match a command that *actually succeeded* in the log (normalize whitespace, allow env-var renames). Reject/regenerate on violation. This is what makes the output trustworthy rather than plausible.
- `commands.sh` requirements: `#!/usr/bin/env bash`, `set -euxo pipefail`, non-interactive flags everywhere (`-y`, `--yes`, `DEBIAN_FRONTEND=noninteractive`), ends by asserting the success criteria (e.g. grep expected output).
- `demo.tape` generation is templated in Python, not free-formed by the LLM: the LLM picks *which* commands and *what* pacing/comments; Python emits the tape from a template:
  ```tape
  Output demo.mp4
  Output demo.gif
  Set FontSize 18
  Set Width 1200
  Set Height 700
  Set TypingSpeed 50ms
  Type "pip install -r requirements.txt"  Enter
  Wait+Screen /Successfully installed/    # wait-for-pattern, not Sleep guessing
  ...
  ```
  Long/boring sections (big installs) wrapped in `Hide`/`Show` or pre-baked into the image layer for the render container (see M7). Use `Wait` on output patterns instead of `Sleep` wherever possible — renders become robust to timing variance.

---

## M6 — Verifier (the moat)

**Job:** prove `commands.sh` works from zero, in a container the agent never touched.

- Fresh sandbox from the same base image digest → copy in `commands.sh` only (not the agent's `/work`!) → `bash commands.sh` → require exit 0 + success-criteria assertion.
- Capture full replay output to `verify.log`; diff key expected-output snippets for tutorial accuracy (M8 quotes *replay* output, not agent-run output).
- On failure: one automatic loop — feed `verify.log` back to the distiller ("command X failed in clean env, agent run had it working; likely missing step") and retry once. Second failure → run marked `unverified`, artifacts still produced but clearly labeled. Never silently publish unverified.
- Flakiness realism: network hiccups happen. Retry the *script* once on failure before invoking the distiller loop. Record both attempts.

---

## M7 — Renderer

**Job:** VHS renders `demo.tape` into `demo.mp4` + `demo.gif`.

- Run VHS in a container (official `ghcr.io/charmbracelet/vhs` image, or bake vhs+ttyd+ffmpeg into our base). The tape executes for real, so the render container needs the same starting state as the verify container.
- **Speed trick:** after M6 passes, `docker commit` the verified container *at the post-setup checkpoint* (optional split: `commands.sh` = `setup.sh` + `demo.sh`; commit after setup, tape only types the demo part). Result: videos show the interesting 30 seconds, not a 6-minute pip install. MVP can skip the split (render everything, Hide the installs); the split is Milestone 3 polish.
- Validate outputs: file exists, nonzero size, duration sane (< 5 min), first/last frame not blank (ffprobe checks).
- GIF for README embeds (`Output demo.gif`), MP4 for docs sites. Both from one tape — VHS supports multiple Outputs natively.

---

## M8 — Tutorial Generator

**Job:** final LLM pass producing human-facing docs from verified data only.

- Inputs: `tutorial_outline.json`, `verify.log` (canonical expected outputs), `FIX:`/`BLOCKED:` markers from M4, repo metadata.
- `tutorial.md`: title, prereqs (from plan.json), per-step: command block + what it does + expected output block (quoted from verify.log, truncated), embedded `demo.gif`, footer: "Verified on <date> · <base-image> · commit <sha>" — the verification badge is the product's signature.
- `troubleshooting.md`: generated from every error the *agent* hit and fixed during M3 (error text → cause → fix). This is content no doc writer produces because they never hit the errors. If the agent hit none, say so ("README worked as written").
- Same grounding rule as M5: every command block must match `commands.sh` exactly; enforced in code.

---

## M9 — CLI & Orchestrator

**Job:** tie it together; own state, resumability, config, and UX.

- **CLI (typer):**
  ```
  readme2demo run <repo-url> [--engine claude-code|openhands] [--output-dir] [--model] [--timeout] [--budget-usd] [--skip-video]
  readme2demo resume <run-id> [--from-stage distill|verify|render|tutorial]
  readme2demo report <run-id>        # print manifest summary + cost
  ```
- Orchestrator = explicit state machine over stages `ingest → sandbox → agent → normalize → distill → verify → render → tutorial`; each stage: idempotent, reads/writes run dir, updates `manifest.json` (status, started/finished, cost, error). Crash-safe resume comes free.
- Config precedence: CLI flags > `readme2demo.toml` > defaults. Key settings: model, base image ref, max turns, budget, egress mode.
- Structured logging (`rich` for human console + JSONL log file per run). Cost aggregation across all LLM calls (planner, agent, distiller, tutorial) into one number in `manifest.json`.
- **Project layout:**
  ```
  readme2demo/
    pyproject.toml
    src/readme2demo/
      cli.py  orchestrator.py  manifest.py
      ingest.py  sandbox.py  normalize.py
      engines/  (base.py, claude_code.py, openhands.py)
      distill.py  verify.py  render.py  tutorial.py
      prompts/  (planner.md, agent.md, distill.md, tutorial.md)
      templates/ (demo.tape.j2, tutorial.md.j2)
    images/base/Dockerfile
    tests/  (unit: normalize+distill grounding; integration: 3 golden repos)
  ```

---

## Milestones

| # | Deliverable | Scope | Proof |
|---|-------------|-------|-------|
| **0. Walking skeleton** (~week 1) | `run` produces tutorial.md, no video | M1 (minimal planner) + M2 (plain-ish Docker) + M3 + M4 + M8-lite (tutorial straight from command_log) | Works end-to-end on one friendly repo (e.g. a simple Python CLI) |
| **1. Verified pipeline** | Distill + replay gate | M5 + M6, grounding rules enforced, `unverified` labeling | 3 golden repos pass verify; a deliberately broken README fails loudly |
| **2. Video** | VHS render | M7, tape templating, Wait-on-pattern, gif+mp4 | Clean demo video for the golden repos |
| **3. Robustness & polish** | Setup/demo split + docker commit checkpoint, retry loops, `resume`, cost caps, blocked-repo reports, **OpenHands engine implementation** | Run against 10 diverse repos (CLI tools, MCP servers, SDKs); ≥6 fully verified, 0 silent failures; one golden repo passes with `--engine openhands` |
| **4. Security hardening** | Egress proxy with key injection (key never enters sandbox), domain allowlist, disk quotas | Red-team README (exfil attempt, fork bomb, crypto miner) contained |

## Top risks

1. **API-key exfiltration from sandbox** (until Milestone 4 proxy) → dedicated low-limit key, spend alerts. Documented, not hidden.
2. **Repos that can't work headlessly** (creds, GPUs, GUIs, long-running servers) → M1 feasibility gate + `BLOCKED` protocol; a clear "here's what you need" report is still a good output. Servers: v2 feature — background the process, curl it, that's the demo.
3. **stream-json format drift** across Claude Code versions → pin CLI version in base image; all parsing isolated in M4.
4. **VHS timing flakiness** → `Wait` on output patterns over `Sleep`; post-setup checkpoint keeps tapes short.
5. **Distiller hallucinating commands** → code-enforced grounding against the success log (the single most important correctness rule in the system).
6. **Cost per run** → planner gate kills infeasible runs for pennies; budget cap aborts runaways; typical happy-path run should be a few dollars of agent time, measured from day one via `total_cost_usd`.
