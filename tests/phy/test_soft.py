from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.phy import MCS, PhyCodecError, demap_symbols, map_symbols
from ofdm_link.phy.soft import (
    DEFAULT_LLR_CLIP,
    LLR_CONVENTION,
    llrs_to_hard_bits,
    soft_demap_symbols,
)


def test_qpsk_analytic_llrs_use_positive_for_bit_zero() -> None:
    symbols = np.array([(1 + 1j) / np.sqrt(2), (-1 + 1j) / np.sqrt(2)])

    result = soft_demap_symbols(symbols, MCS.QPSK, 0.5)

    np.testing.assert_allclose(result.llrs, [4.0, 4.0, -4.0, 4.0], rtol=1e-6)
    assert LLR_CONVENTION == "positive-log-p0-over-p1"


def test_gray_qam16_analytic_max_log_llrs_follow_wire_order() -> None:
    symbols = np.array([(-3 - 3j) / np.sqrt(10), (1 - 1j) / np.sqrt(10)])

    result = soft_demap_symbols(symbols, MCS.QAM16, 0.5)

    np.testing.assert_allclose(
        result.llrs,
        [3.2, 0.8, 3.2, 0.8, -0.8, -0.8, 0.8, -0.8],
        rtol=1e-6,
    )


def test_gray_qam64_analytic_max_log_llrs_follow_wire_order() -> None:
    symbols = np.array([(-7 - 7j) / np.sqrt(42), (1 + 5j) / np.sqrt(42)])

    result = soft_demap_symbols(symbols, MCS.QAM64, 0.5)

    # Squared distance in units of 1/42, divided by 0.5: d / 21.
    np.testing.assert_allclose(
        result.llrs,
        np.array([64, 16, 4, 64, 16, 4, -4, -16, 4, -36, 4, -4]) / 21.0,
        rtol=1e-6,
    )


def test_qam64_per_axis_llrs_equal_the_full_constellation_max_log() -> None:
    from itertools import product

    labels = np.array(list(product((0, 1), repeat=6)), dtype=np.uint8)
    constellation = map_symbols(labels.reshape(-1), MCS.QAM64).astype(np.complex128)
    rng = np.random.default_rng(2)
    symbols = rng.normal(scale=0.7, size=(500, 2)) @ np.array([1.0, 1j])
    variances = rng.uniform(0.05, 0.5, symbols.size)

    result = soft_demap_symbols(symbols, MCS.QAM64, variances, llr_clip=1e9)

    distances = np.abs(symbols[:, None] - constellation[None, :]) ** 2
    expected = np.column_stack(
        [
            (
                np.min(distances[:, labels[:, bit] == 1], axis=1)
                - np.min(distances[:, labels[:, bit] == 0], axis=1)
            )
            / variances
            for bit in range(6)
        ]
    ).reshape(-1)
    np.testing.assert_allclose(result.llrs, expected, rtol=1e-5, atol=1e-4)


def test_qam64_chunking_does_not_change_the_result() -> None:
    rng = np.random.default_rng(3)
    symbols = rng.normal(scale=0.7, size=(20_000, 2)) @ np.array([1.0, 1j])

    whole = soft_demap_symbols(symbols, MCS.QAM64, 0.1).llrs
    pieces = np.concatenate(
        [soft_demap_symbols(part, MCS.QAM64, 0.1).llrs for part in np.split(symbols, 8)]
    )

    np.testing.assert_array_equal(whole, pieces)


@pytest.mark.parametrize("mcs", list(MCS))
def test_llr_sign_matches_existing_hard_demapper_away_from_ties(mcs: MCS) -> None:
    bits = np.tile(np.arange(256, dtype=np.uint16).view(np.uint8), 4)
    bit_count = bits.size - bits.size % mcs.bits_per_symbol
    wire_bits = np.unpackbits(bits, bitorder="big")[:bit_count]
    symbols = map_symbols(wire_bits, mcs)

    result = soft_demap_symbols(symbols, mcs, 0.2)

    np.testing.assert_array_equal(llrs_to_hard_bits(result.llrs), demap_symbols(symbols, mcs))


def test_llr_magnitude_scales_inverse_to_noise_variance_and_equalizer_gain() -> None:
    symbol = np.array([(1 + 1j) / np.sqrt(2)])

    baseline = soft_demap_symbols(symbol, MCS.QPSK, 0.5).llrs
    twice_noise = soft_demap_symbols(symbol, MCS.QPSK, 1.0).llrs
    half_gain_noise_enhancement = soft_demap_symbols(symbol, MCS.QPSK, 2.0).llrs

    np.testing.assert_allclose(twice_noise, baseline / 2.0)
    np.testing.assert_allclose(half_gain_noise_enhancement, baseline / 4.0)


def test_llr_clipping_reports_saturation_without_nonfinite_output() -> None:
    symbols = np.array([(1 + 1j) / np.sqrt(2)])

    result = soft_demap_symbols(symbols, MCS.QPSK, 1e-300)

    np.testing.assert_array_equal(result.llrs, [DEFAULT_LLR_CLIP, DEFAULT_LLR_CLIP])
    assert result.saturation_count == 2
    assert result.saturation_rate == 1.0


def test_empty_input_is_well_defined_with_scalar_variance() -> None:
    result = soft_demap_symbols([], MCS.QPSK, 0.25)

    assert result.llrs.size == 0
    assert result.saturation_count == 0
    assert result.noise_variance_mean == 0.25


@pytest.mark.parametrize(
    ("symbols", "variance", "message"),
    [
        ([complex(np.nan, 0.0)], 1.0, "finite"),
        ([1 + 1j], 0.0, "positive"),
        ([1 + 1j], np.inf, "positive"),
        ([1 + 1j], [1.0, 2.0], "match"),
        ([], [], "scalar"),
    ],
)
def test_soft_demapper_fails_closed_on_invalid_inputs(
    symbols: object,
    variance: object,
    message: str,
) -> None:
    with pytest.raises(PhyCodecError, match=message):
        soft_demap_symbols(symbols, MCS.QPSK, variance)
