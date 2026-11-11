"""The native v5 header probe (_header_native) against the Python probe.

Header fields must be identical; the LLRs agree to float32 rounding; every
failure is classified the same way; a stream decodes to identical bursts and
counters with the native probe on or off.
"""

from __future__ import annotations

import numpy as np
import pytest
from native_corpus import CASES, Case, build

from ofdm_link.phy import BurstConfig, BurstDecodeError, native_backend
from ofdm_link.phy.burst import _decode_burst_with_acquisition
from ofdm_link.phy.burst_v5 import (
    _SIG_LLR_CLIP,
    _native_header_constants,
    _valid_mcs,
    probe_v5_burst,
)
from ofdm_link.phy.channel import RefinedChannel, equalize_with_pilots, refine_channel_estimate
from ofdm_link.phy.codec import DEFAULT_DUAL_VERSION_FRAME_DECODER, demap_symbols
from ofdm_link.phy.header_v5 import V5HeaderDecodeError, decode_v5_header
from ofdm_link.phy.streaming import StreamingBurstDecoder, _rebased
from ofdm_link.phy.sync import acquire_all
from ofdm_link.phy.sync_v5 import (
    V5_FIXED_LENGTH,
    V5_SIG_OFFSET,
    V5Acquisition,
    correct_v5_cfo,
    measure_ltf_pair,
)

native = pytest.importorskip("ofdm_link.phy._header_native")
SETTINGS = BurstConfig()


@pytest.fixture(autouse=True)
def _compare_both_backends(monkeypatch):
    """These tests compare native with Python themselves; a global OFDM_LINK_NATIVE=python
    (used to run the whole suite on the reference path) must not turn both sides Python."""

    monkeypatch.delenv(native_backend.GLOBAL_ENV, raising=False)
    native_backend.reset()
    yield
    native_backend.reset()


def _python_sig_llrs(values, acquisition, settings):
    """The SIG LLRs exactly as burst_v5._decode_v5_acquired computes them."""

    rx = settings.v5
    numerology = settings.numerology
    backoff = rx.fft_backoff_samples
    ltf = measure_ltf_pair(values, acquisition, numerology, average=rx.ltf_average,
                           backoff=backoff)
    if ltf is None:
        return None
    refined = (
        refine_channel_estimate(ltf.least_squares, numerology,
                                preamble_noise_variance=ltf.noise_variance,
                                preamble_degrees_of_freedom=ltf.degrees_of_freedom,
                                averaged_symbols=2 if rx.ltf_average else 1)
        if rx.channel_smoothing
        else RefinedChannel(ltf.least_squares, ltf.noise_variance, False)
    )
    sig_start = acquisition.stf_start + V5_SIG_OFFSET
    fixed_end = acquisition.stf_start + V5_FIXED_LENGTH
    corrected = correct_v5_cfo(values[sig_start - backoff : fixed_end - backoff],
                               start=sig_start - backoff, acquisition=acquisition)
    equalized = equalize_with_pilots(corrected, refined.estimate, numerology,
                                     first_symbol_index=0, common_phase_only=True)
    return np.clip(
        4.0 * equalized.data_symbols.reshape(-1).real
        / (equalized.noise_enhancement.reshape(-1) * refined.noise_variance),
        -_SIG_LLR_CLIP, _SIG_LLR_CLIP,
    ).astype(np.float32)


def _python_probe(values, acquisition, settings):
    """(kind, detail) of the full decoder's probe: ('wait', n) or ('fail', message)."""

    try:
        _decode_burst_with_acquisition(
            values, settings, acquisition, frame_decoder=DEFAULT_DUAL_VERSION_FRAME_DECODER,
            symbol_demapper=demap_symbols, soft_symbol_demapper=None, input_noise_variance=None,
        )
    except BurstDecodeError as error:
        if error.required_sample_count is not None and error.required_sample_count > values.size:
            return ("wait", error.required_sample_count)
        return ("fail", str(error))
    return ("decoded", None)


def _native_probe(values, acquisition, settings):
    try:
        required = probe_v5_burst(values, acquisition, settings)
    except BurstDecodeError as error:
        return ("fail", str(error))
    return ("wait", required)


