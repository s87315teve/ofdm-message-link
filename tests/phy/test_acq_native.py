"""Native acquisition (_acq_native) against the Python reference in sync.py / sync_v5.py.

Tolerances (report_phase2 section 5.1): identical start positions, |dCFO| <=
1e-6 subcarrier, confidence (and metric, STF confidence, amplitude) within
1e-5 relative.  Run over the fixed-seed corpus, whole buffers and the
8192-sample windows a stream feeds.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import numpy as np
import pytest
from native_corpus import CASES, Case, build

from ofdm_link.phy import BurstConfig, native_backend, sync
from ofdm_link.phy.streaming import StreamingBurstDecoder
from ofdm_link.phy.sync import SyncConfig, acquire_all
from ofdm_link.phy.sync_v5 import V5SyncConfig

native = pytest.importorskip("ofdm_link.phy._acq_native")

LEGACY = SyncConfig(detection_threshold=0.25, correlation_threshold=0.30)
V5 = V5SyncConfig()


@pytest.fixture(autouse=True)
def _compare_both_backends(monkeypatch):
    """These tests compare native with Python themselves; a global OFDM_LINK_NATIVE=python
    (used to run the whole suite on the reference path) must not turn both sides Python."""

    monkeypatch.delenv(native_backend.GLOBAL_ENV, raising=False)
    native_backend.reset()
    yield
    native_backend.reset()


def _close(a: float, b: float, rel: float) -> bool:
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-12)


def _compare(values: np.ndarray, legacy, v5, where: str) -> int:
    ref_legacy, ref_v5 = sync._family_candidates(values, legacy, v5)
    got_legacy, got_v5 = sync._native_family_candidates(native, values, legacy, v5)
    assert [c.preamble_start for c in got_legacy] == [c.preamble_start for c in ref_legacy], where
    assert [c.stf_start for c in got_v5] == [c.stf_start for c in ref_v5], where
    for got, ref in zip(got_legacy, ref_legacy, strict=True):
        assert got.useful_start == ref.useful_start, where
        assert abs(got.normalized_cfo - ref.normalized_cfo) <= 1e-6, where
        for name in ("confidence", "metric", "amplitude_gain", "signal_power"):
            assert _close(getattr(got, name), getattr(ref, name), 1e-5), (where, name)
    for got, ref in zip(got_v5, ref_v5, strict=True):
        assert got.ltf_start == ref.ltf_start, where
        assert abs(got.normalized_cfo - ref.normalized_cfo) <= 1e-6, where
        assert abs(got.coarse_cfo - ref.coarse_cfo) <= 1e-6, where
        for name in ("confidence", "metric", "stf_confidence"):
            assert _close(getattr(got, name), getattr(ref, name), 1e-5), (where, name)
    return len(ref_legacy) + len(ref_v5)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("families", ["both", "v5", "legacy"])
def test_whole_buffers_match_the_reference(case: Case, families: str):
    values = build(case)
    legacy = None if families == "v5" else LEGACY
    v5 = None if families == "legacy" else V5
    _compare(values, legacy, v5, f"{case.name} {families}")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_stream_windows_match_the_reference(case: Case):
    values = build(case)
    for start in range(0, max(1, values.size - 640), 5000):
        window = values[start : start + 8192]
        _compare(window, LEGACY, V5, f"{case.name} @{start}")


def test_the_corpus_finds_what_it_was_built_with():
    found = acquire_all(build(Case("count", snr_db=25.0, v5_bursts=4, legacy_bursts=2)),
                        LEGACY, V5)
    assert len(found) == 6


def test_threshold_variants_match():
    values = build(Case("variants", snr_db=4.0, v5_bursts=4, legacy_bursts=2, dc=0.004, seed=31))
    for v5 in (V5SyncConfig(detection_threshold=0.05, stf_threshold=0.1),
               V5SyncConfig(correlation_threshold=0.5, balance_ratio=0.8, fine_timing_search=8),
               V5SyncConfig(fine_timing_search=0)):
        for legacy in (LEGACY, SyncConfig(detection_threshold=0.5, correlation_threshold=0.6)):
            _compare(values, legacy, v5, f"{v5} {legacy}")


def test_acquire_all_uses_the_backend_switch(monkeypatch):
    values = build(Case("switch", snr_db=12.0, v5_bursts=2, legacy_bursts=1, seed=41))
    results = {}
    for choice in ("python", "native"):
        monkeypatch.setenv(native_backend.GLOBAL_ENV, "auto")
        monkeypatch.setenv(native_backend.env_name("acq"), choice)
        native_backend.reset()
        assert native_backend.uses_native("acq") == (choice == "native")
        results[choice] = acquire_all(values, LEGACY, V5)
    native_backend.reset()
    assert [type(c) for c in results["native"]] == [type(c) for c in results["python"]]
    assert [c.preamble_start for c in results["native"]] == [
        c.preamble_start for c in results["python"]
    ]


def test_global_python_overrides_the_piece(monkeypatch):
    monkeypatch.setenv(native_backend.GLOBAL_ENV, "python")
    monkeypatch.setenv(native_backend.env_name("acq"), "native")
    native_backend.reset()
    try:
        assert not native_backend.uses_native("acq")
    finally:
        native_backend.reset()


def test_a_missing_module_falls_back_with_one_warning(monkeypatch):
    monkeypatch.setitem(native_backend.PIECES, "acq", "ofdm_link.phy._no_such_module")
    monkeypatch.delenv(native_backend.GLOBAL_ENV, raising=False)
    monkeypatch.delenv(native_backend.env_name("acq"), raising=False)
    native_backend.reset()
    native_backend._warned.discard("acq")
    try:
        with pytest.warns(RuntimeWarning, match="not built"):
            assert native_backend.module("acq") is None
        values = build(Case("fallback", snr_db=15.0, v5_bursts=1, seed=3))
        assert len(acquire_all(values, LEGACY, V5)) == 1
        monkeypatch.setenv(native_backend.env_name("acq"), "native")
        native_backend.reset()
        with pytest.raises(native_backend.NativeBackendError):
            native_backend.module("acq")
    finally:
        native_backend.reset()


def _decode_all(values: np.ndarray, choice: str, monkeypatch, chunk: int = 4096):
    monkeypatch.setenv(native_backend.env_name("acq"), choice)
    native_backend.reset()
    decoder = StreamingBurstDecoder(BurstConfig())
    bursts = []
    for start in range(0, values.size, chunk):
        bursts.extend(decoder.feed(values[start : start + chunk]))
    native_backend.reset()
    return bursts, decoder.snapshot()


@pytest.mark.parametrize("case", [c for c in CASES if c.snr_db is None or c.snr_db >= 6.0][:8],
                         ids=lambda case: case.name)
def test_streaming_decode_is_identical(case: Case, monkeypatch):
    values = build(case)
    python_bursts, python_counts = _decode_all(values, "python", monkeypatch)
    native_bursts, native_counts = _decode_all(values, "native", monkeypatch)
    assert native_counts == python_counts
    assert [(b.burst_start, b.burst_end) for b in native_bursts] == [
        (b.burst_start, b.burst_end) for b in python_bursts
    ]


def test_the_native_scan_releases_the_gil():
    values = build(Case("gil", snr_db=10.0, v5_bursts=40, gap=(200, 400), seed=51))
    params = (sync._native_legacy_parameters(LEGACY), sync._native_v5_parameters(V5))
    ticks = 0
    done = threading.Event()

    def count():
        nonlocal ticks
        while not done.is_set():
            ticks += 1

    sys.setswitchinterval(0.0005)
    counter = threading.Thread(target=count)
    counter.start()
    try:
        started = time.perf_counter()
        before = ticks
        for _ in range(20):
            native.acquire(values, *params)
        elapsed = time.perf_counter() - started
        during = ticks - before
    finally:
        done.set()
        counter.join()
        sys.setswitchinterval(0.005)
    # Holding the GIL for the whole scan would leave the counter a handful of
    # switch-interval slots; released, it runs alongside the scan.
    assert elapsed > 0 and during > 1000


def test_the_python_reference_still_runs_when_forced(tmp_path):
    code = (
        "import numpy as np\n"
        "from ofdm_link.phy import native_backend\n"
        "from ofdm_link.phy.sync import acquire_all, SyncConfig\n"
        "from ofdm_link.phy.sync_v5 import V5SyncConfig\n"
        "assert not native_backend.uses_native('acq')\n"
        "print(len(acquire_all(np.zeros(4000, np.complex64), SyncConfig(), V5SyncConfig())))\n"
    )
    env = {**os.environ, native_backend.GLOBAL_ENV: "python"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         check=True)
    assert out.stdout.strip() == "0"
