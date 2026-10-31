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
from typing import TYPE_CHECKING

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from numpy.typing import ArrayLike, NDArray

from ._arrays import all_finite

if TYPE_CHECKING:
    from .sync_v5 import AnyAcquisition, V5SyncConfig

# Floor on the preamble noise estimate, as a fraction of the signal power:
# caps the reported SNR at 60 dB when the input carries no noise at all.
_MINIMUM_NOISE_FRACTION = 1e-6


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
    link must work at: once the receiver decoded bursts at 3-4 dB, the old
    0.40 was rejecting half of them there, and 0.25 is what it takes.

    ``correlation_threshold`` gates the coherent preamble correlation and is
    the only stage that separates signal from noise.  Its ceiling is set by the
    channel, not by SNR: the stage models the channel as one complex scalar, so
    a response that varies across the band caps the correlation however strong
    the signal is -- 0.77 at 11 dB of in-band spread, 0.63 at 18 dB.  0.40
    accepts up to about 25 dB of spread, and 140 s of signal-free air reached
    only 0.199 (0.203 in a later 90 s at 2.45 GHz).  At 3 dB the preamble's own
    correlation is about 0.47 at the median, so 0.30 keeps most of those
    bursts while staying 0.1 above anything noise produced.
    """

    fft_size: int = 64
    cyclic_prefix_length: int = 16
    detection_threshold: float = 0.25
    correlation_threshold: float = 0.30
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


@dataclass(frozen=True, slots=True)
class PreambleMeasurement:
    """In-band signal power and noise variance of one acquired preamble.

    Both are per active carrier on the unitary FFT, which for white noise is
    also the noise variance per complex sample.  ``degrees_of_freedom`` is
    the number of complex values the noise estimate rests on.
    """

    signal_power: float
    noise_variance: float
    degrees_of_freedom: int


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


def measure_preamble(
    samples: ArrayLike,
    acquisition: AcquisitionResult,
    active_carriers: tuple[int, ...],
    config: SyncConfig | None = None,
) -> PreambleMeasurement | None:
    """Split one acquired preamble's in-band power into signal and noise.

    The channel and the CFO treat the two repeated halves alike, so whatever
    differs between them is noise.  Each half is transformed on its own: half
    -length bin ``m`` is full-length carrier ``2m``, and only the bins that
    land on ``active_carriers`` are used.  Measuring in band matters because
    receivers are not white: a Pluto's noise in the guard band was ten times
    its noise under the data carriers, which a time-domain estimate would
    have folded in, reading the SNR about 4 dB low.  The half-length transform
    of the quadratic-phase preamble has unit power in every bin, so the signal
    power is ``|H|^2`` on the carriers the data uses.  ``None`` when the
    samples do not hold the whole useful preamble or no bin is in band.
    """

    settings = _require_config(config)
    values = _as_complex_samples(samples, "samples")
    half = settings.half_length
    start = acquisition.useful_start
    if start < 0 or start + settings.fft_size > values.size:
        return None
    first = values[start : start + half].astype(np.complex128)
    second = values[start + half : start + settings.fft_size].astype(np.complex128)
    # Remove the half-to-half CFO rotation so the shared part lines up.
    rotation = np.vdot(first, second)
    if rotation == 0:
        return None
    second = second * np.conj(rotation) / abs(rotation)
    in_band = _half_bins_in_band(half, tuple(active_carriers))
    if not in_band.size:
        return None
    first_bins = np.fft.fft(first, norm="ortho")[in_band]
    second_bins = np.fft.fft(second, norm="ortho")[in_band]
    noise = float(np.mean(np.abs(second_bins - first_bins) ** 2)) / 2.0
    signal = float(np.mean(np.abs(first_bins + second_bins) ** 2)) / 4.0 - noise / 2.0
    if not signal > 0.0:
        return None
    return PreambleMeasurement(
        signal_power=signal,
        noise_variance=max(noise, signal * _MINIMUM_NOISE_FRACTION),
        degrees_of_freedom=int(in_band.size),
    )


@lru_cache(maxsize=8)
def _half_bins_in_band(half_length: int, active_carriers: tuple[int, ...]) -> NDArray[np.int64]:
    """Half-length FFT bins whose full-length carrier ``2m`` is active."""

    signed = np.arange(half_length)
    signed[signed >= half_length // 2] -= half_length
    bins = np.flatnonzero(np.isin(2 * signed, np.asarray(active_carriers)))
    bins.setflags(write=False)
    return bins


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


@dataclass(frozen=True, slots=True)
class SharedCorrelation:
    """Cumulative sums of one buffer's lag-``lag`` autocorrelation.

    ``p(n) = conj(r(n)) r(n + lag)`` and ``e(n) = (|r(n)|^2 + |r(n + lag)|^2) / 2``
    for every ``n`` with both samples in the buffer.  Both preamble families
    -- the legacy repeated-half chirp and the 802.11a STF -- are periodic in
    32 samples, so their detectors differ only in how wide a window they sum
    over.  Keeping the cumulative sums lets each family take its window with
    three subtractions instead of a second pass over the buffer.  The sums are
    float64: a single-precision cumulative sum over a whole buffer would lose
    far more than the window it is differenced over.
    """

    real_cum: NDArray[np.float64]
    imag_cum: NDArray[np.float64]
    energy_cum: NDArray[np.float64]
    lag: int
    sample_count: int

    def window(
        self, width: int
    ) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
        """Running sums of Re p, Im p and e over every ``width``-product window.

        Entry ``d`` sums products ``d .. d + width - 1``.  The arithmetic is
        exactly :func:`_moving_sum`'s, so the legacy detector sees bit-identical
        sums whether or not the correlation is shared.
        """

        if type(width) is not int or not 1 <= width <= self.real_cum.size:
            raise SyncError("window width must fit the correlated buffer")
        return (
            _window_sums(self.real_cum, width),
            _window_sums(self.imag_cum, width),
            _window_sums(self.energy_cum, width),
        )


def _window_sums(cumulative: NDArray[np.float64], width: int) -> NDArray[np.float64]:
    sums = np.empty(cumulative.size - width + 1, dtype=np.float64)
    sums[0] = cumulative[width - 1]
    np.subtract(cumulative[width:], cumulative[:-width], out=sums[1:])
    return sums


def shared_correlation(samples: ArrayLike, lag: int = 32) -> SharedCorrelation:
    """Compute one buffer's lag-``lag`` correlation sums for both detectors."""

    values = _as_complex_samples(samples, "samples")
    if type(lag) is not int or lag < 1:
        raise SyncError("lag must be a positive integer")
    if values.size <= lag:
        raise SyncError("samples must be longer than the correlation lag")
    return _shared_correlation(values, lag)


