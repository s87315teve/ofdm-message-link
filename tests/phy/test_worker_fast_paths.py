"""The worker's NumPy fast paths against the full-search code they replaced.

``measure_quality`` feeds the JMS1 SNR/EVM logger, so its outputs must match
the argmin over every reference point: |dSNR| <= 1e-6 dB, |dEVM| <= 1e-9,
including symbols on a decision boundary.  The separable 16QAM demapper must
give the 16-point max-log LLRs to within rounding.
"""

from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.phy import soft
from ofdm_link.phy.channel import normalized_evm
from ofdm_link.phy.codec import MCS
from ofdm_message_link.link import _constellation, _nearest_reference, measure_quality


def _measure_quality_full_search(payload_symbols, mcs):
    """The pre-2026-11-11 implementation: argmin over all reference points."""

    symbols = np.asarray(payload_symbols, dtype=np.complex128).ravel()
    if symbols.size == 0:
        return (None, None)
    power = float(np.mean(np.abs(symbols) ** 2))
    if not np.isfinite(power) or power <= 0.0:
        return (None, None)
    scaled = symbols / np.sqrt(power)
    reference = np.asarray(_constellation(mcs), dtype=np.complex128)
    nearest = reference[np.argmin(np.abs(scaled[:, None] - reference[None, :]), axis=1)]
    return normalized_evm(nearest, scaled)


def _qam16_full_search(symbols, variances):
    """The pre-2026-11-11 16QAM demapper: masked minima over all 16 points."""

    axis_pairs = np.array(((0, 0), (0, 1), (1, 1), (1, 0)), dtype=np.uint8)
    axis_levels = np.array((-3.0, -1.0, 1.0, 3.0), dtype=np.float64) / np.sqrt(10.0)
    labels = np.column_stack((np.repeat(axis_pairs, 4, axis=0), np.tile(axis_pairs, (4, 1))))
    constellation = (np.repeat(axis_levels, 4) + 1j * np.tile(axis_levels, 4)).astype(
        np.complex128
    )
    distances = np.abs(symbols[:, None] - constellation[None, :]) ** 2
    output = np.empty((symbols.size, 4), dtype=np.float64)
    for bit_index in range(4):
        minimum_zero = np.min(distances[:, labels[:, bit_index] == 0], axis=1)
        minimum_one = np.min(distances[:, labels[:, bit_index] == 1], axis=1)
        output[:, bit_index] = (minimum_one - minimum_zero) / variances
    return output.reshape(-1)


def _received(mcs, count, snr_db, seed, gain=3.2 * np.exp(0.2j)):
    rng = np.random.default_rng(seed)
    reference = np.asarray(_constellation(mcs), dtype=np.complex128)
    sent = reference[rng.integers(0, reference.size, count)]
    noise = (rng.standard_normal(count) + 1j * rng.standard_normal(count)) * np.sqrt(
        10 ** (-snr_db / 10) / 2
    )
    return ((sent + noise) * gain).astype(np.complex64)


@pytest.mark.parametrize("mcs", [MCS.QPSK, MCS.QAM16, MCS.QAM64])
@pytest.mark.parametrize("snr_db", [-3.0, 8.0, 18.0, 30.0])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_measure_quality_matches_the_full_search(mcs, snr_db, seed):
    received = _received(mcs, 4000, snr_db, seed)
    fast = measure_quality(received, mcs)
    full = _measure_quality_full_search(received, mcs)
    assert abs(fast[0] - full[0]) <= 1e-9
    assert abs(fast[1] - full[1]) <= 1e-6


@pytest.mark.parametrize("mcs", [MCS.QPSK, MCS.QAM16, MCS.QAM64])
def test_nearest_point_is_the_argmin_even_on_decision_boundaries(mcs):
    reference = np.asarray(_constellation(mcs), dtype=np.complex128)
    levels = np.unique(reference.real)
    midpoints = (levels[:-1] + levels[1:]) / 2.0
    edges = np.concatenate((levels, midpoints, [levels[0] - 1.0, levels[-1] + 1.0, 0.0]))
    rng = np.random.default_rng(4)
    grid = (edges[:, None] + 1j * edges[None, :]).ravel()
    jitter = rng.choice(edges, 3000) + 1j * rng.uniform(-1.5, 1.5, 3000)
    scaled = np.concatenate((grid, jitter, jitter.imag + 1j * jitter.real, -grid))
    expected = reference[np.argmin(np.abs(scaled[:, None] - reference[None, :]), axis=1)]
    assert np.array_equal(_nearest_reference(scaled, mcs), expected)


def test_degenerate_inputs_still_give_none():
    assert measure_quality(np.zeros(0, np.complex64), MCS.QAM64) == (None, None)
    assert measure_quality(np.zeros(10, np.complex64), MCS.QAM64) == (None, None)


@pytest.mark.parametrize("snr_db", [0.0, 10.0, 20.0, 35.0])
def test_separable_16qam_llrs_match_the_16_point_search(snr_db):
    rng = np.random.default_rng(int(snr_db) + 7)
    symbols = _received(MCS.QAM16, 20000, snr_db, 5, gain=1.0).astype(np.complex128)
    symbols[:50] = 0.0  # exactly on every boundary
    variances = rng.uniform(0.001, 0.5, symbols.size)
    fast = soft._qam16_max_log_llrs(symbols, variances)
    full = _qam16_full_search(symbols, variances)
    scale = np.maximum(1.0, np.abs(full))
    assert np.max(np.abs(fast - full) / scale) <= 1e-9
    clipped = soft.soft_demap_symbols(symbols, MCS.QAM16, variances)
    raw = np.clip(full, -soft.DEFAULT_LLR_CLIP, soft.DEFAULT_LLR_CLIP).astype(np.float32)
    assert np.max(np.abs(clipped.llrs - raw)) <= 1e-5 * soft.DEFAULT_LLR_CLIP
    assert clipped.saturation_count == int(np.count_nonzero(np.abs(full) > soft.DEFAULT_LLR_CLIP))
