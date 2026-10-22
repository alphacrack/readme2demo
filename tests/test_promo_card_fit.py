"""Geometry checks for the drawtext filters emitted for promo cards."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from readme2demo import promo, promo_script
from readme2demo.config import Config
from readme2demo.types import AgentResult, CommandLog, PromoScene, PromoScript


@pytest.mark.parametrize("style", ["minimal", "tech", "energetic"])
@pytest.mark.parametrize(
    "text",
    [
        "alphacrack/readme2demo — verified in a fresh container",
        "W" * 119,
        "W" * 120,
    ],
    ids=["repo-title", "boundary-minus-one", "boundary"],
)
def test_card_argv_lines_fit_canvas(text: str, style: str, tmp_path: Path) -> None:
    """Regression: accepted long cards emit lines inside the canvas margins."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("requires ffmpeg with drawtext for real font measurements")
    script = PromoScript(
        total_duration_s=6.0,
        scenes=[
            PromoScene(kind="title_card", text=text, duration_s=2.0),
            PromoScene(
                kind="demo_segment", start_s=0.0, end_s=2.0, duration_s=2.0
            ),
            PromoScene(kind="end_card", text="Verified", duration_s=2.0),
        ],
    )
    assert promo_script._card_violations(
        "title", script.scenes[0], set(),
        CommandLog(engine="claude-code", result=AgentResult(outcome="success")), "",
    ) == []
    argv = promo.build_promo_argv(
        script, tmp_path, Config(), source_duration_s=2.0, style=style
    )
    graph = argv[argv.index("-filter_complex") + 1]
    card = graph.split("[1:v]", 1)[1].split(",setsar=1", 1)[0]
    # Keep the emitted text/font/size, asking drawtext itself for the width.
    filters = re.findall(r"drawtext=.*?:y=\(h-text_h\)/2(?:[+-]\d+)?", card)
    assert filters, graph
    for fragment in filters:
        measured = fragment.replace("x=(w-text_w)/2", "x=print(text_w)")
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "info", "-f", "lavfi",
                "-i", f"color=s={promo.CANVAS_W}x{promo.CANVAS_H}",
                "-vf", measured, "-frames:v", "1", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=10, check=True,
        )
        widths = re.findall(r"^([0-9]+\.[0-9]+)$", result.stderr, re.MULTILINE)
        assert widths, result.stderr
        assert float(widths[0]) <= promo.CANVAS_W - 48, (
            f"{style}: emitted card line width {widths[0]} exceeds "
            f"{promo.CANVAS_W - 48}px usable canvas: {fragment}"
        )


@pytest.mark.parametrize("length, accepted", [(119, True), (120, True), (121, False)])
def test_card_validator_length_boundary(length: int, accepted: bool) -> None:
    """Regression: the card validator accepts its boundary and rejects its near miss."""
    violations = promo_script._card_violations(
        "title", PromoScene(kind="title_card", text="W" * length, duration_s=2.0),
        set(), CommandLog(engine="claude-code", result=AgentResult(outcome="success")), "",
    )
    assert (violations == []) is accepted
