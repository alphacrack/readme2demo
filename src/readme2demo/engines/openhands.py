"""OpenHands headless engine (opt-in, experimental).

Runs OpenHands headless mode (``python -m openhands.core.main``) inside *our*
sandbox — we bypass OpenHands' own runtime-sandbox logic and treat it like any
process in the container (one sandbox model, not two). Selected via
``--engine openhands`` when ``LLM_API_KEY`` and ``LLM_MODEL`` (litellm-style)
are provided.

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
* File edits: ``{"action": "write" | "edit", "args": {"path": ...}}``.

There is no terminal ``result`` event in a trajectory, so cost and turn count
are ``None`` and the outcome is derived purely from markers.
"""

from __future__ import annotations

import json
import posixpath
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


@register
class OpenHandsEngine(AgentEngine):
    """OpenHands headless mode inside the sandbox (experimental)."""

    name = "openhands"

    def required_env(self) -> list[str]:
        return ["LLM_API_KEY", "LLM_MODEL"]

    def build_command(self, limits: Limits) -> str:
        transcript_dir = posixpath.dirname(TRANSCRIPT_CONTAINER_PATH)
        return (
            f"mkdir -p {transcript_dir} && "
            f'python -m openhands.core.main -t "$(cat {PROMPT_CONTAINER_PATH})" '
            f"--max-iterations {limits.max_turns} "
            f"--save-trajectory-path {TRANSCRIPT_CONTAINER_PATH}"
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

            elif action == "message":
                text = _message_text(event)
                if SUCCESS_MARKER in text:
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
