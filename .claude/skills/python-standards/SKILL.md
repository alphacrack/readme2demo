---
name: python-standards
description: Python coding standards, testing patterns, and pydantic/typing conventions for the readme2demo codebase. Use when writing or reviewing any Python in src/readme2demo/ or tests/, adding a module, or before committing code changes.
---

# Python standards for readme2demo

Python ≥3.10. Match the surrounding code; these are the invariants.

## Types & models

- Type hints on every public function signature; return types included.
- Data contracts are pydantic v2 models in `types.py` — the single source of
  truth every stage serializes through. Add a field THERE first, never invent
  an ad-hoc dict that crosses a stage boundary. Use `model_dump_json(indent=2)`
  / `model_validate_json` for run-dir I/O.
- `Optional[X]` for nullable; default containers via `Field(default_factory=...)`.
- New fields must be backward-compatible (defaulted) — old run dirs on disk
  must still load.

## Purity boundary (enforced, not stylistic)

- `normalize.py` and every engine `parse_transcript` are PURE and
  deterministic: no LLM calls, no network, no docker, no clock/random that
  changes output. They are tested against fixtures. If you need an LLM, you
  are in the wrong module.
- LLM calls live only in ingest (planner), distill, tutorial — via `llm.py`.

## Docstrings

- One-line summary; then WHY when non-obvious. Regression-critical functions
  carry the failure they defend against (search for "Regression (" in tests).
- Public functions in the pipeline path get a docstring naming their invariant.

## Testing (this is where the repo's value lives)

- `python -m pytest tests/ -q` runs in <1s with NO docker/network/API. Keep it
  that way: mock `llm.complete_json`, feed fixture transcripts, build models
  inline. Anything needing real infra is marked `@pytest.mark.integration`.
- EVERY bug fix carries a regression test whose docstring starts
  `"""Regression (<repo> run): ..."""` and describes the real failure. It must
  fail on the old code.
- For grounding/equivalence changes: test BOTH directions — the new-variant is
  accepted AND an unproven/evil command is still rejected.
- Prefer table-style asserts on the actual artifact (the rendered commands.sh,
  the tape text, the guide markdown), not on internal state.

## Style

- stdlib > third-party; the runtime deps are deliberately few (typer, rich,
  pydantic, jinja2, anthropic). Don't add a dependency for something stdlib
  does.
- `subprocess.run(..., capture_output=True, text=True, timeout=..., errors="replace")`
  is the house pattern for shelling out; always set a timeout.
- Fail with a specific, actionable message (name the env var, the file, the
  fix command) — see the preflight and EngineError messages for the tone.

## Before you commit

Run `/ship-check`. Minimum: full suite green, `python -m compileall -q src/`
clean, no `runs/` or secrets staged.
