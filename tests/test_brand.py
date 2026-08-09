"""Unit tests for the pure brand-kit ffmpeg-fragment helpers (#173).

These exercise string building only — no ffmpeg, no docker, no I/O — so they
stay inside the sub-second suite. The drawtext-escaping cases are where the
real bugs live (repo names carry ``:``, quotes and brackets), so they get the
most coverage.
"""

from __future__ import annotations

from pathlib import Path

import re

from readme2demo.brand import escape_drawtext, logo_overlay, title_card_drawtext
from readme2demo.config import Config


def _png(tmp_path: Path) -> Path:
    """A minimal existing .png so Config's brand_logo validator accepts it."""
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n")
    return logo


# --- escape_drawtext ----------------------------------------------------------


class TestEscapeDrawtext:
    r"""Escape depth is per-character and NOT uniform.

    ffmpeg unescapes a ``-filter_complex`` value in more than one pass, so how
    many backslashes a character needs depends on which pass consumes it. Every
    count below was verified by RENDERING a frame with the base image's ffmpeg
    and reading the pixels — a graph that merely parses can still draw the wrong
    text, which is exactly how the ``'`` and ``\`` cases hid (#281, run
    readme2demo-20260806-184032).
    """

    def test_regression_colon_needs_two_backslashes(self) -> None:
        r"""Regression (#281): ':' is special to the INNER (filter-option) pass,
        so it must survive the outer one to reach it. A single '\:' is eaten by
        the outer pass and ffmpeg then rejects the ENTIRE option — 'Error
        parsing global options: Invalid argument', no frame rendered. This cost
        a verified run its promo cut."""
        assert escape_drawtext("v2.0: go") == "v2.0" + "\\" * 2 + ": go"

    def test_regression_single_quote_needs_three_backslashes(self) -> None:
        r"""Regression (#281): the quote is consumed by both passes. With too
        few backslashes the filtergraph still PARSES — and the card renders
        BLANK. Silent, so only a pixel check catches it."""
        assert escape_drawtext("it's") == "it" + "\\" * 3 + "'s"

    def test_regression_backslash_needs_four(self) -> None:
        r"""Regression (#281): the escape character is halved by every pass, so
        a literal backslash needs four. With two it silently VANISHES from the
        rendered text rather than failing."""
        assert escape_drawtext("a\\b") == "a" + "\\" * 4 + "b"

    def test_backslash_escaped_first(self) -> None:
        r"""Backslash is escaped BEFORE the other metacharacters, so the
        backslashes added for a following ':' are not themselves re-escaped."""
        assert escape_drawtext("a\\b:c") == "a" + "\\" * 4 + "b" + "\\" * 2 + ":c"

    def test_regression_percent_is_not_escaped_at_all(self) -> None:
        r"""Regression (#281): '%' is not an escaping problem — it is drawtext
        TEMPLATE EXPANSION. No backslash count fixes it (all of them still warn
        'Stray %' and render the card empty); the fix is ``expansion=none`` on
        the filter, so the text is taken literally and '%' needs no escape."""
        assert escape_drawtext("100% done") == "100% done"

    def test_graph_separators_take_exactly_one_backslash(self) -> None:
        r"""',' ';' '[' ']' are special to the OUTER (graph) pass only. Doubling
        them breaks parsing just as surely as under-escaping ':' does."""
        assert escape_drawtext("release [beta]") == "release " + "\\" + "[beta" + "\\" + "]"
        assert escape_drawtext("a, b; c") == "a" + "\\" + ", b" + "\\" + "; c"

    def test_double_quote_passes_through(self) -> None:
        """The double quote is NOT an ffmpeg drawtext metacharacter."""
        assert escape_drawtext('say "hi"') == 'say "hi"'

    def test_realistic_repo_name(self) -> None:
        r"""A repo/version string exercising ':', space and brackets at once."""
        assert escape_drawtext("user/repo: v2.0 [beta]") == (
            "user/repo" + "\\" * 2 + ": v2.0 " + "\\" + "[beta" + "\\" + "]"
        )

    def test_plain_text_unchanged(self) -> None:
        assert escape_drawtext("Getting Started") == "Getting Started"

    def test_empty_string(self) -> None:
        assert escape_drawtext("") == ""


# --- title_card_drawtext ------------------------------------------------------


