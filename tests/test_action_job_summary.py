"""Regression (#249 Piece 0): the Action must write $GITHUB_STEP_SUMMARY, and
docs/whats-new-0.7.0.md must not claim it already does.

The issue: `report --markdown` shipped in #159 but was never plumbed into
action.yml, while docs/whats-new-0.7.0.md:42 says "the Action does this for
you" and :68-69 says "the job summary come[s] along for free". Neither was
true. This test pins both halves of the fix to the exact YAML shape from the
issue so a future refactor cannot silently un-wire the summary.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_action_yml_writes_github_step_summary() -> None:
    """action.yml gains a job-summary step piping `report --markdown` into
    $GITHUB_STEP_SUMMARY, guarded by always(), a run-dir guard, and `|| true`
    so report's nonzero exits (1=unverified, 2=failed stage) never fail the
    job — the check step owns the red X."""
    text = _read("action.yml")

    # The summary step pipes report --markdown into the step summary file.
    pipe = re.search(
        r"run:\s*\|?\s*\n?\s*readme2demo report \"?\$\{?R2D_RUN_DIR\}?\"?"
        r"[^\n]*--markdown[^\n]*>>\s*\"\$GITHUB_STEP_SUMMARY\"",
        text,
    )
    assert pipe, (
        "action.yml has no step appending `readme2demo report <run-dir> "
        "--markdown` to $GITHUB_STEP_SUMMARY (#249)"
    )

    # The step's shell command carries || true so it can never fail the job.
    step_start = text.rfind("- name:", 0, pipe.start())
    m = re.search(
        r"run:\s*\|?\s*\n?(.*?)(?=\n\s*- name:|\n\s*if:|\Z)",
        text[step_start:pipe.end() + 200],
        re.DOTALL,
    )
    assert m, "could not isolate the summary step's run block"
    run_block = m.group(1)
    assert "|| true" in run_block or "||true" in run_block, (
        "the job-summary step must tolerate nonzero report exit codes with "
        "`|| true` — the check step owns failing the build (#249)"
    )

    # Guarded by always() AND by a non-empty run-dir output (partial runs).
    # The if: line sits inside the step block, right after the name.
    step_region = text[step_start:pipe.end() + 400]
    assert re.search(r"if:.*always\(\).*run-dir != ''", step_region), (
        "job-summary step must be guarded by always() and by a non-empty "
        "steps.pipeline.outputs.run-dir (#249)"
    )


def test_docs_whats_new_070_job_summary_claims_are_true() -> None:
    """docs/whats-new-0.7.0.md must no longer claim a summary wiring that did
    not exist; after #249 the claims describe what the Action actually does."""
    docs = _read("docs/whats-new-0.7.0.md")
    action = _read("action.yml")

    assert "$GITHUB_STEP_SUMMARY" in action, "precondition: action.yml wires the summary"

    line42_claim = re.search(r"Pipe it to `\$GITHUB_STEP_SUMMARY` in CI \(the Action does this for you\)", docs)
    assert not line42_claim, (
        "docs/whats-new-0.7.0.md still says 'the Action does this for you' as an "
        "unconditional fact; it must now be accurate post-fix wording"
    )
    assert "come along for free" not in docs, (
        "docs/whats-new-0.7.0.md still claims 'Artifacts and the job summary come "
        "along for free' — replace with what the Action actually does (#249)"
    )


def test_docs_do_not_still_promise_free_job_summary() -> None:
    """The Step 5 section must describe the summary as written by the Action
    only in terms that match action.yml's actual behavior."""
    docs = _read("docs/whats-new-0.7.0.md")
    action = _read("action.yml")
    # If docs mention GITHUB_STEP_SUMMARY at all, the Action must really write it.
    if "$GITHUB_STEP_SUMMARY" in docs:
        assert "GITHUB_STEP_SUMMARY" in action, (
            "docs reference $GITHUB_STEP_SUMMARY but action.yml does not write it (#249)"
        )
