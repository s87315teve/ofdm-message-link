"""Wire-v5 preamble: 802.11a L-STF and L-LTF, acquisition and LTF measurement.

The legacy preamble is one 80-sample repeated-half chirp, so its lag-32
detector averages only 32 products and needs about 0 dB wideband SNR; at the
link's lowest operating points most lost bursts were missed preambles.  The
802.11a short training field is lag-32 periodic over 160 samples, so the same
correlation can be averaged over 128 products: four times less metric noise,
which lets the detection threshold drop from 0.25 to 0.10, about 3 dB.  The
long training field then gives two identical 64-sample symbols: their average
halves the channel-estimate noise and their difference measures the noise on
52 carriers (the legacy preamble halves give 26).

Layout, in samples from the first STF sample (``stf_start``)::

    | STF 160 | GI2 32 | L1 64 | L2 64 | SIG 3x80 | payload ... |
    0        160      192     256     320        560

Acquisition, for each candidate (all stages vectorized over candidates):

1. ``M128(d) = |sum p| ^ 2 / (sum e) ^ 2`` over 128 lag-32 products, from the
   correlation sums the legacy detector already computed
   (:class:`~ofdm_link.phy.sync.SharedCorrelation`), against
   ``detection_threshold`` and an energy gate.
2. One candidate per run of eligible offsets (runs split at gaps over 32):
   the offset of the largest ``|sum p|``, which is a triangle peaking at
   ``stf_start`` whatever the guard before it holds.  The normalized metric
   is flat over an exact-zero guard and would not locate the peak.
3. Coarse CFO from the lag-16 correlation over STF samples 16..159 (range
   +-2 subcarriers, enough for two +-25 ppm radios at 2.45 GHz), then a
   coherent check against the STF over its 16 cyclic phases: tones, DC and
   spurs score about 1/12 and are rejected by ``stf_threshold``.
4. Fine timing on the LTF pair over +-``fine_timing_search`` samples, with a
   balance test: a legacy burst's single training symbol matches L1 but not
   L2, so ``min(c1, c2) >= balance_ratio * c_pair`` rejects it.
5. Fine CFO from the L1/L2 phase difference (range +-0.5 after the coarse
   step).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from numpy.typing import ArrayLike, NDArray

from .channel import _TRAINING_BPSK_V1, ChannelEstimate, ChannelEstimationError
from .ofdm import OfdmNumerology
from .sync import (
    AcquisitionResult,
    SharedCorrelation,
    SyncError,
    _as_complex_samples,
    _is_finite_real,
    _validate_unit_interval,
)

V5_STF_LENGTH = 160
V5_LTF_LENGTH = 160
V5_PREAMBLE_LENGTH = V5_STF_LENGTH + V5_LTF_LENGTH
V5_SIG_LENGTH = 3 * 80
V5_FIXED_LENGTH = V5_PREAMBLE_LENGTH + V5_SIG_LENGTH
# Offsets from ``stf_start``.
V5_LTF_GUARD_OFFSET = V5_STF_LENGTH
V5_LTF_FIRST_SYMBOL_OFFSET = V5_STF_LENGTH + 32
V5_SIG_OFFSET = V5_PREAMBLE_LENGTH

_CORRELATION_LAG = 32
_STF_PERIOD = 16
_METRIC_WIDTH = 128
_COARSE_CFO_LAG = 16
# The first STF period is skipped by the CFO and coherent stages: it carries
# the transmitter's switch-on transient.
_STF_SKIP = 16
_STF_CHECK_LENGTH = 128
# STF frequency-domain values (IEEE 802.11-2016 17.3.3, L-STF), before the
# sqrt(13/6) normalization that gives the field the data symbols' mean power.
_STF_CARRIERS = (-24, -20, -16, -12, -8, -4, 4, 8, 12, 16, 20, 24)
_STF_VALUES = (
    1 + 1j, -1 - 1j, 1 + 1j, -1 - 1j, -1 - 1j, 1 + 1j,
    -1 - 1j, -1 - 1j, 1 + 1j, 1 + 1j, 1 + 1j, 1 + 1j,
)
# Floor on the LTF noise estimate as a fraction of the signal power: caps a
# noiseless reading at 60 dB, as the legacy preamble measurement does.
_MINIMUM_NOISE_FRACTION = 1e-6
_DEFAULT_NUMEROLOGY = OfdmNumerology()

LlrNoiseSource = Literal["max", "header", "ltf"]


@dataclass(frozen=True, slots=True)
class V5SyncConfig:
    """Thresholds of the v5 detector; defaults are predictions until measured.

    ``detection_threshold`` gates ``M128``.  Its mean on a preamble is about
    ``(s / (1 + s)) ^ 2`` at wideband SNR ``s``, and 128 products put white
    noise's chance of reaching 0.10 near ``exp(-0.1 * 128)`` per offset, so
    0.10 reaches about -3.4 dB.  ``stf_threshold`` and
    ``correlation_threshold`` are the coherent stages that actually separate
    signal from noise; they are set from signal-free air captures like the
    legacy ones were.  ``balance_ratio`` is the L1/L2 balance test that keeps
    a legacy burst from being acquired as v5.  ``fine_timing_search`` must
    stay below 64 or the LTF search reaches its own one-symbol alias.
    ``prefilter`` (experiment only) low-pass filters the acquisition copy of
    the samples to the occupied band for both families.
    """

    detection_threshold: float = 0.10
    stf_threshold: float = 0.20
    correlation_threshold: float = 0.25
    balance_ratio: float = 0.5
    fine_timing_search: int = 32
    prefilter: bool = False
    minimum_power: float = 1e-8

    def __post_init__(self) -> None:
        _validate_unit_interval(self.detection_threshold, "detection_threshold")
        _validate_unit_interval(self.stf_threshold, "stf_threshold")
        _validate_unit_interval(self.correlation_threshold, "correlation_threshold")
        _validate_unit_interval(self.balance_ratio, "balance_ratio")
        if type(self.fine_timing_search) is not int or not 0 <= self.fine_timing_search < 64:
            raise SyncError("fine_timing_search must be an integer in [0, 64)")
        if type(self.prefilter) is not bool:
            raise SyncError("prefilter must be a bool")
        if not _is_finite_real(self.minimum_power) or self.minimum_power <= 0.0:
            raise SyncError("minimum_power must be finite and greater than zero")


@dataclass(frozen=True, slots=True)
class V5Acquisition:
    """One acquired v5 preamble.

    ``stf_start`` is the first STF sample in the supplied buffer and
    ``ltf_start`` the first sample of the first useful LTF symbol L1
    (``stf_start + 192``).  ``normalized_cfo`` is the total CFO in subcarrier
    spacings and ``coarse_cfo`` its lag-16 STF part.  ``metric`` is ``M128``
    at the chosen offset, ``stf_confidence`` the coherent STF correlation and
    ``confidence`` the LTF-pair correlation, the value cross-family resolution
    compares against ``correlation_threshold``.
    """

    stf_start: int
    ltf_start: int
    normalized_cfo: float
    coarse_cfo: float
    metric: float
    stf_confidence: float
    confidence: float

    @property
    def preamble_start(self) -> int:
        """Alias of ``stf_start`` so generic stream code handles both families."""

        return self.stf_start


AnyAcquisition = AcquisitionResult | V5Acquisition


@dataclass(frozen=True, slots=True)
class V5RxConfig:
    """Receiver settings for the dual-family RX; all but the limits are A/B toggles.

    ``legacy_acquisition`` / ``v5_acquisition`` switch each detector off
    (CPU, cross-family tests).  ``ltf_average=False`` estimates the channel
    from L1 alone, ``channel_smoothing=False`` keeps the raw LS estimate, and
    ``llr_noise_source`` picks the payload LLR noise variance: the larger of
    the header-measured and LTF-measured values (default), or one of them.
    ``max_psdu_bytes`` bounds the PSDU length a SIG may signal, and
    ``turbo_max_iterations`` the payload decoder.
    """

    acquisition: V5SyncConfig = field(default_factory=V5SyncConfig)
    legacy_acquisition: bool = True
    v5_acquisition: bool = True
    ltf_average: bool = True
    channel_smoothing: bool = True
    llr_noise_source: LlrNoiseSource = "max"
    max_psdu_bytes: int = 16384
    turbo_max_iterations: int = 8

    def __post_init__(self) -> None:
        if not isinstance(self.acquisition, V5SyncConfig):
            raise SyncError("acquisition must be a V5SyncConfig")
        for name in ("legacy_acquisition", "v5_acquisition", "ltf_average", "channel_smoothing"):
            if type(getattr(self, name)) is not bool:
                raise SyncError(f"{name} must be a bool")
        if self.llr_noise_source not in ("max", "header", "ltf"):
            raise SyncError("llr_noise_source must be 'max', 'header' or 'ltf'")
        if type(self.max_psdu_bytes) is not int or not 19 <= self.max_psdu_bytes <= 65535:
            raise SyncError("max_psdu_bytes must be an integer in [19, 65535]")
        if self.turbo_max_iterations not in (2, 4, 6, 8) or type(
            self.turbo_max_iterations
        ) is not int:
            raise SyncError("turbo_max_iterations must be one of 2, 4, 6, 8")


@dataclass(frozen=True, slots=True)
class LtfMeasurement:
    """LS channel estimate and in-band noise/signal from the LTF pair.

    ``noise_variance`` is per complex sample (= per carrier on the unitary
    FFT); ``signal_power`` is the mean ``|H|^2`` on the active carriers, so
    :func:`~ofdm_link.phy.streaming.link_snr_db` reads it like the legacy
    preamble measurement.
    """

    least_squares: ChannelEstimate
    noise_variance: float
    signal_power: float
    degrees_of_freedom: int


def _require_numerology(numerology: OfdmNumerology) -> None:
    if not isinstance(numerology, OfdmNumerology):
        raise SyncError("numerology must be an OfdmNumerology")
    if numerology.fft_size != 64 or numerology.cp_length != 16:
        raise SyncError("the v5 preamble is defined for the 64-point, 16-sample-CP numerology")


def generate_stf(numerology: OfdmNumerology = _DEFAULT_NUMEROLOGY) -> NDArray[np.complex64]:
    """The 160-sample 802.11a short training field (ten 16-sample periods).

    Mean power 52/64, the same as the LTF and the data symbols, so per-burst
    peak normalization treats the whole burst alike.
    """

    _require_numerology(numerology)
    return _stf_period().astype(np.complex64)[np.arange(V5_STF_LENGTH) % _STF_PERIOD]


@lru_cache(maxsize=1)
def _stf_period() -> NDArray[np.complex128]:
    grid = np.zeros(64, dtype=np.complex128)
    grid[np.asarray(_STF_CARRIERS) % 64] = np.sqrt(13.0 / 6.0) * np.asarray(_STF_VALUES)
    period = np.fft.ifft(grid, norm="ortho")[:_STF_PERIOD].copy()
    period.setflags(write=False)
    return period


@lru_cache(maxsize=1)
def _ltf_symbol() -> NDArray[np.complex128]:
    """The 64-sample useful LTF symbol: the legacy training symbol's body."""

    grid = np.zeros(64, dtype=np.complex128)
    grid[np.asarray(_DEFAULT_NUMEROLOGY.active_carriers) % 64] = np.asarray(
        _TRAINING_BPSK_V1, dtype=np.float64
    )
    symbol = np.fft.ifft(grid, norm="ortho")
    symbol.setflags(write=False)
    return symbol


