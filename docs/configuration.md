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
llm_backend = "auto"        # auto | api | claude-cli
max_turns = 60
budget_usd = 5.0
base_image = "readme2demo/base:latest"
skip_video = false
```

## Authentication

Set credentials in a `.env` file (gitignored) or your shell. Copy
`.env.example` to get started. You need one of the Claude options:

- **Claude subscription (no API key)** — a local Claude Code install. The
  planner/distiller/tutorial passes run via `--llm-backend claude-cli`
  (`claude -p`), and the in-sandbox agent authenticates with
  `CLAUDE_CODE_OAUTH_TOKEN` (create one with `claude setup-token`). Supported
  for self-hosted, single-operator runs against your own repos.
- **`ANTHROPIC_API_KEY`** — metered API billing; best for scale and
  concurrency, and required if you host readme2demo as a service for others.

Optional: `LLM_API_KEY` + `LLM_MODEL` for the experimental
`--engine openhands` backend.
