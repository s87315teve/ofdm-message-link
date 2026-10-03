"""Deterministic vectorized complex-baseband channel impairments."""

from __future__ import annotations

import cmath
import functools
import math
from dataclasses import dataclass
from fractions import Fraction
from numbers import Real

import numpy as np

_MAX_CACHED_CFO_SAMPLES = 100_000


@dataclass(frozen=True, slots=True)
class SimulationChannelConfig:
    """Parameters for one deterministic simulation-channel application.

    Positive ``sample_rate_offset_ppm`` means the receiver samples faster than
    the transmitter. ``cfo_subcarriers`` is measured in OFDM subcarrier
    spacings, so its cycles/sample value is ``cfo_subcarriers / fft_size``.
    ``snr_db=None`` disables AWGN and makes the defaults an exact pass-through.
    """

    amplitude_gain: float = 1.0
    phase_offset_radians: float = 0.0
    taps: tuple[complex, ...] = (1.0 + 0.0j,)
    sample_rate_offset_ppm: float = 0.0
    cfo_subcarriers: float = 0.0
    fft_size: int = 64
    snr_db: float | None = None
    seed: int = 0

    def __post_init__(self) -> None:
        amplitude_gain = _finite_real("amplitude_gain", self.amplitude_gain)
        if amplitude_gain < 0.0:
            raise ValueError("amplitude_gain must be non-negative")
        _finite_real("phase_offset_radians", self.phase_offset_radians)
        if type(self.taps) is not tuple:
            raise TypeError("taps must be a tuple")
        if not self.taps:
            raise ValueError("taps must not be empty")
        if any(not _is_finite_complex(tap) for tap in self.taps):
            raise ValueError("taps must contain only finite complex values")
        sample_rate_offset_ppm = _finite_real(
            "sample_rate_offset_ppm", self.sample_rate_offset_ppm
        )
        if 1.0 + sample_rate_offset_ppm * 1e-6 <= 0.0:
            raise ValueError("sample_rate_offset_ppm must produce a positive sample rate")
        _finite_real("cfo_subcarriers", self.cfo_subcarriers)
        if type(self.fft_size) is not int or self.fft_size <= 0:
            raise ValueError("fft_size must be a positive integer")
        if self.snr_db is not None:
            _finite_real("snr_db", self.snr_db)
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class SimulationChannelDiagnostics:
    """Observable values produced while applying the simulated channel.

    ``noise_power`` is the configured complex AWGN variance
    :math:`E[|n|^2]`. ``measured_noise_power`` is the finite seeded
    realization measured before it is added to the signal.
    """

    input_samples: int
    output_samples: int
    sample_rate_ratio: float
    normalized_cfo_cycles_per_sample: float
    signal_power: float
    noise_power: float
    measured_noise_power: float
    measured_snr_db: float | None
    seed: int


