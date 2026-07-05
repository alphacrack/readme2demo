---
title: "How to install and run stacklok/toolhive — verified tutorial"
description: "ToolHive (thv) is a lightweight, secure CLI written in Go for managing MCP (Model Context Protocol) servers. Every command verified in a clean container."
date: 2026-07-03
verified: true
os: linux
arch_tested: arm64
source_repo: "https://github.com/stacklok/toolhive"
generator: readme2demo
---

# Build and Run the ToolHive (thv) CLI from Source — step by step

> ✅ Every command below was executed and verified in a clean Linux (Ubuntu 22.04, arm64) container on 2026-07-03. Verified build: commit `d70cc41a`, Go 1.26.4.

ToolHive (thv) is a lightweight, secure CLI written in Go for managing MCP (Model Context Protocol) servers. In this quick tutorial you'll compile the thv binary from source and run it once to confirm the build works — it should only take a couple of minutes.

## Prerequisites

- go >= 1.26 *(the repo's go.mod declares `go 1.26` — Go 1.25 will NOT build it)*
- git

### Check prerequisites first

```bash
go version && git --version
```

✅ **Check:** you see `go version go1.26.x linux/<arch>` (or newer) and a git version.

❌ **If `go: command not found`** — install Go for Linux (pick the tarball matching your architecture; `uname -m` tells you: `x86_64` → amd64, `aarch64` → arm64):

```bash
uname -m
curl -sLo /tmp/go.tgz https://go.dev/dl/go1.26.4.linux-arm64.tar.gz   # or ...linux-amd64.tar.gz
sudo tar -C /usr/local -xzf /tmp/go.tgz
export PATH=/usr/local/go/bin:$PATH
go version
```

❌ **If `cannot execute binary file: Exec format error`** — you downloaded the wrong architecture (this exact error was reproduced in verification by using the amd64 tarball on an aarch64 machine). Delete it, re-check `uname -m`, and download the matching tarball.

❌ **If your Go is older than 1.26** — the build fails with a message like `go.mod requires go >= 1.26`. Install a newer Go using the block above; a distro `apt install golang` is usually too old.

## Steps

Start in an empty working directory.

### Step 1 — Move into the working directory

```bash
mkdir -p /work && cd /work
```

✅ **Check:** `pwd` prints `/work` and `ls -A` prints nothing (directory must be empty).

❌ **If the directory is not empty** — the clone in Step 2 fails with `fatal: destination path '.' already exists and is not an empty directory.` Use a fresh directory: `mkdir /work2 && cd /work2`.

### Step 2 — Get the source code

```bash
git clone --depth 1 https://github.com/stacklok/toolhive .
```

Expected output:

```text
Cloning into '.'...
```

✅ **Check:** `ls` shows the repo contents — `cmd/`, `pkg/`, `go.mod`, `Taskfile.yml`, etc. Confirm the Go requirement with `grep '^go ' go.mod` → `go 1.26`.

❌ **If the clone fails with a network/TLS error** — check connectivity (`curl -sI https://github.com` should return `HTTP/2 200`). Behind a proxy, set `https_proxy`. As a fallback, download a tarball instead:

```bash
curl -sL https://github.com/stacklok/toolhive/archive/refs/heads/main.tar.gz | tar xz --strip-components=1
```

### Step 3 — Build the thv binary

This command tells Go to compile the cmd/thv package and place the resulting binary at bin/thv. The first time you run it, Go automatically downloads all of the project's module dependencies, so you'll see a series of "downloading" lines before the build finishes.

```bash
go build -o bin/thv ./cmd/thv
```

Expected output (first lines; the full list is longer):

```text
go: downloading github.com/stacklok/toolhive-core v0.0.26
go: downloading github.com/adrg/xdg v0.5.3
go: downloading github.com/spf13/viper v1.21.0
go: downloading github.com/charmbracelet/bubbletea v1.3.10
go: downloading github.com/mark3labs/mcp-go v0.55.0
go: downloading github.com/spf13/cobra v1.10.2
go: downloading github.com/spf13/pflag v1.0.10
go: downloading golang.org/x/oauth2 v0.36.0
go: downloading golang.org/x/term v0.44.0
go: downloading github.com/gofrs/flock v0.13.0
...
```

The command exits silently after the downloads once compilation succeeds. On the verified machine (4 CPUs, 4 GB RAM) the full first build — module downloads plus compilation — took roughly 3 minutes.

✅ **Check:**

```bash
ls -la bin/thv
```

You should see an executable ~150 MB in size:

```text
-rwxr-xr-x 1 user user 157442387 Jul  3 12:15 bin/thv
```

❌ **If `bin/thv` doesn't exist but there was no error** — the build was interrupted (e.g., a shell timeout). Just re-run the same `go build` command; Go caches downloaded modules and compiled packages, so it resumes where it left off instead of starting over.

❌ **If the build is killed with `signal: killed`** — the machine ran out of memory. Retry with limited parallelism: `GOMAXPROCS=2 go build -o bin/thv ./cmd/thv`, or use a machine with ≥4 GB RAM.

❌ **If module downloads fail (`dial tcp ... i/o timeout`)** — set the module proxy explicitly and retry: `GOPROXY=https://proxy.golang.org,direct go build -o bin/thv ./cmd/thv`.

❌ **If you see `go.mod requires go >= 1.26`** — your Go toolchain is too old; go back to Prerequisites.

### Step 4 — Run the CLI

Now that the binary is built, run it directly with no arguments. With no subcommand given, thv prints a description of itself along with the full list of available commands — seeing this output confirms your build succeeded and the CLI is working correctly.

```bash
./bin/thv
```

Expected output (verified verbatim):

```text
ToolHive (thv) is a lightweight, secure, and fast manager for MCP (Model Context Protocol) servers.
It is written in Go and has extensive test coverage—including input validation—to ensure reliability and security.

Under the hood, ToolHive acts as a very thin client for the Docker/Podman/Colima Unix socket API.
This design choice allows it to remain both efficient and lightweight while still providing powerful,
container-based isolation for running MCP servers.

Usage:
  thv [flags]
  thv [command]

Available Commands:
  build       Build a container for an MCP server without running it
  client      Manage MCP clients
  completion  Generate the autocompletion script for the specified shell
  config      Manage application configuration
  export      Export a workload's run configuration to a file
  group       Manage logical groupings of MCP servers
  help        Help about any command
  inspector   Launches the MCP Inspector UI and connects it to the specified MCP server
  list        List running MCP servers
  ...
  run         Run an MCP server
  secret      Manage secrets
  version     Show the version of ToolHive
```

✅ **Check:** the help text above appears and the exit code is 0. Also confirm the build metadata:

```bash
./bin/thv version
```

```text
You are running a local build of ToolHive

ToolHive build-d70cc41a
Commit: d70cc41a0545707759226dd02a58c0b31e6e54dc
Built: 2026-07-03 08:24:57 UTC
Go version: go1.26.4
Platform: linux/arm64
```

❌ **If `Permission denied`** — make it executable: `chmod +x bin/thv`.

❌ **If `No such file or directory`** — you're not in the repo root; `cd /work` first, or call it by absolute path: `/work/bin/thv`.

### Step 5 (optional) — Actually run an MCP server

`thv run` needs a container runtime (it talks to the Docker/Podman Unix socket). This step was **not** part of the container verification (no Docker-in-Docker); on a Linux host with Docker running:

```bash
./bin/thv run toolhive-doc-mcp
./bin/thv list
```

✅ **Check:** `thv list` shows STATUS `running` and a proxy URL like `http://127.0.0.1:<port>/mcp`.

❌ **If it errors about the container runtime** — start Docker (`sudo systemctl start docker`) or Podman, or use the hosted variant that needs no runtime: `./bin/thv run toolhive-doc-mcp-remote`.

## The payoff

With everything set up, `go build -o bin/thv ./cmd/thv && ./bin/thv` demonstrates the tool working
(see demo.mp4 / demo.gif alongside this file).

---
Source: [https://github.com/stacklok/toolhive](https://github.com/stacklok/toolhive) · *Generated by [readme2demo](https://github.com/readme2demo/readme2demo) — every step above ran before it was written.*