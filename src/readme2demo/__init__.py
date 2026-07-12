"""readme2demo — verified tutorial + demo video generation.

An AI agent actually runs your README inside a hardened Docker sandbox; only
after a clean-room replay passes do we render the demo video and publish the
tutorial.
"""

from __future__ import annotations

import importlib.metadata as _metadata


def _get_version() -> str:
    """Return installed package version, or ``0.0.0-dev`` if not installed."""
    try:
        return _metadata.version("readme2demo")
    except _metadata.PackageNotFoundError:
        return "0.0.0-dev"


__version__: str = _get_version()
