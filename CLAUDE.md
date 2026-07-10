# CLAUDE.md — maintainer instructions for readme2demo

readme2demo points an AI agent at a repo, runs its README inside a hardened
Docker sandbox, replays the distilled result in a FRESH container, and only
then publishes: tutorial.md, step_by_step.md, troubleshooting.md,
howto.jsonld, and a VHS demo video executing every step on camera.

## The one non-negotiable invariant

**The LLM never publishes anything a fresh container didn't independently
execute.** Grounding is enforced in code, not prompts. Any change that lets
an unverified command reach `tutorial.md`, `step_by_step.md`, `commands.sh`,
or `demo.tape` is wrong regardless of how useful it seems. When you need an
LLM to behave, add BOTH a prompt rule AND a code-level check — prompts are
suggestions, parsers are law.

## Pipeline & module map

Stages (see `manifest.STAGES`, orchestrated by `orchestrator.py` over a
crash-safe `manifest.json`; every stage resumable via
`readme2demo resume <run> [--from-stage <s>]`):

| Stage | Module | LLM? | Key invariant |
|---|---|---|---|
| ingest | `ingest.py` | planner call | infeasibility gate fails for pennies before agent time |
| agent | `agent.py`, `engines/` | the agent | runs INSIDE the sandbox; guide mode = execute EVERY step |
| normalize | `normalize.py` | pure Python | deterministic; owns pattern reality-check, findings marking, cheat detection, coverage warnings |
| distill | `distill.py` | one call | grounding validator; step_by_step.md materialized here; commands.sh written |
| verify | `verify.py` | none | fresh container, zero agent state — the only source of "verified" |
| tutorial | `tutorial.py` | polish call | finalizes step_by_step.md with verified outputs; `enforce_commands` restores commands/outputs regardless of LLM output |
| render | `render.py` | none | tape derived FROM the FINAL step_by_step.md (video follows the guide); heredocs typed line-by-line; duration gate; image preflight |

**Stage order matters:** tutorial runs BEFORE render. step_by_step.md is finalized (verified outputs, payoff step) first; the demo tape is then built from that published guide (`distill.build_tape_from_step_by_step`) so the video provably follows step_by_step.md.

Prompts live in `src/readme2demo/prompts/*.md`; jinja templates in
`src/readme2demo/templates/`. Engines are plugins (`engines/base.py`):
`claude-code` (default), `openhands` (experimental) — both normalize to the
same `command_log.json`; downstream never knows which agent ran. Provider
presets (`--gemini` / `--openai` / `--anthropic`, table `llm.PROVIDERS`) pair
the OpenHands engine with the matching LLM backend off one API key; no flags
still means claude-code on the operator's Claude subscription.

## Dev loop

```bash
pip install -e ".[dev]"
python -m pytest tests/ -q          # <1s, no docker/network/API — run after EVERY change
docker build -t readme2demo/base:latest images/base/   # only for real runs
readme2demo run <url> --llm-backend claude-cli          # dev runs on subscription
```

Rules:
- Every bug fix carries a regression test named for the failure that found it
  (grep the test files for "Regression" to see the pattern).
- Engine `parse_transcript` and everything in `normalize.py` stays pure and
  deterministic — no LLM calls, testable against fixtures.
- Parsers are tolerant by design (stream-json and trajectory formats drift);
  isolate format assumptions in the engine adapters.
- Never weaken `sandbox.py` hardening flags without explicit discussion.
- Prompt changes (`prompts/*.md`) need evidence: a before/after run report.
- The pinned Claude Code version in `images/base/Dockerfile` is coupled to
  the stream-json parser (`engines/claude_code.py`) — bump them together.

## Known failure classes (hard-won — check here FIRST when a run fails)

Each was found on a real run; each has code defenses and a regression test.

1. **Infra-missing rabbit hole** — demo needs Docker/K8s/DB the sandbox lacks;
   agent must ADJUSTED_SUCCESS or BLOCKED, never fake sockets (agent.md rule 5)
   or patch source (rule 6; detected by `normalize.repo_files_edited`).
2. **Knowledge-cutoff denial** — agent claims a tool version "isn't released";
   agent.md rule 4 forces checking the official source. Keep base-image
   toolchains current.
3. **LLM-authored pattern wrong** — `\brun\b` vs "running";
   `normalize.validate_success_pattern` drops patterns that don't match
   captured output.
4. **Findings tools exit nonzero ON SUCCESS** (drift detectors, linters):
   assertion block uses `set +e` + pattern-as-criterion
   (`distill._render_commands_sh`); `normalize.mark_findings_success` makes
   such entries count for grounding/tape/outputs.
5. **Grounding false-negatives from syntax drift** — whitespace, `2>&1`,
   env prefixes, chain segments, pipe-capped variants (`cmd | head`),
   heredoc bodies (matched by PREFIX only — see `distill.heredoc_prefix`).
   When adding a new equivalence, add it to `normalize_cmd`/
   `_grounded_candidates` symmetrically and test both directions.
