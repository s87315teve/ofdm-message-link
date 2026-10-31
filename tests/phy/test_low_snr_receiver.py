"""Receiver behaviour near its threshold: noise estimation, smoothing, headers.

Each test pins one of the receive-only changes that lowered the SNR a burst
needs, without changing a single transmitted sample.
"""

from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    TURBO_BURST_HEADER_VERSION,
    BurstDecodeError,
    Frame,
    FrameKind,
    decode_burst,
    encode_burst,
)
from ofdm_link.phy.burst import BurstConfig, _header_candidates
from ofdm_link.phy.burst_header import (
    BurstHeader,
    BurstHeaderIntegrityError,
    TurboBurstHeader,
    decode_burst_header_list,
    encode_burst_header,
)
from ofdm_link.phy.channel import (
    ChannelEstimate,
    estimate_channel,
    generate_training_symbol,
    refine_channel_estimate,
)
from ofdm_link.phy.ofdm import OfdmNumerology
from ofdm_link.phy.streaming import StreamingBurstDecoder, link_snr_db
from ofdm_link.phy.sync import acquire_preamble, generate_preamble

NUMEROLOGY = OfdmNumerology()
TAPS = np.array([0.82 + 0.13j, 0, 0.21 - 0.17j, 0, 0.08j])
TAPS = TAPS / np.linalg.norm(TAPS)


def _frame(seed: int, mcs: MCS = MCS.QPSK, size: int = 900) -> Frame:
    payload = np.random.default_rng(seed).integers(0, 256, size, dtype=np.uint8).tobytes()
    return Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, mcs, seed & 0xFFFF, payload)


def _channel(tx, snr_db: float, seed: int, *, multipath: bool = False, pad: int = 400):
    """The burst through a fixed channel, 10 kHz CFO at 5 MS/s, and AWGN.

    The SNR is the burst's mean sample power over the noise variance.
    """

    rng = np.random.default_rng(seed)
    x = np.concatenate((np.zeros(pad), tx, np.zeros(pad))).astype(np.complex128)
    if multipath:
        x = np.convolve(x, TAPS)[: x.size]
    x *= np.exp(2j * np.pi * 10e3 / 5e6 * np.arange(x.size))
    variance = np.mean(np.abs(tx) ** 2) / 10 ** (snr_db / 10)
    noise = rng.standard_normal(x.size) + 1j * rng.standard_normal(x.size)
    return (x + np.sqrt(variance / 2) * noise).astype(np.complex64), variance


# -- preamble noise estimate ------------------------------------------------


@pytest.mark.parametrize("multipath", [False, True])
def test_preamble_splits_power_into_signal_and_noise(multipath):
    """Averaged over bursts the estimate is unbiased, with or without multipath."""

    tx = encode_burst(_frame(1), wire_version=TURBO_BURST_HEADER_VERSION).samples
    ratios = []
    for seed in range(60):
        rx, variance = _channel(tx, 10.0, seed, multipath=multipath)
        acquisition = acquire_preamble(rx)
        assert acquisition is not None
        ratios.append(acquisition.noise_variance / variance)
    assert np.mean(ratios) == pytest.approx(1.0, abs=0.08)


def test_link_snr_reads_the_burst_snr_within_half_a_db():
    tx = encode_burst(_frame(2), wire_version=TURBO_BURST_HEADER_VERSION).samples
    for snr_db in (5.0, 10.0, 15.0):
        readings = [
            link_snr_db(acquire_preamble(_channel(tx, snr_db, seed)[0]), NUMEROLOGY)
            for seed in range(80)
        ]
        assert np.mean(readings) == pytest.approx(snr_db, abs=0.5)


def test_noiseless_preamble_reports_a_finite_capped_snr():
    acquisition = acquire_preamble(
        np.concatenate((np.zeros(50), generate_preamble(), np.zeros(200))).astype(np.complex64)
    )
    assert acquisition is not None
    assert acquisition.noise_variance > 0.0
    assert 10 * np.log10(acquisition.preamble_snr) == pytest.approx(60.0, abs=0.1)


