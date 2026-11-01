"""Bounded continuous-sample adapter around the finite burst decoder."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import ArrayLike, NDArray

from ._arrays import all_finite
from .burst import (
    BurstConfig,
    BurstDecodeError,
    BurstValidationError,
    FrameDecoder,
    SoftSymbolDemapper,
    SymbolDemapper,
    _decode_burst_with_acquisition,
)
from .codec import DEFAULT_DUAL_VERSION_FRAME_DECODER, demap_symbols
from .mcs_table import V5_WIRE_VERSION
from .ofdm import OfdmNumerology
from .sync import (
    PreambleMeasurement,
    acquire_all,
    measure_preamble,
    separated_candidates,
)
from .sync_v5 import (
    V5_FIXED_LENGTH,
    V5_PREAMBLE_LENGTH,
    V5_RETAIN_SAMPLES,
    AnyAcquisition,
    LtfMeasurement,
    V5Acquisition,
    measure_ltf_pair,
)

if TYPE_CHECKING:
    from .burst_v5 import AnyDecodedBurst

# Preamble SNR readings kept between two drains; a caller that stops draining
# loses the oldest rather than growing the decoder without bound.
LINK_SNR_HISTORY = 4096
# Preamble SNRs kept by stream offset for :meth:`StreamingBurstDecoder.snr_at`.
# A burst is looked up once its payload has decoded, which with decode
# workers can be several bursts after its acquisition; this is ample.
SNR_AT_HISTORY = 512


def link_snr_db(
    measurement: PreambleMeasurement | LtfMeasurement | None,
    numerology: OfdmNumerology,
) -> float | None:
    """SNR of one burst in dB, from its preamble, or ``None`` if not measured.

    The measurement is per active carrier.  An OFDM symbol puts unit power on
    each active carrier, so it averages ``active / fft_size`` per sample;
    scaling by that ratio states the SNR of the payload samples -- the mean
    burst power over the noise variance, which is also what the udp
    transport's ``--snr-db`` sets when the noise is white.  It needs no
    successful decode, so a burst whose header or payload failed still
    reports what the channel was.  A v5 burst's LTF-pair measurement reads
    the same way, on twice the degrees of freedom.
    """

    if measurement is None:
        return None
    ratio = len(numerology.active_carriers) / numerology.fft_size
    snr = measurement.signal_power / measurement.noise_variance * ratio
    return float(10.0 * np.log10(snr))


@dataclass(frozen=True, slots=True)
class BurstSegment:
    """One burst bounded by its CRC-checked header, with the payload not decoded.

    ``samples`` start at the acquired preamble and run to the signalled end
    plus the sample-clock timing margin.  ``stream_offset`` is the absolute
    stream index of ``samples[0]``.  Decoding a segment needs no state from the
    stream that produced it, which is what lets payload decoding leave the
    single owner that keeps acquisition and burst-boundary state.
    """

    samples: NDArray[np.complex64]
    acquisition: AnyAcquisition
    stream_offset: int


@dataclass(frozen=True, slots=True)
class PayloadFailure:
    """What the counters need from a failed segment decode; cheap to pickle."""

    diagnosed: bool
    crc_failed: bool
    v5: bool = False

    @classmethod
    def from_error(cls, error: BurstDecodeError) -> PayloadFailure:
        diagnostics = error.diagnostics
        return cls(
            diagnosed=diagnostics is not None,
            crc_failed=diagnostics is not None and diagnostics.outer_crc_ok is False,
            v5=diagnostics is not None and diagnostics.wire_version == V5_WIRE_VERSION,
        )

    @classmethod
    def from_burst(cls, burst: AnyDecodedBurst) -> PayloadFailure | None:
        """The failure a returned burst stands for: a v5 PSDU whose FCS failed.

        A v5 payload whose FCS fails is still returned (its CRC-valid
        subframes are delivered), but it counts as a payload CRC failure.
        """

        if getattr(burst, "fcs_ok", True):
            return None
        return cls(diagnosed=True, crc_failed=True, v5=True)


@dataclass(frozen=True, slots=True)
class StreamingDecoderSnapshot:
    """Bounded stage counters from one continuous decoder owner.

    These counters deliberately retain no samples, frames, or observations.
    A header is successful once its protected contents have supplied a valid
    payload length. A payload attempt is counted only when enough samples are
    present to try the payload path, not while waiting for a later chunk.
    """

    detected_burst_candidates: int = 0
    header_decode_success: int = 0
    header_decode_failure: int = 0
    payload_decode_attempts: int = 0
    payload_decode_failure: int = 0
    crc_failures: int = 0
    valid_decoded_bursts: int = 0
    unclassified_decode_failures: int = 0
    # Per-family counts.  ``valid_decoded_bursts`` keeps counting every
    # payload that checked, of either family; a v5 PSDU whose FCS failed is
    # a ``crc_failures`` and a ``psdu_fcs_failures`` even though its burst is
    # still returned for its CRC-valid subframes.
    detected_legacy_candidates: int = 0
    detected_v5_candidates: int = 0
    bursts_legacy: int = 0
    bursts_v5: int = 0
    psdu_fcs_failures: int = 0


class StreamingBurstDecoder:
    """Decode bursts from arbitrary chunks while keeping bounded RX state.

    ``feed`` is single-owner and synchronous: it creates no worker threads and
    accepts only received samples.  Returned burst ranges use absolute offsets
    in the continuous stream, even after old samples have been discarded.
    """

    def __init__(
        self,
        config: BurstConfig | None = None,
        *,
        frame_decoder: FrameDecoder = DEFAULT_DUAL_VERSION_FRAME_DECODER,
        symbol_demapper: SymbolDemapper = demap_symbols,
        soft_symbol_demapper: SoftSymbolDemapper | None = None,
        input_noise_variance: float | None = None,
    ) -> None:
        if config is None:
            config = BurstConfig()
        if not isinstance(config, BurstConfig):
            raise BurstValidationError("config must be a BurstConfig")
        if not callable(frame_decoder):
            raise BurstValidationError("frame_decoder must be callable")
        if not callable(symbol_demapper):
            raise BurstValidationError("symbol_demapper must be callable")
        if soft_symbol_demapper is not None and not callable(soft_symbol_demapper):
            raise BurstValidationError("soft_symbol_demapper must be callable or None")
        if soft_symbol_demapper is not None and (
            isinstance(input_noise_variance, bool)
            or not isinstance(input_noise_variance, (int, float))
            or not np.isfinite(input_noise_variance)
            or input_noise_variance <= 0.0
        ):
            raise BurstValidationError(
                "soft decoding requires a positive finite input_noise_variance"
            )

        self._config = config
        self._frame_decoder = frame_decoder
        self._symbol_demapper = symbol_demapper
        self._soft_symbol_demapper = soft_symbol_demapper
        self._input_noise_variance = input_noise_variance
        self._buffer = np.empty(0, dtype=np.complex64)
        self._buffer_start = 0
        # Whether samples arrived since the last drain finished.  A drain
        # always runs until it can make no more progress on the buffer it
        # has, so draining again before new samples arrive only rescans the
        # retained tail it just judged -- a whole acquisition pass per chunk.
        self._unscanned = False
        self._pending_acquisition: AnyAcquisition | None = None
        self._required_sample_count: int | None = None
        self._next_candidate_start: int | None = None
        self._header_validated = False
        self._detected_burst_candidates = 0
        self._header_decode_success = 0
        self._header_decode_failure = 0
        self._payload_decode_attempts = 0
        self._payload_decode_failure = 0
        self._crc_failures = 0
        self._valid_decoded_bursts = 0
        self._unclassified_decode_failures = 0
        self._detected_legacy_candidates = 0
        self._detected_v5_candidates = 0
        self._bursts_legacy = 0
        self._bursts_v5 = 0
        self._psdu_fcs_failures = 0
        rx = config.v5
        self._legacy_sync = config.sync if rx.legacy_acquisition else None
        self._v5_sync = rx.acquisition if rx.v5_acquisition else None
        self._mode: str | None = None
        self._link_snrs: deque[float] = deque(maxlen=LINK_SNR_HISTORY)
        self._last_snr_start: int | None = None
        self._snr_by_start: deque[tuple[int, float]] = deque(maxlen=SNR_AT_HISTORY)

    @property
    def config(self) -> BurstConfig:
        """Return the immutable limits and receive settings."""

        return self._config

    @property
    def buffered_sample_count(self) -> int:
        """Number of samples currently retained for a future decode."""

        return int(self._buffer.size)

    def snapshot(self) -> StreamingDecoderSnapshot:
        """Return constant-size stage counters without changing decoder state."""

        return StreamingDecoderSnapshot(
            detected_burst_candidates=self._detected_burst_candidates,
            header_decode_success=self._header_decode_success,
            header_decode_failure=self._header_decode_failure,
            payload_decode_attempts=self._payload_decode_attempts,
            payload_decode_failure=self._payload_decode_failure,
            crc_failures=self._crc_failures,
            valid_decoded_bursts=self._valid_decoded_bursts,
            unclassified_decode_failures=self._unclassified_decode_failures,
            detected_legacy_candidates=self._detected_legacy_candidates,
            detected_v5_candidates=self._detected_v5_candidates,
            bursts_legacy=self._bursts_legacy,
            bursts_v5=self._bursts_v5,
            psdu_fcs_failures=self._psdu_fcs_failures,
        )

    def drain_link_snr_db(self) -> tuple[float, ...]:
        """Take the preamble SNR, in dB, of every burst acquired since the last call.

        One reading per physical preamble, whether or not the burst then
        decoded: see :func:`link_snr_db`.
        """

        drained = tuple(self._link_snrs)
        self._link_snrs.clear()
        return drained

    def snr_at(
        self, burst_start: int, *, tolerance: int = V5_PREAMBLE_LENGTH
    ) -> float | None:
        """Preamble SNR, in dB, of the burst that starts at ``burst_start``.

        ``burst_start`` is an absolute stream offset, as a decoded burst
        reports it on either path (:meth:`feed` or a decoded
        :class:`BurstSegment`).  The reading recorded nearest to it within
        ``tolerance`` samples is returned, or None if there is none: the
        burst is older than the bounded history, or its preamble could not
        be measured.  Unlike :meth:`drain_link_snr_db`, this does not consume
        anything.
        """

        best: tuple[int, float] | None = None
        for start, snr in reversed(self._snr_by_start):
            if start < burst_start - tolerance:
                break
            distance = abs(start - burst_start)
            if distance <= tolerance and (best is None or distance < best[0]):
                best = (distance, snr)
        return None if best is None else best[1]

    def _note_acquisition(self, acquisition: AnyAcquisition) -> None:
        """Count one acquisition and record its SNR, once per physical preamble.

        After a failed header the stream steps forward a sample and can find
        the same preamble again further along its cyclic prefix; those repeats
        lie within one preamble length and are not counted twice.  A v5
        preamble is measured on its LTF pair, a legacy one on its halves.
        """

        self._detected_burst_candidates += 1
        is_v5 = isinstance(acquisition, V5Acquisition)
        if is_v5:
            self._detected_v5_candidates += 1
        else:
            self._detected_legacy_candidates += 1
        start = self._buffer_start + acquisition.preamble_start
        length = V5_PREAMBLE_LENGTH if is_v5 else self._config.sync.preamble_length
        if self._last_snr_start is not None and start - self._last_snr_start < length:
            return
        self._last_snr_start = start
        numerology = self._config.numerology
        measurement: PreambleMeasurement | LtfMeasurement | None
        if is_v5:
            measurement = measure_ltf_pair(self._buffer, acquisition, numerology)
        else:
            measurement = measure_preamble(
                self._buffer, acquisition, numerology.active_carriers, self._config.sync
            )
        snr = link_snr_db(measurement, numerology)
        if snr is not None:
            self._link_snrs.append(snr)
            self._snr_by_start.append((start, snr))

    def feed(self, complex_chunk: ArrayLike) -> tuple[AnyDecodedBurst, ...]:
        """Consume one contiguous sample chunk and emit every complete burst.

        Legacy bursts come back as :class:`DecodedBurst`, v5 bursts as
        :class:`~ofdm_link.phy.burst_v5.DecodedPsduBurst` -- including one
        whose FCS failed, for its CRC-valid subframes.
        """

        self._enter_mode("decode")
        values = _stream_samples(complex_chunk)
        decoded: list[AnyDecodedBurst] = []
        cursor = 0
        while cursor < values.size:
            if self._unscanned:
                decoded.extend(self._drain())
            if self._buffer.size == self._config.max_input_samples:
                self._retain_possible_preamble_prefix()

            room = self._config.max_input_samples - self._buffer.size
            take = min(room, values.size - cursor)
            self._append(values[cursor : cursor + take])
            cursor += take

        decoded.extend(self._drain())
        return tuple(decoded)

    def _append(self, values: NDArray[np.complex64]) -> None:
        self._buffer = np.concatenate((self._buffer, values)).astype(np.complex64, copy=False)
        self._unscanned = True

    def _drain(self) -> list[AnyDecodedBurst]:
        self._unscanned = False
        decoded: list[AnyDecodedBurst] = []
        while True:
            if self._pending_acquisition is None and not self._acquire_next():
                return decoded

            fixed_sample_count = self._fixed_sample_count
            if self._buffer.size < fixed_sample_count:
                return decoded
            timing_margin = (
                int(np.ceil(self._config.sample_clock_tracking.max_abs_timing_shift_samples)) + 1
            )
            if self._required_sample_count is not None:
                if self._buffer.size < self._required_sample_count:
                    return decoded
                segment_end = min(
                    self._buffer.size,
                    self._required_sample_count + timing_margin,
                )
            elif self._next_candidate_start is not None:
                segment_end = min(
                    self._buffer.size,
                    self._next_candidate_start + timing_margin,
                )
            else:
                # The first complete fixed header reveals the burst length.
                # Probe it once, then wait without repeatedly scanning the
                # growing payload buffer or accepting TX length metadata.
                segment_end = self._buffer.size
            if isinstance(self._pending_acquisition, V5Acquisition):
                # A later candidate never cuts a v5 burst short of its SIG.
                segment_end = max(segment_end, fixed_sample_count)
            try:
                burst = _decode_burst_with_acquisition(
                    self._buffer[:segment_end],
                    self._config,
                    self._pending_acquisition,
                    frame_decoder=self._frame_decoder,
                    symbol_demapper=self._symbol_demapper,
                    soft_symbol_demapper=self._soft_symbol_demapper,
                    input_noise_variance=self._input_noise_variance,
                )
            except BurstDecodeError as error:
                if error.required_sample_count is not None and (
                    error.required_sample_count > segment_end
                ):
                    # The signalled length comes from a CRC-checked header, so
                    # it outranks a bare correlation peak.  A candidate that
                    # starts inside this burst is the next preamble reported
                    # early, not proof that this acquisition was false, so the
                    # length is decoded before any candidate is believed.  The
                    # loop waits above when the samples have not arrived yet,
                    # and only a strictly longer segment is retried, so this
                    # owner-thread loop always makes progress.
                    self._record_header_success()
                    self._required_sample_count = error.required_sample_count
                    continue
                if error.diagnostics is not None:
                    self._record_header_success()
                    self.record_payload_outcome(PayloadFailure.from_error(error))
                elif self._header_validated:
                    self._payload_decode_attempts += 1
                    self._payload_decode_failure += 1
                    self._unclassified_decode_failures += 1
                elif str(error).startswith("fixed signaling header decode failed:") or str(
                    error
                ).startswith("signaled "):
                    self._header_decode_failure += 1
                else:
                    self._unclassified_decode_failures += 1
                # A fixed header or complete payload that fails validation is
                # not a boundary oracle.  Advance one sample and reacquire so
                # the next real preamble can resynchronize the stream, and
                # never discard past a preamble the scan already found.
                bounds = [
                    bound
                    for bound in (self._required_sample_count, self._next_candidate_start)
                    if bound
                ]
                discard = min(bounds) if bounds else 1
                self._discard(min(discard, self._buffer.size))
                self._pending_acquisition = None
                self._required_sample_count = None
                self._next_candidate_start = None
                self._header_validated = False
                continue

            self._record_header_success()
            self.record_payload_outcome(PayloadFailure.from_burst(burst), burst)
            absolute = replace(
                burst,
                burst_start=self._buffer_start + burst.burst_start,
                burst_end=self._buffer_start + burst.burst_end,
            )
            decoded.append(absolute)
            self._discard(burst.burst_end)
            self._pending_acquisition = None
            self._required_sample_count = None
            self._next_candidate_start = None
            self._header_validated = False

    def feed_segments(self, complex_chunk: ArrayLike) -> tuple[BurstSegment, ...]:
        """Consume one chunk and emit each header-bounded burst undecoded.

        Acquisition, the fixed-header probe and the burst boundary stay here,
        on the single owner; the payload of each returned segment is decoded
        elsewhere with :func:`decode_burst_segment`, and its outcome reported
        back through :meth:`record_payload_outcome` in stream order.

        The one behavioural difference from :meth:`feed` is on a payload
        failure: the stream has already moved past the signalled burst end, so
        a failed payload is not rescanned for a preamble inside its own span.
        """

        self._enter_mode("segments")
        values = _stream_samples(complex_chunk)
        segments: list[BurstSegment] = []
        cursor = 0
        while cursor < values.size:
            if self._unscanned:
                segments.extend(self._drain_segments())
            if self._buffer.size == self._config.max_input_samples:
                self._retain_possible_preamble_prefix()
            room = self._config.max_input_samples - self._buffer.size
            take = min(room, values.size - cursor)
            self._append(values[cursor : cursor + take])
            cursor += take
        segments.extend(self._drain_segments())
        return tuple(segments)

    def record_payload_outcome(
        self,
        failure: PayloadFailure | None,
        burst: AnyDecodedBurst | None = None,
    ) -> None:
        """Count one segment's payload result exactly as :meth:`feed` would.

        Pass the decoded ``burst`` when there is one: it names the family for
        the per-family counters.  For a v5 burst whose FCS failed, pass
        ``PayloadFailure.from_burst(burst)`` as ``failure``.
        """

        self._payload_decode_attempts += 1
        is_v5 = (
            failure.v5
            if failure is not None
            else burst is not None and burst.diagnostics.wire_version == V5_WIRE_VERSION
        )
        if failure is None:
            self._valid_decoded_bursts += 1
            if burst is not None:
                if is_v5:
                    self._bursts_v5 += 1
                else:
                    self._bursts_legacy += 1
            return
        self._payload_decode_failure += 1
        if not failure.diagnosed:
            self._unclassified_decode_failures += 1
        elif failure.crc_failed:
            self._crc_failures += 1
            if failure.v5:
                self._psdu_fcs_failures += 1

    def _enter_mode(self, mode: str) -> None:
        if self._mode is None:
            self._mode = mode
        elif self._mode != mode:
            raise BurstValidationError("feed and feed_segments cannot share one decoder")

    def _drain_segments(self) -> list[BurstSegment]:
        self._unscanned = False
        segments: list[BurstSegment] = []
        while True:
            if self._pending_acquisition is None and not self._acquire_next():
                return segments
            fixed_sample_count = self._fixed_sample_count
            if self._buffer.size < fixed_sample_count:
                return segments
            timing_margin = (
                int(np.ceil(self._config.sample_clock_tracking.max_abs_timing_shift_samples)) + 1
            )
            if self._required_sample_count is None:
                probe_end = fixed_sample_count
                if self._next_candidate_start is not None and not isinstance(
                    self._pending_acquisition, V5Acquisition
                ):
                    probe_end = min(probe_end, self._next_candidate_start + timing_margin)
                try:
                    _decode_burst_with_acquisition(
                        self._buffer[:probe_end],
                        self._config,
                        self._pending_acquisition,
                        frame_decoder=self._frame_decoder,
                        symbol_demapper=self._symbol_demapper,
                        soft_symbol_demapper=self._soft_symbol_demapper,
                        input_noise_variance=self._input_noise_variance,
                    )
                except BurstDecodeError as error:
                    if error.required_sample_count is not None and (
                        error.required_sample_count > probe_end
                    ):
                        self._record_header_success()
                        self._required_sample_count = error.required_sample_count
                        continue
                    self._reject_acquisition(error)
                    continue
                # A payload short enough to fit the fixed probe cannot carry a
                # frame; treat it like any other unusable acquisition.
                self._reject_acquisition(BurstDecodeError("signaled payload is empty"))
                continue
            wanted = self._required_sample_count + timing_margin
            if self._buffer.size < wanted:
                return segments
            segments.append(
                BurstSegment(
                    samples=self._buffer[:wanted].copy(),
                    acquisition=self._pending_acquisition,
                    stream_offset=self._buffer_start,
                )
            )
            self._discard(self._required_sample_count)
            self._pending_acquisition = None
            self._required_sample_count = None
            self._next_candidate_start = None
            self._header_validated = False

    def _acquire_next(self) -> bool:
        candidates = acquire_all(self._buffer, self._legacy_sync, self._v5_sync)
        if not candidates:
            self._retain_possible_preamble_prefix()
            return False
        first = candidates[0]
        discarded = first.preamble_start
        if discarded:
            self._discard(discarded)
            # The scan ran before the discard, so rebase the result onto the
            # buffer the decoder will actually be handed.
            first = _rebased(first, discarded)
        self._next_candidate_start = (
            None if len(candidates) < 2 else candidates[1].preamble_start - discarded
        )
        self._pending_acquisition = first
        self._header_validated = False
        self._note_acquisition(first)
        return True

    def _reject_acquisition(self, error: BurstDecodeError) -> None:
        if str(error).startswith("fixed signaling header decode failed:") or str(
            error
        ).startswith("signaled "):
            self._header_decode_failure += 1
        else:
            self._unclassified_decode_failures += 1
        bound = self._next_candidate_start
        self._discard(min(bound if bound else 1, self._buffer.size))
        self._pending_acquisition = None
        self._required_sample_count = None
        self._next_candidate_start = None
        self._header_validated = False

    def _record_header_success(self) -> None:
        if not self._header_validated:
            self._header_validated = True
            self._header_decode_success += 1

    @property
    def _fixed_sample_count(self) -> int:
        if isinstance(self._pending_acquisition, V5Acquisition):
            return V5_FIXED_LENGTH
        block_length = (
            self._config.numerology.fft_size + self._config.numerology.cp_length
        )
        # Training is one OFDM symbol and the signaling header is three.
        return self._config.sync.preamble_length + 4 * block_length

    def _retain_possible_preamble_prefix(self) -> None:
        # A v5 candidate is judged only once its whole M128 run and LTF
        # search fit in the buffer, so keep enough tail for that; legacy-only
        # keeps exactly what it always kept.
        keep = self._config.sync.preamble_length - 1
        if self._v5_sync is not None:
            # Only from the first non-zero sample of that tail: exact-zero
            # idle cannot hold an STF start, so an idle stream stays as small
            # as it always was.  Capped at half the input bound so a tiny
            # configured buffer still makes progress.
            tail = self._buffer[-V5_RETAIN_SAMPLES:]
            nonzero = np.flatnonzero(tail)
            if nonzero.size:
                v5_keep = min(
                    int(tail.size - nonzero[0]),
                    self._config.max_input_samples // 2,
                )
                keep = max(keep, v5_keep)
        if self._buffer.size > keep:
            self._discard(self._buffer.size - keep)

    def _discard(self, sample_count: int) -> None:
        self._buffer = self._buffer[sample_count:].copy()
        self._buffer_start += sample_count


def decode_burst_segment(
    segment: BurstSegment,
    config: BurstConfig,
    *,
    frame_decoder: FrameDecoder = DEFAULT_DUAL_VERSION_FRAME_DECODER,
    symbol_demapper: SymbolDemapper = demap_symbols,
    soft_symbol_demapper: SoftSymbolDemapper | None = None,
    input_noise_variance: float | None = None,
) -> AnyDecodedBurst:
    """Decode one segment; burst offsets in the result are absolute."""

    if not isinstance(segment, BurstSegment):
        raise BurstValidationError("segment must be a BurstSegment")
    burst = _decode_burst_with_acquisition(
        segment.samples,
        config,
        segment.acquisition,
        frame_decoder=frame_decoder,
        symbol_demapper=symbol_demapper,
        soft_symbol_demapper=soft_symbol_demapper,
        input_noise_variance=input_noise_variance,
    )
    return replace(
        burst,
        burst_start=segment.stream_offset + burst.burst_start,
        burst_end=segment.stream_offset + burst.burst_end,
    )


def _stream_samples(complex_chunk: ArrayLike) -> NDArray[np.complex64]:
    values = np.asarray(complex_chunk)
    if values.ndim != 1:
        raise BurstDecodeError("complex_chunk must be a one-dimensional array")
    if not np.issubdtype(values.dtype, np.number):
        raise BurstDecodeError("complex_chunk must be numeric")
    converted = values.astype(np.complex64, copy=False)
    if not all_finite(converted):
        raise BurstDecodeError("complex_chunk must contain only finite values")
    return converted


def _rebased(acquisition: AnyAcquisition, discarded: int) -> AnyAcquisition:
    """The same acquisition indexed into a buffer ``discarded`` samples shorter."""

    if isinstance(acquisition, V5Acquisition):
        return replace(
            acquisition,
            stf_start=acquisition.stf_start - discarded,
            ltf_start=acquisition.ltf_start - discarded,
        )
    return replace(
        acquisition,
        preamble_start=acquisition.preamble_start - discarded,
        useful_start=acquisition.useful_start - discarded,
    )


# Kept for callers of the old private name.
_separated_candidates = separated_candidates
