"""Claude Code headless engine (the default).

Runs ``claude -p`` inside the sandbox with ``--output-format stream-json`` and
parses the resulting NDJSON transcript into the normalized
:class:`~readme2demo.types.CommandLog`.

Stream-json shapes this parser understands (one JSON object per line):

* ``{"type": "assistant", "message": {"content": [block, ...]}}`` where a
  block is ``{"type": "text", "text": ...}`` or
  ``{"type": "tool_use", "id": ..., "name": ..., "input": {...}}``.
* ``{"type": "user", "message": {"content": [{"type": "tool_result",
  "tool_use_id": ..., "content": ..., "is_error": bool}, ...]}}`` where the
  result ``content`` may be a plain string or a list of text blocks.
* ``{"type": "result", "subtype": "success" | "error_max_turns" | ...,
  "total_cost_usd": float, "num_turns": int, "duration_ms": int,
  "result": str}``.

The format has drifted across Claude Code versions before, so the parser is
deliberately tolerant: malformed lines are skipped, missing fields fall back
to sensible defaults, and unknown event types are ignored.
"""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Any, Optional

from readme2demo.engines.base import (
    PROMPT_CONTAINER_PATH,
    TRANSCRIPT_CONTAINER_PATH,
    AgentEngine,
    Limits,
    register,
)
from readme2demo.types import (
    ADJUSTED_MARKER,
    BLOCKED_MARKER,
    FIX_MARKER,
    SUCCESS_MARKER,
    AgentResult,
    CommandEntry,
    CommandLog,
    FixMarker,
    Outcome,
)

# Output truncation: keep the head and the tail, drop the middle.
MAX_OUTPUT_BYTES = 4096
HEAD_BYTES = 3072
TAIL_BYTES = 1024
TRUNCATION_SEPARATOR = "\n...[truncated]...\n"

# Tool names whose input.file_path counts as a file edit.
FILE_EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# "FIX: <what> BECAUSE: <why>" — BECAUSE part is optional.
_FIX_RE = re.compile(
    rf"^{re.escape(FIX_MARKER)}\s*(?P<what>.*?)(?:\s+BECAUSE:\s*(?P<because>.*))?\s*$"
)
_BLOCKED_RE = re.compile(rf"^{re.escape(BLOCKED_MARKER)}\s*(?P<reason>.*)$")

# "ADJUSTED_SUCCESS: <command> EXPECT: <regex>" — EXPECT part optional.
_ADJUSTED_RE = re.compile(
    rf"^{re.escape(ADJUSTED_MARKER)}\s*(?P<command>.*?)(?:\s+EXPECT:\s*(?P<pattern>.*))?\s*$"
)

# API keys / OAuth tokens: printable token characters only, no whitespace or
# ANSI escapes. Deliberately loose about prefix so future formats still pass.
_CREDENTIAL_RE = re.compile(r"[A-Za-z0-9_\-.~+/=]{20,512}")


def truncate_output(text: str) -> str:
    """Truncate command output to ~4KB, keeping head and tail.

    Shared by all engine parsers so ``command_log.json`` entries stay small
    enough to feed to the distiller regardless of which agent ran.
    """
    if len(text) <= MAX_OUTPUT_BYTES:
        return text
    return text[:HEAD_BYTES] + TRUNCATION_SEPARATOR + text[-TAIL_BYTES:]


def scan_markers(
    text: str, fixes: list[FixMarker]
) -> Optional[str]:
    """Scan assistant text for FIX/BLOCKED markers.

    Appends parsed :class:`FixMarker` objects to ``fixes`` and returns the
    blocked reason if a BLOCKED marker was found (else None).
    """
    blocked_reason: Optional[str] = None
    for line in text.splitlines():
        line = line.strip()
        fix = _FIX_RE.match(line)
        if fix and fix.group("what"):
            fixes.append(
                FixMarker(
                    what=fix.group("what").strip(),
                    because=(fix.group("because") or "").strip(),
                )
            )
            continue
        blocked = _BLOCKED_RE.match(line)
        if blocked and blocked_reason is None:
            blocked_reason = blocked.group("reason").strip()
    return blocked_reason


def scan_adjusted(text: str) -> Optional[tuple[str, Optional[str]]]:
    """Scan assistant text for an ADJUSTED_SUCCESS marker.

    Returns ``(command, expected_pattern_or_None)`` for the first marker
    found, else None. Shared by all engine parsers.
    """
    for line in text.splitlines():
        m = _ADJUSTED_RE.match(line.strip())
        if m and m.group("command"):
            pattern = (m.group("pattern") or "").strip() or None
            return m.group("command").strip().strip("`"), pattern
    return None


