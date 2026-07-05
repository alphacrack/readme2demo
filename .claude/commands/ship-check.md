---
description: Pre-commit gauntlet for readme2demo — invariant audit, tests, hygiene
---

Run the pre-commit gauntlet on the current working tree:

1. Run `python -m pytest tests/ -q` — must be fully green.
2. `python -m compileall -q src/` — must be clean.
3. Use the grounding-auditor agent on the staged/changed files if anything
   under src/readme2demo/{distill,tutorial,normalize,engines,prompts,templates}
   changed.
4. Hygiene greps: no `runs/` paths staged (`git status --short | grep runs/`
   must be empty), no credentials
   (`git diff --cached | grep -iE 'sk-ant-[a-z0-9]{8}'` empty).
5. If prompts/*.md changed: confirm the change has a sibling code check or
   test, and note which real run motivated it.
6. If images/base/Dockerfile changed: remind that users must rebuild
   (`docker build --no-cache -t readme2demo/base:latest images/base/`) and
   check the Claude Code pin ↔ stream-json parser coupling.
7. Report: test count, files changed by area, any CLAUDE.md failure-class
   entries that should be added for this change, and a suggested
   conventional commit message.
