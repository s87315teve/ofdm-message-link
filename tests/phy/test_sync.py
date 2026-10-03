from __future__ import annotations

from dataclasses import FrozenInstanceError

import numpy as np
import pytest

# White-box exception: the moving-sum kernel is a performance-sensitive
# vectorization primitive. Public acquisition tests cover outcomes, while these
# focused checks pin numerical equivalence and accumulator precision.
from ofdm_link.phy.sync import (
    AcquisitionResult,
    SyncConfig,
    SyncError,
    _moving_sum,
    acquire_preamble,
    generate_preamble,
    prepend_preamble,
)


def _received_burst(
    *,
    offset: int,
    amplitude: float,
    normalized_cfo: float,
    snr_db: float | None = None,
    seed: int = 0,
) -> tuple[np.ndarray, SyncConfig]:
    config = SyncConfig()
    burst = prepend_preamble(np.zeros(24, dtype=np.complex64), config)
    samples = np.concatenate((np.zeros(offset, dtype=np.complex64), burst))
    indices = np.arange(samples.size, dtype=np.float64)
    samples = samples * amplitude * np.exp(
        2j * np.pi * normalized_cfo * indices / config.fft_size
    )
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        noise_power = amplitude**2 / (10.0 ** (snr_db / 10.0))
        noise = np.sqrt(noise_power / 2.0) * (
            rng.standard_normal(samples.size) + 1j * rng.standard_normal(samples.size)
        )
        samples = samples + noise
    return samples.astype(np.complex64), config


def test_preamble_is_reproducible_and_has_two_identical_halves() -> None:
    config = SyncConfig()

    first = generate_preamble(config)
    second = generate_preamble(config)
    useful = first[config.cyclic_prefix_length :]

    np.testing.assert_array_equal(first, second)
    np.testing.assert_array_equal(useful[: config.half_length], useful[config.half_length :])
    np.testing.assert_array_equal(
        first[: config.cyclic_prefix_length], useful[-config.cyclic_prefix_length :]
    )
    assert first.dtype == np.complex64
    assert first.size == 80
    assert np.mean(np.abs(useful) ** 2) == pytest.approx(1.0, abs=1e-6)


@pytest.mark.parametrize("offset", [0, 1, 19, 137])
@pytest.mark.parametrize("amplitude", [0.5, 2.0])
@pytest.mark.parametrize("normalized_cfo", [-0.2, 0.0, 0.2])
def test_acquisition_recovers_offset_cfo_and_gain(
    offset: int, amplitude: float, normalized_cfo: float
) -> None:
    samples, config = _received_burst(
        offset=offset,
        amplitude=amplitude,
        normalized_cfo=normalized_cfo,
    )

    result = acquire_preamble(samples, config)

    assert isinstance(result, AcquisitionResult)
    assert result.preamble_start == offset
    assert result.useful_start == offset + config.cyclic_prefix_length
    assert result.normalized_cfo == pytest.approx(normalized_cfo, abs=2e-6)
    assert result.amplitude_gain == pytest.approx(amplitude, rel=2e-6)
    assert result.signal_power == pytest.approx(amplitude**2, rel=4e-6)
    assert result.metric == pytest.approx(1.0, abs=2e-6)
    assert result.confidence == pytest.approx(1.0, abs=2e-6)


@pytest.mark.parametrize("normalized_cfo", [-0.2, 0.0, 0.2])
@pytest.mark.parametrize("amplitude", [0.5, 2.0])
def test_acquisition_is_accurate_at_15_db_snr(
    normalized_cfo: float, amplitude: float
) -> None:
    samples, config = _received_burst(
        offset=47,
        amplitude=amplitude,
        normalized_cfo=normalized_cfo,
        snr_db=15.0,
        seed=20260917,
    )

    result = acquire_preamble(samples, config)

    assert result is not None
    assert result.preamble_start == 47
    assert result.normalized_cfo == pytest.approx(normalized_cfo, abs=0.035)
    assert result.amplitude_gain == pytest.approx(amplitude, rel=0.08)
    assert result.metric > 0.8
    assert result.confidence > 0.9


def test_acquisition_rejects_zero_noise_and_ambiguous_buffers() -> None:
    config = SyncConfig()
    rng = np.random.default_rng(1927)
    noise = (
        rng.standard_normal(4096) + 1j * rng.standard_normal(4096)
    ).astype(np.complex64)
    one = prepend_preamble(np.zeros(10, dtype=np.complex64), config)
    ambiguous = np.concatenate((one, np.zeros(96, dtype=np.complex64), one))

    assert acquire_preamble(np.zeros(512, dtype=np.complex64), config) is None
    assert acquire_preamble(noise, config) is None
    assert acquire_preamble(ambiguous, config) is None


def test_sync_contract_is_immutable_and_validated() -> None:
    config = SyncConfig()
    result = AcquisitionResult(2, 18, 0.0, 1.0, 1.0, 1.0, 1.0)

    with pytest.raises(FrozenInstanceError):
        config.fft_size = 128  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.metric = 0.0  # type: ignore[misc]
    with pytest.raises(SyncError, match="even"):
        SyncConfig(fft_size=63)
    with pytest.raises(SyncError, match="cyclic_prefix_length"):
        SyncConfig(cyclic_prefix_length=64)
    with pytest.raises(SyncError, match="one-dimensional"):
        acquire_preamble(np.zeros((4, 4), dtype=np.complex64), config)
    with pytest.raises(SyncError, match="finite"):
        acquire_preamble(np.array([complex(np.nan, 0)] * 80), config)


