# Configuration

Precedence is **CLI flags > `readme2demo.toml` > built-in defaults**.

Copy the tracked template and edit it:

```bash
cp readme2demo.toml.example readme2demo.toml
```

`readme2demo.toml` itself is gitignored so your local settings never get
committed; `readme2demo.toml.example` is the tracked reference.

```toml
# Agent engine
engine = "claude-code"        # or "openhands" (experimental)
model = "claude-sonnet-5"     # planner/distiller/tutorial passes
llm_backend = "auto"          # auto | api | claude-cli | gemini | openai
max_turns = 60
agent_timeout_s = 1500
budget_usd = 5.0

# Sandbox
base_image = "readme2demo/base:latest"
network = "bridge"
memory = "4g"
cpus = "2"
pids_limit = 512
allow_docker_socket = false  # SECURITY: mounts host Docker socket — trusted repos only

# Stages
dry_run = false
verify_timeout_s = 900
verify_retries = 1
distill_retries = 1
skip_video = false
formats = ["demo", "gif"]  # demo, gif, promo (brand_* keys are optional polish for promo)

# Layout
runs_dir = "runs"
# step_by_step = "path/to/guide.md"  # optional external guide (-s/--step-by-step)
# brand_logo = "assets/logo.png"     # optional: point at a real .png/.jpg — validated at load
brand_color = "#7C6BF2"
brand_font = "Arial"
```

That block is copy-paste runnable: CI loads it verbatim through `Config.load`
(`tests/test_config.py::TestDocumentedExample`), from an arbitrary working
directory, so it can never document a file this repo happens to own. Keys whose
value is a path the loader validates stay commented out for that reason —
`brand_logo` must already exist and be a `.png`/`.jpg`/`.jpeg` or `Config.load`
raises `brand_logo file not found` before a run starts. Uncomment it, pointing
at a real file, when you want branded cuts:

| Commented key | Uncomment as | Loader requires |
|-----|---------|---------|
| `brand_logo` | `brand_logo = "assets/logo.png"` | existing file, `.png`/`.jpg`/`.jpeg` |
| `step_by_step` | `step_by_step = "docs/guide.md"` | nothing at load; read at run time |

### All `Config` keys — defaults & purpose

| Key | Default | Purpose |
|-----|---------|---------|
| `engine` | `"claude-code"` | Agent engine (`claude-code` or `openhands` (experimental)) |
| `model` | `"claude-sonnet-5"` | Model for planner/distiller/tutorial LLM calls |
| `llm_backend` | `"auto"` | LLM backend (`auto`/`api`/`claude-cli`/`gemini`/`openai`) |
| `max_turns` | `60` | Max agent turns before timeout |
| `agent_timeout_s` | `1500` | Agent wall-clock timeout (s) |
| `budget_usd` | `5.0` | Abort if agent spend exceeds this (USD) |
| `base_image` | `"readme2demo/base:latest"` | Sandbox base image (VHS + toolchains) |
| `network` | `"bridge"` | Docker network for sandbox containers |
| `memory` | `"4g"` | Container memory limit |
| `cpus` | `"2"` | Container CPU limit |
| `pids_limit` | `512` | Container PID limit |
| `allow_docker_socket` | `false` | Mount host Docker socket (pierces isolation — trusted repos only) |
| `dry_run` | `false` | Stop after ingest/planning (feasibility only) |
| `verify_timeout_s` | `900` | Verify stage timeout (s) |
| `verify_retries` | `1` | Plain-script retries before distiller feedback |
| `distill_retries` | `1` | Distiller feedback loops on verify failure |
| `skip_video` | `false` | Skip video render (tutorial + script only) |
| `formats` | `["demo","gif"]` | Extra output formats (`demo`/`gif`/`promo`) |
| `step_by_step` | `null` | Path to external step-by-step guide |
| `runs_dir` | `"runs"` | Directory for run outputs |
| `brand_logo` | `null` | Raster logo (`.png`/`.jpg`/`.jpeg`) for promo title/end cards |
| `brand_color` | `"#7C6BF2"` | Hex accent (`#RRGGBB`) for promo drawtext |
| `brand_font` | `null` | Font name for promo drawtext (resolved in container) |
| `vhs_image` | `null` | Deprecated since v0.6.1 — accepted and ignored; use `base_image` |

## Authentication

Set credentials in a `.env` file (gitignored) or your shell. Copy
`.env.example` to get started. You need one of these:

- **Claude subscription (no API key)** — a local Claude Code install. The
  planner/distiller/tutorial passes run via `--llm-backend claude-cli`
  (`claude -p`), and the in-sandbox agent authenticates with
  `CLAUDE_CODE_OAUTH_TOKEN` (create one with `claude setup-token`). Supported
  for self-hosted, single-operator runs against your own repos. This is the
  default when no provider flag is given.
- **`ANTHROPIC_API_KEY`** — metered API billing; best for scale and
  concurrency, and required if you host readme2demo as a service for others.
  Add `--anthropic [model]` to run the sandboxed agent on the OpenHands
  engine with a Claude model instead of claude-code.
- **`OPENAI_API_KEY`** — run the whole session on OpenAI with
  `--openai [model]`: the OpenHands engine drives the sandboxed agent and the
  planner/distiller/tutorial passes use OpenAI. No model name is built in —
  name it per run (`--openai gpt-5.1`) or export `OPENAI_MODEL`. Install the
  extra: `pip install 'readme2demo[openai]'`.
- **`GEMINI_API_KEY`** — run the whole session on Google Gemini with
  `--gemini [model]`, same shape as OpenAI. No model name is built in — name
  it per run (`--gemini gemini-3.5-flash`) or export `GEMINI_MODEL`. Install
  the extra: `pip install 'readme2demo[gemini]'`.

The provider presets are mutually exclusive, and the `--openai` / `--gemini` /
`--anthropic` runs need the OpenHands sandbox image built once:
`docker build -t readme2demo/openhands:latest images/openhands/`.

Optional: `LLM_API_KEY` + `LLM_MODEL` (litellm-style) for the experimental
`--engine openhands` backend with any other provider — the presets above fill
them automatically.
