from __future__ import annotations

from functools import lru_cache

import numpy as np
import pytest

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    Frame,
    FrameKind,
    encode_burst,
    encode_burst_v5,
    mcs_entry,
)
from ofdm_link.phy.channel import generate_training_symbol
from ofdm_link.phy.sync import (
    AcquisitionResult,
    SyncConfig,
    _acquisition_candidates,
    acquire_all,
    separated_candidates,
    shared_correlation,
)
from ofdm_link.phy.sync_v5 import (
    V5_LTF_FIRST_SYMBOL_OFFSET,
    V5Acquisition,
    V5SyncConfig,
    generate_ltf,
    generate_stf,
    measure_ltf_pair,
    v5_candidates,
)

LEGACY = SyncConfig()
V5 = V5SyncConfig()


@lru_cache(maxsize=8)
def _v5_burst(body_bytes: int = 200, mcs_index: int = 8, seed: int = 0) -> np.ndarray:
    body = np.random.default_rng(seed).integers(0, 256, body_bytes, dtype=np.uint8).tobytes()
    samples = encode_burst_v5(body, mcs_entry(mcs_index), scrambler_seed=1 + seed % 127).samples
    return np.asarray(samples)


@lru_cache(maxsize=8)
def _legacy_burst(wire_version: int, size: int = 200) -> np.ndarray:
    payload = np.random.default_rng(wire_version).integers(0, 256, size, dtype=np.uint8).tobytes()
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, wire_version, payload)
    return np.asarray(encode_burst(frame, wire_version=wire_version).samples)


def _awgn(samples: np.ndarray, snr_db: float, rng: np.random.Generator, power: float) -> np.ndarray:
    variance = power / 10 ** (snr_db / 10)
    noise = rng.standard_normal(samples.size) + 1j * rng.standard_normal(samples.size)
    return (samples + np.sqrt(variance / 2) * noise).astype(np.complex64)


def test_stf_is_sixteen_periodic_with_the_data_symbols_mean_power() -> None:
    stf = generate_stf()

    assert stf.shape == (160,)
    assert np.allclose(stf[16:], stf[:-16])
    assert np.isclose(np.mean(np.abs(stf) ** 2), 52 / 64, rtol=1e-6)
    spectrum = np.fft.fft(stf[:64], norm="ortho")
    occupied = np.flatnonzero(np.abs(spectrum) > 1e-6)
    assert set(((occupied + 32) % 64 - 32).tolist()) == {
        -24, -20, -16, -12, -8, -4, 4, 8, 12, 16, 20, 24
    }


def test_ltf_is_the_legacy_training_symbol_twice_behind_a_double_guard() -> None:
    ltf = generate_ltf()
    useful = generate_training_symbol().samples[16:]

    assert ltf.shape == (160,)
    assert np.allclose(ltf[32:96], useful)
    assert np.allclose(ltf[96:], useful)
    assert np.allclose(ltf[:32], useful[32:])
    assert np.isclose(np.mean(np.abs(ltf) ** 2), 52 / 64, rtol=1e-6)


@pytest.mark.parametrize("offset", [0, 1, 7, 16, 31])
def test_clean_burst_is_acquired_at_its_exact_start(offset: int) -> None:
    burst = _v5_burst()
    stream = np.concatenate((np.zeros(16 + offset), burst, np.zeros(64))).astype(np.complex64)

    found = acquire_all(stream, LEGACY, V5)

    assert len(found) == 1 and isinstance(found[0], V5Acquisition)
    assert found[0].stf_start == 16 + offset
    assert found[0].ltf_start == 16 + offset + V5_LTF_FIRST_SYMBOL_OFFSET
    assert found[0].preamble_start == found[0].stf_start


def test_clean_burst_after_random_noise_and_long_zero_idle_is_acquired_exactly() -> None:
    rng = np.random.default_rng(3)
    burst = _v5_burst()
    stream = np.concatenate(
        (0.01 * rng.standard_normal(500), np.zeros(1000), burst, np.zeros(400))
    ).astype(np.complex64)

    (found,) = acquire_all(stream, LEGACY, V5)

    assert isinstance(found, V5Acquisition) and found.stf_start == 1500