def test_prepend_preamble_preserves_payload_without_aliasing() -> None:
    config = SyncConfig()
    payload = np.array([1 + 2j, -3 + 0.5j], dtype=np.complex64)

    burst = prepend_preamble(payload, config)
    payload[0] = 99

    assert burst.size == config.preamble_length + 2
    np.testing.assert_array_equal(burst[-2:], [1 + 2j, -3 + 0.5j])
    with pytest.raises(SyncError, match="finite"):
        prepend_preamble(np.array([complex(np.inf, 0)]), config)


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
@pytest.mark.parametrize("width", [1, 2, 32])
def test_moving_sum_matches_the_ones_convolution_it_replaces(dtype, width) -> None:
    rng = np.random.default_rng(20260919)
    values = rng.normal(size=4096).astype(dtype)

    sums = _moving_sum(values, width)
    expected = np.convolve(values, np.ones(width, dtype=np.float64), mode="valid")

    assert sums.shape == expected.shape
    assert np.allclose(sums, expected, rtol=1e-12, atol=1e-12)


def test_moving_sum_handles_a_window_covering_the_whole_input() -> None:
    values = np.arange(1, 9, dtype=np.float32)

    sums = _moving_sum(values, values.size)

    assert sums.shape == (1,)
    assert sums[0] == pytest.approx(36.0)


def test_moving_sum_accumulates_at_double_precision() -> None:
    """Single-precision accumulation over a burst would lose more than it saves."""

    assert _moving_sum(np.ones(64, dtype=np.float32), 32).dtype == np.float64
    assert _moving_sum(np.ones(64, dtype=np.float64), 32).dtype == np.float64


def _selective_burst(
    *,
    echo_gain: float,
    echo_delay: int = 3,
    snr_db: float = 30.0,
    seed: int = 20260920,
) -> tuple[np.ndarray, SyncConfig]:
    """Return a preamble through a two-tap channel of a known in-band spread.

    ``1 + a z^-d`` varies by ``20*log10((1 + a) / (1 - a))`` dB across the
    band, which is how the bench channel is quantified.
    """

    config = SyncConfig()
    burst = prepend_preamble(np.zeros(240, dtype=np.complex64), config)
    samples = np.concatenate((np.zeros(600, dtype=np.complex64), burst))
    taps = np.zeros(echo_delay + 1, dtype=np.complex128)
    taps[0] = 1.0
    taps[echo_delay] = echo_gain
    faded = np.convolve(samples.astype(np.complex128), taps)
    rng = np.random.default_rng(seed)
    noise_power = float(np.mean(np.abs(faded) ** 2)) / (10.0 ** (snr_db / 10.0))
    noise = np.sqrt(noise_power / 2.0) * (
        rng.standard_normal(faded.size) + 1j * rng.standard_normal(faded.size)
    )
    return (faded + noise).astype(np.complex64), config


def test_default_thresholds_are_the_bench_measured_values() -> None:
    """Both defaults are measurements; changing one needs new measurements."""

    config = SyncConfig()

    assert config.detection_threshold == pytest.approx(0.40)
    assert config.correlation_threshold == pytest.approx(0.40)


@pytest.mark.parametrize(
    ("echo_gain", "spread_db"),
    [(0.56, 11.0), (0.776, 18.0)],
)
def test_acquisition_survives_the_measured_in_band_spread(
    echo_gain: float, spread_db: float
) -> None:
    """A channel this selective is what the bench has, so it must acquire.

    The coherent stage models the channel as one complex scalar, so its
    correlation is capped by the spread however strong the signal: about 0.77
    at 11 dB and 0.63 at 18 dB.  A ``correlation_threshold`` above that cap
    cannot acquire at any SNR, which is exactly how the shipped 0.78 lost 3 of
    10 bench runs.
    """

    samples, config = _selective_burst(echo_gain=echo_gain)

    result = acquire_preamble(samples, config)

    assert result is not None, f"{spread_db:g} dB of spread must still acquire"
    assert result.preamble_start == 600
    assert result.confidence < 0.78, (
        "this fixture must stay below the old threshold, or it no longer "
        "covers the defect it was written for"
    )
    assert result.confidence >= config.correlation_threshold


def test_acquisition_holds_at_the_lowest_specified_burst_snr() -> None:
    """+4 dB is the lowest burst SNR the chosen thresholds are specified for."""

    samples, config = _selective_burst(echo_gain=0.56, snr_db=4.0, seed=4242)

    result = acquire_preamble(samples, config)

    assert result is not None
    assert abs(result.preamble_start - 600) <= 1
    assert result.metric >= config.detection_threshold


def test_noise_alone_stays_far_below_the_correlation_threshold() -> None:
    """Measured noise reached 0.199 over 140 s; the gate sits at 0.40."""

    config = SyncConfig()
    rng = np.random.default_rng(20260920)
    noise = (
        rng.standard_normal(400_000) + 1j * rng.standard_normal(400_000)
    ).astype(np.complex64) / np.sqrt(2.0)

    assert acquire_preamble(noise, config) is None