# -- channel smoothing ------------------------------------------------------


def _received_training(channel_taps, noise_variance, seed, *, delay=0):
    rng = np.random.default_rng(seed)
    training = generate_training_symbol(NUMEROLOGY).samples.astype(np.complex128)
    block = np.concatenate((np.zeros(delay + 32), training, np.zeros(32)))
    received = np.convolve(block, channel_taps)[: block.size]
    received += np.sqrt(noise_variance / 2) * (
        rng.standard_normal(received.size) + 1j * rng.standard_normal(received.size)
    )
    start = 32 + delay
    return received[start : start + training.size]


def _true_response(channel_taps, delay=0):
    carriers = np.asarray(NUMEROLOGY.active_carriers)
    lags = np.arange(len(channel_taps)) + delay
    return np.exp(-2j * np.pi * np.outer(carriers, lags) / NUMEROLOGY.fft_size) @ channel_taps


def test_smoothing_cuts_the_channel_error_and_estimates_the_noise():
    noise_variance = 0.1
    raw_errors, smooth_errors, variances = [], [], []
    truth = _true_response(TAPS)
    for seed in range(200):
        received = _received_training(TAPS, noise_variance, seed)
        raw = estimate_channel(received, NUMEROLOGY)
        refined = refine_channel_estimate(raw, NUMEROLOGY)
        assert refined.smoothed
        raw_errors.append(np.mean(np.abs(raw.coefficients - truth) ** 2))
        smooth_errors.append(np.mean(np.abs(refined.estimate.coefficients - truth) ** 2))
        variances.append(refined.noise_variance)
    # 20 taps fitted to 52 carriers keep 20/52 of the noise: about -4 dB.
    assert np.mean(smooth_errors) / np.mean(raw_errors) == pytest.approx(20 / 52, rel=0.15)
    assert np.mean(variances) == pytest.approx(noise_variance, rel=0.1)


def test_smoothing_pools_with_the_preamble_and_falls_back_off_the_window():
    received = _received_training(TAPS, 0.01, 3)
    raw = estimate_channel(received, NUMEROLOGY)
    pooled = refine_channel_estimate(
        raw, NUMEROLOGY, preamble_noise_variance=0.01, preamble_degrees_of_freedom=32
    )
    assert pooled.smoothed
    assert pooled.noise_variance == pytest.approx(0.01, rel=0.4)

    # A response 30 samples late does not fit the delay window, so the raw
    # estimate is kept and only the preamble's variance is believed.
    late = ChannelEstimate(
        _true_response(np.array([1.0]), delay=30).astype(np.complex64),
        NUMEROLOGY.active_carriers,
    )
    fallback = refine_channel_estimate(late, NUMEROLOGY, preamble_noise_variance=0.01)
    assert not fallback.smoothed
    assert fallback.estimate is late
    assert fallback.noise_variance == 0.01


# -- soft payload input -----------------------------------------------------


def test_turbo_decoder_no_longer_receives_saturated_llrs():
    """Without a noise variance the LLRs were all clipped: hard decisions."""

    tx = encode_burst(_frame(4), wire_version=TURBO_BURST_HEADER_VERSION).samples
    rx, variance = _channel(tx, 8.0, 4)
    decoded = decode_burst(rx)
    diagnostics = decoded.diagnostics
    assert diagnostics.llr_saturation_rate < 0.05
    # The equalized variance is what the demapper used; its mean over the
    # data carriers tracks the channel noise it was derived from.
    assert diagnostics.estimated_noise_variance == pytest.approx(variance, rel=0.5)


