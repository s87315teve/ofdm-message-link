"""The simulation channel's sample-clock offset model."""

from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.radio import SimulationChannelConfig, apply_simulation_channel


@pytest.mark.parametrize("frequency", [0.05, 0.25, 0.375, 0.41])
@pytest.mark.parametrize("ppm", [50.0, -50.0])
def test_sfo_resampling_is_band_limited_across_the_occupied_band(
    frequency: float, ppm: float
) -> None:
    # A long burst sweeps through every fractional offset.  Linear
    # interpolation lost 8 dB at a half-sample offset for a carrier at 0.375
    # cycles/sample; a band-limited resampler keeps every carrier intact.
    samples = np.exp(2j * np.pi * frequency * np.arange(60_000))
    received, diagnostics = apply_simulation_channel(
        samples, SimulationChannelConfig(sample_rate_offset_ppm=ppm)
    )

    ratio = 1.0 + ppm * 1e-6
    assert received.size == (samples.size - 1) * (1_000_000 + int(ppm)) // 1_000_000 + 1
    assert diagnostics.sample_rate_ratio == pytest.approx(ratio)
    ideal = np.exp(2j * np.pi * frequency * np.arange(received.size) / ratio)
    core = slice(32, received.size - 32)
    error = np.abs(received[core] - ideal[core])
    assert np.max(error) < 1e-3


def test_zero_sfo_is_still_an_exact_pass_through() -> None:
    samples = np.exp(2j * np.pi * 0.3 * np.arange(1000))
    received, _ = apply_simulation_channel(samples, SimulationChannelConfig())
    assert np.array_equal(received, samples)
