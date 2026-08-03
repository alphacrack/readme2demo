"""Regression test for #36: VHS timeout error must report the correct image.

The timeout error message must name the same image actually used for rendering
(``image or cfg.base_image``), not the dead ``cfg.vhs_image`` field.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from readme2demo import render
from readme2demo.config import Config


def test_timeout_error_reports_base_image(tmp_path):
    """RenderError on timeout must reference cfg.base_image, not cfg.vhs_image."""
    tape = tmp_path / "demo.tape"
    tape.write_text("Output demo.mp4\nType echo hello\n")

    cfg = Config()

    # Mock check_render_image so we don't need Docker
    # Mock subprocess.run to raise TimeoutExpired
    with patch.object(render, "check_render_image"):
        with patch(
            "readme2demo.render.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=1800),
        ):
            with pytest.raises(render.RenderError, match=cfg.base_image):
                render.run_render(tmp_path, cfg)


def test_timeout_error_reports_override_image(tmp_path):
    """When an image override is given, the timeout error must name that image."""
    tape = tmp_path / "demo.tape"
    tape.write_text("Output demo.mp4\nType echo hello\n")

    cfg = Config()
    custom_image = "my-custom/image:v2"

    with patch.object(render, "check_render_image"):
        with patch(
            "readme2demo.render.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=1800),
        ):
            with pytest.raises(render.RenderError, match=custom_image):
                render.run_render(tmp_path, cfg, image=custom_image)


def test_vhs_image_is_excluded_from_serialized_config():
    """The deprecated compatibility field must not affect runtime config."""
    with pytest.warns(DeprecationWarning, match="vhs_image.*deprecated"):
        cfg = Config(vhs_image="old/image:tag")
    assert "vhs_image" not in cfg.model_dump()
