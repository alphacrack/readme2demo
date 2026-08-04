# Configuration

Precedence is **CLI flags > `readme2demo.toml` > built-in defaults**.

Copy the tracked template and edit it:

```bash
cp readme2demo.toml.example readme2demo.toml
```

`readme2demo.toml` itself is gitignored so your local settings never get
committed; `readme2demo.toml.example` is the tracked reference.

```toml
engine = "claude-code"      # or "openhands" (experimental)
model = "claude-sonnet-5"   # planner / distiller / tutorial passes
llm_backend = "auto"        # auto | api | claude-cli | gemini | openai
max_turns = 60
budget_usd = 5.0
base_image = "readme2demo/base:latest"
formats = ["demo", "gif"]
skip_video = false
```

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
