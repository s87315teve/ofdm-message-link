from __future__ import annotations

from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from ofdm_link.phy.channel import (
    ChannelEstimate,
    ChannelEstimationError,
    conjugate_rotation,
    equalize_with_pilots,
    estimate_channel,
    generate_training_symbol,
    normalized_evm,
)
from ofdm_link.phy.codec import MCS, demap_symbols, map_symbols
from ofdm_link.phy.ofdm import OfdmNumerology, modulate_ofdm

_KNOWN_TRAINING = np.array(
    [
        1,
        1,
        -1,
        -1,
        1,
        1,
        -1,
        1,
        -1,
        1,
        1,
        1,
        1,
        1,
        1,
        -1,
        -1,
        1,
        1,
        -1,
        1,
        -1,
        1,
        1,
        1,
        1,
        1,
        -1,
        -1,
        1,
        1,
        -1,
        1,
        -1,
        1,
        -1,
        -1,
        -1,
        -1,
        -1,
        1,
        1,
        -1,
        -1,
        1,
        -1,
        1,
        -1,
        1,
        1,
        1,
        1,
    ],
    dtype=np.complex64,
)


def test_training_symbol_is_an_exact_reproducible_known_vector() -> None:
    numerology = OfdmNumerology()

    first = generate_training_symbol(numerology)
    second = generate_training_symbol(numerology)

    np.testing.assert_array_equal(first.active_symbols, _KNOWN_TRAINING)
    np.testing.assert_array_equal(first.samples, second.samples)
    np.testing.assert_array_equal(first.samples[:16], first.samples[-16:])
    np.testing.assert_allclose(
        first.samples[:4],
        np.array(
            [
                0.5 + 0.5j,
                0.9539126158 + 0.0327647328j,
                -0.1798656285 - 1.2852586508j,
                0.4693501592 + 0.1195119619j,
            ],
            dtype=np.complex64,
        ),
        rtol=1e-7,
        atol=1e-7,
    )
    assert first.samples.shape == (80,)
    assert np.all(np.abs(first.active_symbols) == 1)
    assert not first.samples.flags.writeable
    assert not first.active_symbols.flags.writeable
    with pytest.raises(FrozenInstanceError):
        first.samples = np.zeros(80, dtype=np.complex64)  # type: ignore[misc]


def test_flat_complex_gain_channel_estimate() -> None:
    training = generate_training_symbol()
    gain = np.complex64(0.73 * np.exp(0.61j))

    estimate = estimate_channel(training.samples * gain)

    np.testing.assert_allclose(estimate.coefficients, gain, rtol=2e-6, atol=2e-6)
    assert estimate.active_carriers == OfdmNumerology().active_carriers
    assert not estimate.coefficients.flags.writeable
    with pytest.raises(FrozenInstanceError):
        estimate.active_carriers = ()  # type: ignore[misc]


def test_three_tap_channel_within_cp_is_estimated_per_active_carrier() -> None:
    numerology = OfdmNumerology()
    training = generate_training_symbol(numerology)
    taps = np.array([0.82 + 0.13j, 0, 0.21 - 0.17j], dtype=np.complex64)
    received = np.convolve(training.samples, taps)[: training.samples.size]

    estimate = estimate_channel(received, numerology)

    expected = np.fft.fft(taps, n=numerology.fft_size)[
        np.asarray(numerology.active_carriers) % numerology.fft_size
    ]
    np.testing.assert_allclose(estimate.coefficients, expected, rtol=2e-6, atol=2e-6)


def test_equalizer_recovers_cpe_and_linear_phase_slope_for_each_symbol() -> None:
    numerology = OfdmNumerology()
    payload = np.exp(1j * np.arange(3 * 48, dtype=np.float32) / 7).astype(np.complex64)
    waveform = modulate_ofdm(payload, numerology, first_symbol_index=4)
    channel = (
        np.linspace(0.65, 1.31, 52, dtype=np.float32)
        * np.exp(1j * np.linspace(-0.4, 0.7, 52, dtype=np.float32))
    ).astype(np.complex64)
    cpe = np.array([-0.72, 0.23, 1.08], dtype=np.float64)
    slope = np.array([0.014, -0.019, 0.027], dtype=np.float64)
    received = _apply_frequency_response_and_phase(
        waveform.samples,
        numerology,
        channel,
        cpe,
        slope,
    )

    result = equalize_with_pilots(
        received,
        ChannelEstimate(channel, numerology.active_carriers),
        numerology,
        first_symbol_index=4,
    )

    np.testing.assert_allclose(result.common_phase_rad, cpe, rtol=1e-5, atol=1e-6)
    np.testing.assert_allclose(
        result.phase_slope_rad_per_carrier, slope, rtol=1e-5, atol=1e-6
    )
    np.testing.assert_allclose(result.data_symbols.reshape(-1), payload, rtol=2e-5, atol=2e-5)
    assert result.data_symbols.shape == (3, 48)
    assert not result.data_symbols.flags.writeable


