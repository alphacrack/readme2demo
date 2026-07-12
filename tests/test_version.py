"""Tests for readme2demo``s ``__version__`` attribute."""

from __future__ import annotations

import re
from pathlib import Path

from importlib.metadata import PackageNotFoundError, version as pkg_version

import pytest

from readme2demo import __version__


def test_version_matches_pyproject() -> None:
    """``__version__`` must equal the version declared in pyproject.toml.

    # fragile: regex-based parse may fail on non-standard TOML formatting
    # (multiline version, inline comments). Replace with tomllib when
    # Python 3.10 is EOL and the project drops <3.11 support.
    """
    from readme2demo import __version__

    try:
        pkg_version("readme2demo")
    except PackageNotFoundError:
        pytest.skip("package not installed; __version__ is the dev fallback")

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    match = re.search(
        r"^\s*version\s*=\s*\"([^\"]+)\"",
        pyproject.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, "version not found in pyproject.toml"
    assert __version__ == match.group(1)


def test_version_is_nonempty_string() -> None:
    """``__version__`` should be a non-empty string."""
    assert isinstance(__version__, str)
    assert len(__version__) > 0
