---
title: "Install readme2demo and confirm it works — step by step"
description: "Install readme2demo from source, run its 163-test suite, explore the CLI, and read a verified example run — the whole thing checkable in a clean container with no Docker daemon or API key."
source_repo: "https://github.com/alphacrack/readme2demo"
---

# Get readme2demo running from source

This is the guide readme2demo runs against *itself* to produce its own demo.
Every step below is executed inside a hardened sandbox and then independently
replayed in a fresh container before anything is published — the same gate the
tool applies to every repo it documents. None of these steps need a Docker
daemon or a model API key, so the demo is fast and always reproducible.

## Step 1 — Install from source

Install the package and its development extras (test runner included).

```bash
pip install -e ".[dev]"
```

## Step 2 — Run the test suite

The pipeline's grounding, parsing, and orchestration logic is covered by a fast,
dependency-free suite. It runs in well under a second — no Docker, no network,
no API keys.

```bash
python -m pytest tests/ -q
```

You should see all 163 tests pass.

## Step 3 — Explore the CLI

Three commands: `run` a repo through the pipeline, `resume` an interrupted run,
and `report` on any run's verification status and cost.

```bash
readme2demo --help
```

## Step 4 — Read a verified run

The repo ships a real, verified example under `examples/`. Point `report` at it
to see exactly what the pipeline records: per-stage status, the verified flag,
the commit it ran against, and the agent cost.

```bash
readme2demo report examples/toolhive
```

`verified: yes` is the whole point — that summary was produced by an agent
running the target's README in a sandbox and a fresh container independently
replaying it. That is what readme2demo does for any repo you point it at,
including this one.
