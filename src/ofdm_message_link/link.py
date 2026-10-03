"""Message-to-waveform and waveform-to-message halves of the one-way demo.

Both halves use the PHY in :mod:`ofdm_link.phy`: its frame codec, FEC profile,
OFDM numerology, preamble, pilots, channel estimation and equalization.  There
is deliberately no MAC layer on top -- a one-way link has no return path, so
TDD slots, acknowledgements and ARQ would have nothing to run on.

Loss is therefore real and visible.  :class:`MessageReceiver` reports it from
gaps in the PHY frame sequence number, which cannot distinguish a burst whose
preamble was never detected from one that failed its CRC; both are counted as
missing.  There is no mechanism here that could recover either.
"""

from __future__ import annotations

import itertools
import threading
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from ofdm_link.config import LinkConfig, load_config
from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    EncodedBurst,
    Frame,
    FrameKind,
    encode_burst,
    normalized_evm,
)
from ofdm_link.phy.codec import map_symbols
from ofdm_link.phy.mcs_table import McsTableEntry, mcs_entry_for_wire
from ofdm_link.phy.streaming import StreamingBurstDecoder
from ofdm_link.runtime.factory import (
    build_burst_config,
    select_frame_decoder,
    select_frame_decoder_for_entry,
)

from . import datagram
from .datagram import Datagram, ReassembledMessage, Reassembler

DEFAULT_FRAME_PAYLOAD_BYTES = 996
# The streaming decoder rescans its whole buffer for each burst, so its cost
# grows with the chunk it is handed: 4 workers decoded 1139 burst/s from
# 8192-sample chunks but 751 from 65536.  Large transport chunks are split.
_DECODER_CHUNK_SAMPLES = 8192
_SEQUENCE_MODULUS = 1 << 16


def load_link_config(config_path: str | Path, overlays: Sequence[str | Path] = ()) -> LinkConfig:
    """Load and validate the base configuration with its overlays applied."""

    return load_config(config_path, overlays)


@dataclass(frozen=True, slots=True)
class PhyProfile:
    """Everything both halves need to agree on, resolved once from config."""

    config: LinkConfig
    mcs: MCS
    frame_payload_bytes: int
    guard_samples: int

    @property
    def sample_rate(self) -> int:
        return int(self.config.phy.sample_rate)

    @property
    def center_frequency(self) -> float:
        return float(self.config.phy.center_frequency)

    @property
    def max_message_payload_bytes(self) -> int:
        """Application bytes that fit one frame after the datagram header."""

        return self.frame_payload_bytes - datagram.HEADER_SIZE

    def describe(self) -> dict[str, object]:
        selection = select_frame_decoder(self.config)
        return {
            "sample_rate_sps": self.sample_rate,
            "center_frequency_hz": self.center_frequency,
            "mcs": self.mcs.name,
            "wire_version": selection.wire_version,
            "fec_backend": selection.decoder_backend,
            "frame_payload_bytes": self.frame_payload_bytes,
            "max_message_payload_bytes": self.max_message_payload_bytes,
        }


def build_phy_profile(
    config: LinkConfig,
    *,
    mcs: MCS | None = None,
    mcs_entry: McsTableEntry | None = None,
    frame_payload_bytes: int = DEFAULT_FRAME_PAYLOAD_BYTES,
    guard_samples: int = 256,
) -> PhyProfile:
    """Resolve the PHY working point, rejecting sizes the wire cannot carry."""

    if not isinstance(config, LinkConfig):
        raise TypeError("config must be a LinkConfig")
    if type(frame_payload_bytes) is not int or frame_payload_bytes <= datagram.HEADER_SIZE:
        raise ValueError(
            f"frame_payload_bytes must exceed the {datagram.HEADER_SIZE}-byte datagram header"
        )
    if type(guard_samples) is not int or guard_samples < 0:
        raise ValueError("guard_samples must be a non-negative integer")
    if mcs_entry is not None and not isinstance(mcs_entry, McsTableEntry):
        raise TypeError("mcs_entry must be an McsTableEntry or None")
    if mcs is not None and mcs_entry is not None and mcs is not mcs_entry.modulation:
        raise ValueError("mcs and mcs_entry modulation disagree")
    resolved = (
        mcs_entry.modulation
        if mcs_entry is not None
        else (mcs if mcs is not None else MCS[str(config.phy.mcs).upper()])
    )
    profile = PhyProfile(
        config=config,
        mcs=resolved,
        frame_payload_bytes=frame_payload_bytes,
        guard_samples=guard_samples,
    )
    # Fail at construction rather than on the first typed message.
    MessageTransmitter(profile, mcs_entry=mcs_entry).encode_message(
        b"x" * profile.max_message_payload_bytes
    )
    return profile