def apply_simulation_channel(
    samples: np.ndarray,
    config: SimulationChannelConfig,
) -> tuple[np.ndarray, SimulationChannelDiagnostics]:
    """Apply impairments and return a new complex128 vector plus diagnostics.

    Operations have a stable order: complex scalar gain, full FIR convolution,
    linear-interpolation SFO resampling, CFO rotation, then complex AWGN. For an
    FIR/SFO input of length ``N``, resampling returns
    ``floor((N - 1) * (1 + sfo_ppm / 1e6)) + 1`` samples. The local seeded RNG
    never reads or modifies NumPy's process-global RNG state.
    """

    if not isinstance(config, SimulationChannelConfig):
        raise TypeError("config must be SimulationChannelConfig")
    try:
        input_samples = np.asarray(samples, dtype=np.complex128)
    except (TypeError, ValueError) as error:
        raise TypeError("samples must contain complex numeric values") from error
    if input_samples.ndim != 1:
        raise ValueError("samples must be one-dimensional")
    if input_samples.size == 0:
        raise ValueError("samples must not be empty")
    if not np.isfinite(input_samples).all():
        raise ValueError("samples must contain only finite values")
    output = input_samples.copy()
    if config.amplitude_gain != 1.0 or config.phase_offset_radians != 0.0:
        output *= cmath.rect(config.amplitude_gain, config.phase_offset_radians)
    if config.taps != (1.0 + 0.0j,):
        output = np.convolve(output, np.asarray(config.taps, dtype=np.complex128), mode="full")
    sample_rate_ratio = 1.0 + config.sample_rate_offset_ppm * 1e-6
    if config.sample_rate_offset_ppm != 0.0:
        exact_ratio = Fraction(1) + Fraction(str(config.sample_rate_offset_ppm)) / 1_000_000
        output_length = (len(output) - 1) * exact_ratio.numerator // exact_ratio.denominator + 1
        source_positions = np.arange(output_length, dtype=np.float64) / sample_rate_ratio
        input_positions = np.arange(len(output), dtype=np.float64)
        output = np.interp(source_positions, input_positions, output)
    normalized_cfo = config.cfo_subcarriers / config.fft_size
    if config.cfo_subcarriers != 0.0:
        output *= _cfo_rotation(len(output), normalized_cfo)
    signal_power = float(np.mean(np.abs(output) ** 2))
    if not math.isfinite(signal_power):
        raise ValueError("channel impairments produced non-finite signal power")
    noise_power = 0.0
    measured_noise_power = 0.0
    measured_snr_db = None
    if config.snr_db is not None:
        if signal_power <= 0.0:
            raise ValueError("requested SNR requires positive signal power")
        try:
            noise_power = signal_power * math.pow(10.0, -config.snr_db / 10.0)
        except OverflowError as error:
            raise ValueError(
                "derived complex noise variance must be positive and finite"
            ) from error
        if not math.isfinite(noise_power) or noise_power <= 0.0:
            raise ValueError("derived complex noise variance must be positive and finite")
        noise_scale = np.sqrt(noise_power / 2.0)
        generator = np.random.default_rng(config.seed)
        noise = noise_scale * (
            generator.standard_normal(len(output))
            + 1j * generator.standard_normal(len(output))
        )
        measured_noise_power = float(np.mean(np.abs(noise) ** 2))
        if not math.isfinite(measured_noise_power) or measured_noise_power <= 0.0:
            raise ValueError("measured complex noise variance must be positive and finite")
        measured_snr_db = 10.0 * math.log10(signal_power / measured_noise_power)
        output += noise
    diagnostics = SimulationChannelDiagnostics(
        input_samples=len(input_samples),
        output_samples=len(output),
        sample_rate_ratio=sample_rate_ratio,
        normalized_cfo_cycles_per_sample=normalized_cfo,
        signal_power=signal_power,
        noise_power=noise_power,
        measured_noise_power=measured_noise_power,
        measured_snr_db=measured_snr_db,
        seed=config.seed,
    )
    return output, diagnostics


def _cfo_rotation(sample_count: int, normalized_cfo: float) -> np.ndarray:
    if sample_count <= _MAX_CACHED_CFO_SAMPLES:
        return _cached_cfo_rotation(sample_count, normalized_cfo)
    return _make_cfo_rotation(sample_count, normalized_cfo)


@functools.lru_cache(maxsize=8)
def _cached_cfo_rotation(sample_count: int, normalized_cfo: float) -> np.ndarray:
    return _make_cfo_rotation(sample_count, normalized_cfo)


def _make_cfo_rotation(sample_count: int, normalized_cfo: float) -> np.ndarray:
    rotation = np.exp(
        2j * np.pi * normalized_cfo * np.arange(sample_count, dtype=np.float64)
    )
    rotation.setflags(write=False)
    return rotation


def _finite_real(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a real number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite")
    return converted


def _is_finite_complex(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        converted = complex(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(converted.real) and math.isfinite(converted.imag)
