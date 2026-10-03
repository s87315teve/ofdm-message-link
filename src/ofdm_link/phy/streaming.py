"""Bounded continuous-sample adapter around the finite burst decoder."""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
from numpy.typing import ArrayLike, NDArray

from ._arrays import all_finite
from .burst import (
    BurstConfig,
    BurstDecodeError,
    BurstValidationError,
    DecodedBurst,
    FrameDecoder,
    SoftSymbolDemapper,
    SymbolDemapper,
    _decode_burst_with_acquisition,
)
from .codec import DEFAULT_DUAL_VERSION_FRAME_DECODER, demap_symbols
from .sync import AcquisitionResult, _acquisition_candidates


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
    acquisition: AcquisitionResult
    stream_offset: int


@dataclass(frozen=True, slots=True)
class PayloadFailure:
    """What the counters need from a failed segment decode; cheap to pickle."""

    diagnosed: bool
    crc_failed: bool

    @classmethod
    def from_error(cls, error: BurstDecodeError) -> PayloadFailure:
        diagnostics = error.diagnostics
        return cls(
            diagnosed=diagnostics is not None,
            crc_failed=diagnostics is not None and diagnostics.outer_crc_ok is False,
        )


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
        self._pending_acquisition: AcquisitionResult | None = None
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
        self._mode: str | None = None

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
        )

    def feed(self, complex_chunk: ArrayLike) -> tuple[DecodedBurst, ...]:
        """Consume one contiguous sample chunk and emit every complete burst."""

        self._enter_mode("decode")
        values = _stream_samples(complex_chunk)
        decoded: list[DecodedBurst] = []
        cursor = 0
        while cursor < values.size:
            decoded.extend(self._drain())
            if self._buffer.size == self._config.max_input_samples:
                self._retain_possible_preamble_prefix()

            room = self._config.max_input_samples - self._buffer.size
            take = min(room, values.size - cursor)
            self._buffer = np.concatenate((self._buffer, values[cursor : cursor + take])).astype(
                np.complex64, copy=False
            )
            cursor += take

        decoded.extend(self._drain())
        return tuple(decoded)

    def _drain(self) -> list[DecodedBurst]:
        decoded: list[DecodedBurst] = []
        while True:
            if self._pending_acquisition is None:
                candidates = _separated_candidates(
                    _acquisition_candidates(self._buffer, self._config.sync),
                    self._config.sync.preamble_length,
                )
                if not candidates:
                    self._retain_possible_preamble_prefix()
                    return decoded
                first = candidates[0]
                discarded = first.preamble_start
                if discarded:
                    self._discard(discarded)
                    # The scan ran before the discard, so rebase the result
                    # onto the buffer the decoder will actually be handed.
                    first = replace(
                        first,
                        preamble_start=0,
                        useful_start=first.useful_start - discarded,
                    )
                self._next_candidate_start = (
                    None
                    if len(candidates) < 2
                    else candidates[1].preamble_start - discarded
                )
                self._pending_acquisition = first
                self._header_validated = False
                self._detected_burst_candidates += 1

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
                    self._payload_decode_attempts += 1
                    self._payload_decode_failure += 1
                    if error.diagnostics.outer_crc_ok is False:
                        self._crc_failures += 1
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
            self._payload_decode_attempts += 1
            self._valid_decoded_bursts += 1
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
            segments.extend(self._drain_segments())
            if self._buffer.size == self._config.max_input_samples:
                self._retain_possible_preamble_prefix()
            room = self._config.max_input_samples - self._buffer.size
            take = min(room, values.size - cursor)
            self._buffer = np.concatenate((self._buffer, values[cursor : cursor + take])).astype(
                np.complex64, copy=False
            )
            cursor += take
        segments.extend(self._drain_segments())
        return tuple(segments)

    def record_payload_outcome(self, failure: PayloadFailure | None) -> None:
        """Count one segment's payload result exactly as :meth:`feed` would."""

        self._payload_decode_attempts += 1
        if failure is None:
            self._valid_decoded_bursts += 1
            return
        self._payload_decode_failure += 1
        if not failure.diagnosed:
            self._unclassified_decode_failures += 1
        elif failure.crc_failed:
            self._crc_failures += 1

    def _enter_mode(self, mode: str) -> None:
        if self._mode is None:
            self._mode = mode
        elif self._mode != mode:
            raise BurstValidationError("feed and feed_segments cannot share one decoder")

    def _drain_segments(self) -> list[BurstSegment]:
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
                if self._next_candidate_start is not None:
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
        candidates = _separated_candidates(
            _acquisition_candidates(self._buffer, self._config.sync),
            self._config.sync.preamble_length,
        )
        if not candidates:
            self._retain_possible_preamble_prefix()
            return False
        first = candidates[0]
        discarded = first.preamble_start
        if discarded:
            self._discard(discarded)
            first = replace(
                first,
                preamble_start=0,
                useful_start=first.useful_start - discarded,
            )
        self._next_candidate_start = (
            None if len(candidates) < 2 else candidates[1].preamble_start - discarded
        )
        self._pending_acquisition = first
        self._header_validated = False
        self._detected_burst_candidates += 1
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
        block_length = (
            self._config.numerology.fft_size + self._config.numerology.cp_length
        )
        # Training is one OFDM symbol and the signaling header is three.
        return self._config.sync.preamble_length + 4 * block_length

    def _retain_possible_preamble_prefix(self) -> None:
        keep = self._config.sync.preamble_length - 1
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
) -> DecodedBurst:
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


def _separated_candidates(
    candidates: tuple[AcquisitionResult, ...],
    preamble_length: int,
) -> tuple[AcquisitionResult, ...]:
    """Keep the strongest candidate for each physical preamble."""

    selected: list[AcquisitionResult] = []
    for candidate in sorted(candidates, key=lambda item: item.confidence, reverse=True):
        if all(
            abs(candidate.preamble_start - other.preamble_start) >= preamble_length
            for other in selected
        ):
            selected.append(candidate)
    return tuple(sorted(selected, key=lambda item: item.preamble_start))
