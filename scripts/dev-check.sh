#!/bin/sh
# dev-check.sh — runs exactly what .github/workflows/ci.yml runs.
# If you change ci.yml, change this file in the same PR.
# Source of truth: .github/workflows/ci.yml (lint job: ruff check src/ tests/, test job: python -m pytest tests/ -q)
set -e

command -v ruff >/dev/null 2>&1 || {
  echo 'ruff not found. Run: pip install -e ".[dev]"' >&2; exit 1;
}

echo "==> ruff check src/ tests/"
ruff check src/ tests/

echo "==> python -m pytest tests/ -q"
python -m pytest tests/ -q

echo "OK: everything PR CI checks passed locally."
