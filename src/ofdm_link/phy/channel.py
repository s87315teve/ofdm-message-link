"""Aligned OFDM channel estimation and bounded pilot tracking.

This module deliberately starts after burst acquisition and CFO correction.  It
estimates a static frequency response from one known long-training symbol, then
uses payload pilots to remove common phase error and a linear phase ramp from
each already-aligned OFDM symbol. Payload callers can also request a bounded
quadratic log-magnitude correction for time-varying passband droop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from numpy.typing import ArrayLike, NDArray

from ._arrays import all_finite
from .ofdm import OfdmNumerology, pilot_symbols


class ChannelEstimationError(ValueError):
    """Raised when aligned channel-estimation inputs are unusable."""


# Fixed v1 BPSK long-training values in ascending signed-carrier order.  The
# values cover all 52 active carriers; they do not depend on a library PRNG.
_TRAINING_BPSK_V1 = (
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
)

_DEFAULT_NUMEROLOGY = OfdmNumerology()
_MIN_USABLE_MAGNITUDE = 1e-8


@dataclass(frozen=True, slots=True)
class TrainingSymbol:
    """One immutable CP-prefixed training symbol and its active references."""

    samples: NDArray[np.complex64]
    active_symbols: NDArray[np.complex64]

    def __post_init__(self) -> None:
        samples = _frozen_complex_array(self.samples, "samples", ndim=1)
        references = _frozen_complex_array(
            self.active_symbols,
            "active_symbols",
            ndim=1,
            minimum_magnitude=_MIN_USABLE_MAGNITUDE,
        )
        if samples.size == 0 or references.size == 0:
            raise ChannelEstimationError("training arrays must not be empty")
        object.__setattr__(self, "samples", samples)
        object.__setattr__(self, "active_symbols", references)


@dataclass(frozen=True, slots=True)
class ChannelEstimate:
    """Immutable frequency response ordered by ``active_carriers``."""

    coefficients: NDArray[np.complex64]
    active_carriers: tuple[int, ...]

    def __post_init__(self) -> None:
        carriers = tuple(self.active_carriers)
        if not carriers or any(type(carrier) is not int for carrier in carriers):
            raise ChannelEstimationError("active_carriers must contain integers")
        if tuple(sorted(set(carriers))) != carriers or 0 in carriers:
            raise ChannelEstimationError(
                "active_carriers must be unique, ascending, and exclude DC"
            )
        coefficients = _frozen_complex_array(
            self.coefficients,
            "coefficients",
            ndim=1,
            minimum_magnitude=_MIN_USABLE_MAGNITUDE,
        )
        if coefficients.size != len(carriers):
            raise ChannelEstimationError(
                "coefficients must contain one value per active carrier"
            )
        object.__setattr__(self, "coefficients", coefficients)
        object.__setattr__(self, "active_carriers", carriers)


@dataclass(frozen=True, slots=True)
class PilotMagnitudeDiagnostics:
    """Bounded summary of optional per-symbol pilot magnitude correction."""

    enabled: bool
    pilot_magnitude_min: float
    pilot_magnitude_max: float
    correction_gain_min: float
    correction_gain_max: float
    log_magnitude_fit_rmse_p95: float
    log_magnitude_fit_rmse_max: float
    clamp_count: int
    fallback_count: int

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ChannelEstimationError("magnitude diagnostics enabled must be a bool")
        finite_values = (
            self.pilot_magnitude_min,
            self.pilot_magnitude_max,
            self.correction_gain_min,
            self.correction_gain_max,
            self.log_magnitude_fit_rmse_p95,
            self.log_magnitude_fit_rmse_max,
        )
        if not np.all(np.isfinite(finite_values)):
            raise ChannelEstimationError("magnitude diagnostics must be finite")
        if self.pilot_magnitude_min < 0 or self.pilot_magnitude_max < 0:
            raise ChannelEstimationError("pilot magnitudes must be non-negative")
        if self.correction_gain_min <= 0 or self.correction_gain_max <= 0:
            raise ChannelEstimationError("magnitude correction gains must be positive")
        if type(self.clamp_count) is not int or self.clamp_count < 0:
            raise ChannelEstimationError("clamp_count must be a non-negative integer")
        if type(self.fallback_count) is not int or self.fallback_count < 0:
            raise ChannelEstimationError("fallback_count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class EqualizedBatch:
    """Pilot-corrected data symbols and phase diagnostics for an OFDM batch."""

    data_symbols: NDArray[np.complex64]
    noise_enhancement: NDArray[np.float64]
    common_phase_rad: NDArray[np.float64]
    phase_slope_rad_per_carrier: NDArray[np.float64]
    magnitude_diagnostics: PilotMagnitudeDiagnostics

    def __post_init__(self) -> None:
        data = _frozen_complex_array(self.data_symbols, "data_symbols", ndim=2)
        noise_enhancement = _frozen_positive_array(
            self.noise_enhancement,
            "noise_enhancement",
            shape=data.shape,
        )
        common_phase = _frozen_real_vector(self.common_phase_rad, "common_phase_rad")
        slope = _frozen_real_vector(
            self.phase_slope_rad_per_carrier,
            "phase_slope_rad_per_carrier",
        )
        if data.shape[0] == 0 or data.shape[1] == 0:
            raise ChannelEstimationError("data_symbols must not be empty")
        if common_phase.size != data.shape[0] or slope.size != data.shape[0]:
            raise ChannelEstimationError(
                "phase diagnostics must contain one value per OFDM symbol"
            )
        if not isinstance(self.magnitude_diagnostics, PilotMagnitudeDiagnostics):
            raise ChannelEstimationError(
                "magnitude_diagnostics must be PilotMagnitudeDiagnostics"
            )
        object.__setattr__(self, "data_symbols", data)
        object.__setattr__(self, "noise_enhancement", noise_enhancement)
        object.__setattr__(self, "common_phase_rad", common_phase)
        object.__setattr__(self, "phase_slope_rad_per_carrier", slope)

    def post_equalization_noise_variance(
        self,
        input_complex_noise_variance: float,
    ) -> NDArray[np.float64]:
        """Propagate one pre-FFT complex AWGN variance to every DATA symbol."""

        variance = _positive_finite_scalar(
            input_complex_noise_variance,
            "input complex noise variance",
        )
        # The product is already a fresh array this method owns, so copying
        # it again only duplicated a full DATA-carrier grid per burst.
        result = self.noise_enhancement * variance
        if not np.all(np.isfinite(result)) or np.any(result <= 0.0):
            raise ChannelEstimationError(
                "post-equalization noise variance must be positive and finite"
            )
        result.setflags(write=False)
        return result


def generate_training_symbol(
    numerology: OfdmNumerology = _DEFAULT_NUMEROLOGY,
) -> TrainingSymbol:
    """Generate the deterministic v1 BPSK long-training OFDM symbol.

    Every active carrier is nonzero.  Guard carriers and DC remain zero, and
    the returned time-domain symbol includes one cyclic prefix.
    """

    _require_numerology(numerology)
    references = np.asarray(_TRAINING_BPSK_V1, dtype=np.complex64)
    if references.size != len(numerology.active_carriers):
        raise ChannelEstimationError(
            "training sequence length does not match the active carrier allocation"
        )

    grid = np.zeros(numerology.fft_size, dtype=np.complex64)
    active_bins = np.asarray(numerology.active_carriers) % numerology.fft_size
    grid[active_bins] = references
    useful = np.fft.ifft(grid, norm="ortho")
    samples = np.concatenate((useful[-numerology.cp_length :], useful))
    return TrainingSymbol(samples=samples, active_symbols=references)


def estimate_channel(
    received_training_symbol: ArrayLike,
    numerology: OfdmNumerology = _DEFAULT_NUMEROLOGY,
    *,
    reference_active_symbols: ArrayLike | None = None,
    minimum_reference_magnitude: float = 1e-6,
) -> ChannelEstimate:
    """Estimate one coefficient per active carrier from one aligned symbol.

    ``reference_active_symbols`` is in ascending signed-carrier order.  When it
    is omitted, the deterministic v1 long-training values are used.
    """

    _require_numerology(numerology)
    minimum = _positive_finite_scalar(
        minimum_reference_magnitude,
        "minimum_reference_magnitude",
    )
    block_length = numerology.fft_size + numerology.cp_length
    received = _as_complex_array(received_training_symbol, "received_training_symbol")
    if received.ndim != 1 or received.size != block_length:
        raise ChannelEstimationError(
            f"received_training_symbol must contain exactly {block_length} samples"
        )

    if reference_active_symbols is None:
        references = np.asarray(_TRAINING_BPSK_V1, dtype=np.complex64)
    else:
        references = _as_complex_array(
            reference_active_symbols,
            "reference_active_symbols",
        )
    active_count = len(numerology.active_carriers)
    if references.ndim != 1 or references.size != active_count:
        raise ChannelEstimationError(
            "reference_active_symbols must contain one value per active carrier "
            f"({active_count})"
        )
    if np.any(np.abs(references) < minimum):
        raise ChannelEstimationError(
            "reference_active_symbols contains a zero or low-power reference"
        )

    useful = received[numerology.cp_length :]
    grid = np.fft.fft(useful, norm="ortho")
    active_bins = np.asarray(numerology.active_carriers) % numerology.fft_size
    coefficients = grid[active_bins] / references
    return ChannelEstimate(coefficients, numerology.active_carriers)


def equalize_with_pilots(
    received_samples: ArrayLike,
    channel_estimate: ChannelEstimate,
    numerology: OfdmNumerology = _DEFAULT_NUMEROLOGY,
    *,
    first_symbol_index: int = 0,
    pilot_version: int = 1,
    minimum_pilot_magnitude: float = 1e-6,
    correct_pilot_magnitude: bool = False,
    minimum_magnitude_fit_pilot: float = 0.25,
    maximum_magnitude_correction_gain: float = 2.0,
) -> EqualizedBatch:
    """Equalize an aligned batch and remove pilot-derived phase ramps.

    The input is a flat sequence of complete CP-prefixed symbols.  Pilot phase
    is unwrapped across signed carrier number, fitted to ``cpe + slope * k``,
    and removed from all active carriers independently for each OFDM symbol.
    The slope is a timing/SFO phase-ramp diagnostic, not an SFO control loop.
    When ``correct_pilot_magnitude`` is true, a quadratic log-magnitude model
    fitted to the four pilots also compensates bounded, time-varying passband
    droop.  Header callers keep that optional correction disabled.
    """

    _require_numerology(numerology)
    if not isinstance(channel_estimate, ChannelEstimate):
        raise ChannelEstimationError("channel_estimate must be a ChannelEstimate")
    if channel_estimate.active_carriers != numerology.active_carriers:
        raise ChannelEstimationError(
            "channel_estimate active carriers do not match numerology"
        )
    minimum = _positive_finite_scalar(
        minimum_pilot_magnitude,
        "minimum_pilot_magnitude",
    )
    fit_minimum = _positive_finite_scalar(
        minimum_magnitude_fit_pilot,
        "minimum_magnitude_fit_pilot",
    )
    maximum_gain = _positive_finite_scalar(
        maximum_magnitude_correction_gain,
        "maximum_magnitude_correction_gain",
    )
    if maximum_gain < 1.0:
        raise ChannelEstimationError(
            "maximum_magnitude_correction_gain must be at least 1"
        )
    if type(correct_pilot_magnitude) is not bool:
        raise ChannelEstimationError("correct_pilot_magnitude must be a bool")
    if type(first_symbol_index) is not int or first_symbol_index < 0:
        raise ChannelEstimationError("first_symbol_index must be a non-negative integer")

    received = _as_complex_array(received_samples, "received_samples")
    block_length = numerology.fft_size + numerology.cp_length
    if received.ndim != 1 or received.size == 0 or received.size % block_length:
        raise ChannelEstimationError(
            f"received_samples length must be a positive multiple of {block_length}"
        )

    blocks = received.reshape(-1, block_length)
    useful = blocks[:, numerology.cp_length :]
    grid = np.fft.fft(useful, axis=1, norm="ortho")
    indices = _numerology_indices(numerology)
    active_carriers = indices.active_carriers
    equalized = (
        grid[:, indices.active_bins]
        / channel_estimate.coefficients[np.newaxis, :]
    )

    received_pilots = equalized[:, indices.pilot_indices]
    if np.any(np.abs(received_pilots) < minimum):
        raise ChannelEstimationError("received pilot contains a zero or low-power value")
    expected_pilots = pilot_symbols(
        blocks.shape[0],
        first_symbol_index=first_symbol_index,
        version=pilot_version,
    )
    residual = received_pilots / expected_pilots

    # Unwrap in increasing carrier order even if a custom numerology listed its
    # pilots in another order.  All operations remain batch-vectorized.
    pilot_order = indices.pilot_order
    phase = np.unwrap(np.angle(residual[:, pilot_order]), axis=1)
    phase_mean = np.mean(phase, axis=1)
    slope = np.sum(
        (phase - phase_mean[:, np.newaxis]) * indices.pilot_x_centered,
        axis=1,
    ) / indices.pilot_x_centered_square_sum
    intercept = phase_mean - slope * indices.pilot_x_mean
    common_phase = np.angle(np.exp(1j * intercept))

    phase_model = (
        common_phase[:, np.newaxis]
        + slope[:, np.newaxis] * active_carriers[np.newaxis, :]
    )
    # phase_model stays within a few radians, so it needs no wrapping before
    # the float32 rotation.
    phase_corrected = equalized * conjugate_rotation(phase_model)
    pilot_magnitude = np.abs(residual)
    data_indices = indices.data_indices
    channel_power = np.abs(channel_estimate.coefficients[data_indices]) ** 2
    clamp_count = 0
    fallback_count = 0
    if correct_pilot_magnitude:
        design = indices.magnitude_design
        log_magnitude = np.log(np.maximum(pilot_magnitude[:, pilot_order], _MIN_USABLE_MAGNITUDE))
        coefficients = log_magnitude @ indices.magnitude_design_pseudo_inverse_t
        predicted_log_magnitude = coefficients @ indices.active_design_t
        predicted_pilot_log_magnitude = coefficients @ design.T
        fit_rmse = np.sqrt(
            np.mean((predicted_pilot_log_magnitude - log_magnitude) ** 2, axis=1)
        )
        usable = np.all(pilot_magnitude >= fit_minimum, axis=1) & np.all(
            np.isfinite(predicted_log_magnitude), axis=1
        )
        fallback_count = int(np.count_nonzero(~usable))
        unbounded_gain = np.exp(-predicted_log_magnitude)
        lower_gain = 1.0 / maximum_gain
        bounded_gain = np.clip(unbounded_gain, lower_gain, maximum_gain)
        clamp_count = int(
            np.count_nonzero((bounded_gain != unbounded_gain)[usable])
        )
        correction_gain = np.ones_like(equalized.real, dtype=np.float64)
        correction_gain[usable] = bounded_gain[usable]
        fit_rmse[~usable] = 0.0
        corrected = phase_corrected * correction_gain
        data_symbols = corrected[:, data_indices]
        noise_enhancement = (
            correction_gain[:, data_indices] ** 2 / channel_power[np.newaxis, :]
        )
        correction_gain_min = float(np.min(correction_gain))
        correction_gain_max = float(np.max(correction_gain))
        fit_rmse_p95 = float(np.percentile(fit_rmse, 95))
        fit_rmse_max = float(np.max(fit_rmse))
    else:
        # Without the magnitude fit the gain is one everywhere and the fit
        # residual is zero everywhere, so neither the full-size gain matrix nor
        # the percentile over a zero vector buys anything.
        data_symbols = phase_corrected[:, data_indices]
        noise_enhancement = np.broadcast_to(
            1.0 / channel_power,
            data_symbols.shape,
        )
        correction_gain_min = 1.0
        correction_gain_max = 1.0
        fit_rmse_p95 = 0.0
        fit_rmse_max = 0.0
    magnitude_diagnostics = PilotMagnitudeDiagnostics(
        enabled=correct_pilot_magnitude,
        pilot_magnitude_min=float(np.min(pilot_magnitude)),
        pilot_magnitude_max=float(np.max(pilot_magnitude)),
        correction_gain_min=correction_gain_min,
        correction_gain_max=correction_gain_max,
        log_magnitude_fit_rmse_p95=fit_rmse_p95,
        log_magnitude_fit_rmse_max=fit_rmse_max,
        clamp_count=clamp_count,
        fallback_count=fallback_count,
    )
    return EqualizedBatch(
        data_symbols=data_symbols,
        noise_enhancement=noise_enhancement,
        common_phase_rad=common_phase,
        phase_slope_rad_per_carrier=slope,
        magnitude_diagnostics=magnitude_diagnostics,
    )


def normalized_evm(
    expected: ArrayLike,
    received: ArrayLike,
) -> tuple[float | None, float | None]:
    """Return post-equalization EVM and the effective SNR it implies.

    EVM is only defined once the ideal complex gain between the reference
    constellation and the measurement has been removed.  The equalizer keeps
    the channel's amplitude, so comparing its output directly against a
    unit-power reference reports that amplitude as error: an OTA burst
    received at 3.2x the reference scale measures EVM 2.5 and a negative
    effective SNR however clean it is.  The least-squares gain is estimated
    from the same symbols, which costs one complex degree of freedom.

    Returns ``(None, None)`` when the inputs cannot be compared, and
    ``(0.0, None)`` when the measurement is exact.
    """

    reference = np.asarray(expected, dtype=np.complex128)
    measurement = np.asarray(received, dtype=np.complex128)
    if reference.shape != measurement.shape or reference.size == 0:
        return None, None
    total_reference_power = float(np.vdot(reference, reference).real)
    if not math.isfinite(total_reference_power) or total_reference_power <= 0.0:
        return None, None
    gain = complex(np.vdot(reference, measurement) / total_reference_power)
    if not math.isfinite(gain.real) or not math.isfinite(gain.imag) or gain == 0.0:
        return None, None
    error_power = float(np.mean(np.abs(measurement / gain - reference) ** 2))
    if not math.isfinite(error_power):
        return None, None
    if error_power <= np.finfo(np.float64).tiny:
        return 0.0, None
    evm_rms = math.sqrt(error_power / (total_reference_power / reference.size))
    return evm_rms, -20.0 * math.log10(evm_rms)


def conjugate_rotation(phase: NDArray[np.float64]) -> NDArray[np.complex64]:
    """Return ``exp(-1j * phase)`` as complex64, via float32 cos and sin.

    ``np.exp`` on a complex argument runs in complex128 and dominated the
    equalizer.  float32 cos/sin keep about seven significant digits, so the
    caller must pass a phase already reduced to a small range: a phase that
    grows with sample index has to be wrapped first, or the absolute error
    grows with it.
    """

    phase32 = np.asarray(phase, dtype=np.float32)
    rotation = np.empty(phase32.shape, dtype=np.complex64)
    np.cos(phase32, out=rotation.real)
    np.sin(phase32, out=rotation.imag)
    np.negative(rotation.imag, out=rotation.imag)
    return rotation


@dataclass(frozen=True, slots=True)
class _NumerologyIndices:
    """Carrier indices and fit matrices implied by one frozen numerology."""

    active_carriers: NDArray[np.int64]
    active_bins: NDArray[np.int64]
    pilot_indices: NDArray[np.int64]
    data_indices: NDArray[np.int64]
    pilot_order: NDArray[np.int64]
    pilot_x: NDArray[np.float64]
    pilot_x_mean: float
    pilot_x_centered: NDArray[np.float64]
    pilot_x_centered_square_sum: float
    magnitude_design: NDArray[np.float64]
    magnitude_design_pseudo_inverse_t: NDArray[np.float64]
    active_design_t: NDArray[np.float64]


@lru_cache(maxsize=8)
def _numerology_indices(numerology: OfdmNumerology) -> _NumerologyIndices:
    """Derive and retain the per-numerology arrays every equalize call needs.

    ``OfdmNumerology`` is frozen, so these depend on nothing else and were
    being rebuilt on every burst.
    """

    active_carriers = np.asarray(numerology.active_carriers)
    pilot_carriers = np.asarray(numerology.pilot_carriers)
    pilot_order = np.argsort(pilot_carriers)
    pilot_x = pilot_carriers[pilot_order].astype(np.float64)
    pilot_x_mean = float(np.mean(pilot_x))
    pilot_x_centered = pilot_x - pilot_x_mean
    design = np.column_stack((np.ones(pilot_x.size), pilot_x, pilot_x**2))
    active_design = np.column_stack(
        (
            np.ones(active_carriers.size),
            active_carriers,
            active_carriers.astype(np.float64) ** 2,
        )
    )
    indices = _NumerologyIndices(
        active_carriers=active_carriers,
        active_bins=active_carriers % numerology.fft_size,
        pilot_indices=np.searchsorted(active_carriers, pilot_carriers),
        data_indices=np.searchsorted(
            active_carriers,
            np.asarray(numerology.data_carriers),
        ),
        pilot_order=pilot_order,
        pilot_x=pilot_x,
        pilot_x_mean=pilot_x_mean,
        pilot_x_centered=pilot_x_centered,
        pilot_x_centered_square_sum=float(np.sum(pilot_x_centered**2)),
        magnitude_design=design,
        magnitude_design_pseudo_inverse_t=np.linalg.pinv(design).T,
        active_design_t=active_design.T,
    )
    for value in (
        indices.active_carriers,
        indices.active_bins,
        indices.pilot_indices,
        indices.data_indices,
        indices.pilot_order,
        indices.pilot_x,
        indices.pilot_x_centered,
        indices.magnitude_design,
        indices.magnitude_design_pseudo_inverse_t,
        indices.active_design_t,
    ):
        value.setflags(write=False)
    return indices


def _require_numerology(numerology: OfdmNumerology) -> None:
    if not isinstance(numerology, OfdmNumerology):
        raise ChannelEstimationError("numerology must be an OfdmNumerology")


def _as_complex_array(values: ArrayLike, name: str) -> NDArray[np.complex64]:
    array = np.asarray(values)
    if not np.issubdtype(array.dtype, np.number):
        raise ChannelEstimationError(f"{name} must be numeric")
    converted = array.astype(np.complex64, copy=False)
    if not all_finite(converted):
        raise ChannelEstimationError(f"{name} must contain only finite values")
    return converted


def _frozen_complex_array(
    values: ArrayLike,
    name: str,
    *,
    ndim: int,
    minimum_magnitude: float | None = None,
) -> NDArray[np.complex64]:
    array = _as_complex_array(values, name)
    if array.ndim != ndim:
        raise ChannelEstimationError(f"{name} must be {ndim}-dimensional")
    if minimum_magnitude is not None and np.any(np.abs(array) < minimum_magnitude):
        raise ChannelEstimationError(f"{name} contains a zero or near-zero value")
    frozen = np.array(array, dtype=np.complex64, copy=True)
    frozen.setflags(write=False)
    return frozen


def _frozen_real_vector(values: ArrayLike, name: str) -> NDArray[np.float64]:
    array = np.asarray(values)
    if array.ndim != 1 or not np.issubdtype(array.dtype, np.number):
        raise ChannelEstimationError(f"{name} must be a numeric one-dimensional array")
    frozen = np.array(array, dtype=np.float64, copy=True)
    if not np.all(np.isfinite(frozen)):
        raise ChannelEstimationError(f"{name} must contain only finite values")
    frozen.setflags(write=False)
    return frozen


def _frozen_positive_array(
    values: ArrayLike,
    name: str,
    *,
    shape: tuple[int, ...],
) -> NDArray[np.float64]:
    array = np.asarray(values)
    if array.shape != shape or not np.issubdtype(array.dtype, np.number):
        raise ChannelEstimationError(f"{name} must be numeric with shape {shape}")
    frozen = np.array(array, dtype=np.float64, copy=True)
    if not np.all(np.isfinite(frozen)) or np.any(frozen <= 0.0):
        raise ChannelEstimationError(f"{name} must contain positive finite values")
    frozen.setflags(write=False)
    return frozen


def _positive_finite_scalar(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChannelEstimationError(f"{name} must be a positive finite number")
    converted = float(value)
    if not np.isfinite(converted) or converted <= 0:
        raise ChannelEstimationError(f"{name} must be a positive finite number")
    return converted