@dataclass(frozen=True, slots=True)
class OutgoingBurst:
    """One waveform transmission carrying one datagram."""

    samples: NDArray[np.complex64]
    sequence: int
    message_id: int
    fragment_index: int
    fragment_count: int
    payload_bytes: int
    mcs_index: int
    modulation: MCS
    wire_version: int

    @property
    def sample_count(self) -> int:
        return int(self.samples.size)


class MessageTransmitter:
    """Turn application bytes into transmittable OFDM bursts."""

    def __init__(
        self,
        profile: PhyProfile,
        *,
        mcs_entry: McsTableEntry | None = None,
    ) -> None:
        if not isinstance(profile, PhyProfile):
            raise TypeError("profile must be a PhyProfile")
        self._profile = profile
        self._burst_config = build_burst_config(profile.config)
        selection = select_frame_decoder(profile.config)
        entry = (
            mcs_entry_for_wire(profile.mcs, selection.wire_version)
            if mcs_entry is None
            else mcs_entry
        )
        if not isinstance(entry, McsTableEntry):
            raise TypeError("mcs_entry must be an McsTableEntry or None")
        if entry.modulation is not profile.mcs:
            raise ValueError("initial mcs_entry modulation must match the PHY profile")
        self._selection = (
            selection
            if entry.wire_version == selection.wire_version
            else select_frame_decoder_for_entry(profile.config, entry)
        )
        self._mcs_entry = entry
        self._codec_lock = threading.Lock()
        self._sequence = itertools.count()
        self._message_ids = itertools.count(1)
        self._origin = datagram.local_origin()
        self._guard = np.zeros(profile.guard_samples, dtype=np.complex64)

    @property
    def profile(self) -> PhyProfile:
        return self._profile

    @property
    def origin(self) -> int:
        return self._origin

    @property
    def active_mcs_entry(self) -> McsTableEntry:
        with self._codec_lock:
            return self._mcs_entry

    def select_mcs_entry(self, entry: McsTableEntry) -> None:
        """Apply one complete format atomically to the next encoded message."""

        if not isinstance(entry, McsTableEntry):
            raise TypeError("entry must be an McsTableEntry")
        selection = select_frame_decoder_for_entry(self._profile.config, entry)
        with self._codec_lock:
            self._mcs_entry = entry
            self._selection = selection

    def encode_message(self, message: bytes) -> tuple[OutgoingBurst, ...]:
        """Fragment one message and encode each fragment as one burst."""

        if type(message) is not bytes:
            raise TypeError("message must be immutable bytes")
        with self._codec_lock:
            entry = self._mcs_entry
            selection = self._selection
        message_id = next(self._message_ids)
        bursts: list[OutgoingBurst] = []
        for piece in datagram.fragment(
            message,
            message_id=message_id,
            max_payload_bytes=self._profile.max_message_payload_bytes,
            origin=self._origin,
        ):
            bursts.append(self._encode_datagram(piece, entry, selection))
        return tuple(bursts)

    def _encode_datagram(self, fragment: Datagram, entry, selection) -> OutgoingBurst:
        sequence = next(self._sequence) % _SEQUENCE_MODULUS
        frame = Frame(
            protocol_version=CURRENT_PROTOCOL_VERSION,
            kind=FrameKind.DATA,
            mcs=entry.modulation,
            sequence=sequence,
            payload=fragment.encode(),
        )
        encoded: EncodedBurst = encode_burst(
            frame,
            self._burst_config,
            symbol_mapper=selection.symbol_mapper,
            wire_version=selection.wire_version,
            frame_encoder=selection.frame_encoder,
        )
        samples = np.concatenate((self._guard, encoded.samples, self._guard)).astype(
            np.complex64, copy=False
        )
        return OutgoingBurst(
            samples=samples,
            sequence=sequence,
            message_id=fragment.message_id,
            fragment_index=fragment.fragment_index,
            fragment_count=fragment.fragment_count,
            payload_bytes=len(fragment.payload),
            mcs_index=entry.index,
            modulation=entry.modulation,
            wire_version=selection.wire_version,
        )


