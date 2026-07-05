# Examples — verified output, committed as proof

Every folder here is **real, unedited output** from `readme2demo` run against a
public repo. Nothing was hand-written: an AI agent ran the target's README
inside a hardened Docker sandbox, a fresh clean-room container independently
replayed the distilled steps, and only then were these files published. If a
run couldn't be verified, it isn't here — or it's kept as a labelled *blocked*
example (see below), never dressed up as a success.

Reproduce any row yourself:

```bash
readme2demo run <repo-url>
```

## Gallery

| Repo | Result | Verified | Commit | Agent cost | Artifacts |
|---|---|---|---|---|---|
| [stacklok/toolhive](https://github.com/stacklok/toolhive) — Go CLI for MCP servers | ✅ Verified | 2026-07-03 | `a935334` | $0.69 | [tutorial](toolhive/tutorial.md) · [step-by-step](toolhive/step_by_step.md) · [troubleshooting](toolhive/troubleshooting.md) · [commands.sh](toolhive/commands.sh) · [demo.gif](toolhive/demo.gif) |

> Runs are reproducible from the committed `manifest.json` in each folder —
> engine, base image, commit SHA, per-stage status, and total cost included.

### toolhive demo

![toolhive demo](toolhive/demo.gif)

## What's in each folder

- `tutorial.md` — the polished, SEO/GEO-shaped tutorial with the verification badge
- `step_by_step.md` — the numbered guide the demo video is built from (verified outputs inline)
- `troubleshooting.md` — issues the agent hit and how it resolved them (empty when the README just worked)
- `commands.sh` — the exact minimal command sequence the fresh container executed
- `howto.jsonld` — schema.org HowTo structured data for search engines
- `manifest.json` — the reproducibility record (stages, image digest, commit, cost)
- `demo.gif` — the rendered VHS demo (full-resolution `demo.mp4` is produced per run but kept out of git to stay lean)

## Adding an example

Contributions welcome — pick a well-known repo (a Go CLI, a Python SDK, an MCP
server, a security scanner, a Terraform module), run it, and open a PR copying
the verified artifacts into `examples/<repo>/`. A committed **blocked** example
is just as valuable: when a repo needs credentials or infra the sandbox can't
provide, the tool reports `BLOCKED` honestly instead of faking success — that
report belongs here too.