def _shared_correlation(values: NDArray[np.complex64], lag: int) -> SharedCorrelation:
    products = np.conjugate(values[:-lag]) * values[lag:]
    # One squared-magnitude pass over the buffer, shared by both halves.
    # ``real**2 + imag**2`` also avoids the square root that ``abs`` takes
    # only to have it squared again.
    squared_magnitude = values.real * values.real
    squared_magnitude += values.imag * values.imag
    paired_power = squared_magnitude[:-lag] + squared_magnitude[lag:]
    paired_power *= 0.5
    # Keep the correlation as separate real and imaginary running sums: the
    # metric needs only their squares, and two float64 cumulative sums are
    # cheaper than one complex128 cumulative sum over the same samples.
    interleaved = products.view(np.float32).reshape(-1, 2)
    return SharedCorrelation(
        real_cum=np.cumsum(interleaved[:, 0], dtype=np.float64),
        imag_cum=np.cumsum(interleaved[:, 1], dtype=np.float64),
        energy_cum=np.cumsum(paired_power, dtype=np.float64),
        lag=lag,
        sample_count=int(values.size),
    )


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
    return _unambiguous_candidate(_acquisition_candidates(samples, settings), settings)


def _unambiguous_candidate(
    candidates: tuple[AcquisitionResult, ...],
    settings: SyncConfig,
) -> AcquisitionResult | None:
    """The strongest candidate, unless an independent one comes close to it."""

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
    *,
    shared: SharedCorrelation | None = None,
) -> tuple[AcquisitionResult, ...]:
    """Return every threshold-qualified preamble candidate.

    This internal vectorized scan lets the continuous decoder separate multiple
    bursts before applying the public finite-buffer ambiguity rule.  ``shared``
    is the buffer's lag-32 correlation when the caller already computed it for
    the v5 detector: the running sums are the expensive part of the scan and
    both preambles need exactly the same ones, so they are computed once.  The
    result does not depend on whether it was supplied.
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
    if (
        shared is None
        or shared.lag != half_length
        or shared.sample_count != values.size
    ):
        shared = _shared_correlation(values, half_length)
    correlation_real, correlation_imag, repeated_energy = shared.window(half_length)

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


def separated_candidates(
    candidates: tuple[AcquisitionResult, ...],
    preamble_length: int,
) -> tuple[AcquisitionResult, ...]:
    """Keep the strongest legacy candidate for each physical preamble."""

    selected: list[AcquisitionResult] = []
    for candidate in sorted(candidates, key=lambda item: item.confidence, reverse=True):
        if all(
            abs(candidate.preamble_start - other.preamble_start) >= preamble_length
            for other in selected
        ):
            selected.append(candidate)
    return tuple(sorted(selected, key=lambda item: item.preamble_start))


def acquire_all(
    samples: ArrayLike,
    legacy: SyncConfig | None,
    v5: V5SyncConfig | None,
) -> tuple[AnyAcquisition, ...]:
    """Every separated preamble of either family in one buffer, by start.

    One lag-32 correlation feeds both detectors.  Within a family the
    strongest candidate per preamble survives (legacy: 80 samples apart, as
    before; v5: one per 560-sample fixed part).  Across families a candidate
    that starts less than a v5 preamble (320 samples) before a v5 candidate,
    or inside its 560-sample fixed part (preamble and SIG), is the same
    physical preamble seen by both detectors, or one detector's false alarm
    on the other's burst, so only the one with the higher confidence
    relative to its own acceptance threshold is kept.  A candidate left
    inside a v5 fixed part would bound the burst's samples before its SIG
    and drop it.  ``None`` disables a family.  With ``v5`` disabled the
    result is exactly the legacy separated scan; with both disabled it is
    empty.

    ``v5.prefilter`` (experiment only) low-pass filters the acquisition copy
    of the buffer for both families; the decoder still reads the raw samples.
    """

    from .sync_v5 import (
        V5_FIXED_LENGTH,
        V5_PREAMBLE_LENGTH,
        V5Acquisition,
        prefilter_for_acquisition,
        v5_candidates,
    )

    if legacy is None and v5 is None:
        return ()
    values = _as_complex_samples(samples, "samples")
    if v5 is not None and v5.prefilter:
        values = prefilter_for_acquisition(values)
    lag = 32 if legacy is None else legacy.half_length
    shared = (
        _shared_correlation(values, lag)
        if values.size > lag and np.any(values)
        else None
    )
    legacy_found: tuple[AcquisitionResult, ...] = ()
    if legacy is not None:
        legacy_found = separated_candidates(
            _acquisition_candidates(values, legacy, shared=shared),
            legacy.preamble_length,
        )
    if v5 is None:
        return legacy_found
    v5_found = () if shared is None or lag != 32 else v5_candidates(values, shared, v5)
    if not v5_found:
        return legacy_found
    if not legacy_found:
        return v5_found

    legacy_threshold = 1.0 if legacy is None else legacy.correlation_threshold
    v5_threshold = v5.correlation_threshold

    def score(item: AnyAcquisition) -> float:
        if isinstance(item, AcquisitionResult):
            return item.confidence / legacy_threshold
        return item.confidence / v5_threshold

    def clash(first: AnyAcquisition, second: AnyAcquisition) -> bool:
        v5_item, other = (first, second) if isinstance(first, V5Acquisition) else (second, first)
        offset = other.preamble_start - v5_item.preamble_start
        return -V5_PREAMBLE_LENGTH < offset < V5_FIXED_LENGTH

    selected: list[AnyAcquisition] = []
    for candidate in sorted((*legacy_found, *v5_found), key=score, reverse=True):
        if all(
            type(other) is type(candidate) or not clash(candidate, other) for other in selected
        ):
            selected.append(candidate)
    return tuple(sorted(selected, key=lambda item: item.preamble_start))


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