def _v5_probes(case: Case):
    """Every v5 acquisition in a case, each with the 560-sample probe buffer the stream uses."""

    values = build(case)
    for found in acquire_all(values, None, SETTINGS.v5.acquisition):
        start = found.stf_start
        if start + V5_FIXED_LENGTH > values.size:
            continue
        yield values[start : start + V5_FIXED_LENGTH], _rebased(found, start)


def _header(llrs):
    try:
        return decode_v5_header(llrs, valid_mcs=_valid_mcs(),
                                max_psdu_bytes=SETTINGS.v5.max_psdu_bytes)
    except (V5HeaderDecodeError, ValueError) as error:
        return type(error).__name__


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_probe_outcomes_and_headers_match(case: Case):
    for values, acquisition in _v5_probes(case):
        assert _native_probe(values, acquisition, SETTINGS) == _python_probe(
            values, acquisition, SETTINGS
        )
        reference = _python_sig_llrs(values, acquisition, SETTINGS)
        status, payload = native.sig_llrs(
            values, acquisition.stf_start, acquisition.ltf_start, acquisition.normalized_cfo,
            SETTINGS.v5.fft_backoff_samples, True, True, _native_header_constants(1))
        if reference is None:
            assert status == 1
            continue
        assert status == 0
        llrs = np.frombuffer(payload, np.float32)
        assert np.max(np.abs(llrs - reference)) <= 1e-4 * max(1.0, float(np.max(np.abs(reference))))
        assert _header(llrs) == _header(reference)


@pytest.mark.parametrize("average", [True, False])
@pytest.mark.parametrize("smoothing", [True, False])
@pytest.mark.parametrize("backoff", [0, 3, 8])
def test_receive_toggles_match(average, smoothing, backoff):
    from dataclasses import replace

    settings = replace(SETTINGS, v5=replace(SETTINGS.v5, ltf_average=average,
                                            channel_smoothing=smoothing,
                                            fft_backoff_samples=backoff))
    for case in (Case("toggles", snr_db=4.0, v5_bursts=5, seed=61),
                 Case("toggles_dc", snr_db=15.0, v5_bursts=5, dc=0.01, seed=62)):
        for values, acquisition in _v5_probes(case):
            assert _native_probe(values, acquisition, settings) == _python_probe(
                values, acquisition, settings
            )


def test_failures_are_classified_like_the_python_probe():
    rng = np.random.default_rng(5)
    noise = (rng.standard_normal(V5_FIXED_LENGTH) + 1j * rng.standard_normal(V5_FIXED_LENGTH))
    zeros = np.zeros(V5_FIXED_LENGTH, np.complex64)
    fake = V5Acquisition(stf_start=0, ltf_start=192, normalized_cfo=0.1, coarse_cfo=0.1,
                         metric=0.5, stf_confidence=0.5, confidence=0.5)
    for values in (noise.astype(np.complex64), zeros):
        python = _python_probe(values, fake, SETTINGS)
        assert python[0] == "fail"
        assert _native_probe(values, fake, SETTINGS) == python


def _decode_all(values, choice, monkeypatch, chunk=4096):
    monkeypatch.setenv(native_backend.env_name("header"), choice)
    native_backend.reset()
    decoder = StreamingBurstDecoder(SETTINGS)
    segments = []
    for start in range(0, values.size, chunk):
        segments.extend(decoder.feed_segments(values[start : start + chunk]))
    native_backend.reset()
    return [(s.stream_offset, s.samples.size, s.acquisition) for s in segments], decoder.snapshot()


@pytest.mark.parametrize("case", [c for c in CASES if c.v5_bursts][:12], ids=lambda c: c.name)
def test_segments_and_counters_are_identical(case, monkeypatch):
    values = build(case)
    assert _decode_all(values, "native", monkeypatch) == _decode_all(values, "python", monkeypatch)


def test_python_backend_skips_the_native_probe(monkeypatch):
    monkeypatch.setenv(native_backend.env_name("header"), "python")
    native_backend.reset()
    try:
        values, acquisition = next(_v5_probes(Case("skip", snr_db=20.0, v5_bursts=1, seed=3)))
        assert probe_v5_burst(values, acquisition, SETTINGS) is None
    finally:
        native_backend.reset()
