"""Unit tests for the M1 ingest stage. No network, no docker, no API calls."""

from pathlib import Path

import pytest

from readme2demo import ingest as ingest_mod
from readme2demo import llm
from readme2demo.ingest import (
    IngestError,
    clone_repo,
    collect_docs,
    collect_inventory,
    ingest,
    run_planner,
)
from readme2demo.types import Plan, SuccessCriteria


def _canned_plan() -> Plan:
    return Plan(
        project_type="python-cli",
        quickstart_summary="pip install then run the CLI on its own README",
        prereqs=["python>=3.10"],
        steps_expected=["install", "run example"],
        success_criteria=SuccessCriteria(
            command="wordcount README.md",
            expected_pattern=r"\d+ words",
            description="CLI prints a word-count summary",
        ),
        blockers=[],
        feasible=True,
        reasoning="Simple pip-installable CLI.",
    )


# -- clone_repo URL validation -------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://github.com/owner/repo",  # plain http
        "git@github.com:owner/repo.git",  # ssh
        "ssh://git@github.com/owner/repo.git",
        "/local/path/to/repo",  # local path
        "file:///tmp/repo",
        "https://bitbucket.org/owner/repo",  # unsupported host
        "https://github.com/",  # no owner/repo
        "https://github.com/owner-only",
        "https://evil.example/https://github.com/owner/repo",
        "",
    ],
)
def test_clone_repo_rejects_bad_urls(bad_url: str, tmp_path: Path) -> None:
    with pytest.raises(IngestError):
        clone_repo(bad_url, tmp_path / "repo")
    assert not (tmp_path / "repo").exists()  # rejected before any git call


# -- collect_docs --------------------------------------------------------------


def _make_fake_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "README.md").write_text("# Hello\n\nQuickstart: run it.\n")
    (repo / "docs" / "advanced.md").write_text("Advanced usage notes.\n")
    (repo / "docs" / "basics.md").write_text("Basic usage notes.\n")
    return repo


def test_collect_docs_readme_first_with_headers(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    out = collect_docs(repo)
    assert out.startswith("--- FILE: README.md ---\n# Hello")
    # docs/*.md follow, in sorted order
    assert "--- FILE: docs/advanced.md ---" in out
    assert "--- FILE: docs/basics.md ---" in out
    assert out.index("docs/advanced.md") < out.index("docs/basics.md")
    assert "[truncated]" not in out


def test_collect_docs_finds_readme_any_case(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "readme.rst").write_text("plain rst readme\n")
    out = collect_docs(repo)
    assert out.startswith("--- FILE: readme.rst ---\nplain rst readme")


def test_collect_docs_respects_max_bytes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("A" * 500 + "\n")
    max_bytes = 120
    out = collect_docs(repo, max_bytes=max_bytes)
    assert len(out.encode("utf-8")) <= max_bytes
    assert out.startswith("--- FILE: README.md ---\n")
    assert out.rstrip().endswith("[truncated]")


def test_collect_docs_drops_later_files_when_budget_spent(tmp_path: Path) -> None:
    repo = _make_fake_repo(tmp_path)
    readme_only = collect_docs(repo, max_bytes=60)
    assert "README.md" in readme_only
    assert "docs/" not in readme_only


def test_collect_docs_empty_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert collect_docs(repo) == ""


# -- collect_inventory ---------------------------------------------------------


def test_collect_inventory_detects_markers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (repo / "package.json").write_text("{}\n")
    (repo / "main.py").write_text("print('hi')\n")
    inv = collect_inventory(repo)
    assert inv["markers"]["pyproject.toml"] is True
    assert inv["markers"]["package.json"] is True
    assert inv["markers"]["go.mod"] is False
    assert inv["markers"]["Dockerfile"] is False
    assert "main.py" in inv["top_level_files"]
    assert inv["examples"] == []


def test_collect_inventory_lists_examples_and_marks_dirs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "examples" / "nested").mkdir(parents=True)
    (repo / "examples" / "hello.py").write_text("print('hello')\n")
    (repo / "examples" / "nested" / "deep.py").write_text("pass\n")
    inv = collect_inventory(repo)
    assert "examples/" in inv["top_level_files"]
    assert inv["examples"] == ["hello.py", "nested/deep.py"]