@dataclass(frozen=True, slots=True)
class ReceiveStats:
    """Counters the receiver can honestly derive from a one-way stream."""

    samples_consumed: int = 0
    bursts_decoded: int = 0
    payload_bytes_decoded: int = 0
    messages_delivered: int = 0
    message_bytes_delivered: int = 0
    sequence_gaps: int = 0
    missing_bursts: int = 0
    foreign_bursts: int = 0
    incomplete_messages_dropped: int = 0

    @property
    def expected_bursts(self) -> int:
        return self.bursts_decoded + self.missing_bursts

    @property
    def burst_loss_ratio(self) -> float:
        """Fraction of expected bursts that never arrived intact.

        A burst counts as missing when the PHY sequence number skips it.  That
        covers both a preamble the receiver never detected and a burst whose
        CRC failed; a one-way link cannot tell those apart.
        """

        expected = self.expected_bursts
        return 0.0 if expected == 0 else self.missing_bursts / expected


class RateMeter:
    """Bit rate of a growing byte counter over the most recent ``window_s``.

    The windows show this next to the lifetime average, which idle time before
    the first message keeps dragging down long after traffic has started.
    """

    def __init__(self, window_s: float = 2.0) -> None:
        self._window_s = window_s
        self._points: deque[tuple[float, int]] = deque()

    def update(self, now_s: float, total_bytes: int) -> None:
        self._points.append((now_s, total_bytes))
        # Keep one point at or before the window start so the span stays full.
        while len(self._points) > 2 and self._points[1][0] <= now_s - self._window_s:
            self._points.popleft()

    @property
    def bits_per_second(self) -> float:
        if len(self._points) < 2:
            return 0.0
        (start_s, start_bytes), (end_s, end_bytes) = self._points[0], self._points[-1]
        span = end_s - start_s
        return 0.0 if span <= 0.0 else (end_bytes - start_bytes) * 8 / span


@dataclass(frozen=True, slots=True)
class BurstObservation:
    """One decoded burst, whether or not it completed a message.

    The constellation is a property of every burst that decoded, so it must
    not wait for a message: a fragmented transfer would leave the plot frozen
    between fragments, and a burst from another application would never show
    at all even though the receiver demodulated it.
    """

    sequence: int
    wire_version: int
    modulation: MCS
    payload_symbols: NDArray[np.complex64]
    evm: float | None
    effective_snr_db: float | None
    is_ours: bool


@dataclass(frozen=True, slots=True)
class ReceivedMessage:
    """One delivered application message and its measurement context."""

    message: ReassembledMessage
    sequence: int
    wire_version: int
    modulation: MCS
    payload_symbols: NDArray[np.complex64]
    evm: float | None
    effective_snr_db: float | None
    latency_s: float | None
    same_clock_domain: bool


@dataclass
class _SequenceTracker:
    """Track a wrapping 16-bit sequence counter across a lossy link."""

    last: int | None = None
    gaps: int = 0
    missing: int = 0
    _seen: set[int] = field(default_factory=set)

    def observe(self, sequence: int) -> None:
        if self.last is None:
            self.last = sequence
            return
        forward = (sequence - self.last) % _SEQUENCE_MODULUS
        # A wrap-distance in the upper half means the burst arrived out of
        # order rather than that nearly 65k bursts vanished.  Reordering is
        # not expected on this link, so do not invent loss from it.
        if forward == 0 or forward > _SEQUENCE_MODULUS // 2:
            return
        if forward > 1:
            self.gaps += 1
            self.missing += forward - 1
        self.last = sequence


