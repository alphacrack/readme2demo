---
name: add-failure-class
description: The workflow for turning a newly diagnosed readme2demo run failure into a permanent, tested defense. Use after debug-run identifies a failure mechanism that is not yet in CLAUDE.md's known-failure-classes table.
---

# Adding a failure class (the maintenance meta-workflow)

Every robustness feature in this repo came from a real failed run processed
through exactly these steps. Do them in order; skipping the test or the
CLAUDE.md entry is how knowledge evaporates.

## Steps

1. **Name the mechanism, not the symptom.** "verify exit 1" is a symptom;
   "findings tools exit nonzero on success and set -e kills the assertion"
   is a mechanism. You must be able to say which two components disagreed
   about an assumption.

2. **Pick the owning module** (one, occasionally two):
   - agent behavior → `prompts/agent.md` rule AND a detector in
     `normalize.py` (rule 6 + repo_files_edited is the template)
   - command equivalence → `distill.normalize_cmd` / `_grounded_candidates`
     (always symmetric, both directions tested)
   - replay semantics → `distill._render_commands_sh` (assertion block) or
     `verify.py`
   - video → `templates/demo.tape.j2` / `render.py` gates
   - parsing → the engine adapter, kept pure and tolerant

3. **Code first, prompt second.** If the fix involves an LLM behaving
   differently, the prompt edit gets a sibling code-level check or
   validation — prompts alone are not defenses.

4. **Regression test named for the run.** Docstring starts with
   `"""Regression (<repo> run): ..."""` describing the real failure. Must
   fail on the old code. Tests stay free of docker/network/API.

5. **Repair the triggering run cheaply** (see debug-run skill) and give the
   exact resume command.

6. **Document**: add a numbered entry to CLAUDE.md "Known failure classes"
   (symptom → mechanism → defense location). If the class is user-visible,
   consider a line in README or the troubleshooting-relevant prompt.

7. **Full suite green** (`python -m pytest tests/ -q`) before claiming done.

## Quality bar for grounding changes

New equivalences (like the 2>&1 or pipe-variant rules) must:
- apply to BOTH the candidate set and the queried command (symmetry),
- never make an unproven command grounded (add a negative test: the evil
  variant is still rejected),
- be documented in the `is_grounded` docstring's accepted-forms list.
