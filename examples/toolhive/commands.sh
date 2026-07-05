#!/usr/bin/env bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

# --- readme2demo preamble (harness-injected): fresh-container setup ---
cd /work
git clone --depth 1 https://github.com/stacklok/toolhive .

go version && git --version
go build -o bin/thv ./cmd/thv
./bin/thv
./bin/thv version

# --- readme2demo success-criteria assertion ---
r2d_output="$(go build -o bin/thv ./cmd/thv && ./bin/thv 2>&1)"
printf '%s\n' "$r2d_output"
if ! printf '%s\n' "$r2d_output" | grep -qE 'Available Commands:'; then
    echo 'readme2demo: success-criteria pattern not matched: Available Commands:' >&2
    exit 1
fi
echo "R2D_VERIFY_OK"
