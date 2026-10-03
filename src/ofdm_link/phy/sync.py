"""Vectorized coarse acquisition for a repeated-half OFDM training symbol.

The preamble contains one cyclic prefix followed by one useful training symbol.
The useful symbol consists of two identical halves, so its odd-indexed DFT bins
are zero as required by Schmidl--Cox-style synchronization.

This module intentionally stops at burst acquisition, fractional CFO, and scalar
gain estimation.  Tracking, channel estimation, and equalization belong to later
PHY stages.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from numpy.typing import ArrayLike, NDArray

from ._arrays import all_finite


class SyncError(ValueError):
    """Raised when synchronization configuration or input is malformed."""


@dataclass(frozen=True, slots=True)
class SyncConfig:
    """Immutable acquisition settings.

    Defaults implement the project's 64-point FFT and 16-sample cyclic prefix.
    The detector requires an even FFT size because the training symbol is split
    into two equal time-domain halves.

    The two thresholds do different jobs and are measured, not assumed:

    ``detection_threshold`` gates the Schmidl--Cox repeated-half metric, which
    a repeated half of *noise* satisfies as readily as a preamble.  Over 140 s
    of measured signal-free air it admitted 9k-121k candidates per second at
    every setting between 0.20 and 0.72, so it rejects nothing: it is a bound
    on how many windows reach the coherent stage, and its only real cost is
    that a value inside the signal distribution discards true preambles.  It
    therefore sits below the metric a preamble produces at the lowest SNR the
    link must work at (0.40 corresponds to about +4 dB).

    ``correlation_threshold`` gates the coherent preamble correlation and is
    the only stage that separates signal from noise.  Its ceiling is set by the
    channel, not by SNR: the stage models the channel as one complex scalar, so
    a response that varies across the band caps the correlation however strong
    the signal is -- 0.77 at 11 dB of in-band spread, 0.63 at 18 dB.  0.40
    accepts up to about 25 dB of spread, and 140 s of signal-free air reached
    only 0.199.
    """

    fft_size: int = 64
    cyclic_prefix_length: int = 16
    detection_threshold: float = 0.40
    correlation_threshold: float = 0.40
    ambiguity_ratio: float = 1.10
    minimum_power: float = 1e-8

    def __post_init__(self) -> None:
        if type(self.fft_size) is not int or self.fft_size < 8 or self.fft_size % 2:
            raise SyncError("fft_size must be an even integer of at least 8")
        if (
            type(self.cyclic_prefix_length) is not int
            or self.cyclic_prefix_length < 1
            or self.cyclic_prefix_length >= self.fft_size
        ):
            raise SyncError(
                "cyclic_prefix_length must be an integer in [1, fft_size)"
            )
        _validate_unit_interval(self.detection_threshold, "detection_threshold")
        _validate_unit_interval(self.correlation_threshold, "correlation_threshold")
        if (
            not _is_finite_real(self.ambiguity_ratio)
            or self.ambiguity_ratio <= 1.0
        ):
            raise SyncError("ambiguity_ratio must be finite and greater than 1")
        if not _is_finite_real(self.minimum_power) or self.minimum_power <= 0.0:
            raise SyncError("minimum_power must be finite and greater than zero")

    @property
    def half_length(self) -> int:
        """Number of samples in each repeated training half."""

        return self.fft_size // 2

    @property
    def preamble_length(self) -> int:
        """Total cyclic-prefix plus useful-training length in samples."""

        return self.cyclic_prefix_length + self.fft_size


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    """One unambiguous coarse-acquisition result.

    ``preamble_start`` is the index of the first cyclic-prefix sample in the
    supplied buffer. ``useful_start`` is the first sample of the repeated
    64-sample useful training symbol (for the default configuration).
    ``normalized_cfo`` is CFO in subcarrier spacings. ``amplitude_gain`` is the
    magnitude of a scalar least-squares channel estimate; ``signal_power`` is
    its square. ``metric`` is the Schmidl--Cox repeated-half metric and
    ``confidence`` is the normalized coherent preamble correlation.
    """

    preamble_start: int
    useful_start: int
    normalized_cfo: float
    amplitude_gain: float
    signal_power: float
    metric: float
    confidence: float


def generate_preamble(config: SyncConfig | None = None) -> NDArray[np.complex64]:
    """Return a deterministic, unit-power CP-prefixed training symbol.

    The half-symbol uses a fixed constant-amplitude quadratic phase sequence.
    Repeating it exactly makes every odd-indexed DFT bin of the full useful
    symbol zero.  A fresh array is returned so callers cannot mutate shared
    module state.
    """

    settings = _require_config(config)
    sample_index = np.arange(settings.half_length, dtype=np.float64)
    root = _coprime_root(settings.half_length)
    half = np.exp(
        -1j * np.pi * root * sample_index * sample_index / settings.half_length
    )
    useful = np.concatenate((half, half)).astype(np.complex64)
    cyclic_prefix = useful[-settings.cyclic_prefix_length :]
    return np.concatenate((cyclic_prefix, useful)).astype(np.complex64, copy=False)


def prepend_preamble(
    payload_samples: ArrayLike, config: SyncConfig | None = None
) -> NDArray[np.complex64]:
    """Return a new complex64 burst containing the preamble and payload samples."""

    settings = _require_config(config)
    payload = _as_complex_samples(payload_samples, "payload_samples")
    return np.concatenate((generate_preamble(settings), payload)).astype(
        np.complex64, copy=False
    )


def _moving_sum(values: NDArray[np.generic], width: int) -> NDArray[np.float64]:
    """Return every length-``width`` running sum of a real input, as float64.

    This replaces a ``convolve`` with a vector of ones.  Accumulate at double
    precision whatever the input dtype: the inputs here are single precision,
    and a cumulative sum differenced over a whole burst would otherwise lose
    far more than the convolution it replaces.  The scan needs a complex
    correlation's real and imaginary running sums separately anyway, and a
    pair of contiguous float64 cumulative sums costs materially less than one
    complex128 cumulative sum over the same samples.
    """

    cumulative = np.cumsum(values, dtype=np.float64)
    sums = np.empty(cumulative.size - width + 1, dtype=np.float64)
    sums[0] = cumulative[width - 1]
    np.subtract(cumulative[width:], cumulative[:-width], out=sums[1:])
    return sums


def acquire_preamble(
    samples: ArrayLike, config: SyncConfig | None = None
) -> AcquisitionResult | None:
    """Acquire one unambiguous preamble from a finite sample buffer.

    The repeated-half metric is computed for every possible useful-symbol start
    using vectorized sliding sums.  Only threshold-crossing candidates undergo
    vectorized CFO correction and coherent full-preamble correlation.  ``None``
    is returned for short, zero-power, noise-only, or independently ambiguous
    buffers.  Malformed and non-finite arrays raise :class:`SyncError`.
    """

    settings = _require_config(config)
    candidates = _acquisition_candidates(samples, settings)
    if not candidates:
        return None

    confidences = np.asarray(
        [candidate.confidence for candidate in candidates], dtype=np.float64
    )
    starts = np.asarray(
        [candidate.preamble_start for candidate in candidates], dtype=np.int64
    )
    best = int(np.argmax(confidences))
    independently_separated = (
        np.abs(starts - starts[best]) >= settings.preamble_length
    )
    competing = independently_separated & (
        confidences * settings.ambiguity_ratio >= confidences[best]
    )
    if np.any(competing):
        return None
    return candidates[best]


def _acquisition_candidates(
    samples: ArrayLike,
    config: SyncConfig | None = None,
) -> tuple[AcquisitionResult, ...]:
    """Return every threshold-qualified preamble candidate.

    This internal vectorized scan lets the continuous decoder separate multiple
    bursts before applying the public finite-buffer ambiguity rule.
    """

    settings = _require_config(config)
    values = _as_complex_samples(samples, "samples")
    if values.size < settings.preamble_length:
        return ()
    # Exact-channel idle is observed zero energy, never a preamble. Preserve
    # the caller's continuous sample indexing while avoiding a full scan.
    if not np.any(values):
        return ()

    half_length = settings.half_length
    products = np.conjugate(values[:-half_length]) * values[half_length:]
    # One squared-magnitude pass over the buffer, shared by both halves.
    # ``real**2 + imag**2`` also avoids the square root that ``abs`` takes
    # only to have it squared again.
    squared_magnitude = values.real * values.real
    squared_magnitude += values.imag * values.imag
    paired_power = squared_magnitude[:-half_length] + squared_magnitude[half_length:]
    paired_power *= 0.5
    # Keep the correlation as separate real and imaginary running sums: the
    # metric needs only their squares, and two float64 cumulative sums are
    # cheaper than one complex128 cumulative sum over the same samples.
    interleaved = products.view(np.float32).reshape(-1, 2)
    correlation_real = _moving_sum(interleaved[:, 0], half_length)
    correlation_imag = _moving_sum(interleaved[:, 1], half_length)
    repeated_energy = _moving_sum(paired_power, half_length)

    first_useful = settings.cyclic_prefix_length
    last_useful = values.size - settings.fft_size
    # ``fft_size`` is twice ``half_length``, so the useful starts are exactly
    # the tail of the running sums: slice views, never a fancy-index copy.
    useful = slice(first_useful, last_useful + 1)
    real_part = correlation_real[useful]
    imag_part = correlation_imag[useful]
    energy = repeated_energy[useful]
    powered = energy >= settings.minimum_power * half_length
    numerator = real_part * real_part
    numerator += imag_part * imag_part
    # ``metric >= threshold`` is ``|correlation|^2 >= threshold * energy^2``
    # wherever the energy gate passed, and the energy is positive there.
    # Comparing that way keeps the whole-buffer pass to multiplications and
    # leaves the division for the handful of candidates that qualify.
    threshold_energy = energy * energy
    threshold_energy *= settings.detection_threshold
    eligible = numerator >= threshold_energy
    eligible &= powered
    if not np.any(eligible):
        return ()

    eligible_offsets = np.flatnonzero(eligible)
    candidate_useful = eligible_offsets + first_useful
    candidate_starts = candidate_useful - settings.cyclic_prefix_length
    candidate_real = real_part[eligible_offsets]
    candidate_imag = imag_part[eligible_offsets]
    candidate_energy = energy[eligible_offsets]
    candidate_metric = numerator[eligible_offsets] / np.square(candidate_energy)
    candidate_cfo = np.arctan2(candidate_imag, candidate_real) / np.pi

    reference, reference_energy, positions = _coherent_reference(settings)
    windows = sliding_window_view(values, settings.preamble_length)[candidate_starts]
    correction = np.exp(
        -2j
        * np.pi
        * candidate_cfo[:, np.newaxis]
        * positions[np.newaxis, :]
        / settings.fft_size
    )
    corrected = windows * correction
    coherent = corrected @ reference
    window_energy = np.sum(np.abs(corrected) ** 2, axis=1)
    confidence = np.zeros(candidate_starts.size, dtype=np.float64)
    usable = window_energy >= settings.minimum_power * settings.preamble_length
    confidence[usable] = (
        np.abs(coherent[usable]) ** 2
        / (window_energy[usable] * reference_energy)
    )

    accepted = usable & (confidence >= settings.correlation_threshold)
    if not np.any(accepted):
        return ()

    starts = candidate_starts[accepted]
    useful_starts = candidate_useful[accepted]
    cfo = candidate_cfo[accepted]
    selected_metric = candidate_metric[accepted]
    selected_confidence = confidence[accepted]
    selected_coherent = coherent[accepted]
    scalar_gain = selected_coherent / reference_energy
    amplitude_gain = np.abs(scalar_gain)
    return tuple(
        AcquisitionResult(
            preamble_start=int(start),
            useful_start=int(useful_start),
            normalized_cfo=float(normalized_cfo),
            amplitude_gain=float(amplitude),
            signal_power=float(amplitude * amplitude),
            metric=float(candidate_metric),
            confidence=float(candidate_confidence),
        )
        for start, useful_start, normalized_cfo, amplitude, candidate_metric,
        candidate_confidence in zip(
            starts,
            useful_starts,
            cfo,
            amplitude_gain,
            selected_metric,
            selected_confidence,
            strict=True,
        )
    )


@lru_cache(maxsize=8)
def _coherent_reference(
    settings: SyncConfig,
) -> tuple[NDArray[np.complex128], float, NDArray[np.float64]]:
    """Return the conjugated reference preamble, its energy and its positions.

    These depend only on the frozen configuration, and every burst needs the
    same three.  The arrays are read-only so one scan cannot disturb the next.
    """

    preamble = generate_preamble(settings).astype(np.complex128)
    reference = np.conjugate(preamble)
    reference.setflags(write=False)
    positions = np.arange(settings.preamble_length, dtype=np.float64)
    positions.setflags(write=False)
    return reference, float(np.vdot(preamble, preamble).real), positions


def _as_complex_samples(values: ArrayLike, name: str) -> NDArray[np.complex64]:
    array = np.asarray(values)
    if array.ndim != 1:
        raise SyncError(f"{name} must be a one-dimensional array")
    if not np.issubdtype(array.dtype, np.number):
        raise SyncError(f"{name} must be numeric")
    converted = array.astype(np.complex64, copy=False)
    if not all_finite(converted):
        raise SyncError(f"{name} must contain only finite values")
    return converted


def _require_config(config: SyncConfig | None) -> SyncConfig:
    if config is None:
        return SyncConfig()
    if not isinstance(config, SyncConfig):
        raise SyncError("config must be a SyncConfig")
    return config


def _validate_unit_interval(value: object, name: str) -> None:
    if not _is_finite_real(value) or not 0.0 < float(value) <= 1.0:
        raise SyncError(f"{name} must be finite and in (0, 1]")


def _is_finite_real(value: object) -> bool:
    return (
        isinstance(value, (int, float, np.integer, np.floating))
        and not isinstance(value, (bool, np.bool_))
        and bool(np.isfinite(value))
    )


def _coprime_root(length: int) -> int:
    """Return a small deterministic root coprime to ``length``."""

    for candidate in (5, 7, 3, 11, 13, 1):
        if np.gcd(candidate, length) == 1:
            return candidate
    return 1
