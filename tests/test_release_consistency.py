"""Regression (#80): release-versioned files must agree — pyproject vs CITATION.cff vs CHANGELOG."""

from __future__ import annotations

import re
from pathlib import Path


def test_release_consistency() -> None:
    """Regression (#80): CITATION.cff drifted two releases behind (0.4.1 stale through 0.5.0 and 0.6.0).

    Guards the release-versioned files that release.yml and citation depend on:
    - pyproject.toml [project] version (e.g. "0.7.5")
    - CITATION.cff version: (e.g. 0.7.5) — anchored to ^version: to avoid matching cff-version
    - CHANGELOG.md topmost ## [x.y.z] heading (optional but encouraged; also used by release.yml for notes)
    """
    root = Path(__file__).resolve().parent.parent

    # pyproject.toml version
    pyproject_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    m_py = re.search(r'^\s*version\s*=\s*"([^"]+)"', pyproject_text, re.MULTILINE)
    assert m_py, "version not found in pyproject.toml"
    py_version = m_py.group(1).strip()

    # CITATION.cff version: — must anchor to start of line to avoid cff-version
    cff_text = (root / "CITATION.cff").read_text(encoding="utf-8")
    m_cff = re.search(r"^version:\s*(\S+)", cff_text, re.MULTILINE)
    assert m_cff, "version not found in CITATION.cff"
    cff_version = m_cff.group(1).strip()

    assert cff_version == py_version, (
        f"CITATION.cff has {cff_version} but pyproject.toml has {py_version} — bump both together"
    )

    # CHANGELOG.md topmost ## [x.y.z] — optional but failure is loud if present
    changelog_text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    m_log = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog_text, re.MULTILINE)
    assert m_log, "no ## [x.y.z] heading found in CHANGELOG.md"
    log_version = m_log.group(1).strip()
    assert log_version == py_version, (
        f"CHANGELOG.md top heading is {log_version} but pyproject.toml has {py_version} — keep footer and heading in sync"
    )
