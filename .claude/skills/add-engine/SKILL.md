---
name: add-engine
description: Add a new AI agent backend (engine) to readme2demo, or fix parsing for an existing one (claude-code stream-json drift, openhands trajectory changes). Use when adding support for a new coding agent or when transcripts stop parsing after an agent CLI upgrade.
---

# Adding or fixing an agent engine

Engines are the ONLY place that knows which AI agent ran. Everything
downstream consumes `command_log.json`.

## The contract (`engines/base.py`)

Implement `AgentEngine` and decorate with `@register`:

- `name`: CLI value for `--engine`.
- `required_env()` / `resolve_env()`: host env vars forwarded into the
  sandbox at exec time (never baked into images). Support alternative auth
  by overriding `resolve_env` (see ClaudeCodeEngine's key-OR-oauth-token,
  including the credential-format validation — copy it).
- `build_command(limits)`: one shell string run via `bash -lc` inside the
  container; reads the prompt from `PROMPT_CONTAINER_PATH`, writes the raw
  transcript to `TRANSCRIPT_CONTAINER_PATH`, stderr next to it.
- `parse_transcript(path) -> CommandLog`: PURE, deterministic, no LLM calls,
  tolerant of malformed lines and schema drift. Reuse `truncate_output`,
  `scan_markers`, `scan_adjusted` from `claude_code.py` so markers and
  truncation behave identically across engines.

## Parsing requirements

- Pair command events with their results; unpaired commands get
  `exit_code=None` (never fabricate 0).
- Extract: Bash-equivalent commands + outputs (head 3KB + tail 1KB
  truncation), file edits, FIX/BLOCKED/ADJUSTED_SUCCESS markers, outcome
  (blocked > success-marker > failed), cost/turns/duration when available
  (None when not — budget logic treats None as unknown, not zero).

## Checklist

1. `engines/<name>.py` with the above; module docstring stating the
   transcript format assumptions and its drift-tolerance strategy.
2. Fixture transcript in `tests/fixtures/` exercising: success + failure
   pairs, a marker, a malformed line, string-vs-list content shapes.
3. Tests: parse the fixture, verify counts/exit codes/markers/outcome;
   missing-env EngineError; build_command shape (uses the canonical
   container paths, not hardcoded strings).
4. If the format VERSION is coupled to a pinned CLI in
   `images/base/Dockerfile`, bump pin and parser together and say so in the
   commit message.
5. README: add the engine to the auth/requirements section.

## Known drift landmines

- claude-code stream-json: `tool_result.content` is a string OR a list of
  text blocks; the final `result` event can be absent when the agent is
  killed (marker alone then decides success).
- openhands trajectories: JSON array, `{"events": [...]}` wrapper, or JSONL
  — detect all three.