def test_post_equalization_noise_variance_includes_channel_gain() -> None:
    numerology = OfdmNumerology()
    payload = np.ones(2 * 48, dtype=np.complex64)
    waveform = modulate_ofdm(payload, numerology, first_symbol_index=2)
    channel = np.full(52, 0.5 + 0.0j, dtype=np.complex64)
    received = _apply_frequency_response_and_phase(
        waveform.samples,
        numerology,
        channel,
        np.zeros(2),
        np.zeros(2),
    )

    result = equalize_with_pilots(
        received,
        ChannelEstimate(channel, numerology.active_carriers),
        numerology,
        first_symbol_index=2,
    )

    np.testing.assert_allclose(result.noise_enhancement, 4.0)
    np.testing.assert_allclose(result.post_equalization_noise_variance(0.025), 0.1)
    assert not result.noise_enhancement.flags.writeable


@pytest.mark.parametrize("variance", [0.0, -1.0, np.nan, np.inf, True])
def test_post_equalization_noise_variance_rejects_invalid_input(variance: object) -> None:
    waveform = modulate_ofdm(np.ones(48, dtype=np.complex64))
    result = equalize_with_pilots(
        waveform.samples,
        ChannelEstimate(np.ones(52, dtype=np.complex64), OfdmNumerology().active_carriers),
    )

    with pytest.raises(ChannelEstimationError, match="noise variance"):
        result.post_equalization_noise_variance(variance)  # type: ignore[arg-type]


@pytest.mark.parametrize("mcs", list(MCS))
def test_equalized_qpsk_and_qam16_symbols_recover_payload_bits(mcs: MCS) -> None:
    numerology = OfdmNumerology()
    bit_count = 3 * 48 * mcs.bits_per_symbol
    bits = np.random.default_rng(9841).integers(0, 2, bit_count, dtype=np.uint8)
    payload = map_symbols(bits, mcs)
    waveform = modulate_ofdm(payload, numerology, first_symbol_index=7)
    channel = (
        np.linspace(0.77, 1.18, 52, dtype=np.float32)
        * np.exp(1j * np.linspace(-0.3, 0.45, 52, dtype=np.float32))
    ).astype(np.complex64)
    cpe = np.array([0.4, -0.35, 0.82])
    slope = np.array([0.011, -0.006, 0.018])
    received = _apply_frequency_response_and_phase(
        waveform.samples,
        numerology,
        channel,
        cpe,
        slope,
    )

    result = equalize_with_pilots(
        received,
        ChannelEstimate(channel, numerology.active_carriers),
        numerology,
        first_symbol_index=7,
    )

    recovered_bits = demap_symbols(result.data_symbols.reshape(-1), mcs)
    np.testing.assert_array_equal(recovered_bits, bits)


def test_payload_equalizer_corrects_bounded_quadratic_pilot_magnitude_droop() -> None:
    numerology = OfdmNumerology()
    payload = map_symbols(
        np.random.default_rng(8421).integers(0, 2, 3 * 48 * 4, dtype=np.uint8),
        MCS.QAM16,
    )
    waveform = modulate_ofdm(payload, numerology, first_symbol_index=3)
    blocks = waveform.samples.reshape(3, numerology.fft_size + numerology.cp_length)
    grid = np.fft.fft(blocks[:, numerology.cp_length :], axis=1, norm="ortho")
    active_carriers = np.asarray(numerology.active_carriers)
    active_bins = active_carriers % numerology.fft_size
    symbol_scale = np.array([0.2, 0.35, 0.5])[:, np.newaxis]
    log_magnitude = -symbol_scale * (active_carriers[np.newaxis, :] / 26.0) ** 2
    grid[:, active_bins] *= np.exp(log_magnitude)
    useful = np.fft.ifft(grid, axis=1, norm="ortho")
    received = np.concatenate(
        (useful[:, -numerology.cp_length :], useful), axis=1
    ).reshape(-1)

    result = equalize_with_pilots(
        received,
        ChannelEstimate(np.ones(52, dtype=np.complex64), numerology.active_carriers),
        numerology,
        first_symbol_index=3,
        correct_pilot_magnitude=True,
    )

    np.testing.assert_allclose(result.data_symbols.reshape(-1), payload, rtol=2e-5, atol=2e-5)
    assert result.magnitude_diagnostics.enabled
    assert result.magnitude_diagnostics.correction_gain_max < 2.0
    assert result.magnitude_diagnostics.clamp_count == 0
    assert result.magnitude_diagnostics.fallback_count == 0


