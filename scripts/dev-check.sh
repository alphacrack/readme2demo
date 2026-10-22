#!/bin/sh
# dev-check.sh — runs exactly what .github/workflows/ci.yml runs.
# If you change ci.yml, change this file in the same PR.
# Source of truth: .github/workflows/ci.yml
set -e
cd "$(dirname "$0")/.." || exit 1

command -v ruff >/dev/null 2>&1 || {
  echo 'ruff not found. Run: pip install -e ".[dev]"' >&2; exit 1;
}

echo "==> ruff check src/ tests/"
ruff check src/ tests/

command -v mypy >/dev/null 2>&1 || {
  echo 'mypy not found. Run: pip install -e ".[dev]"' >&2; exit 1;
}

echo "==> mypy src/readme2demo"
mypy src/readme2demo

echo "==> python -m pytest tests/ -q --cov=readme2demo --cov-report=term --cov-fail-under=80"
python -m pytest tests/ -q --cov=readme2demo --cov-report=term --cov-fail-under=80

echo "OK: everything PR CI checks passed locally."
