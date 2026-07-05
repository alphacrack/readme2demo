<!--
Thanks for contributing! Keep PRs to one concern. The checklist below maps
directly to the review rules in CONTRIBUTING.md — filling it in makes review
fast.
-->

## What this changes

<!-- One paragraph. What and why. Link the issue it closes: Closes #___ -->

## The grounding invariant

> The LLM never publishes anything a fresh container didn't independently
> execute. Grounding is enforced in code, not prompts.

- [ ] This change does **not** let an unverified command reach `tutorial.md`,
      `step_by_step.md`, `commands.sh`, or the demo tape — **or** it adds a
      code-level check (not just a prompt rule) that keeps that true.
- [ ] Sandbox hardening flags in `sandbox.py` are unchanged (or the change is
      explicitly discussed in the PR body).

## Tests

- [ ] `python -m pytest tests/ -q` passes locally.
- [ ] Bug fixes carry a regression test named for the failure/run that found it.
- [ ] `parse_transcript` / `normalize.py` changes stayed pure and
      deterministic (no LLM calls; testable against fixtures).

## Prompt changes (if any)

- [ ] N/A — this PR doesn't touch `prompts/*.md`.
- [ ] Included a before/after transcript or run report as evidence.

## Housekeeping

- [ ] One concern per PR.
- [ ] Type hints + docstrings on new/changed public functions.
- [ ] If stages or boundaries moved, `architecture/README.md` and `CLAUDE.md`
      are updated to match.