def test_collect_inventory_caps_listings(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    examples = repo / "examples"
    examples.mkdir(parents=True)
    for i in range(120):
        (repo / f"file_{i:03d}.txt").write_text("x")
    for i in range(40):
        (examples / f"ex_{i:03d}.py").write_text("pass")
    inv = collect_inventory(repo)
    assert len(inv["top_level_files"]) == 100
    assert len(inv["examples"]) == 30


# -- run_planner (LLM monkeypatched) -------------------------------------------


def test_run_planner_calls_complete_json_with_prompt_and_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _canned_plan()
    captured: dict = {}

    def fake_complete_json(system, user, model, schema, **kwargs):
        captured.update(system=system, user=user, model=model, schema=schema)
        return plan, 0.0123

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)

    docs = "--- FILE: README.md ---\n# wordcount\n"
    inventory = {"top_level_files": ["README.md"], "markers": {}, "examples": []}
    result, cost = run_planner(docs, inventory, model="test-model")

    assert result is plan
    assert cost == 0.0123
    assert captured["schema"] is Plan
    assert captured["model"] == "test-model"
    # System prompt is prompts/planner.md
    assert "success_criteria" in captured["system"]
    assert "feasible" in captured["system"]
    # User message embeds docs and inventory JSON
    assert docs in captured["user"]
    assert '"top_level_files"' in captured["user"]


# -- ingest orchestration (clone + LLM monkeypatched) ---------------------------


def test_ingest_writes_plan_json(
    monkeypatch: pytest.MonkeyPatch, tmp_run_dir: Path
) -> None:
    plan = _canned_plan()
    sha = "a" * 40

    def fake_clone(repo_url: str, dest: Path, timeout: int = 300) -> str:
        dest.mkdir(parents=True)
        (dest / "README.md").write_text("# demo\n")
        (dest / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
        return sha

    monkeypatch.setattr(ingest_mod, "clone_repo", fake_clone)
    monkeypatch.setattr(llm, "complete_json", lambda **kw: (plan, 0.02))

    got_plan, got_sha, cost = ingest(
        "https://github.com/owner/demo", tmp_run_dir, model="test-model"
    )

    assert got_plan is plan
    assert got_sha == sha
    assert cost == 0.02
    plan_path = tmp_run_dir / "plan.json"
    assert plan_path.exists()
    assert Plan.model_validate_json(plan_path.read_text()) == plan


def test_ingest_writes_plan_json_even_when_infeasible(
    monkeypatch: pytest.MonkeyPatch, tmp_run_dir: Path
) -> None:
    blocked = _canned_plan().model_copy(
        update={"feasible": False, "blockers": ["requires OPENAI_API_KEY"]}
    )

    def fake_clone(repo_url: str, dest: Path, timeout: int = 300) -> str:
        dest.mkdir(parents=True)
        (dest / "README.md").write_text("# demo\n")
        return "b" * 40

    monkeypatch.setattr(ingest_mod, "clone_repo", fake_clone)
    monkeypatch.setattr(llm, "complete_json", lambda **kw: (blocked, 0.01))

    got_plan, _, _ = ingest(
        "https://github.com/owner/demo", tmp_run_dir, model="test-model"
    )

    assert got_plan.feasible is False
    written = Plan.model_validate_json((tmp_run_dir / "plan.json").read_text())
    assert written.blockers == ["requires OPENAI_API_KEY"]


# -- step_by_step.md guide support ------------------------------------------------


def _guide_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# readme\nquickstart here\n")
    (repo / "step_by_step.md").write_text("# guide\n1. do this\n2. do that\n")
    return repo


def test_find_step_by_step_at_root(tmp_path):
    from readme2demo.ingest import find_step_by_step

    repo = _guide_repo(tmp_path)
    assert find_step_by_step(repo).name == "step_by_step.md"


def test_find_step_by_step_case_and_dash_variants(tmp_path):
    from readme2demo.ingest import find_step_by_step

    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "Step-By-Step.md").write_text("guide")
    assert find_step_by_step(repo) is not None


def test_find_step_by_step_absent_is_none(tmp_path):
    from readme2demo.ingest import find_step_by_step

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("readme")
    assert find_step_by_step(repo) is None