def generate_ltf(numerology: OfdmNumerology = _DEFAULT_NUMEROLOGY) -> NDArray[np.complex64]:
    """The 160-sample long training field: ``l[32:64] ‖ l ‖ l``."""

    _require_numerology(numerology)
    symbol = _ltf_symbol()
    return np.concatenate((symbol[32:], symbol, symbol)).astype(np.complex64)


@lru_cache(maxsize=1)
def _prefilter_taps() -> NDArray[np.float64]:
    # 31-tap Hamming-windowed sinc, cutoff 0.44 cycles/sample (2.2 MHz at
    # 5 MS/s): passes the 26 occupied carriers on each side and attenuates the
    # guard band, where a Pluto's noise sits about 10 dB above in-band.
    index = np.arange(31) - 15
    taps = 2 * 0.44 * np.sinc(2 * 0.44 * index) * np.hamming(31)
    taps /= taps.sum()
    taps.setflags(write=False)
    return taps


def prefilter_for_acquisition(samples: ArrayLike) -> NDArray[np.complex64]:
    """Low-pass the acquisition copy of the samples (experiment only).

    Linear phase and centred (``mode='same'``), so acquired offsets still
    index the raw buffer.
    """

    values = _as_complex_samples(samples, "samples")
    return np.convolve(values, _prefilter_taps(), mode="same").astype(np.complex64)