@pytest.mark.parametrize("cfo", [-1.5, -0.7, 0.0, 0.4, 1.5])
def test_cfo_up_to_one_and_a_half_subcarriers_is_estimated_within_0_01(cfo: float) -> None:
    rng = np.random.default_rng(int(10 * cfo) + 50)
    burst = _v5_burst()
    stream = np.concatenate((np.zeros(37), burst, np.zeros(300)))
    stream = stream * np.exp(2j * np.pi * cfo * np.arange(stream.size) / 64)
    stream = _awgn(stream, 20.0, rng, 52 / 64)

    (found,) = acquire_all(stream, LEGACY, V5)

    assert isinstance(found, V5Acquisition)
    assert found.stf_start == 37
    assert abs(found.normalized_cfo - cfo) < 0.01


def test_v5_preamble_is_detected_at_0_db_wideband_snr() -> None:
    rng = np.random.default_rng(20261101)
    burst = _v5_burst()
    detected = 0
    timing_errors: list[int] = []
    for _ in range(40):
        stream = np.concatenate((np.zeros(300), burst, np.zeros(300)))
        stream = stream * np.exp(2j * np.pi * 0.2 * np.arange(stream.size) / 64)
        found = [item for item in acquire_all(_awgn(stream, 0.0, rng, 52 / 64), None, V5)]
        if found:
            detected += 1
            timing_errors.append(abs(found[0].stf_start - 300))
    assert detected >= 38
    assert max(timing_errors) <= 2


def test_white_noise_produces_no_v5_candidate() -> None:
    rng = np.random.default_rng(11)
    noise = (rng.standard_normal(1_000_000) + 1j * rng.standard_normal(1_000_000)).astype(
        np.complex64
    )

    assert acquire_all(noise, None, V5) == ()


def test_tones_and_dc_are_rejected_by_the_coherent_stf_stage() -> None:
    n = np.arange(20_000)
    rng = np.random.default_rng(12)
    for carrier in (0.0, 4.0, 8.0, 12.5):
        signal = np.exp(2j * np.pi * carrier * n / 64) + 0.3
        signal = _awgn(signal, 30.0, rng, 1.0)
        shared = shared_correlation(signal)
        # The M128 gate passes a tone everywhere; the STF stage must stop it.
        assert v5_candidates(signal, shared, V5) == ()


@pytest.mark.parametrize("wire_version", [1, 2, 3, 4])
def test_legacy_bursts_never_produce_a_v5_acquisition(wire_version: int) -> None:
    rng = np.random.default_rng(wire_version)
    stream = np.concatenate((np.zeros(200), _legacy_burst(wire_version), np.zeros(600)))
    for snr in (40.0, 15.0):
        noisy = _awgn(stream, snr, rng, 52 / 64)
        assert acquire_all(noisy, None, V5) == ()
        found = acquire_all(noisy, LEGACY, V5)
        assert [type(item) for item in found] == [AcquisitionResult]
        assert found[0].preamble_start == 200


def test_v5_bursts_never_produce_a_legacy_acquisition() -> None:
    rng = np.random.default_rng(13)
    for mcs_index in (8, 12, 16):
        stream = np.concatenate((np.zeros(200), _v5_burst(300, mcs_index), np.zeros(600)))
        for snr in (40.0, 15.0):
            noisy = _awgn(stream, snr, rng, 52 / 64)
            assert acquire_all(noisy, LEGACY, None) == ()
            found = acquire_all(noisy, LEGACY, V5)
            assert [type(item) for item in found] == [V5Acquisition]