def test_collect_docs_guide_first_and_marked(tmp_path):
    from readme2demo.ingest import collect_docs

    repo = _guide_repo(tmp_path)
    docs = collect_docs(repo)
    assert docs.index("step_by_step.md") < docs.index("README.md")
    assert "(AUTHORITATIVE STEP-BY-STEP GUIDE)" in docs


def test_agent_prompt_includes_guide_note(tmp_path):
    from pathlib import Path as P

    from readme2demo.agent import render_agent_prompt
    from readme2demo.types import Plan, SuccessCriteria

    template = P("src/readme2demo/prompts/agent.md")
    plan = Plan(
        quickstart_summary="follow the guide",
        success_criteria=SuccessCriteria(command="./run.sh"),
        guide_path="step_by_step.md",
    )
    rendered = render_agent_prompt(plan, template)
    assert "authoritative step-by-step guide at `step_by_step.md`" in rendered
    plan.guide_path = None
    rendered = render_agent_prompt(plan, template)
    assert "{{guide_note}}" not in rendered
    assert "authoritative step-by-step guide" not in rendered


def test_ingest_injects_user_guide(tmp_path, monkeypatch):
    """-s/--step-by-step: the file lands in the clone and wins as guide_path."""
    from readme2demo import ingest as ingest_mod
    from readme2demo import llm
    from readme2demo.types import Plan, SuccessCriteria

    user_guide = tmp_path / "my_guide.md"
    user_guide.write_text("# my guide\n```bash\n./run.sh\n```\n")

    def fake_clone(repo_url, dest, timeout=300):
        dest.mkdir(parents=True)
        (dest / "README.md").write_text("# readme")
        return "abc1234"

    def fake_complete_json(system, user, model, schema, **kw):
        assert "AUTHORITATIVE STEP-BY-STEP GUIDE" in user
        assert "my guide" in user
        return (
            Plan(
                quickstart_summary="q",
                success_criteria=SuccessCriteria(command="./run.sh"),
            ),
            0.01,
        )

    monkeypatch.setattr(ingest_mod, "clone_repo", fake_clone)
    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    run_dir = tmp_path / "run"
    plan, sha, cost = ingest_mod.ingest(
        "https://github.com/x/y", run_dir, "m", guide_file=user_guide
    )
    assert plan.guide_path == "step_by_step.md"
    assert (run_dir / "repo" / "step_by_step.md").read_text().startswith("# my guide")


# -- guide-only runs (repo made optional) ----------------------------------------


def test_ingest_guide_only_skips_clone(tmp_path, monkeypatch):
    """No repo URL: ingest must NOT clone; the -s guide becomes step_by_step.md
    and drives the plan. Returns an empty commit sha."""
    from readme2demo import ingest as ingest_mod
    from readme2demo import llm
    from readme2demo.types import Plan, SuccessCriteria

    user_guide = tmp_path / "my_guide.md"
    user_guide.write_text("# my guide\n```bash\npip install cowsay\ncowsay hi\n```\n")

    def boom_clone(*a, **k):
        raise AssertionError("clone_repo must not run for a guide-only ingest")

    def fake_complete_json(system, user, model, schema, **kw):
        assert "AUTHORITATIVE STEP-BY-STEP GUIDE" in user
        return (
            Plan(
                quickstart_summary="q",
                success_criteria=SuccessCriteria(command="cowsay hi"),
            ),
            0.01,
        )

    monkeypatch.setattr(ingest_mod, "clone_repo", boom_clone)
    monkeypatch.setattr(llm, "complete_json", fake_complete_json)

    run_dir = tmp_path / "run"
    plan, sha, cost = ingest_mod.ingest(None, run_dir, "m", guide_file=user_guide)
    assert sha == ""
    assert plan.guide_path == "step_by_step.md"
    assert (run_dir / "repo" / "step_by_step.md").read_text().startswith("# my guide")


def test_ingest_no_repo_and_no_guide_raises(tmp_path, monkeypatch):
    """Neither a repo nor a guide: nothing to ingest -> IngestError (no clone)."""
    from readme2demo import ingest as ingest_mod

    def boom_clone(*a, **k):
        raise AssertionError("clone_repo must not run when there is no repo URL")

    monkeypatch.setattr(ingest_mod, "clone_repo", boom_clone)
    with pytest.raises(IngestError, match="Nothing to ingest"):
        ingest_mod.ingest(None, tmp_path / "run", "m", guide_file=None)