class MessageReceiver:
    """Recover application messages from a free-running sample stream."""

    def __init__(
        self,
        profile: PhyProfile,
        *,
        max_pending_messages: int = 16,
        max_observations: int = 64,
        decode_workers: int = 1,
    ) -> None:
        if not isinstance(profile, PhyProfile):
            raise TypeError("profile must be a PhyProfile")
        if type(decode_workers) is not int or decode_workers < 1:
            raise ValueError("decode_workers must be a positive integer")
        self._profile = profile
        self._selection = select_frame_decoder(profile.config)
        self._decoder = StreamingBurstDecoder(
            build_burst_config(profile.config),
            frame_decoder=self._selection.frame_decoder,
            symbol_demapper=self._selection.symbol_demapper,
        )
        self._reassembler = Reassembler(max_pending=max_pending_messages)
        self._tracker = _SequenceTracker()
        self._origin = datagram.local_origin()
        self._stats = ReceiveStats()
        self._observations: deque[BurstObservation] = deque(maxlen=max_observations)
        # One worker keeps the original inline decoder untouched.  More move
        # each burst's payload decode, and its quality measurement, to worker
        # processes while acquisition and headers stay on this owner.
        self._pool = None
        if decode_workers > 1:
            from ofdm_link.runtime.segment_decode_pool import SegmentDecodePool

            self._pool = SegmentDecodePool(
                profile.config,
                workers=decode_workers,
                annotate=burst_quality,
            )

    @property
    def profile(self) -> PhyProfile:
        return self._profile

    @property
    def decode_workers(self) -> int:
        return 1 if self._pool is None else self._pool.workers

    @property
    def stats(self) -> ReceiveStats:
        return replace(
            self._stats,
            sequence_gaps=self._tracker.gaps,
            missing_bursts=self._tracker.missing,
            incomplete_messages_dropped=self._reassembler.incomplete_dropped,
        )

    def drain_observations(self) -> tuple[BurstObservation, ...]:
        """Take every burst decoded since the last call, oldest first.

        Bounded: if a caller stops draining, the oldest observations are
        dropped rather than the receiver growing without limit.
        """

        drained = tuple(self._observations)
        self._observations.clear()
        return drained

    def decoder_snapshot(self):
        """Return bounded acquisition/header/payload counters for observers."""

        return self._decoder.snapshot()

    def feed(self, samples: NDArray[np.complex64]) -> tuple[ReceivedMessage, ...]:
        """Consume one sample chunk and return every message it completed.

        With decode workers, a message is returned once its burst's payload
        has been decoded, which can be a later call than the one that carried
        its samples; the order is still the stream's.
        """

        self._stats = replace(
            self._stats,
            samples_consumed=self._stats.samples_consumed + int(np.asarray(samples).size),
        )
        values = np.asarray(samples)
        pieces = [
            values[start : start + _DECODER_CHUNK_SAMPLES]
            for start in range(0, values.size, _DECODER_CHUNK_SAMPLES)
        ] or [values]
        if self._pool is None:
            delivered: list[ReceivedMessage] = []
            for piece in pieces:
                for burst in self._decoder.feed(piece):
                    delivered.extend(self._accept(burst, None))
            return tuple(delivered)
        results: list = []
        for piece in pieces:
            for segment in self._decoder.feed_segments(piece):
                results.extend(self._pool.submit(segment))
        results.extend(self._pool.collect())
        return self._accept_results(results)

    def flush(self, timeout: float | None = None) -> tuple[ReceivedMessage, ...]:
        """Wait for payloads still being decoded and return their messages."""

        if self._pool is None:
            return ()
        return self._accept_results(self._pool.drain(timeout))

    def decode_pool_snapshot(self):
        """Pool counters, or None when decoding inline."""

        return None if self._pool is None else self._pool.snapshot()

    def decode_worker_cpu_seconds(self) -> float | None:
        """CPU seconds used so far by the decode workers; None when inline."""

        return None if self._pool is None else self._pool.worker_cpu_seconds()

    def close(self) -> None:
        """Stop any decode workers; safe to call more than once."""

        if self._pool is not None:
            self._pool.close()

    def _accept_results(self, results) -> tuple[ReceivedMessage, ...]:
        delivered: list[ReceivedMessage] = []
        for result in results:
            self._decoder.record_payload_outcome(result.failure)
            if result.burst is not None:
                delivered.extend(self._accept(result.burst, result.annotation))
        return tuple(delivered)

    def _accept(self, burst, quality) -> list[ReceivedMessage]:
        frame = burst.frame
        if quality is None:
            quality = measure_quality(burst.payload_symbols, frame.mcs)
        try:
            parsed = datagram.decode(frame.payload)
        except datagram.DatagramError:
            # A CRC-valid frame that is not one of ours: another
            # application on the same channel, not a link error.
            self._stats = replace(
                self._stats,
                foreign_bursts=self._stats.foreign_bursts + 1,
            )
            self._observations.append(
                BurstObservation(
                    sequence=frame.sequence,
                    wire_version=burst.diagnostics.wire_version,
                    modulation=frame.mcs,
                    payload_symbols=burst.payload_symbols,
                    evm=quality[0],
                    effective_snr_db=quality[1],
                    is_ours=False,
                )
            )
            return []

        self._observations.append(
            BurstObservation(
                sequence=frame.sequence,
                wire_version=burst.diagnostics.wire_version,
                modulation=frame.mcs,
                payload_symbols=burst.payload_symbols,
                evm=quality[0],
                effective_snr_db=quality[1],
                is_ours=True,
            )
        )
        self._tracker.observe(frame.sequence)
        self._stats = replace(
            self._stats,
            bursts_decoded=self._stats.bursts_decoded + 1,
            payload_bytes_decoded=self._stats.payload_bytes_decoded + len(parsed.payload),
        )

        message = self._reassembler.accept(parsed)
        if message is None:
            return []
        self._stats = replace(
            self._stats,
            messages_delivered=self._stats.messages_delivered + 1,
            message_bytes_delivered=(
                self._stats.message_bytes_delivered + len(message.payload)
            ),
        )
        return [
            ReceivedMessage(
                message=message,
                sequence=frame.sequence,
                wire_version=burst.diagnostics.wire_version,
                modulation=frame.mcs,
                payload_symbols=burst.payload_symbols,
                evm=quality[0],
                effective_snr_db=quality[1],
                latency_s=self._latency_for(message),
                same_clock_domain=message.origin == self._origin,
            )
        ]

    def _latency_for(self, message: ReassembledMessage) -> float | None:
        """One-way latency, or None when the clocks are not comparable.

        ``CLOCK_MONOTONIC`` is system-wide on Linux, so two processes on one
        host can be differenced.  Two hosts cannot, and this demo does not
        discipline either radio to a shared time reference, so there is no
        honest number to report across a real over-the-air hop between hosts.
        """

        if message.origin != self._origin:
            return None
        import time

        elapsed_ns = time.monotonic_ns() - message.tx_monotonic_ns
        return elapsed_ns / 1e9 if elapsed_ns >= 0 else None