6. **cwd drift in distilled scripts** — `cd /tmp` then `pip install -e .`;
   distill.md rule 5b + `verify.cwd_hints` feeds precise analysis into the
   retry loop.
7. **Heredocs** — one multi-line command everywhere: guide parser, commands.sh
   parser, grounding (prefix-matched). ON the tape they type line-by-line
   (`TapeCommand.lines`) so file creation plays on camera — a shell reads a
   heredoc until its terminator.
8. **VHS fragility** — Wait+Screen matches only the visible buffer (never use);
   pacing = `Wait` (prompt) + Sleep; 24fps, mp4-only + ffmpeg gif preview
   (full-length GIFs once filled the Docker VM disk); duration gate in
   `render.validate_outputs` makes short/incomplete videos a hard error. The
   tape is built from the FINAL step_by_step.md in the render stage so the
   video follows the published guide step-for-step.
9. **Docker socket permissions** — `--allow-docker-socket` requires
   `--group-add $(socket gid)` (probed in `sandbox.docker_socket_gid`) or
   non-root gets EACCES and tools report "no container runtime".
10. **Pipe-masked failures** — `cmd | head` exits 0 even when cmd failed
    (agent.md rule 7); prefer bare runs when judging success.
11. **State machine subtleties** — `reset_from` clears `verified` only when
    verify itself is reset; run dirs must be absolute (docker -v).
12. **Credential hygiene** — `$(claude setup-token)` captures TUI garbage;
    credentials are format-validated at preflight and sanitized for host
    `claude -p` calls.
13. **Findings tool as a STEP under `set -e`** — the success command (a
    drift detector/scanner) is emitted both as a step and in the assertion;
    the bare step exits nonzero (found something) and aborts the script before
    the assertion. `distill._tolerate_findings_steps` appends `|| true` to
    steps the log marked `findings_success`. Related to class 4 (same tool,
    different location).
14. **Engine runtime missing from the sandbox image** — `--engine openhands`
    against the base image dies with a bare exit 127 and no transcript (no
    OpenHands, no `python` alias). OpenHands gets its own image
    (`images/openhands/`, pinned 0.x — bump with the parser like the Claude
    Code pin); engines carry `default_image` (auto-selected when base_image is
    unset) and `check_image` (preflight probe with build instructions). The
    engine command sets `RUNTIME=local` and captures output to `agent.stderr`.
15. **Optional SDK missing + Rich eats the fix** — a `--openai` run without
    the `openai` package passed preflight, burned ingest on an ImportError,
    AND the printed hint lost its `[openai]` extra to Rich markup parsing
    (`pip install 'readme2demo'` — subtly wrong advice). Defenses:
    `llm.check_sdk` (preflight gate, single message source shared with the
    call-time check; distinguishes absent vs broken vs too-old SDK),
    `llm.check_model` (bare `--llm-backend gemini/openai` with no model
    named anywhere fails preflight too), and `rich.markup.escape()` on ALL
    dynamic console text — errors, summaries, agent commands (`[ -f x ]`),
    regexes (`[0-9]+`). Any new `console.print` interpolating non-literal
    text must escape it; escape AFTER repr, never before.
16. **Prompt echo scanned as agent output** — OpenHands writes the TASK
    PROMPT into the trajectory as a `source:"user"` message action; scanning
    it harvested the prompt's own marker documentation (`BLOCKED: <reason>`,
    `ADJUSTED_SUCCESS: <new command>`, the literal R2D_SUCCESS example) as
    real markers — a run whose agent genuinely printed R2D_SUCCESS was
    reported blocked, and plan.json's success command was overwritten with
    `<new command>`. Defenses: the OpenHands parser skips user-sourced
    messages, and the shared scanners (`claude_code._is_placeholder`) reject
    captures that are one whole un-filled `<...>` token — protecting
    claude-code too when a model restates its instructions verbatim.

## The maintenance meta-workflow (how every fix above happened)

1. Read the run dir: `manifest.json` → failing stage; `verify.log` tail;
   `command_log.json` (what the agent ACTUALLY ran); `tape_coverage.json`.
2. Match against the failure classes above; if new, diagnose mechanism first.
3. Fix systemically in code (+ prompt if agent behavior), never only in prompt.
4. Add a regression test named for the run.
5. Hand-repair the user's existing run dir when cheap (patch commands.sh /
   plan.json / regenerate guide+tape with the pure functions), and give the
   exact `resume --from-stage` command — don't burn agent cost on re-runs.
6. Full suite green before claiming done.

## Repo conventions

- Python ≥3.10, type hints + docstrings on public functions, pydantic v2.
- Runs land in `runs/` (gitignored — NEVER commit; publishable examples are
  hand-copied to `examples/`).
- SEO/GEO shape of generated docs is deliberate (front matter, provenance
  footers, howto.jsonld) — keep it when touching templates.
- `launch/` holds go-public material; `architecture/README.md` has the
  mermaid diagrams — update it when stages/boundaries change.
