"""Tests for the derived-artifact manifest contract."""

from __future__ import annotations

import json

import pytest

from readme2demo.manifest import Manifest
from readme2demo.orchestrator import summarize, summarize_markdown
from readme2demo.tutorial import render_badge


def test_manifest_without_derived_key_loads_empty(tmp_path) -> None:
    """Regression: a manifest.json written before the derived field must load."""
    (tmp_path / "manifest.json").write_text(
        json.dumps({"run_id": "legacy-run"}),
        encoding="utf-8",
    )

    manifest = Manifest.load(tmp_path)

    assert manifest.derived == []


def test_record_derived_round_trips_through_save_and_load(tmp_path) -> None:
    """Regression: record_derived must persist through the atomic save."""
    manifest = Manifest.create(tmp_path, repo_url="https://github.com/owner/repo")
    manifest.record_derived(
        path="architecture.svg",
        stage="ingest",
        tool="codeflow",
        tool_version="v1.2.3",
        source_commit_sha="abcdef1234567890",
        note="graph only",
    )

    loaded = Manifest.load(tmp_path)

    assert len(loaded.derived) == 1
    artifact = loaded.derived[0]
    assert artifact.path == "architecture.svg"
    assert artifact.stage == "ingest"
    assert artifact.tool == "codeflow"
    assert artifact.tool_version == "v1.2.3"
    assert artifact.source_commit_sha == "abcdef1234567890"
    assert artifact.note == "graph only"


@pytest.mark.parametrize(
    "path",
    ["commands.sh", "step_by_step.md", "demo.tape", "demo.mp4"],
)
def test_record_derived_rejects_protected_verified_filenames(tmp_path, path) -> None:
    """Regression: a derived record must never claim a verified filename."""
    manifest = Manifest.create(tmp_path)

    with pytest.raises(ValueError, match="verified artifact"):
        manifest.record_derived(
            path=path,
            stage="ingest",
            tool="codeflow",
            tool_version="v1",
            source_commit_sha="a" * 40,
        )


def test_record_derived_rejects_unknown_stage(tmp_path) -> None:
    """Regression: a derived record must name a real pipeline stage."""
    manifest = Manifest.create(tmp_path)

    with pytest.raises(ValueError, match="Unknown derived-artifact stage"):
        manifest.record_derived(
            path="architecture.svg",
            stage="not-a-stage",
            tool="codeflow",
            tool_version="v1",
            source_commit_sha="a" * 40,
        )


def test_reset_from_drops_reproduced_derived_records(tmp_path) -> None:
    """Regression: resetting a stage must not carry stale derived records."""
    manifest = Manifest.create(tmp_path)
    manifest.record_derived(
        path="early.svg", stage="ingest", tool="codeflow",
        tool_version="v1", source_commit_sha="a" * 40,
    )
    manifest.record_derived(
        path="mid.svg", stage="normalize", tool="codeflow",
        tool_version="v1", source_commit_sha="b" * 40,
    )
    manifest.record_derived(
        path="late.svg", stage="verify", tool="codeflow",
        tool_version="v1", source_commit_sha="c" * 40,
    )

    manifest.reset_from("normalize")

    assert [artifact.path for artifact in manifest.derived] == ["early.svg"]


def test_render_badge_ignores_derived_records(tmp_path) -> None:
    """Regression: derived records must never change the verified badge."""
    manifest = Manifest.create(tmp_path)
    for tool in ("codeflow", "other-tool"):
        manifest.record_derived(
            path=f"{tool}.svg", stage="ingest", tool=tool,
            tool_version="v1", source_commit_sha="a" * 40,
        )

    assert render_badge(manifest) == {
        "schemaVersion": 1,
        "label": "readme2demo",
        "message": "unverified",
        "color": "red",
    }

    manifest.verified = True
    manifest.stages["verify"].finished_at = "2026-08-06T12:00:00+00:00"
    manifest.save()

    badge = render_badge(Manifest.load(tmp_path))
    assert badge["message"].startswith("verified 2026-08-06")
    assert all(tool not in badge["message"] for tool in ("codeflow", "other-tool"))


def test_derived_provenance_line_never_says_verified(tmp_path) -> None:
    """Regression: provenance wording must not inherit the verified claim."""
    manifest = Manifest.create(tmp_path, repo_url="https://github.com/owner/repo")
    manifest.record_derived(
        path="map.svg",
        stage="ingest",
        tool="codeflow",
        tool_version="v1.2.3",
        source_commit_sha="abcdef1234567890",
    )

    line = manifest.derived_provenance_line(manifest.derived[0])

    assert line.startswith("Derived from https://github.com/owner/repo @ abcdef1")
    assert "verif" not in line.lower()


def test_summaries_keep_derived_section_separate(tmp_path) -> None:
    """Regression: derived artifacts must not ride the verified line."""
    manifest = Manifest.create(tmp_path, repo_url="https://github.com/owner/repo")
    manifest.record_derived(
        path="architecture.svg",
        stage="ingest",
        tool="codeflow",
        tool_version="v1.2.3",
        source_commit_sha="abcdef1234567890",
    )

    human = summarize(manifest)
    markdown = summarize_markdown(manifest, artifacts=[])

    assert "Derived (parsed from source at abcdef1 — not executed)" in human
    assert "Derived (parsed from source at abcdef1 — not executed)" in markdown
    assert "parsed, not executed." in human
    assert "parsed, not executed." in markdown
