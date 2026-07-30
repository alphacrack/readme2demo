"""Unit tests for the pure brand-kit ffmpeg-fragment helpers (#173).

These exercise string building only — no ffmpeg, no docker, no I/O — so they
stay inside the sub-second suite. The drawtext-escaping cases are where the
real bugs live (repo names carry ``:``, quotes and brackets), so they get the
most coverage.
"""

from __future__ import annotations

from pathlib import Path

from readme2demo.brand import escape_drawtext, logo_overlay, title_card_drawtext
from readme2demo.config import Config


def _png(tmp_path: Path) -> Path:
    """A minimal existing .png so Config's brand_logo validator accepts it."""
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"\x89PNG\r\n")
    return logo


# --- escape_drawtext ----------------------------------------------------------


class TestEscapeDrawtext:
    def test_colon_escaped(self) -> None:
        r"""Regression (#173): ':' separates drawtext options, so a literal
        colon in a repo name must become '\:' or it truncates the text."""
        assert escape_drawtext("v2.0: go") == r"v2.0\: go"

    def test_single_quote_escaped(self) -> None:
        r"""Regression (#173): a single quote opens a filtergraph quote and
        must be backslash-escaped."""
        assert escape_drawtext("it's") == r"it\'s"

    def test_backslash_escaped_first(self) -> None:
        r"""Regression (#173): backslash is doubled, and — because it is
        escaped BEFORE the other metacharacters — the backslash we add for a
        following ':' is not itself re-escaped. Input a\b:c -> a\\b\:c."""
        assert escape_drawtext("a\\b:c") == r"a\\b\:c"

    def test_percent_escaped(self) -> None:
        r"""Regression (#173): '%' introduces drawtext text expansion (%{...}),
        so a literal percent must be escaped."""
        assert escape_drawtext("100% done") == r"100\% done"

    def test_square_brackets_escaped(self) -> None:
        r"""Regression (#173): '[' and ']' are filtergraph pad-label syntax;
        version tags like [beta] must survive as literal text."""
        assert escape_drawtext("release [beta]") == r"release \[beta\]"

    def test_double_quote_passes_through(self) -> None:
        """Regression (#173): the double quote is NOT an ffmpeg drawtext
        metacharacter, so it is left untouched (only the single quote is)."""
        assert escape_drawtext('say "hi"') == 'say "hi"'

    def test_realistic_repo_name(self) -> None:
        r"""Regression (#173): a repo/version string exercising ':', space and
        brackets at once — the exact case this escaping exists for."""
        assert escape_drawtext("user/repo: v2.0 [beta]") == r"user/repo\: v2.0 \[beta\]"

    def test_comma_and_semicolon_escaped(self) -> None:
        r"""Regression (#173): ',' and ';' separate filters/chains in a
        filtergraph and must not leak from the text."""
        assert escape_drawtext("a, b; c") == r"a\, b\; c"

    def test_plain_text_unchanged(self) -> None:
        assert escape_drawtext("Getting Started") == "Getting Started"

    def test_empty_string(self) -> None:
        assert escape_drawtext("") == ""


# --- title_card_drawtext ------------------------------------------------------


class TestTitleCardDrawtext:
    def test_applies_default_brand_color(self) -> None:
        frag = title_card_drawtext("Hello", Config())
        assert frag.startswith("drawtext=text=Hello:")
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
        assert r"text=v2.0\: go:fontcolor=" in frag

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
