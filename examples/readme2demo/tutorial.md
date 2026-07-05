---
title: "How to install and run alphacrack/readme2demo — verified tutorial"
description: "readme2demo turns a project's README into a verified tutorial and demo video: an AI agent runs the quickstart in a sandbox, a fresh container replays every step"
date: 2026-07-05
verified: true
source_repo: "https://github.com/alphacrack/readme2demo"
source_commit: "f677e211e3b435f326408b624309aa3e5377a69c"
generator: readme2demo
---

# Getting Started with readme2demo: Install, Test, and Explore

> ✅ Verified on 2026-07-05 · image readme2demo/base:latest · commit f677e21

readme2demo turns a project's README into a verified tutorial and demo video: an AI agent runs the quickstart in a sandbox, a fresh container replays every step, and only verified results get published. In this walkthrough you'll install readme2demo from source, run its test suite, check out its command-line interface, and inspect a real, independently-verified example run. It takes just a few minutes.

## Prerequisites

- Python 3.10 or newer
- pip
- git

## Step 1 — Install readme2demo with development dependencies

This installs readme2demo in "editable" mode, meaning changes to the source code take effect immediately without reinstalling, along with the extra packages needed for development and testing. The --break-system-packages flag tells pip it's okay to install here even though the system considers this Python environment externally managed — safe to do in this disposable sandbox.

```bash
pip install --break-system-packages -e ".[dev]"
```

Expected output:

```text
Defaulting to user installation because normal site-packages is not writeable
Obtaining file:///work
  Installing build dependencies: started
  Installing build dependencies: finished with status 'done'
  Checking if build backend supports build_editable: started
  Checking if build backend supports build_editable: finished with status 'done'
  Getting requirements to build editable: started
  Getting requirements to build editable: finished with status 'done'
  Installing backend dependencies: started
  Installing backend dependencies: finished with status 'done'
  Preparing editable metadata (pyproject.toml): started
  Preparing editable metadata (pyproject.toml): finished with status 'done'
Collecting anthropic>=0.34 (from readme2demo==0.1.0)
  Downloading anthropic-0.116.0-py3-none-any
```

## Step 2 — Run the test suite to confirm the install works

This runs readme2demo's full suite of automated tests, which is a quick way to confirm your install is healthy before you rely on the tool. None of these tests need Docker or a network connection, so they finish almost instantly. We use python3 here because plain python isn't available in this environment.

```bash
python3 -m pytest tests/ -q
```

Expected output:

```text
........................................................................ [ 41%]
........................................................................ [ 82%]
...............................                                          [100%]
175 passed in 0.36s
```

## Step 3 — Explore the CLI's available commands

This prints readme2demo's built-in help text, showing you the subcommands available (like run, resume, and report) along with global options. It's a good first stop any time you want a reminder of what the tool can do.

```bash
readme2demo --help
```

Expected output:

```text
Usage: readme2demo [OPTIONS] COMMAND [ARGS]...                                 
                                                                                
 Verified tutorial + demo video generation from a repo's README.                
                                                                                
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --install-completion          Install completion for the current shell.      │
│ --show-completion             Show completion for the current shell, to copy │
│                               it or customize the installation.              │
│ --help                        Show this message and exit.                    │
╰───────────────────────────────────────────────────────────────────────
```

## Step 4 — Inspect a bundled, verified example run

This prints a summary of a real run that readme2demo already performed against the ToolHive repository, including every pipeline stage it went through, how much it cost, and confirmation that a fresh container independently verified the result. It's a quick way to see what a completed, trustworthy run looks like before you kick off your own.

```bash
readme2demo report examples/toolhive
```

Expected output:

```text
run:      toolhive-20260703-124148-9db7f4
repo:     https://github.com/stacklok/toolhive @ a935334
engine:   claude-code
verified: yes
cost:     $0.6869
stages:
  ingest     completed  {"feasible": true}
  agent      completed 
  normalize  completed  {"outcome": "success", "commands": 14, 
"adjusted_success": null, "source_modified": null, "guide_steps_unattempted": 
["uname -m", "curl -sLo /tmp/go.tgz 
https://go.dev/dl/go1.26.4.linux-arm64.tar.gz   # or ...linux-amd64.tar.gz", 
"sudo tar -C /usr/local -xzf /tmp/go.tgz", "export 
PATH=/usr/local/go/bin:$PATH", "go version", "mkdir -p /work && cd /work", "curl
-sL https://github.com/stacklok/toolhive/archive/refs/heads/main.tar.gz | tar xz
--strip-components=1"]}
  distill    completed  {"tape_coverage": "9/15 guide steps"}
  verify     c
```

---

Source: [https://github.com/alphacrack/readme2demo](https://github.com/alphacrack/readme2demo) @ `f677e21`
Generated by [readme2demo](https://github.com/readme2demo/readme2demo) on
2026-07-05 — every command in this tutorial was executed and verified
in a clean Linux container before publication.
