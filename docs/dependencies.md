# Dependency policy

This document is the canonical reference for how `readme2demo` pins its
dependencies. The `pyproject.toml` is the source of truth for the actual
pins; this page explains the reasoning so a reviewer can approve or
reject a bump in seconds instead of re-deriving it.

## The two rules, in one sentence

> **A pin is a floor when an API surface we depend on is stable across
> versions; a pin is a deliberate freeze when the dependency's output is
> fed into a parser that breaks silently across major versions.**

Everything below is an elaboration of that one sentence.

## Floors vs ceilings

Every requirement in `pyproject.toml` is a bare `>=` floor:

```toml
dependencies = [
    "typer>=0.12",
    "rich>=13.7",
    "pydantic>=2.7",
    "jinja2>=3.1",
    "anthropic>=0.34",
]
```

**Default rule: floor only, no ceiling.** We don't add upper bounds
unless a known-incompatible major version has been released *and* we
have not yet migrated. A floor means "oldest version whose API we
actually use" — it does not promise the latest version works, only
that a reasonable operator with a current distro can `pip install`
this package and run it.

**Why no ceilings on the runtime deps:** readme2demo runs in a
hardened sandbox that destroys the container after every stage (see
[the security model](security.md)). A broken upgrade is recoverable
by bumping the floor; a stuck upgrade that the lockfile rejects is a
forever-broken install. The bias is toward "always installable",
not "always reproducible".

## Runtime vs dev vs optional extras

`pyproject.toml` has three kinds of dependencies. The policy for each
is different:

### `dependencies` (runtime)

The minimum set needed to invoke `readme2demo` as a CLI. Currently:

- `typer` — CLI framework
- `rich` — terminal rendering
- `pydantic` — data models (`manifest.py`, `llm.py`, etc.)
- `jinja2` — tutorial / tape templates
- `anthropic` — see the "anthropic placement" callout below

These get a floor. Nothing else belongs in `[project.dependencies]`.

### `dev` extra

Tooling contributors and CI install: `pytest`, `pytest-cov`,
`pytest-mock`, `ruff`, and **`mypy`** (added below in this PR — see
the "what was missing from Dependabot" callout).

Dev deps can float freely. They are not part of the published wheel
and they are not installed by end users. The `dev` floor is
deliberately permissive so contributors don't have to re-resolve
their venvs every week.

### `docs` extra

Mkdocs Material for the published docs site. Same rules as `dev`:
floor only, never in the runtime path.

### Optional LLM backends (`gemini`, `openai`)

These extras exist because not every user wants every backend, and
forcing the SDK on everyone makes the install footprint larger than
necessary. They are *user-installed* on demand via
`pip install "readme2demo[gemini]"` (or `[openai]`).

**The floor on these is a compatibility contract the code already
enforces at runtime.** The code calls `llm.check_sdk` (see
`src/readme2demo/llm.py`) which distinguishes `absent / broken /
too-old` and prints the matching `pip install` line. Raising a floor
in `[project.optional-dependencies]` and bumping the sentinel
attribute in `_BACKEND_SDKS` is the *same decision* — they should
land in the same PR.

## The two PINNED-IN-IMAGE tools (do not automate)

Two dependencies are pinned inside the sandbox **Docker images**,
not in `pyproject.toml`. They are deliberately exempt from
Dependabot because they are coupled to transcript parsers that
break silently across versions:

### `@anthropic-ai/claude-code@2.0.14`

Pinned in [`images/base/Dockerfile`](../images/base/Dockerfile) at
the npm install line. The M4 transcript parser
(`engines/claude_code.py`) reads the stream-json output format, and
that format has changed between major versions of the Claude Code
CLI.

> **Do not add a `package.json` or a pip `package-ecosystem` entry
> that would let a bot move this pin without the matching parser
> change.** The Dockerfile comment in `images/base/Dockerfile`
> (`UPDATE THIS PIN deliberately`) is the standing rule; this doc
> is the place new contributors will look first.

### `openhands-ai==0.48.0`

Pinned in [`images/openhands/Dockerfile`](../images/openhands/Dockerfile)
at the `uv pip install` line. 0.48.0 is the newest release that
installs on BOTH amd64 and arm64 (the 0.49+ series ships
cp312 manylinux_x86_64-only wheels — Apple Silicon builds fail).
The trajectory parser in `engines/openhands.py` is also coupled to
this version's `SAVE_TRAJECTORY_PATH` env-var interface (no
`--save-trajectory-path` flag in 0.48).

