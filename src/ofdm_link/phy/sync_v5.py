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

1. ``M128(d) = |sum p - 128 m| ^ 2 / (sum e - 128 |m|) ^ 2`` over 128 lag-32
   products, from the correlation sums the legacy detector already computed
   (:class:`~ofdm_link.phy.sync.SharedCorrelation`), against
   ``detection_threshold`` and an energy gate.  ``m`` is the mean product
   over the surrounding 1024: the steady part a DC offset or CW tone adds to
   every product, which would otherwise hold the metric above threshold
   over the whole buffer.
2. Non-maximum suppression: the offset of the largest ``|sum p - 128 m|``
   within +-160 eligible offsets, which is a triangle peaking at
   ``stf_start`` whatever the guard before it holds.  The normalized metric
   is flat over an exact-zero guard and would not locate the peak.
3. Coarse CFO from the lag-16 correlation over STF samples 16..159 (range
   +-2 subcarriers, enough for two +-25 ppm radios at 2.45 GHz), with the
   local DC and steady lag-16 product removed, then a coherent check against
   the STF over its 16 cyclic phases: tones, DC and spurs score about 1/12
   and are rejected by ``stf_threshold``.
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
#: Largest FFT backoff into the 16-sample CP; the rest is left for the
#: channel's delay spread.
MAX_FFT_BACKOFF_SAMPLES = 8


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

    ``fft_backoff_samples`` starts every v5 FFT window -- LTF, SIG and
    payload alike -- that many samples inside the cyclic prefix instead of
    at its end.  A window that starts late takes samples of the next symbol
    (ISI) the moment fine timing is a sample late or the sample clock drifts
    the burst later; starting early only costs a linear phase, which the
    LTF-based channel estimate absorbs because every window moves alike.  It
    must stay well below the 16-sample CP minus the channel's delay spread.
    """

    acquisition: V5SyncConfig = field(default_factory=V5SyncConfig)
    legacy_acquisition: bool = True
    v5_acquisition: bool = True
    ltf_average: bool = True
    channel_smoothing: bool = True
    llr_noise_source: LlrNoiseSource = "max"
    max_psdu_bytes: int = 16384
    turbo_max_iterations: int = 8
    fft_backoff_samples: int = 3

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
        if (
            type(self.fft_backoff_samples) is not int
            or not 0 <= self.fft_backoff_samples <= MAX_FFT_BACKOFF_SAMPLES
        ):
            raise SyncError(
                f"fft_backoff_samples must be an integer in [0, {MAX_FFT_BACKOFF_SAMPLES}]"
            )


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
#: preamble was found: a peak is only judged once its whole suppression
#: neighbourhood and its LTF search fit in the buffer.
V5_RETAIN_SAMPLES = 640

# Half-width of the peak search: the M128 triangle spans +-128 offsets around
# ``stf_start``, and two v5 preambles are never closer than a fixed header.
_PEAK_RADIUS = V5_PREAMBLE_LENGTH // 2
# Products on each side of the 128-product metric window whose mean is taken
# as the steady (DC or CW tone) part of the correlation.  Long against the
# STF, so the mean is well averaged, short against the seconds a radio's DC
# offset drifts over.
_STEADY_SIDE = 448
# Fewest products a side needs before its mean is trusted.
_STEADY_MINIMUM = _METRIC_WIDTH
# Offsets between two measurements of the steady part.
_STEADY_STEP = 16
# Samples around a candidate over which its local DC and steady lag-16 and
# lag-64 products are measured for the coherent stages.
_LOCAL_SPAN = 1024
# Standard deviations of white-noise scatter a local steady product must
# exceed before it is subtracted.
_STEADY_SIGNIFICANCE = 4.0


def _steady_mean(
    shared: SharedCorrelation, limit: int
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Steady lag-32 product around each metric window offset.

    A DC offset ``c`` puts ``|c|^2`` into every product and a CW tone a
    constant phasor; neither averages out over 128 products, so without this
    the normalized metric stays above threshold over the whole buffer and no
    preamble triangle stands out.  The mean is taken separately over the
    ``_STEADY_SIDE`` products before the window and those after it, and the
    side with the smaller mean is used (smallest-of, as in a CFAR detector):
    a steady interferer is on both sides, a preamble near the window only on
    one, and must not be subtracted from the noise in front of it.
    """

    products = shared.real_cum.size
    side = _STEADY_SIDE
    step = _STEADY_STEP
    # The steady part changes slowly by definition, so it is measured every
    # ``step`` offsets and held in between: a sixteenth of the work of the
    # full-rate metric it corrects.
    grid = np.arange(0, limit, step)
    before_count = np.minimum(grid, side)
    after_count = np.minimum(products - _METRIC_WIDTH - grid, side)
    trusted_before = before_count >= _STEADY_MINIMUM
    trusted_after = after_count >= _STEADY_MINIMUM
    before_scale = np.where(trusted_before, 1.0 / np.maximum(before_count, 1), 0.0)
    after_scale = np.where(trusted_after, 1.0 / np.maximum(after_count, 1), 0.0)
    # Each side's sum is a difference of the cumulative sum at two shifted
    # positions; padding the cumulative sum with its end values on both sides
    # turns every clipped position into a plain strided slice.
    start = side + _METRIC_WIDTH
    means = []
    for cumulative in (shared.real_cum, shared.imag_cum):
        z = np.concatenate(
            (np.zeros(side + 1), cumulative, np.full(side + _METRIC_WIDTH, cumulative[-1]))
        )
        before = z[side : side + limit : step] - z[:limit:step]
        after = z[start + side : start + side + limit : step] - z[start : start + limit : step]
        means.append((before * before_scale, after * after_scale))
    (before_real, after_real), (before_imag, after_imag) = means
    before_power = np.where(trusted_before, before_real**2 + before_imag**2, np.inf)
    after_power = np.where(trusted_after, after_real**2 + after_imag**2, np.inf)
    # An untrusted side never wins the smallest-of choice; with neither side
    # trusted both means are zero and nothing is subtracted.
    use_before = before_power <= after_power
    real = np.where(use_before, before_real, after_real)
    imag = np.where(use_before, before_imag, after_imag)
    return np.repeat(real, step)[:limit], np.repeat(imag, step)[:limit]


