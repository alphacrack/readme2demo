---
name: debug-run
description: Debug a failed, unverified, or low-quality readme2demo pipeline run. Use when a run fails at any stage, verify won't pass, the demo video is short or missing steps, generated docs lack detail, or the user pastes readme2demo CLI output showing a failure.
---

# Debugging a readme2demo run

Every run leaves complete evidence in `runs/<run-id>/`. Read evidence before
theorizing — the answer is almost always in three files.

## Evidence map

| File | Tells you |
|---|---|
| `manifest.json` | which stage failed, stage meta (outcome, adjusted_success, source_modified, guide_steps_unattempted, tape_coverage), verified flag, cost |
| `command_log.json` | every command the agent ran, exit codes, outputs, FIX markers, findings_success flags |
| `verify.log` | the clean-container replay, bash -x traced; last `+ cmd` before silence = point of death; `===== ATTEMPT n =====` separates tries |
| `commands.sh` | what verify executed — preamble (harness clone), steps, assertion block |
| `step_by_step.md` + `tape_coverage.json` | what made the docs/video and what was dropped (with reasons) |
| `transcript.ndjson` | raw agent stream (only needed for parser bugs) |

## Decision tree

1. **ingest failed** → planner/LLM issue or infeasible repo (read plan.json
   blockers). Infeasible is often CORRECT behavior.
2. **agent failed/blocked** → read final assistant text in command_log
   result + failed entries. Check for the cutoff-denial pattern ("X isn't
   released yet") and infrastructure rabbit holes (fake sockets). Blocked
   for credentials/GPU is correct behavior.
3. **distill failed (ungrounded)** → compare the rejected command against
   the log: usually a syntax-equivalence gap (whitespace, 2>&1, env prefix,
   chain, pipe-cap, heredoc body). Fix in `normalize_cmd` /
   `_grounded_candidates` symmetrically, never by weakening validation.
4. **verify failed** → tail the last attempt in verify.log. Common causes in
   order: missing setup step, cwd drift (run `verify.cwd_hints` on the
   script), findings-tool nonzero exit reaching `set -e`, wrong
   expected_pattern, agent had patched source (manifest source_modified).
5. **render failed** → duration gate (steps didn't play → stale image or
   tape bug), VHS parse error (quoting), disk exhaustion (docker system
   prune), socket permissions if the tape manages containers.
6. **docs/video missing steps** → `tape_coverage.json` dropped list +
   normalize's guide_steps_unattempted meta. Unattempted = agent never ran
   it (needs fresh agent run); dropped = grounding gap (fixable + tape
   regenerable without LLM).

## Cheap repairs (no agent re-run)

The artifact writers are pure functions. From the repo root:

```python
# regenerate guide + tape for an existing run after a code fix
PYTHONPATH=src python3 - <<'EOF'
from pathlib import Path
from readme2demo.distill import tape_from_guide, write_tape
from readme2demo.tutorial import write_step_by_step
from readme2demo.types import CommandLog, Plan, TutorialOutline
rd = Path("runs/<run-id>")
plan = Plan.model_validate_json((rd/"plan.json").read_text())
log = CommandLog.model_validate_json((rd/"command_log.json").read_text())
outline = TutorialOutline.model_validate_json((rd/"tutorial_outline.json").read_text())
write_step_by_step(rd, plan, outline, log, verified=False, repo_url="<url>")
write_tape(tape_from_guide((rd/"step_by_step.md").read_text(), log, [], "<url>"), rd)
EOF
```

`commands.sh` and `plan.json` can be patched by hand for one run; then
`readme2demo resume runs/<run-id> --from-stage <verify|tutorial|render>`.
Resume from the EARLIEST stage whose inputs changed, never earlier.

## After the diagnosis

If the cause is new, follow the add-failure-class skill: systemic code fix +
regression test + CLAUDE.md failure-class entry. A run-specific patch without
a systemic fix just schedules the same debugging session for the next repo.