def _result_text(content: Any) -> str:
    """Extract text from a tool_result ``content`` field.

    Handles both shapes Claude Code emits: a plain string, or a list of
    ``{"type": "text", "text": ...}`` blocks.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


@register
class ClaudeCodeEngine(AgentEngine):
    """Claude Code in headless print mode inside the sandbox."""

    name = "claude-code"

    # Either of these authenticates the in-sandbox Claude Code process.
    # CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) lets developers on a
    # subscription run without an API key.
    AUTH_ENV_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")

    def required_env(self) -> list[str]:
        return ["ANTHROPIC_API_KEY"]  # canonical; see resolve_env for the OR

    def resolve_env(self) -> dict[str, str]:
        """Forward whichever auth var is set (API key preferred).

        Values are validated: a common failure mode is
        ``export CLAUDE_CODE_OAUTH_TOKEN=$(claude setup-token)`` capturing the
        interactive TUI output (ANSI escapes, "Opening browser…") instead of
        the token. That garbage would otherwise surface as a cryptic
        "invalid header value" API error deep inside the run.
        """
        import os

        from readme2demo.engines.base import EngineError

        for var in self.AUTH_ENV_VARS:
            value = os.environ.get(var)
            if value:
                if not _CREDENTIAL_RE.fullmatch(value.strip()):
                    raise EngineError(
                        f"{var} is set but doesn't look like a credential "
                        "(contains whitespace/control characters — likely captured "
                        "interactive output). Fix:\n"
                        f"  unset {var}\n"
                        "  claude setup-token   # run it plain, approve in browser\n"
                        f"  export {var}=<the sk-ant-... token it prints>"
                    )
                return {var: value.strip()}
        raise EngineError(
            "Engine 'claude-code' needs ANTHROPIC_API_KEY or "
            "CLAUDE_CODE_OAUTH_TOKEN (create one with: claude setup-token).\n"
            "Exports are per-terminal — add the line to your shell profile "
            "(e.g. ~/.zshrc) so it survives new sessions."
        )

    def build_command(self, limits: Limits) -> str:
        transcript_dir = posixpath.dirname(TRANSCRIPT_CONTAINER_PATH)
        stderr_path = posixpath.join(transcript_dir, "agent.stderr")
        return (
            f"mkdir -p {transcript_dir} && "
            f'claude -p "$(cat {PROMPT_CONTAINER_PATH})" '
            f"--output-format stream-json --verbose "
            f"--dangerously-skip-permissions "
            f"--max-turns {limits.max_turns} "
            f"> {TRANSCRIPT_CONTAINER_PATH} 2>{stderr_path}"
        )

    def parse_transcript(self, transcript_path: Path) -> CommandLog:
        """Parse a stream-json NDJSON transcript into a CommandLog.

        Pure and deterministic; tolerant of malformed lines and missing
        fields. Phase tagging is intentionally left to normalize.py — every
        entry comes out with ``phase="unknown"``.
        """
        # Bash tool_use ids waiting for their tool_result, in emission order.
        pending_bash: dict[str, CommandEntry] = {}
        entries: list[CommandEntry] = []
        fixes: list[FixMarker] = []
        file_edits: list[str] = []
        blocked_reason: Optional[str] = None
        adjusted: Optional[tuple[str, Optional[str]]] = None
        marker_seen = False
        result_subtype: Optional[str] = None
        cost_usd: Optional[float] = None
        num_turns: Optional[int] = None
        duration_s: Optional[float] = None

        raw = transcript_path.read_text(encoding="utf-8", errors="replace")
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            etype = event.get("type")

            if etype == "assistant":
                message = event.get("message") or {}
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text") or ""
                        if SUCCESS_MARKER in text:
                            marker_seen = True
                        reason = scan_markers(text, fixes)
                        if reason is not None and blocked_reason is None:
                            blocked_reason = reason
                        if adjusted is None:
                            adjusted = scan_adjusted(text)
                    elif btype == "tool_use":
                        tool_name = block.get("name") or ""
                        tool_id = block.get("id") or ""
                        tool_input = block.get("input") or {}
                        if not isinstance(tool_input, dict):
                            tool_input = {}
                        if tool_name == "Bash":
                            cmd = tool_input.get("command")
                            if isinstance(cmd, str) and cmd:
                                entry = CommandEntry(cmd=cmd)
                                entries.append(entry)
                                if tool_id:
                                    pending_bash[tool_id] = entry
                        elif tool_name in FILE_EDIT_TOOLS:
                            file_path = tool_input.get("file_path")
                            if isinstance(file_path, str) and file_path:
                                if file_path not in file_edits:
                                    file_edits.append(file_path)

            elif etype == "user":
                message = event.get("message") or {}
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    tool_use_id = block.get("tool_use_id") or ""
                    entry = pending_bash.pop(tool_use_id, None)
                    if entry is None:
                        continue
                    is_error = bool(block.get("is_error", False))
                    entry.output = truncate_output(_result_text(block.get("content")))
                    entry.exit_code = 1 if is_error else 0

            elif etype == "result":
                result_subtype = event.get("subtype")
                raw_cost = event.get("total_cost_usd")
                if isinstance(raw_cost, (int, float)):
                    cost_usd = float(raw_cost)
                raw_turns = event.get("num_turns")
                if isinstance(raw_turns, int):
                    num_turns = raw_turns
                raw_ms = event.get("duration_ms")
                if isinstance(raw_ms, (int, float)):
                    duration_s = float(raw_ms) / 1000.0
                final_text = event.get("result")
                if isinstance(final_text, str):
                    if SUCCESS_MARKER in final_text:
                        marker_seen = True
                    reason = scan_markers(final_text, fixes)
                    if reason is not None and blocked_reason is None:
                        blocked_reason = reason
                    if adjusted is None:
                        adjusted = scan_adjusted(final_text)

        outcome: Outcome
        if blocked_reason is not None:
            outcome = "blocked"
        elif marker_seen and (result_subtype is None or result_subtype == "success"):
            # If the result event is missing (agent killed after printing the
            # marker), the marker alone counts; if present, it must agree.
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
                cost_usd=cost_usd,
                num_turns=num_turns,
                duration_s=duration_s,
            ),
        )
