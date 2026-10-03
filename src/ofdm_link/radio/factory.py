"""Translate validated link configuration into radio adapter settings."""

from __future__ import annotations

from ofdm_link.config import LinkConfig

from .simulation import SimulationChannelConfig
from .uhd import B210Settings


def build_simulation_channel_config(
    config: LinkConfig,
    *,
    amplitude_gain: float = 1.0,
    phase_offset_radians: float = 0.0,
    seed: int = 0,
) -> SimulationChannelConfig:
    """Build one deterministic impairment realization from the YAML profile."""

    _require_link_config(config)
    if config.radio.adapter != "simulation":
        raise ValueError("configuration must select the simulation adapter")
    if not config.simulation.amplitude_min <= amplitude_gain <= config.simulation.amplitude_max:
        raise ValueError("amplitude_gain must be within the configured amplitude range")
    return SimulationChannelConfig(
        amplitude_gain=amplitude_gain,
        phase_offset_radians=phase_offset_radians,
        taps=config.simulation.taps,
        sample_rate_offset_ppm=config.simulation.sample_rate_offset_ppm,
        cfo_subcarriers=config.simulation.cfo_subcarrier_fraction,
        fft_size=config.phy.fft_len,
        snr_db=config.simulation.snr_db,
        seed=seed,
    )


def build_b210_settings(config: LinkConfig) -> B210Settings:
    """Build conservative B2xx settings; OTA remains runtime-gated and unpassed."""

    _require_link_config(config)
    if config.radio.adapter != "uhd":
        raise ValueError("configuration must select the UHD adapter")
    return B210Settings(
        device_args=config.radio.device_args,
        sample_rate=config.phy.sample_rate,
        center_frequency=config.phy.center_frequency,
        rx_gain=config.radio.rx_gain,
        tx_gain=config.radio.tx_gain,
        bandwidth=config.radio.bandwidth,
        rx_antenna=config.radio.rx_antenna,
        tx_antenna=config.radio.tx_antenna,
        clock_source=config.radio.clock_source,
    )


def _require_link_config(config: LinkConfig) -> None:
    if not isinstance(config, LinkConfig):
        raise TypeError("config must be a LinkConfig")
