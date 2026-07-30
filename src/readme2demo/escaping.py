"""DSL escaping helpers for VHS tape syntax and grep -E patterns.

Extracted from distill.py so the pure string-escaping surface can evolve
without touching grounding logic.
"""

from __future__ import annotations

import re


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

    GNU grep -E does not understand Python inline flags like ``(?i)`` /
    ``(?is)``. Strip a leading ``(?...)`` group: map ``i`` to grep's ``-i``,
    drop ``m``/``s`` (grep -E is line-oriented anyway), and raise
    :class:`DistillError` for ``x`` (verbose) patterns — those would silently
    fail at verify time if passed through.
    """
    m = re.match(r"^\(\?([a-zA-Z]+)\)(.*)$", pattern, flags=re.DOTALL)
    if not m:
        return "-qE", pattern
    flags, body = m.group(1), m.group(2)
    # Reject extended/verbose mode — no faithful grep equivalent for (?x).
    if "x" in flags.lower():
        raise DistillError(
            f"success pattern uses Python (?x) verbose mode, which grep -E "
            f"cannot express: {pattern!r}. Rewrite without (?x) (use a "
            f"compact pattern) so the verify assertion can run."
        )
    # i → case-insensitive; m/s ignored (line-oriented grep, DOTALL N/A)
    ignore_case = "i" in flags.lower()
    flag_str = "-qiE" if ignore_case else "-qE"
    return flag_str, body


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