def _constellation(mcs: MCS) -> NDArray[np.complex64]:
    """Return the unit-power reference constellation for one MCS."""

    bits_per_symbol = mcs.bits_per_symbol
    patterns = np.array(
        [
            [(index >> (bits_per_symbol - 1 - bit)) & 1 for bit in range(bits_per_symbol)]
            for index in range(1 << bits_per_symbol)
        ],
        dtype=np.uint8,
    ).ravel()
    return map_symbols(patterns, mcs)


def burst_quality(burst) -> tuple[float | None, float | None]:
    """:func:`measure_quality` for one decoded burst; runs in decode workers."""

    return measure_quality(burst.payload_symbols, burst.frame.mcs)


def measure_quality(
    payload_symbols: NDArray[np.complex64],
    mcs: MCS,
) -> tuple[float | None, float | None]:
    """Decision-directed EVM and effective SNR for one burst's payload.

    The equalizer preserves the channel's amplitude, so the symbols are first
    scaled to the reference constellation's unit power before each is assigned
    to its nearest ideal point.  ``normalized_evm`` then removes the residual
    complex gain it estimates.  Being decision-directed, this saturates once
    errors are frequent enough to pick the wrong reference point; it describes
    a burst that decoded, not the link's behaviour at its failure threshold.
    """

    symbols = np.asarray(payload_symbols, dtype=np.complex128).ravel()
    if symbols.size == 0:
        return (None, None)
    power = float(np.mean(np.abs(symbols) ** 2))
    if not np.isfinite(power) or power <= 0.0:
        return (None, None)
    scaled = symbols / np.sqrt(power)
    reference = np.asarray(_constellation(mcs), dtype=np.complex128)
    nearest = reference[np.argmin(np.abs(scaled[:, None] - reference[None, :]), axis=1)]
    return normalized_evm(nearest, scaled)


def concatenate_bursts(bursts: Iterable[OutgoingBurst]) -> NDArray[np.complex64]:
    """Join bursts into one contiguous buffer, for tests and offline runs."""

    blocks = [burst.samples for burst in bursts]
    if not blocks:
        return np.zeros(0, dtype=np.complex64)
    return np.concatenate(blocks).astype(np.complex64, copy=False)