@pytest.mark.parametrize(
    "reference",
    [
        np.ones(51, dtype=np.complex64),
        np.r_[np.zeros(1, dtype=np.complex64), np.ones(51, dtype=np.complex64)],
        np.r_[np.array([complex(np.nan, 0)]), np.ones(51)],
    ],
)
def test_channel_estimator_rejects_malformed_or_low_reference(
    reference: np.ndarray,
) -> None:
    training = generate_training_symbol()

    with pytest.raises(ChannelEstimationError, match="reference_active_symbols"):
        estimate_channel(training.samples, reference_active_symbols=reference)


def test_channel_estimator_and_equalizer_reject_unusable_inputs() -> None:
    training = generate_training_symbol()
    with pytest.raises(ChannelEstimationError, match="80 samples"):
        estimate_channel(training.samples[:-1])
    with pytest.raises(ChannelEstimationError, match="finite"):
        estimate_channel(np.r_[training.samples[:-1], complex(np.inf, 0)])
    with pytest.raises(ChannelEstimationError, match="near-zero"):
        ChannelEstimate(np.r_[np.zeros(1), np.ones(51)], OfdmNumerology().active_carriers)

    estimate = estimate_channel(training.samples)
    payload_waveform = modulate_ofdm(np.ones(48, dtype=np.complex64))
    silent_pilots = _zero_pilots(payload_waveform.samples, OfdmNumerology())
    with pytest.raises(ChannelEstimationError, match="pilot"):
        equalize_with_pilots(silent_pilots, estimate)
    with pytest.raises(ChannelEstimationError, match="positive multiple of 80"):
        equalize_with_pilots(payload_waveform.samples[:-1], estimate)


def _apply_frequency_response_and_phase(
    samples: np.ndarray,
    numerology: OfdmNumerology,
    channel: np.ndarray,
    common_phase: np.ndarray,
    phase_slope: np.ndarray,
) -> np.ndarray:
    block_length = numerology.fft_size + numerology.cp_length
    blocks = samples.reshape(-1, block_length)
    grid = np.fft.fft(blocks[:, numerology.cp_length :], axis=1, norm="ortho")
    active_carriers = np.asarray(numerology.active_carriers)
    active_bins = active_carriers % numerology.fft_size
    phase = common_phase[:, np.newaxis] + phase_slope[:, np.newaxis] * active_carriers
    grid[:, active_bins] *= channel[np.newaxis, :] * np.exp(1j * phase)
    useful = np.fft.ifft(grid, axis=1, norm="ortho")
    return np.concatenate(
        (useful[:, -numerology.cp_length :], useful), axis=1
    ).reshape(-1)


def _zero_pilots(samples: np.ndarray, numerology: OfdmNumerology) -> np.ndarray:
    block = samples.reshape(1, numerology.fft_size + numerology.cp_length)
    grid = np.fft.fft(block[:, numerology.cp_length :], axis=1, norm="ortho")
    grid[:, np.asarray(numerology.pilot_carriers) % numerology.fft_size] = 0
    useful = np.fft.ifft(grid, axis=1, norm="ortho")
    return np.concatenate((useful[:, -numerology.cp_length :], useful), axis=1).reshape(-1)


def test_conjugate_rotation_matches_the_complex_exponential_it_replaces() -> None:
    phase = np.linspace(-np.pi, np.pi, 4096)

    rotation = conjugate_rotation(phase)

    assert rotation.dtype == np.complex64
    assert rotation.shape == phase.shape
    assert np.allclose(rotation, np.exp(-1j * phase), atol=2e-6, rtol=0.0)
    assert np.allclose(np.abs(rotation), 1.0, atol=2e-6, rtol=0.0)


def test_conjugate_rotation_keeps_the_two_dimensional_shape_of_a_phase_model() -> None:
    phase = np.linspace(-3.0, 3.0, 260 * 52).reshape(260, 52)

    rotation = conjugate_rotation(phase)

    assert rotation.shape == (260, 52)
    assert np.max(np.abs(rotation - np.exp(-1j * phase))) < 2e-6


def _equalizable_batch(
    symbols: int = 4,
) -> tuple[np.ndarray, ChannelEstimate, OfdmNumerology]:
    numerology = OfdmNumerology()
    rng = np.random.default_rng(20260919)
    grid = np.zeros((symbols, numerology.fft_size), dtype=np.complex64)
    active = np.asarray(numerology.active_carriers) % numerology.fft_size
    grid[:, active] = (
        rng.choice([-1 - 1j, -1 + 1j, 1 - 1j, 1 + 1j], size=(symbols, active.size))
        / np.sqrt(2)
    ).astype(np.complex64)
    from ofdm_link.phy.ofdm import pilot_symbols

    pilots = np.asarray(numerology.pilot_carriers) % numerology.fft_size
    grid[:, pilots] = pilot_symbols(symbols, first_symbol_index=0)
    useful = np.fft.ifft(grid, axis=1, norm="ortho")
    blocks = np.concatenate(
        (useful[:, -numerology.cp_length :], useful), axis=1
    ).astype(np.complex64)
    coefficients = np.full(
        len(numerology.active_carriers), 0.8 + 0.1j, dtype=np.complex64
    )
    estimate = ChannelEstimate(coefficients, numerology.active_carriers)
    return blocks.reshape(-1), estimate, numerology