@pytest.mark.parametrize(
    ("mcs", "snr_db", "multipath"),
    [(MCS.QPSK, 5.0, False), (MCS.QPSK, 7.0, True), (MCS.QAM16, 9.0, False)],
)
def test_bursts_decode_where_the_v1_receiver_lost_most_of_them(mcs, snr_db, multipath):
    """v1.0.0 delivered none of these 60 bursts; the simulation sweep has the curves."""

    delivered = 0
    for seed in range(20):
        frame = _frame(100 + seed, mcs)
        tx = encode_burst(frame, wire_version=TURBO_BURST_HEADER_VERSION).samples
        rx, _ = _channel(tx, snr_db, 500 + seed, multipath=multipath)
        try:
            delivered += decode_burst(rx).frame == frame
        except BurstDecodeError:
            pass
    assert delivered >= 18


# -- maximum-likelihood header ----------------------------------------------


def _candidates():
    config = BurstConfig()
    return _header_candidates(config.header_list_max_payload_bytes, config.max_coded_payload_bits)


def test_header_list_holds_every_header_the_link_sends():
    candidates = _candidates()
    for wire_version in (1, 2, 3, 4):
        for mcs in MCS:
            for size in (0, 1, 900, 996):
                frame = _frame(size, mcs, size)
                header = encode_burst(frame, wire_version=wire_version).header
                assert header in candidates.headers


def test_header_list_recovers_a_header_the_crc_paths_cannot():
    candidates = _candidates()
    header = TurboBurstHeader(TURBO_BURST_HEADER_VERSION, MCS.QPSK, 88 + 8 * 900)
    signs = 1.0 - 2.0 * encode_burst_header(header).astype(np.float64)
    rng = np.random.default_rng(7)
    recovered = 0
    for _ in range(50):
        # 0 dB per QPSK symbol: about 15 % of the coded bits arrive wrong.
        llrs = signs / np.sqrt(2) + np.sqrt(0.5) * rng.standard_normal(signs.size)
        recovered += decode_burst_header_list(llrs, candidates) == header
    assert recovered == 50


def test_header_list_refuses_noise():
    candidates = _candidates()
    rng = np.random.default_rng(8)
    for _ in range(200):
        with pytest.raises(BurstHeaderIntegrityError):
            decode_burst_header_list(rng.standard_normal(288), candidates)


def test_header_list_size_is_configurable():
    assert BurstConfig(header_list_max_payload_bytes=0).header_list_max_payload_bytes == 0
    with pytest.raises(ValueError):
        BurstConfig(header_list_max_payload_bytes=-1)
    small = _header_candidates(100, BurstConfig().max_coded_payload_bits)
    assert len(small.headers) < len(_candidates().headers)
    assert BurstHeader(1, MCS.QPSK, 100) not in small.headers


# -- per-burst SNR from the stream ------------------------------------------


def test_stream_reports_one_snr_per_burst_including_lost_ones():
    """A burst whose payload fails still reports what the channel was."""

    good = encode_burst(_frame(10), wire_version=TURBO_BURST_HEADER_VERSION).samples
    corrupted = encode_burst(_frame(11), wire_version=TURBO_BURST_HEADER_VERSION).samples.copy()
    # Wreck part of the payload; preamble, training and header stay intact.
    corrupted[2000:4000] = np.random.default_rng(11).standard_normal(2000)
    stream = np.concatenate(
        (np.zeros(500), good, np.zeros(500), corrupted, np.zeros(500), good, np.zeros(500))
    ).astype(np.complex64)
    rng = np.random.default_rng(12)
    noise = rng.standard_normal(stream.size) + 1j * rng.standard_normal(stream.size)
    stream += (0.01 * noise).astype(np.complex64)
    decoder = StreamingBurstDecoder()
    decoded = decoder.feed(stream)
    snrs = decoder.drain_link_snr_db()
    assert len(decoded) == 2
    assert len(snrs) == 3
    assert all(30.0 < snr < 40.0 for snr in snrs)
    assert decoder.drain_link_snr_db() == ()