# Samples a candidate offset needs after it: the LTF search reaches
# ``d + 192 + search`` and correlates 128 samples from there.
def _required_tail(config: V5SyncConfig) -> int:
    return V5_LTF_FIRST_SYMBOL_OFFSET + config.fine_timing_search + 128


#: How many trailing samples a streaming receiver must keep when no v5
#: preamble was found: a run of eligible offsets is only judged once its
#: whole triangle and its LTF search fit in the buffer.
V5_RETAIN_SAMPLES = 640


def v5_candidates(
    samples: ArrayLike,
    shared: SharedCorrelation,
    config: V5SyncConfig,
) -> tuple[V5Acquisition, ...]:
    """Every v5 preamble in the buffer, at most one per 320 samples, by start.

    ``shared`` must be the lag-32 correlation of these same samples.  An
    eligible run that reaches within 32 offsets of the last judgeable offset is
    left for a later, longer buffer: its peak may not have arrived yet.
    """

    if not isinstance(config, V5SyncConfig):
        raise SyncError("config must be a V5SyncConfig")
    if not isinstance(shared, SharedCorrelation) or shared.lag != _CORRELATION_LAG:
        raise SyncError("shared must be the lag-32 SharedCorrelation of the samples")
    values = _as_complex_samples(samples, "samples")
    if shared.sample_count != values.size:
        raise SyncError("shared correlation does not belong to these samples")
    limit = values.size - _required_tail(config)
    if limit <= 0 or shared.real_cum.size < _METRIC_WIDTH:
        return ()
    real, imag, energy = shared.window(_METRIC_WIDTH)
    real, imag, energy = real[:limit], imag[:limit], energy[:limit]
    numerator = real * real
    numerator += imag * imag
    threshold = energy * energy
    threshold *= config.detection_threshold
    eligible = numerator >= threshold
    eligible &= energy >= config.minimum_power * _METRIC_WIDTH
    offsets = np.flatnonzero(eligible)
    if not offsets.size:
        return ()

    # Non-maximum suppression: one candidate per run of eligible offsets.
    breaks = np.flatnonzero(np.diff(offsets) > 32) + 1
    run_starts = np.concatenate(([0], breaks))
    run_ends = np.concatenate((breaks, [offsets.size]))
    peaks: list[int] = []
    for first, last in zip(run_starts.tolist(), run_ends.tolist(), strict=True):
        if offsets[last - 1] >= limit - 32:
            continue
        run = offsets[first:last]
        peaks.append(int(run[np.argmax(numerator[run])]))
    if not peaks:
        return ()
    starts = np.asarray(peaks, dtype=np.int64)
    metric = numerator[starts] / np.square(energy[starts])

    # Coarse CFO from the lag-16 STF correlation, skipping the first period.
    span = np.arange(_STF_SKIP, _STF_SKIP + _STF_CHECK_LENGTH)
    first = values[starts[:, None] + span[None, :]].astype(np.complex128)
    lagged = values[starts[:, None] + span[None, :] + _COARSE_CFO_LAG].astype(np.complex128)
    coarse = 4.0 * np.angle(np.sum(np.conj(first) * lagged, axis=1)) / (2.0 * np.pi)

    # Coherent STF check over the 16 cyclic phases of the period.
    rotation = np.exp(-2j * np.pi * coarse[:, None] * span[None, :] / 64.0)
    corrected = first * rotation
    folded = corrected.reshape(starts.size, _STF_CHECK_LENGTH // _STF_PERIOD, _STF_PERIOD).sum(
        axis=1
    )
    phases, reference_energy = _stf_phase_matrix()
    coherent = folded @ phases.T
    window_energy = np.sum(np.abs(corrected) ** 2, axis=1)
    stf_confidence = np.zeros(starts.size)
    powered = window_energy >= config.minimum_power * _STF_CHECK_LENGTH
    stf_confidence[powered] = np.max(np.abs(coherent[powered]) ** 2, axis=1) / (
        window_energy[powered] * reference_energy
    )
    keep = stf_confidence >= config.stf_threshold
    if not np.any(keep):
        return ()
    starts, metric, coarse, stf_confidence = (
        starts[keep],
        metric[keep],
        coarse[keep],
        stf_confidence[keep],
    )

    # Fine timing on the LTF pair, coarse CFO removed.
    search = config.fine_timing_search
    block_offsets = np.arange(
        V5_LTF_FIRST_SYMBOL_OFFSET - search, V5_LTF_FIRST_SYMBOL_OFFSET + search + 128
    )
    block = values[starts[:, None] + block_offsets[None, :]].astype(np.complex128)
    block *= np.exp(-2j * np.pi * coarse[:, None] * block_offsets[None, :] / 64.0)
    windows = sliding_window_view(block, 128, axis=1)  # (candidates, 2*search+1, 128)
    reference = np.conj(_ltf_symbol())
    reference_power = 64.0 * float(np.mean(np.abs(_ltf_symbol()) ** 2))
    first_corr = windows[:, :, :64] @ reference
    second_corr = windows[:, :, 64:] @ reference
    power = np.abs(block) ** 2
    cumulative = np.concatenate(
        (np.zeros((starts.size, 1)), np.cumsum(power, axis=1)), axis=1
    )
    positions = np.arange(2 * search + 1)
    first_energy = cumulative[:, positions + 64] - cumulative[:, positions]
    second_energy = cumulative[:, positions + 128] - cumulative[:, positions + 64]
    total_energy = first_energy + second_energy
    with np.errstate(divide="ignore", invalid="ignore"):
        pair = np.abs(first_corr + second_corr) ** 2 / (2.0 * total_energy * reference_power)
    pair = np.where(total_energy >= config.minimum_power * 128, pair, 0.0)
    best = np.argmax(pair, axis=1)
    rows = np.arange(starts.size)
    confidence = pair[rows, best]
    with np.errstate(divide="ignore", invalid="ignore"):
        first_single = np.abs(first_corr[rows, best]) ** 2 / (
            first_energy[rows, best] * reference_power
        )
        second_single = np.abs(second_corr[rows, best]) ** 2 / (
            second_energy[rows, best] * reference_power
        )
    balanced = np.minimum(np.nan_to_num(first_single), np.nan_to_num(second_single)) >= (
        config.balance_ratio * confidence
    )
    ltf_starts = starts + V5_LTF_FIRST_SYMBOL_OFFSET - search + best
    stf_starts = ltf_starts - V5_LTF_FIRST_SYMBOL_OFFSET
    accepted = (confidence >= config.correlation_threshold) & balanced & (stf_starts >= 0)
    if not np.any(accepted):
        return ()

    # Fine CFO from the L1 -> L2 rotation of the selected window.
    chosen = windows[rows, best]
    fine = np.angle(np.sum(chosen[:, 64:] * np.conj(chosen[:, :64]), axis=1)) / (2.0 * np.pi)
    found = [
        V5Acquisition(
            stf_start=int(stf),
            ltf_start=int(ltf),
            normalized_cfo=float(c + f),
            coarse_cfo=float(c),
            metric=float(m),
            stf_confidence=float(s),
            confidence=float(q),
        )
        for stf, ltf, c, f, m, s, q, ok in zip(
            stf_starts,
            ltf_starts,
            coarse,
            fine,
            metric,
            stf_confidence,
            confidence,
            accepted,
            strict=True,
        )
        if ok
    ]
    return separated_v5_candidates(tuple(found))