def test_disabled_magnitude_correction_reports_unit_gain_and_zero_fit_residual() -> None:
    """The skipped branch must publish the same diagnostics it used to compute."""

    samples, estimate, numerology = _equalizable_batch()

    batch = equalize_with_pilots(
        samples,
        estimate,
        numerology,
        correct_pilot_magnitude=False,
    )

    diagnostics = batch.magnitude_diagnostics
    assert diagnostics.enabled is False
    assert diagnostics.correction_gain_min == 1.0
    assert diagnostics.correction_gain_max == 1.0
    assert diagnostics.log_magnitude_fit_rmse_p95 == 0.0
    assert diagnostics.log_magnitude_fit_rmse_max == 0.0
    assert diagnostics.clamp_count == 0
    assert diagnostics.fallback_count == 0


def test_disabled_magnitude_correction_gives_noise_enhancement_of_one_over_h_squared() -> None:
    samples, estimate, numerology = _equalizable_batch()

    batch = equalize_with_pilots(
        samples,
        estimate,
        numerology,
        correct_pilot_magnitude=False,
    )

    data_indices = np.searchsorted(
        np.asarray(numerology.active_carriers),
        np.asarray(numerology.data_carriers),
    )
    expected = 1.0 / np.abs(estimate.coefficients[data_indices]) ** 2
    assert batch.noise_enhancement.shape == batch.data_symbols.shape
    assert np.allclose(batch.noise_enhancement, expected[np.newaxis, :])
    # The broadcast result must still be an independent, frozen copy.
    assert not batch.noise_enhancement.flags.writeable


def test_repeated_equalize_calls_do_not_corrupt_the_shared_numerology_cache() -> None:
    samples, estimate, numerology = _equalizable_batch()

    first = equalize_with_pilots(samples, estimate, numerology)
    for _ in range(3):
        equalize_with_pilots(samples, estimate, numerology)
    last = equalize_with_pilots(samples, estimate, numerology)

    assert np.array_equal(first.data_symbols, last.data_symbols)
    assert np.array_equal(first.noise_enhancement, last.noise_enhancement)


def _qpsk_reference(symbol_count: int) -> np.ndarray:
    bits = np.tile(np.array([0, 0, 0, 1, 1, 0, 1, 1], dtype=np.uint8), symbol_count // 4)
    return np.asarray(map_symbols(bits, MCS.QPSK), dtype=np.complex128)


def test_normalized_evm_ignores_the_channel_gain_the_equalizer_keeps() -> None:
    # The OTA equalizer preserves the channel amplitude, so an otherwise exact
    # measurement arrives scaled and rotated.  That is not error.
    reference = _qpsk_reference(64)

    evm, snr_db = normalized_evm(reference, reference * (3.174 * np.exp(1j * 0.023)))

    # Before the gain was removed this measured EVM 2.5 and a negative SNR.
    assert evm < 1e-12
    assert snr_db is None or snr_db > 200.0


def test_normalized_evm_reports_the_error_left_after_removing_the_gain() -> None:
    reference = _qpsk_reference(64)
    rng = np.random.default_rng(20260920)
    noise = rng.normal(size=reference.size) + 1j * rng.normal(size=reference.size)
    noise *= 0.1 / np.sqrt(np.mean(np.abs(noise) ** 2))

    scaled_evm, scaled_snr = normalized_evm(reference, (reference + noise) * 3.174)
    unscaled_evm, unscaled_snr = normalized_evm(reference, reference + noise)

    assert scaled_evm == pytest.approx(unscaled_evm, rel=1e-9)
    assert scaled_snr == pytest.approx(unscaled_snr, rel=1e-9)
    assert scaled_evm == pytest.approx(0.1, abs=0.02)
    assert scaled_snr == pytest.approx(20.0, abs=2.0)


def test_normalized_evm_rejects_inputs_it_cannot_compare() -> None:
    reference = _qpsk_reference(64)

    assert normalized_evm(reference, reference[:-1]) == (None, None)
    assert normalized_evm(reference[:0], reference[:0]) == (None, None)
    assert normalized_evm(reference, np.zeros_like(reference)) == (None, None)
