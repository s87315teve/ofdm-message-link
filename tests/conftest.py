"""Shared selection rules for optional-dependency tests.

The CPU/headless gate must stay green in an environment that installs only the
project's declared pip dependencies.  GNU Radio is an optional runtime, so a
test that builds a native flowgraph carries the ``gnuradio`` marker and is
skipped -- never failed -- when that runtime is absent.  Tests that assert the
*unavailable* behaviour deliberately stay unmarked so they keep running on a
CPU-only installation.
"""

from __future__ import annotations

import importlib.util

import pytest

GNU_RADIO_AVAILABLE = importlib.util.find_spec("gnuradio") is not None

_SKIP_WITHOUT_GNURADIO = pytest.mark.skip(
    reason="GNU Radio is not installed in this Python environment"
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``gnuradio``-marked tests when the optional runtime is missing."""

    del config
    if GNU_RADIO_AVAILABLE:
        return
    for item in items:
        if "gnuradio" in item.keywords:
            item.add_marker(_SKIP_WITHOUT_GNURADIO)