> **Do not add a docker `package-ecosystem` entry for
> `/images/openhands`.** The Dockerfile header (`bump the pin and
> the engine adapter TOGETHER, exactly like the Claude Code pin`)
> is the standing rule.

Both pins are also upstream of the grounding path: `command_log.json`
is the record of what the agent actually ran, and `normalize.py` +
`distill.py`'s grounding validator have nothing else to check
published commands against. If a Claude Code or OpenHands upgrade
silently changes its output format and the parser is not updated
in the same change, the failure is not a loud crash — it is a
**degraded or wrongly-populated command log**, and grounding then
validates against the wrong ground truth. That is exactly the
invariant the project refuses to weaken, which is why these pins
are deliberately *not* in `pyproject.toml` and not in
Dependabot's reach.

## What Dependabot covers today

The `.github/dependabot.yml` configuration tracks three ecosystems:

- **pip** — `pyproject.toml` (weekly, max 5 open PRs)
- **github-actions** — workflow file versions (weekly)
- **docker** — `images/base/Dockerfile` parent image digest
  (weekly) — the `charmbracelet/vhs:latest` parent, **not** the
  pinned `claude-code` and `openhands-ai` lines.

Three things it does *not* see (and the reasons why):

1. **`mypy` in the CI typecheck job.** The typecheck step
   (`pip install -e ".[dev]" mypy`) installs mypy out-of-band of
   the `dev` extra, so a Dependabot bump for mypy is impossible
   today. This PR adds `mypy` to the `dev` extra to make it
   visible to the pip ecosystem entry.
2. **The `tomli; python_version < "3.11"` conditional import.**
   Conditional/marker deps for the `tomli` fallback on
   Python ≤3.10 are a separate decision (issue #39 — `readme2demo.toml
   is silently ignored on Python 3.10 without tomli`); that fix
   lands in its own PR.
3. **The two pinned-in-image tools.** See the section above. The
   `images/base` and `images/openhands` Dockerfiles are
   intentionally outside Dependabot's reach for the package pins,
   even though Dependabot *can* track the base image digest
   itself.

## What this PR does and does not change

This PR is documentation plus one mechanical `pyproject.toml`
correction:

- **Adds** `docs/dependencies.md` and links it from the docs nav.
- **Adds** `mypy` to the `dev` extra so Dependabot can see and
  bump it. CI currently does `pip install -e ".[dev]" mypy`, which
  silently pins nothing.
- **Records the decision** on whether `anthropic` belongs in core
  `dependencies` or in an extra. It is currently in core (it's
  imported lazily inside `_complete_api` in
  `src/readme2demo/llm.py:253` and is only reachable on
  `--llm-backend api`; the default path is `claude-cli`). The
  decision is to **leave it where it is** — moving it would be a
  packaging break for existing `api` users, and the lazy import
  already prevents the install footprint from growing for the
  default path. This PR records the decision; it does not move
  the dep.

**Out of scope for this PR** (each is a separate decision with its
own CI cost):

- Adopting a lockfile (`uv.lock`, `requirements*.txt`, hash
  pinning).
- Adding pip caching or new CI jobs.
- The `tomli` conditional dependency (issue #39).
- Bumping either image pin or touching `engines/claude_code.py` /
  `engines/openhands.py`.
- Any change to sandbox hardening flags or to what the base
  image installs.

## Pointers

All verified at `main` (commit `af6fd6c`).

- `pyproject.toml:38-44` — runtime deps, all bare `>=` floors.
- `pyproject.toml:46-52` — `dev` and `docs` extras, with `mypy`
  added in this PR.
- `pyproject.toml:54-58` — `gemini` and `openai` extras with
  compatibility-contract floors.
- `images/base/Dockerfile:55-59` — Claude Code pin and
  `UPDATE THIS PIN deliberately` rule.
- `images/openhands/Dockerfile:7-23` — OpenHands pin rationale
  (cp312 wheel story, trajectory parser coupling).
- `.github/dependabot.yml` — what Dependabot actually covers.
- `src/readme2demo/llm.py:check_sdk` — the runtime SDK version
  check that the optional-extras floors coordinate with.
- `CONTRIBUTING.md` — the project invariant this policy is
  designed to protect.
