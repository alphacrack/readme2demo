#!/usr/bin/env bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive

# --- readme2demo preamble (harness-injected): fresh-container setup ---
cd /work
git clone --depth 1 https://github.com/alphacrack/readme2demo .

pip install --break-system-packages -e ".[dev]"
export PATH="/home/demo/.local/bin:$PATH" && python3 -m pytest tests/ -q
export PATH="/home/demo/.local/bin:$PATH" && readme2demo --help
export PATH="/home/demo/.local/bin:$PATH" && readme2demo report examples/toolhive

# --- readme2demo success-criteria assertion ---
set +e
r2d_output="$(readme2demo report examples/toolhive 2>&1)"
r2d_exit=$?
set -e
printf '%s\n' "$r2d_output"
if ! printf '%s\n' "$r2d_output" | grep -qE 'verified:\s*yes'; then
    echo 'readme2demo: success-criteria pattern not matched: verified:\s*yes' >&2
    exit 1
fi
echo "R2D_VERIFY_OK"
