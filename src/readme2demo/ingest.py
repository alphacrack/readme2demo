"""M1 — Ingest & Planner.

Turns a repo URL into a structured ``plan.json`` before any agent spins up:

1. Shallow-clone the repo (host side; URL is validated first — remote
   ``https://github.com/...`` / ``https://gitlab.com/...`` only in MVP).
2. Collect candidate docs (README*, ``docs/**/*.md``) and a file inventory
   with ecosystem markers.
3. Run a single planner LLM pass that emits a :class:`~readme2demo.types.Plan`
   with machine-checkable success criteria and a feasibility verdict.

The feasibility gate lives here because it is the cheapest place to fail: if
the quickstart needs credentials, GPUs, or a GUI, we bail for pennies instead
of burning an agent run.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from readme2demo import llm
from readme2demo.types import Plan, UrlVerdict

_URL_RE = re.compile(
    r"^https://(?:github|gitlab)\.com/"  # allowed hosts only (MVP)
    r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+/?$"  # owner/repo (+ gitlab subgroups)
)

_ECOSYSTEM_MARKERS: tuple[str, ...] = (
    "package.json",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    "Dockerfile",
    "Makefile",
    "docker-compose.yml",
)

_MAX_TOP_LEVEL_ENTRIES = 100
_MAX_EXAMPLES_ENTRIES = 30

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


class IngestError(RuntimeError):
    """Raised when cloning or planning cannot proceed (bad URL, git failure)."""



# Hosts we treat as cloneable git forges in the MVP. Keep in sync with _URL_RE.
_GIT_HOSTS = ("github.com", "gitlab.com")
# Path segments that indicate a browser deep-link into a repo (not extra GitLab groups).
_GIT_DEEP_LINK_MARKERS = frozenset({"tree", "blob", "commit", "pulls", "pull", "issues", "wiki", "releases", "actions", "settings", "projects", "network", "security", "pulse", "graphs"})


def classify_url(url: str) -> UrlVerdict:
    """Classify *url* as a git repository, hosted docs page, or unsupported input.

    Pure string logic only — no network, filesystem, or subprocess. Intended as
    the vocabulary for docs-site ingestion (#67); not yet wired into the pipeline.
    """
    raw = (url or "").strip()
    if not raw:
        return UrlVerdict(kind="unsupported", reason="empty URL")

    # Local paths / file URLs — unsupported here; local acceptance is #74.
    if raw.startswith("file:") or raw.startswith("/") or raw.startswith("."):
        return UrlVerdict(kind="unsupported", reason="local path is not supported")

    # SSH remotes are not accepted by the MVP clone path.
    if raw.startswith("git@") or raw.startswith("ssh://"):
        return UrlVerdict(kind="unsupported", reason="ssh remotes are not supported — use https")

    # Reject embedded-URL smuggling early (must never be classified as git).
    lower = raw.casefold()
    if lower.count("://") > 1 or "https://" in lower[8:]:
        return UrlVerdict(kind="unsupported", reason="URL contains an embedded URL and is not a plain git host path")

    from urllib.parse import urlparse

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").casefold()
    if scheme == "http":
        return UrlVerdict(kind="unsupported", reason="plain http is not supported — use https")
    if scheme != "https":
        return UrlVerdict(kind="unsupported", reason=f"unsupported scheme {parsed.scheme!r}")

    host = (parsed.hostname or "").casefold()
    if host.startswith("www."):
        host = host[4:]

    # GitHub Pages sites are docs, not cloneable repos.
    if host.endswith(".github.io"):
        return UrlVerdict(kind="docs", reason="GitHub Pages site")

    path = (parsed.path or "").strip("/")
    # Drop trailing .git for matching
    path_no_git = path[:-4] if path.endswith(".git") else path
    segments = [s for s in path_no_git.split("/") if s]

    if host in _GIT_HOSTS:
        if len(segments) < 2:
            return UrlVerdict(
                kind="unsupported",
                reason="no owner/repo path segment",
            )
        # Reduce deep links to owner/repo root. GitLab subgroups keep extra
        # segments until a known deep-link marker appears.
        owner_repo: list[str] = []
        for i, seg in enumerate(segments):
            if seg in _GIT_DEEP_LINK_MARKERS and i >= 2:
                break
            owner_repo.append(seg)
        if len(owner_repo) < 2:
            return UrlVerdict(kind="unsupported", reason="no owner/repo path segment")
        repo_url = f"https://{host}/{'/'.join(owner_repo)}"
        return UrlVerdict(kind="git", repo_url=repo_url, reason="cloneable repository URL")

    # Any other https URL is treated as a potential docs page for this slice.
    return UrlVerdict(kind="docs", reason="non-git https URL treated as docs page")


def clone_repo(repo_url: str, dest: Path, timeout: int = 300) -> str:
    """Shallow-clone ``repo_url`` into ``dest`` and return the HEAD commit sha.

    The URL must match ``https://github.com/...`` or ``https://gitlab.com/...``
    (local paths, ssh remotes, and plain-http URLs are rejected with
    :class:`IngestError` before any subprocess runs).
    """
    if not _URL_RE.match(repo_url):
        raise IngestError(
            f"Invalid repo URL: {repo_url!r} — expected "
            "https://github.com/<owner>/<repo> or https://gitlab.com/<owner>/<repo>"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dest)],
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        raise IngestError(f"git clone timed out after {timeout}s: {repo_url}") from e
    except FileNotFoundError as e:
        raise IngestError("git not found — install git and ensure it is on PATH") from e
    if proc.returncode != 0:
        raise IngestError(
            f"git clone failed ({proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}"
        )
    rev = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=60,
        errors="replace",
    )
    if rev.returncode != 0:
        raise IngestError(f"git rev-parse failed: {rev.stderr.strip()}")
    return rev.stdout.strip()


_GUIDE_NAMES = ("step_by_step.md", "step-by-step.md")


def find_step_by_step(repo_dir: Path) -> Path | None:
    """Locate a repo-provided step-by-step guide, if any.

    Checked (case-insensitively): ``step_by_step.md`` / ``step-by-step.md`` at
    the repo root, then under ``docs/``. First hit wins. The guide is
    OPTIONAL — None simply means the README drives the plan.
    """
    for base in (repo_dir, repo_dir / "docs"):
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if p.is_file() and p.name.lower() in _GUIDE_NAMES:
                return p
    return None


def _candidate_doc_files(repo_dir: Path) -> list[Path]:
    """Guide (if any) first — it is authoritative — then READMEs, then docs/**/*.md."""
    guide = find_step_by_step(repo_dir)
    readmes = sorted(
        (
            p
            for p in repo_dir.iterdir()
            if p.is_file() and p.name.upper().startswith("README")
        ),
        key=lambda p: p.name.lower(),
    )
    docs = sorted(
        (p for p in repo_dir.glob("docs/**/*.md") if p.is_file()),
        key=lambda p: p.relative_to(repo_dir).as_posix(),
    )
    ordered = ([guide] if guide else []) + readmes + docs
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in ordered:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def collect_docs(repo_dir: Path, max_bytes: int = 50_000) -> str:
    """Concatenate candidate docs with ``--- FILE: <relpath> ---`` headers.

    A step-by-step guide in the repo directory comes first when present,
    followed by READMEs, then ``docs/**/*.md`` in sorted order. Total output is
    capped at ``max_bytes`` (UTF-8): the file that would exceed the budget is
    cut at a character boundary and closed with a ``[truncated]`` marker; any
    remaining files are dropped.
    """
    marker = "\n[truncated]\n"
    marker_bytes = len(marker.encode("utf-8"))
    guide = find_step_by_step(repo_dir)
    parts: list[str] = []
    used = 0
    for path in _candidate_doc_files(repo_dir):
        rel = path.relative_to(repo_dir).as_posix()
        if guide is not None and path == guide:
            header = f"--- FILE: {rel} (AUTHORITATIVE STEP-BY-STEP GUIDE) ---\n"
        else:
            header = f"--- FILE: {rel} ---\n"
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not content.endswith("\n"):
            content += "\n"
        segment = header + content + "\n"
        seg_bytes = len(segment.encode("utf-8"))
        if used + seg_bytes <= max_bytes:
            parts.append(segment)
            used += seg_bytes
            continue
        # This file blows the budget: truncate it cleanly and stop.
        budget = max_bytes - used - len(header.encode("utf-8")) - marker_bytes
        if budget > 0:
            cut = content.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
            parts.append(header + cut + marker)
        break
    return "".join(parts)


def collect_inventory(repo_dir: Path) -> dict:
    """Summarize the repo layout for the planner.

    Returns a dict with:

    - ``top_level_files``: sorted top-level entry names (dirs get a trailing
      ``/``), capped at 100.
    - ``markers``: presence booleans for common ecosystem files
      (package.json, pyproject.toml, go.mod, Dockerfile, ...).
    - ``examples``: sorted file listing under ``examples/`` if that directory
      exists (relative paths, capped at 30), else an empty list.
    """
    top_level = sorted(
        (p.name + "/" if p.is_dir() else p.name) for p in repo_dir.iterdir()
    )[:_MAX_TOP_LEVEL_ENTRIES]
    markers = {name: (repo_dir / name).is_file() for name in _ECOSYSTEM_MARKERS}
    examples: list[str] = []
    examples_dir = repo_dir / "examples"
    if examples_dir.is_dir():
        examples = sorted(
            p.relative_to(examples_dir).as_posix()
            for p in examples_dir.rglob("*")
            if p.is_file()
        )[:_MAX_EXAMPLES_ENTRIES]
    return {
        "top_level_files": top_level,
        "markers": markers,
        "examples": examples,
    }


def run_planner(docs: str, inventory: dict, model: str) -> tuple[Plan, float]:
    """Single planner LLM pass: docs + inventory in, validated Plan out.

    Loads ``prompts/planner.md`` as the system prompt and delegates JSON
    parsing/validation (with self-correction retries) to
    :func:`readme2demo.llm.complete_json`. Returns ``(plan, cost_usd)``.
    """
    system = (_PROMPTS_DIR / "planner.md").read_text(encoding="utf-8")
    user = (
        "## Repository documentation\n\n"
        f"{docs if docs.strip() else '(no README or docs found)'}\n\n"
        "## File inventory\n\n"
        f"```json\n{json.dumps(inventory, indent=2)}\n```\n\n"
        "Respond with ONLY the JSON plan object."
    )
    return llm.complete_json(system=system, user=user, model=model, schema=Plan)


def ingest(
    repo_url: str | None,
    run_dir: Path,
    model: str,
    guide_file: Path | None = None,
) -> tuple[Plan, str, float]:
    """Run the full M1 stage: clone (if any), collect, plan, write ``plan.json``.

    Writes ``run_dir / "plan.json"`` unconditionally — even when
    ``plan.feasible`` is False, the plan (with its blockers) is persisted so
    the orchestrator can decide what to do.

    ``repo_url`` is OPTIONAL. When given, the repo is shallow-cloned into
    ``run_dir / "repo"``. When empty/None this is a *guide-only* run: no clone
    happens and the ``-s/--step-by-step`` guide is the sole source (it must be
    self-contained — install a published package, or clone whatever it needs
    as an explicit guide step). The returned commit sha is then ``""``.

    ``guide_file`` (the CLI's ``-s/--step-by-step``) is copied into
    ``run_dir / "repo"`` as ``step_by_step.md`` before planning, overriding any
    guide the repo ships — from there it flows through the normal
    authoritative-guide path. At least one of ``repo_url`` / a guide is
    required.

    Returns ``(plan, commit_sha, cost_usd)``.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = run_dir / "repo"
    if repo_url:
        commit_sha = clone_repo(repo_url, repo_dir)
    else:
        # Guide-only run: nothing to clone. The step-by-step guide drives
        # everything; the fresh-container replay in verify still grounds it.
        repo_dir.mkdir(parents=True, exist_ok=True)
        commit_sha = ""
    if guide_file is not None:
        if not guide_file.is_file():
            raise IngestError(f"--step-by-step file not found: {guide_file}")
        (repo_dir / "step_by_step.md").write_text(
            guide_file.read_text(encoding="utf-8", errors="replace"),
            encoding="utf-8",
        )
    if not repo_url and find_step_by_step(repo_dir) is None:
        # No repo AND no guide (nor a guide carried over from a prior ingest on
        # resume): there is literally nothing to build a plan from.
        raise IngestError(
            "Nothing to ingest: no repository URL and no step-by-step guide. "
            "Pass a repo (-gr/--github-repo) or a guide (-s/--step-by-step)."
        )
    docs = collect_docs(repo_dir)
    inventory = collect_inventory(repo_dir)
    plan, cost_usd = run_planner(docs, inventory, model)
    # guide_path is ground truth from the filesystem, never trusted from the LLM.
    guide = find_step_by_step(repo_dir)
    plan.guide_path = guide.relative_to(repo_dir).as_posix() if guide else None
    (run_dir / "plan.json").write_text(
        plan.model_dump_json(indent=2), encoding="utf-8"
    )
    return plan, commit_sha, cost_usd