def test_mixed_stream_is_acquired_in_order_with_the_right_families() -> None:
    gap = np.zeros(80)
    parts = [
        _legacy_burst(1),
        _v5_burst(150, 8, 1),
        _legacy_burst(2),
        _v5_burst(400, 13, 2),
        _legacy_burst(4),
        _v5_burst(19 - 4, 17, 3),
    ]
    pieces = [gap]
    starts = []
    cursor = gap.size
    for part in parts:
        starts.append(cursor)
        pieces.extend((part, gap))
        cursor += part.size + gap.size
    stream = _awgn(np.concatenate(pieces + [np.zeros(400)]), 25.0, np.random.default_rng(14), 0.8)

    found = acquire_all(stream, LEGACY, V5)

    assert [item.preamble_start for item in found] == starts
    assert [type(item) for item in found] == [
        AcquisitionResult,
        V5Acquisition,
        AcquisitionResult,
        V5Acquisition,
        AcquisitionResult,
        V5Acquisition,
    ]


def test_shared_correlation_leaves_the_legacy_scan_bit_identical() -> None:
    rng = np.random.default_rng(15)
    stream = np.concatenate(
        (np.zeros(100), _legacy_burst(2), np.zeros(80), _v5_burst(), np.zeros(500))
    )
    noisy = _awgn(stream, 8.0, rng, 0.8)

    plain = _acquisition_candidates(noisy, LEGACY)
    shared = _acquisition_candidates(noisy, LEGACY, shared=shared_correlation(noisy))

    assert plain == shared
    assert acquire_all(noisy, LEGACY, None) == separated_candidates(plain, LEGACY.preamble_length)


def test_ltf_pair_measures_the_channel_snr_and_flat_gain() -> None:
    rng = np.random.default_rng(16)
    burst = _v5_burst()
    stream = 0.5 * np.exp(1j * 0.7) * np.concatenate((np.zeros(100), burst, np.zeros(400)))
    signal_power = 0.25
    noise_variance = signal_power / 10 ** (12 / 10)
    readings = []
    for _ in range(30):
        noisy = (
            stream
            + np.sqrt(noise_variance / 2)
            * (rng.standard_normal(stream.size) + 1j * rng.standard_normal(stream.size))
        ).astype(np.complex64)
        (acquisition,) = acquire_all(noisy, None, V5)
        measurement = measure_ltf_pair(noisy, acquisition)
        assert measurement is not None and measurement.degrees_of_freedom == 52
        readings.append(measurement)
    signal = np.mean([item.signal_power for item in readings])
    noise = np.mean([item.noise_variance for item in readings])
    assert signal == pytest.approx(signal_power, rel=0.05)
    assert noise == pytest.approx(noise_variance, rel=0.08)
    gains = np.mean([item.least_squares.coefficients for item in readings], axis=0)
    # Per carrier the averaged estimate keeps about 0.016 rms of noise here.
    assert np.allclose(gains, 0.5 * np.exp(1j * 0.7), atol=0.07)
    assert abs(np.mean(gains) - 0.5 * np.exp(1j * 0.7)) < 0.015


def test_v5_config_is_validated() -> None:
    with pytest.raises(ValueError):
        V5SyncConfig(detection_threshold=0.0)
    with pytest.raises(ValueError):
        V5SyncConfig(fine_timing_search=64)
    with pytest.raises(ValueError):
        V5SyncConfig(prefilter=1)  # type: ignore[arg-type]


def test_prefilter_keeps_acquisition_aligned_with_the_raw_samples() -> None:
    rng = np.random.default_rng(17)
    stream = np.concatenate((np.zeros(123), _v5_burst(), np.zeros(300)))
    noisy = _awgn(stream, 10.0, rng, 0.8)

    (found,) = acquire_all(noisy, LEGACY, V5SyncConfig(prefilter=True))

    assert isinstance(found, V5Acquisition) and abs(found.stf_start - 123) <= 1


def test_no_cross_family_acquisition_at_low_snr() -> None:
    """Near the detection edge each detector must still reject the other's bursts."""

    rng = np.random.default_rng(18)
    legacy = np.concatenate((np.zeros(200), _legacy_burst(2), np.zeros(600)))
    v5 = np.concatenate((np.zeros(200), _v5_burst(), np.zeros(600)))
    for snr in (0.0, 3.0, 6.0):
        for _ in range(20):
            assert acquire_all(_awgn(legacy, snr, rng, 0.8), None, V5) == ()
            assert acquire_all(_awgn(v5, snr, rng, 0.8), LEGACY, None) == ()
