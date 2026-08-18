"""OpenHands headless engine (opt-in, experimental).

Runs OpenHands headless mode (``python -m openhands.core.main``) inside *our*
sandbox — RUNTIME=local bypasses OpenHands' own runtime-sandbox logic and
treats it like any process in the container (one sandbox model, not two).
Selected via ``--engine openhands`` or any provider preset (``--gemini`` /
``--openai`` / ``--anthropic``, which fill the litellm-style ``LLM_API_KEY``
and ``LLM_MODEL`` from the provider's key); needs the
``readme2demo/openhands`` image (pinned OpenHands 0.48 — see
images/openhands/Dockerfile before bumping).

.. warning::
   OpenHands support is **experimental**. The trajectory schema has drifted
   between OpenHands releases and may drift again; this parser is tolerant by
   design — it detects both a JSON array and JSONL layouts, skips events it
   does not recognize, and falls back to sensible defaults for missing fields.

Trajectory shapes this parser understands:

* Actions: ``{"action": "run", "args": {"command": ...}}`` — a shell command.
* Observations: ``{"observation": "run", "content": ...,
  "extras": {"exit_code": int}}`` — the paired output. Actions and
  observations are paired in file order.
* Message actions: ``{"action": "message", "args": {"content": ...}}`` (or a
  top-level ``"content"``/``"message"`` field) — scanned for FIX/BLOCKED/
  SUCCESS markers.
* Finish actions: ``{"action": "finish", ...}`` — OpenHands' NATIVE completion
  signal and the other place those same markers arrive; scanned exactly like
  an agent message. See :func:`_finish_text` for the shapes it can take.
* File edits: ``{"action": "write" | "edit", "args": {"path": ...}}``.

There is no terminal ``result`` event in a trajectory, so cost and turn count
are ``None`` and the outcome is derived purely from markers.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Optional

from readme2demo.engines.base import (
    PROMPT_CONTAINER_PATH,
    TRANSCRIPT_CONTAINER_PATH,
    AgentEngine,
    Limits,
    register,
)
from readme2demo.engines.claude_code import scan_adjusted, scan_markers, truncate_output
from readme2demo.types import (
    SUCCESS_MARKER,
    AgentResult,
    CommandEntry,
    CommandLog,
    FixMarker,
    Outcome,
)

# Action names whose args.path counts as a file edit.
_FILE_EDIT_ACTIONS = frozenset({"write", "edit"})

# Actions whose agent-authored text carries FIX/BLOCKED/ADJUSTED/SUCCESS
# markers. ``finish`` is OpenHands' native "I'm done" action: an agent that
# follows the prompt exactly may deliver R2D_SUCCESS through it instead of
# printing it to a shell, so it is a marker source too (failure class 17).
_MARKER_ACTIONS = frozenset({"message", "finish"})

# Depth cap for the tolerant walks below. The deepest real shape seen is the
# finish tool call at event -> tool_call_metadata -> model_response ->
# choices -> [i] -> message -> tool_calls (6 levels); the cap keeps a
# pathological or cyclic-looking payload from costing anything.
_MAX_SCAN_DEPTH = 8


def _load_events(raw: str) -> list[dict[str, Any]]:
    """Load trajectory events from either a JSON array or JSONL text.

    Returns an empty list rather than raising when the file is unparseable —
    the caller reports a 'failed' outcome with zero entries, which the
    orchestrator surfaces clearly.
    """
    stripped = raw.strip()
    if not stripped:
        return []
    # Whole-file JSON first (the documented --save-trajectory-path format is
    # a single JSON array).
    try:
        data = json.loads(stripped)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
        if isinstance(data, dict):
            # Some versions wrap the list, e.g. {"events": [...]}.
            for value in data.values():
                if isinstance(value, list):
                    return [e for e in value if isinstance(e, dict)]
            return [data]
    except (json.JSONDecodeError, ValueError):
        pass
    # Fall back to JSONL: one event per line, skip malformed lines.
    events: list[dict[str, Any]] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _message_text(event: dict[str, Any]) -> str:
    """Best-effort extraction of human-readable text from a message event."""
    args = event.get("args")
    if isinstance(args, dict):
        content = args.get("content")
        if isinstance(content, str):
            return content
    for key in ("content", "message"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    return ""


def _iter_strings(node: Any, depth: int = 0) -> Iterator[str]:
    """Yield every string reachable inside a nested dict/list payload.

    Deliberately schema-free: OpenHands has moved the finish text between
    ``final_thought``, ``message`` and ``thought`` across releases, so the
    parser reads whatever strings the payload carries rather than betting on
    one key name. Pure; depth-limited by :data:`_MAX_SCAN_DEPTH`.
    """
    if depth > _MAX_SCAN_DEPTH:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_strings(value, depth + 1)


def _iter_tool_calls(node: Any, depth: int = 0) -> Iterator[dict[str, Any]]:
    """Yield tool-call dicts found under any ``tool_calls`` list in ``node``.

    The list sits at the top level of the event in some trajectory versions
    and inside ``tool_call_metadata.model_response.choices[].message`` in
    others (the shape OpenHands 0.48 writes) — searching for the key instead
    of a fixed path survives both.
    """
    if depth > _MAX_SCAN_DEPTH:
        return
    if isinstance(node, dict):
        calls = node.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                if isinstance(call, dict):
                    yield call
        for value in node.values():
            yield from _iter_tool_calls(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_tool_calls(value, depth + 1)


def _tool_call_arguments(call: dict[str, Any], named_finish: bool) -> Optional[Any]:
    """Return a ``finish`` tool call's arguments payload, else None.

    Handles the nested (``{"function": {"name", "arguments"}}``) and flat
    (``{"name", "arguments"}``) shapes; ``arguments`` may be a JSON string or
    an already-parsed dict, and an unparseable string is returned as-is so a
    marker inside it is still visible to a substring scan.

    Identifying the tool is default-DENY: only a call that names ``finish``
    is read, because another call's arguments are a command the agent
    *intends* to run (``echo R2D_SUCCESS``), not a claim about the outcome.
    An UNNAMED call is read only when ``named_finish`` says the event itself
    is anchored to the finish tool (``tool_call_metadata.function_name``) —
    that keeps streamed/partial tool calls, where the name arrives in an
    earlier delta than the arguments, working without trusting an anonymous
    payload on its own.
    """
    function = call.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        arguments = function.get("arguments")
    else:
        name = call.get("name")
        arguments = call.get("arguments", call.get("args"))
    if isinstance(name, str):
        if name != "finish":
            return None
    elif not named_finish:
        return None
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except (json.JSONDecodeError, ValueError):
            return arguments  # drifted/partial JSON — scan the raw text
    if isinstance(arguments, (dict, list)):
        return arguments
    return None


def _finish_text(event: dict[str, Any]) -> str:
    """Collect every agent-authored string a ``finish`` action carries.

    A ``finish`` action is OpenHands' native completion signal, and its text
    lives in a different place in almost every version/provider combination:

    * ``event["message"]`` — the human-readable summary;
    * ``event["args"]`` — ``final_thought`` (0.48), ``message``, ``thought``;
    * the ``arguments`` of the ``finish`` tool call, as a JSON string or an
      already-parsed dict, either at the top of the event or nested in
      ``tool_call_metadata.model_response``.

    All of them are read and joined, so a marker in any one is seen. The
    agent's own ``task_completed`` flag rides along in the text as
    corroboration; it never grounds success by itself — see
    :meth:`OpenHandsEngine.parse_transcript`.
    """
    parts: list[str] = []
    message = event.get("message")
    if isinstance(message, str):
        parts.append(message)
    parts.extend(_iter_strings(event.get("args")))
    metadata = event.get("tool_call_metadata")
    named_finish = (
        isinstance(metadata, dict) and metadata.get("function_name") == "finish"
    )
    for call in _iter_tool_calls(event):
        arguments = _tool_call_arguments(call, named_finish)
        if arguments is not None:
            parts.extend(_iter_strings(arguments))
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            unique.append(part)
    return "\n".join(unique)


@register
class OpenHandsEngine(AgentEngine):
    """OpenHands headless mode inside the sandbox (experimental)."""

    name = "openhands"

    # The standard base image has neither OpenHands nor a `python` alias — a
    # run against it dies with a bare exit 127 and no transcript. This image
    # (images/openhands/Dockerfile) bakes in the pinned 0.x runtime; the CLI
    # uses it automatically for this engine unless base_image is set
    # explicitly, and check_image probes for it at preflight.
    default_image = "readme2demo/openhands:latest"

    def required_env(self) -> list[str]:
        return ["LLM_API_KEY", "LLM_MODEL"]

    def credential_env_vars(self) -> set[str]:
        # DEFENSIVE override: the fail-closed default already yields exactly
        # {"LLM_API_KEY"} today (LLM_MODEL matches nothing). This pins the
        # intent — LLM_MODEL is config, never a credential — and guards a
        # future rename of the key var that the name-regex might not catch.
        return {"LLM_API_KEY"}

    def resolve_env(self) -> dict[str, str]:
        """Collect litellm-style credentials, with preset guidance on failure.

        The provider presets (``--gemini`` / ``--openai`` / ``--anthropic``)
        normally fill these from the provider's own key before this runs, so a
        miss here usually means bare ``--engine openhands`` without them.
        """
        import os

        from readme2demo.engines.base import EngineError

        missing = [k for k in self.required_env() if not os.environ.get(k)]
        if missing:
            raise EngineError(
                f"Engine 'openhands' needs litellm-style credentials; not set: "
                f"{', '.join(missing)}. Use a provider preset (--gemini / "
                "--openai / --anthropic) to fill them from GEMINI_API_KEY / "
                "OPENAI_API_KEY / ANTHROPIC_API_KEY, or export LLM_API_KEY and "
                "LLM_MODEL (e.g. LLM_MODEL=openai/gpt-5.1) yourself."
            )
        return {k: os.environ[k] for k in self.required_env()}

    def check_image(self, image: str) -> None:
        """Fail fast when ``image`` can't import the OpenHands 0.x runtime.

        Docker-level problems (docker missing, daemon down) are NOT reported
        here — the docker preflight check owns those; this probe only speaks
        up when docker ran the image and OpenHands wasn't importable.
        """
        import subprocess

        from readme2demo.engines.base import EngineError

        try:
            proc = subprocess.run(
                [
                    "docker", "run", "--rm", "--entrypoint", "bash", image,
                    "-lc", "openhands-python -c 'import openhands.core.main'",
                ],
                capture_output=True, text=True, timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return  # docker itself is missing/wedged — reported elsewhere
        if proc.returncode == 0:
            return
        detail = (proc.stderr or proc.stdout or "").strip()
        if "docker daemon" in detail.lower() or "docker api" in detail.lower():
            return  # daemon down, not an image problem — docker's own check owns it
        raise EngineError(
            f"Sandbox image {image!r} can't run OpenHands "
            f"(probe: {detail[-300:] or 'import failed'}).\n"
            "Build the OpenHands sandbox image and retry:\n"
            "  docker build -t readme2demo/base:latest images/base\n"
            "  docker build -t readme2demo/openhands:latest images/openhands\n"
            "It is the default image for --engine openhands and the provider "
            "presets (--gemini / --openai / --anthropic); pass --base-image "
            "to use a different one."
        )

    def build_command(self, limits: Limits) -> str:
        # openhands-python: the image's /usr/local/bin symlink to the venv —
        # NOT `python3` (the distro python has no OpenHands, and a PATH
        # prepend is wiped by `bash -lc` sourcing /etc/profile). RUNTIME=local
        # executes actions directly in THIS sandbox — without it OpenHands
        # tries to manage its own Docker runtime, which cannot work inside
        # the hardened container. SAVE_TRAJECTORY_PATH is an env var, not a
        # flag: the pinned 0.48 CLI has no --save-trajectory-path (that flag
        # arrived in later, x86_64-only releases); both map onto the same
        # OpenHandsConfig field via load_from_env. stdout+stderr go to
        # agent.stderr so a failure surfaces in AgentRunError, not "(empty)".
        transcript_dir = posixpath.dirname(TRANSCRIPT_CONTAINER_PATH)
        stderr_path = posixpath.join(transcript_dir, "agent.stderr")
        env_prefix = (
            # docker never sets USER; unset, OpenHands' get_user_info falls
            # back to username 'openhands' and BashSession then runs
            # `su openhands -` — a nonexistent user, and `su` is impossible
            # under cap-drop ALL anyway. The current (non-root) user is
            # always right in this one-sandbox model.
            'USER="$(id -un)" '
            "RUNTIME=local "
            # LocalRuntime's dependency check probes the OpenHands DEV repo
            # layout (poetry project); this venv install isn't one. The bits
            # the check guards (tmux, kernel gateway) are baked into the
            # image, so skipping is safe.
            "SKIP_DEPENDENCY_CHECK=1 "
            # Where the agent's actions execute; /work holds the repo copy.
            # Unset, OpenHands runs in a fresh temp dir and the agent would
            # find an empty workspace.
            "WORKSPACE_BASE=/work "
            # No playwright browsers in the image (and headless chromium is
            # unlikely to survive the hardened sandbox); the README workflow
            # is shell-driven, so the agent gets bash + editing only.
            "AGENT_ENABLE_BROWSING=false "
            "AGENT_ENABLE_JUPYTER=false "
            f"SAVE_TRAJECTORY_PATH={TRANSCRIPT_CONTAINER_PATH} "
        )
        return (
            f"mkdir -p {transcript_dir} && "
            f"{env_prefix}"
            f'openhands-python -m openhands.core.main -t "$(cat {PROMPT_CONTAINER_PATH})" '
            f"--max-iterations {limits.max_turns} "
            f">{stderr_path} 2>&1"
        )

    def parse_transcript(self, transcript_path: Path) -> CommandLog:
        """Parse an OpenHands trajectory into a CommandLog.

        Pure and deterministic. Phase tagging is left to normalize.py —
        entries come out with ``phase="unknown"``.
        """
        raw = transcript_path.read_text(encoding="utf-8", errors="replace")
        events = _load_events(raw)

        entries: list[CommandEntry] = []
        pending: list[CommandEntry] = []  # run actions awaiting observations
        fixes: list[FixMarker] = []
        file_edits: list[str] = []
        blocked_reason: Optional[str] = None
        adjusted: Optional[tuple[str, Optional[str]]] = None
        marker_seen = False

        for event in events:
            action = event.get("action")
            observation = event.get("observation")

            if action == "run":
                args = event.get("args")
                cmd = args.get("command") if isinstance(args, dict) else None
                if isinstance(cmd, str) and cmd:
                    entry = CommandEntry(cmd=cmd)
                    entries.append(entry)
                    pending.append(entry)

            elif action in _MARKER_ACTIONS:
                if event.get("source") == "user":
                    # OpenHands echoes the TASK PROMPT into the trajectory as
                    # a source="user" message action (plus its own
                    # auto-continue nudges). The prompt DOCUMENTS the markers,
                    # so scanning it harvests the literal templates
                    # (`BLOCKED: <reason>`, `ADJUSTED_SUCCESS: <new command>`,
                    # R2D_SUCCESS) as real markers — a run whose agent
                    # genuinely succeeded was once reported blocked by its own
                    # instructions. Only agent-sourced text carries markers.
                    # (Guards `finish` too: same rule, one gate.)
                    continue
                # A finish action hides its text in several places; a message
                # action keeps it in args.content. Everything downstream is
                # identical — same scanners, same placeholder rejection.
                text = (
                    _finish_text(event) if action == "finish" else _message_text(event)
                )
                if SUCCESS_MARKER in text:
                    # The marker is the proof. `task_completed: true` is in
                    # `text` as corroboration only — an agent can call finish
                    # having failed, so it never flips the outcome alone.
                    marker_seen = True
                reason = scan_markers(text, fixes)
                if reason is not None and blocked_reason is None:
                    blocked_reason = reason
                if adjusted is None:
                    adjusted = scan_adjusted(text)

            elif action in _FILE_EDIT_ACTIONS:
                args = event.get("args")
                path = args.get("path") if isinstance(args, dict) else None
                if isinstance(path, str) and path and path not in file_edits:
                    file_edits.append(path)

            elif observation == "run":
                if not pending:
                    continue  # orphan observation — schema drift, skip
                entry = pending.pop(0)
                content = event.get("content")
                entry.output = truncate_output(
                    content if isinstance(content, str) else ""
                )
                extras = event.get("extras")
                exit_code = extras.get("exit_code") if isinstance(extras, dict) else None
                entry.exit_code = exit_code if isinstance(exit_code, int) else 0

        outcome: Outcome
        if blocked_reason is not None:
            outcome = "blocked"
        elif marker_seen:
            outcome = "success"
        else:
            outcome = "failed"

        return CommandLog(
            engine=self.name,
            entries=entries,
            fixes=fixes,
            file_edits=file_edits,
            adjusted_success_command=adjusted[0] if adjusted else None,
            adjusted_success_pattern=adjusted[1] if adjusted else None,
            result=AgentResult(
                outcome=outcome,
                blocked_reason=blocked_reason,
                cost_usd=None,
                num_turns=None,
                duration_s=None,
            ),
        )
