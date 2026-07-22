"""DSL escaping helpers for VHS tape syntax and grep -E patterns.

Extracted from distill.py so the pure string-escaping surface can evolve
without touching grounding logic.
"""

from __future__ import annotations


class DistillError(RuntimeError):
    """Raised when the distiller cannot produce a fully grounded output.

    Carries ``cost_usd`` so spend already incurred before the failure is not
    lost: the grounding retry means this error can arrive *after* two paid
    LLM calls, and the orchestrator records it against the failed stage.
    """

    def __init__(self, *args, cost_usd: float = 0.0) -> None:
        super().__init__(*args)
        self.cost_usd = cost_usd


def _grep_flags_and_pattern(pattern: str) -> tuple[str, str]:
    """Translate a Python-style regex to grep -E usage.

    GNU grep -E does not understand inline flags like ``(?i)``; the planner
    (an LLM) writes Python-style patterns. Handle the common case by
    stripping a leading ``(?i)`` and adding grep's ``-i`` flag.
    """
    if pattern.startswith("(?i)"):
        return "-qiE", pattern[4:]
    return "-qE", pattern


# Go-regexp metacharacters (VHS Wait+Screen patterns), plus the / delimiter.
_VHS_REGEX_METAS = set("\\.+*?()|[]{}^$/")


def vhs_wait_pattern(s: str, max_len: int = 40) -> str:
    """Make a Wait+Screen pattern safe: treat it as a LITERAL substring.

    The distiller is told to use plain substrings, but an LLM instruction is
    not enforcement — e.g. "ToolHive (thv) is a lightweight" silently becomes
    a regex with a capture group that never matches the on-screen parens and
    times the render out. Escape every metacharacter, and truncate so the
    pattern can't span a wrapped terminal line.
    """
    s = s[:max_len]
    return "".join("\\" + ch if ch in _VHS_REGEX_METAS else ch for ch in s)


def vhs_quote(s: str) -> str:
    """Quote a string for a VHS ``Type`` argument.

    VHS string literals do not support backslash escapes; instead VHS accepts
    three delimiters. Pick one the string doesn't contain: double quotes,
    then backticks, then single quotes.
    """
    if '"' not in s:
        return f'"{s}"'
    if "`" not in s:
        return f"`{s}`"
    if "'" not in s:
        return f"'{s}'"
    raise DistillError(
        f"Command cannot be quoted for VHS (contains \", ` and '): {s!r}"
    )