class _LocalInterference:
    """Local DC and steady lag products around each candidate.

    The DC is the mean of the ``_LOCAL_SPAN`` samples around a candidate: the
    preamble and payload carry no DC carrier, so they leave it alone.  A
    steady lag-``L`` product (a CW tone's ``|a|^2 exp(j 2 pi f L)``) is the
    mean, DC removed, of every product over the same span that does not
    touch the candidate's 320-sample preamble, and for lag 64 none that
    start in a SIG or payload cyclic prefix, which the burst itself
    correlates.
    """

    def __init__(self, values: NDArray[np.complex64], starts: NDArray[np.int64]) -> None:
        span = min(_LOCAL_SPAN, values.size)
        first = np.clip(starts - (span - V5_PREAMBLE_LENGTH) // 2, 0, values.size - span)
        self._local = values[first[:, None] + np.arange(span)[None, :]].astype(np.complex128)
        self.dc = self._local.mean(axis=1)
        self._local -= self.dc[:, None]
        self._preamble = (starts - first)[:, None]
        self._power = np.mean(self._local.real**2 + self._local.imag**2, axis=1)

    def keep(self, mask: NDArray[np.bool_]) -> None:
        """Drop the candidates a stage rejected."""

        self._local = self._local[mask]
        self.dc = self.dc[mask]
        self._preamble = self._preamble[mask]
        self._power = self._power[mask]

    def steady(self, lag: int) -> NDArray[np.complex128]:
        """The significant steady lag-``lag`` product per candidate, else zero."""

        products = np.conj(self._local[:, :-lag]) * self._local[:, lag:]
        position = np.arange(products.shape[1])[None, :]
        after = position - (self._preamble + V5_PREAMBLE_LENGTH)
        outside = (position < self._preamble - lag) | (after >= 0)
        if lag == 64:
            # A cyclic prefix repeats its symbol's last 16 samples 64 later:
            # those products are the burst's own, not an interferer's.
            outside &= (after < 0) | (after % 80 >= 16)
        count = np.maximum(np.count_nonzero(outside, axis=1), 1)
        products[~outside] = 0.0
        mean = products.sum(axis=1) / count
        # The mean of ``count`` white products scatters by ``power /
        # sqrt(count)``; below a few of those it is noise, and subtracting it
        # would only add that noise to the CFO estimates.
        significant = np.abs(mean) > _STEADY_SIGNIFICANCE * self._power / np.sqrt(count)
        return np.where(significant, mean, 0.0)


def v5_candidates(
    samples: ArrayLike,
    shared: SharedCorrelation,
    config: V5SyncConfig,
) -> tuple[V5Acquisition, ...]:
    """Every v5 preamble in the buffer, at most one per 560 samples, by start.

    ``shared`` must be the lag-32 correlation of these same samples.  A peak
    whose +-160-offset neighbourhood reaches past the last judgeable offset is
    left for a later, longer buffer: a larger one may not have arrived yet.
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
    real, imag, raw_energy = shared.window(_METRIC_WIDTH)
    steady_real, steady_imag = _steady_mean(shared, limit)
    real = real[:limit] - _METRIC_WIDTH * steady_real
    imag = imag[:limit] - _METRIC_WIDTH * steady_imag
    raw_energy = raw_energy[:limit]
    numerator = real * real
    numerator += imag * imag
    # The steady part removed from the products is removed from their power
    # too, so a preamble under a DC offset or a tone scores what it would
    # without it.  Where nothing steady is present the correction is the
    # small mean of white products, a few percent of the energy.
    energy = raw_energy - _METRIC_WIDTH * np.hypot(steady_real, steady_imag)
    threshold = energy * energy
    threshold *= config.detection_threshold
    eligible = numerator >= threshold
    eligible &= energy > 0.0
    eligible &= raw_energy >= config.minimum_power * _METRIC_WIDTH
    if not np.any(eligible):
        return ()

    # Non-maximum suppression: the largest ``|sum p|`` within +-_PEAK_RADIUS
    # offsets is one candidate, the triangle's apex at ``stf_start``.  A
    # long eligible stretch -- a strong interferer the steady-part removal
    # did not cancel -- yields one candidate per neighbourhood instead of
    # one for the whole stretch, and a peak is left for a later buffer only
    # while its own neighbourhood is incomplete, so no run is deferred
    # forever however long it is.
    score = np.where(eligible, numerator, -1.0)
    peaks: list[int] = []
    while True:
        peak = int(np.argmax(score))
        if score[peak] < 0.0:
            break
        score[max(0, peak - _PEAK_RADIUS) : peak + _PEAK_RADIUS + 1] = -1.0
        if peak + _PEAK_RADIUS < limit:
            peaks.append(peak)
    if not peaks:
        return ()
    starts = np.sort(np.asarray(peaks, dtype=np.int64))
    metric = numerator[starts] / np.square(np.maximum(energy[starts], 1e-300))

    # Coarse CFO from the lag-16 STF correlation, skipping the first period,
    # with the local DC removed from the samples and the local steady lag-16
    # product (a tone's) removed from the correlation: either would otherwise
    # pull the estimate towards its own phase.
    interference = _LocalInterference(values, starts)
    dc = interference.dc
    span = np.arange(_STF_SKIP, _STF_SKIP + _STF_CHECK_LENGTH)
    first = values[starts[:, None] + span[None, :]].astype(np.complex128) - dc[:, None]
    lagged = (
        values[starts[:, None] + span[None, :] + _COARSE_CFO_LAG].astype(np.complex128)
        - dc[:, None]
    )
    lag16 = np.sum(np.conj(first) * lagged, axis=1)
    lag16 -= _STF_CHECK_LENGTH * interference.steady(_COARSE_CFO_LAG)
    coarse = 4.0 * np.angle(lag16) / (2.0 * np.pi)

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
    interference.keep(keep)
    dc = interference.dc

    # Fine timing on the LTF pair, coarse CFO removed.
    search = config.fine_timing_search
    block_offsets = np.arange(
        V5_LTF_FIRST_SYMBOL_OFFSET - search, V5_LTF_FIRST_SYMBOL_OFFSET + search + 128
    )
    block = values[starts[:, None] + block_offsets[None, :]].astype(np.complex128)
    block -= dc[:, None]
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

    # Fine CFO from the L1 -> L2 rotation of the selected window, less the
    # local steady lag-64 product (rotated like the window by the coarse
    # correction), which a tone would otherwise add at its own phase.
    chosen = windows[rows, best]
    rotation64 = np.sum(chosen[:, 64:] * np.conj(chosen[:, :64]), axis=1)
    rotation64 -= 64 * interference.steady(64) * np.exp(-2j * np.pi * coarse)
    fine = np.angle(rotation64) / (2.0 * np.pi)
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
    """Keep the strongest v5 candidate per 560-sample fixed part, by start.

    No two v5 bursts start closer than a preamble and SIG, and a candidate
    inside another's fixed part would bound that burst before its SIG.
    """

    selected: list[V5Acquisition] = []
    for candidate in sorted(candidates, key=lambda item: item.confidence, reverse=True):
        if all(
            abs(candidate.stf_start - other.stf_start) >= V5_FIXED_LENGTH
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
    backoff: int = 0,
) -> LtfMeasurement | None:
    """Channel estimate, noise and signal power from the two LTF symbols.

    Whatever differs between L1 and L2 after CFO removal is noise, so
    ``sigma^2 = mean|Y1 - Y2|^2 / 2`` on the 52 active carriers, in band (a
    receiver's guard-band noise does not enter), and the LS estimate
    ``(Y1 + Y2) / (2 l_k)`` carries half the noise of one symbol.
    ``average=False`` (experiment) estimates from L1 alone; the noise still
    comes from the pair.  ``backoff`` starts both FFT windows that many
    samples early, inside the guard and L1 respectively, as the decoder does
    for every v5 window.  ``None`` when the samples do not hold both symbols
    or the estimate is unusable.
    """

    _require_numerology(numerology)
    values = _as_complex_samples(samples, "samples")
    start = acquisition.ltf_start - backoff
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