def separated_v5_candidates(
    candidates: tuple[V5Acquisition, ...],
) -> tuple[V5Acquisition, ...]:
    """Keep the strongest v5 candidate per 320-sample preamble, by start."""

    selected: list[V5Acquisition] = []
    for candidate in sorted(candidates, key=lambda item: item.confidence, reverse=True):
        if all(
            abs(candidate.stf_start - other.stf_start) >= V5_PREAMBLE_LENGTH
            for other in selected
        ):
            selected.append(candidate)
    return tuple(sorted(selected, key=lambda item: item.stf_start))


@lru_cache(maxsize=1)
def _stf_phase_matrix() -> tuple[NDArray[np.complex128], float]:
    """Conjugated STF period in each of its 16 cyclic phases, and the
    reference energy of 128 STF samples."""

    period = _stf_period()
    index = (np.arange(_STF_PERIOD)[:, None] + np.arange(_STF_PERIOD)[None, :]) % _STF_PERIOD
    phases = np.conj(period[index])
    phases.setflags(write=False)
    energy = float(np.sum(np.abs(period) ** 2)) * (_STF_CHECK_LENGTH // _STF_PERIOD)
    return phases, energy


def correct_v5_cfo(
    samples: NDArray[np.complex64],
    *,
    start: int,
    acquisition: V5Acquisition,
) -> NDArray[np.complex64]:
    """Remove the acquired CFO from ``samples`` that begin at buffer index ``start``.

    The phase reference is ``stf_start``, so every part of one burst --
    LTF, SIG, payload -- is rotated consistently and the channel estimate's
    common phase carries over.
    """

    offset = start - acquisition.stf_start
    phase = np.arange(offset, offset + samples.size, dtype=np.float64)
    phase *= acquisition.normalized_cfo / 64.0
    np.remainder(phase, 1.0, out=phase)
    phase *= -2.0 * np.pi
    return (samples * np.exp(1j * phase)).astype(np.complex64)


def measure_ltf_pair(
    samples: ArrayLike,
    acquisition: V5Acquisition,
    numerology: OfdmNumerology = _DEFAULT_NUMEROLOGY,
    *,
    average: bool = True,
) -> LtfMeasurement | None:
    """Channel estimate, noise and signal power from the two LTF symbols.

    Whatever differs between L1 and L2 after CFO removal is noise, so
    ``sigma^2 = mean|Y1 - Y2|^2 / 2`` on the 52 active carriers, in band (a
    receiver's guard-band noise does not enter), and the LS estimate
    ``(Y1 + Y2) / (2 l_k)`` carries half the noise of one symbol.
    ``average=False`` (experiment) estimates from L1 alone; the noise still
    comes from the pair.  ``None`` when the samples do not hold both symbols
    or the estimate is unusable.
    """

    _require_numerology(numerology)
    values = _as_complex_samples(samples, "samples")
    start = acquisition.ltf_start
    if start < 0 or start + 128 > values.size:
        return None
    corrected = correct_v5_cfo(values[start : start + 128], start=start, acquisition=acquisition)
    spectra = np.fft.fft(corrected.astype(np.complex128).reshape(2, 64), axis=1, norm="ortho")
    active = np.asarray(numerology.active_carriers) % 64
    first, second = spectra[0, active], spectra[1, active]
    references = np.asarray(_TRAINING_BPSK_V1, dtype=np.float64)
    noise = float(np.mean(np.abs(first - second) ** 2)) / 2.0
    signal = float(np.mean(np.abs(first + second) ** 2)) / 4.0 - noise / 2.0
    if not signal > 0.0:
        return None
    coefficients = (first + second) / (2.0 * references) if average else first / references
    try:
        estimate = ChannelEstimate(coefficients.astype(np.complex64), numerology.active_carriers)
    except ChannelEstimationError:
        return None
    return LtfMeasurement(
        least_squares=estimate,
        noise_variance=max(noise, signal * _MINIMUM_NOISE_FRACTION),
        signal_power=signal,
        degrees_of_freedom=int(active.size),
    )
