"""The receive processes' allocator setting applies on glibc and is harmless elsewhere."""

from __future__ import annotations

import ctypes.util
import platform

import pytest

from ofdm_link.runtime import allocator


@pytest.mark.skipif(
    platform.system() != "Linux" or platform.libc_ver()[0] != "glibc",
    reason="mallopt thresholds are a glibc setting",
)
def test_freed_memory_is_kept_on_glibc() -> None:
    assert allocator.keep_freed_memory() is True


def test_no_c_library_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)

    assert allocator.keep_freed_memory() is False