class TestTitleCardDrawtext:
    def test_applies_default_brand_color(self) -> None:
        frag = title_card_drawtext("Hello", Config())
        assert frag.startswith("drawtext=text=Hello:")
        assert ":expansion=none:" in frag  # card text is literal, never a template
        assert "fontcolor=#7C6BF2" in frag

    def test_font_omitted_when_unset(self) -> None:
        # ':font=' is the font OPTION; ':fontcolor='/':fontsize=' don't match it.
        assert ":font=" not in title_card_drawtext("Hello", Config())

    def test_font_included_when_set(self) -> None:
        frag = title_card_drawtext("Hello", Config(brand_font="Inter"))
        assert ":font=Inter:" in frag

    def test_text_is_escaped_inside_fragment(self) -> None:
        r"""Regression (#173): a colon in the card text is escaped so it cannot
        be misread as the boundary to the next drawtext option."""
        frag = title_card_drawtext("v2.0: go", Config())
        assert r"text=v2.0\\: go:" in frag

    def test_custom_geometry(self) -> None:
        frag = title_card_drawtext("Hi", Config(), fontsize=72, x="10", y="20")
        assert "fontsize=72" in frag
        assert ":x=10:" in frag
        assert ":y=20" in frag


# --- logo_overlay -------------------------------------------------------------


class TestLogoOverlay:
    def test_none_when_logo_unset(self) -> None:
        """Regression (#173): no brand_logo -> None, so a consumer can skip the
        overlay entirely."""
        assert logo_overlay(Config()) is None

    def test_fragments_reference_configured_path(self, tmp_path: Path) -> None:
        """Regression (#173): with a logo set, return the extra -i input args
        and an overlay filter core positioned top-right by default."""
        logo = _png(tmp_path)
        result = logo_overlay(Config(brand_logo=logo))
        assert result is not None
        input_args, overlay = result
        assert input_args == ["-i", str(logo)]
        assert overlay == "overlay=W-w-24:24"

    def test_custom_position(self, tmp_path: Path) -> None:
        logo = _png(tmp_path)
        result = logo_overlay(Config(brand_logo=logo), x="0", y="0")
        assert result is not None
        _, overlay = result
        assert overlay == "overlay=0:0"


# --- ffmpeg escaping contract (#281) ------------------------------------------


class TestDrawtextEscapingContract:
    r"""Properties that must hold for ANY card text, whatever the escape table.

    The unit tests above pin specific backslash counts; these pin the reasons
    those counts exist. Every previous test passed while the promo was in fact
    unrenderable (#281), because they all asserted the escaper's OUTPUT and
    never what ffmpeg would do with it — the loop was closed by eye, against a
    rendered frame, and these are the invariants that survived it.
    """

    #: The card texts from the run that found the bug, plus the metacharacters
    #: that each fail in a DIFFERENT way: ':' rejects the whole ffmpeg option,
    #: "'" renders the card blank, '\' silently drops from the text, '%' makes
    #: the card empty via template expansion.
    CASES = [
        "alphacrack/readme2demo — verified in a fresh container",
        "Try it: readme2demo report examples/toolhive",
        "user/repo: v2.0 [beta], x; 100% it's a\\b",
        "50% done",
        "it's",
        "a\\b",
        "[a],b;c",
    ]

    def test_no_literal_colon_survives_unescaped(self) -> None:
        """An unescaped ':' would end the text= option and make the rest of the
        card's geometry parse as text — the failure that cost a verified run
        its promo cut."""
        for text in self.CASES:
            frag = title_card_drawtext(text, Config())
            body = frag[len("drawtext=text="):frag.index(":expansion=none")]
            # every colon in the emitted text must carry its escape
            for i, ch in enumerate(body):
                if ch == ":":
                    assert body[max(0, i - 2):i] == "\\\\", (text, body)

    def test_expansion_is_always_disabled(self) -> None:
        """Card text is literal. Without expansion=none a bare '%' renders the
        card EMPTY and '%{...}' is evaluated — machine state burned into a
        published video."""
        for text in self.CASES:
            assert ":expansion=none:" in title_card_drawtext(text, Config())

    def test_escaping_is_reversible_to_the_original_text(self) -> None:
        r"""Simulating ffmpeg's passes must recover the author's text exactly.

        This is the property the backslash counts encode: unescaping in the
        order ffmpeg does returns the input. A count that is wrong in EITHER
        direction breaks this, including the silent ones a parse check misses.
        """
        for text in self.CASES:
            esc = escape_drawtext(text)
            # outer (graph) pass: consumes one level of backslash
            outer = re.sub(r"\\(.)", r"\1", esc)
            # inner (filter-option) pass: consumes the next
            inner = re.sub(r"\\(.)", r"\1", outer)
            assert inner == text, (text, esc, inner)
