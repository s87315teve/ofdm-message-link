"""Message-to-waveform and waveform-to-message halves of the one-way demo.

Both halves use the PHY in :mod:`ofdm_link.phy`: its frame codec, FEC profile,
OFDM numerology, preamble, pilots, channel estimation and equalization.  There
is deliberately no MAC layer on top -- a one-way link has no return path, so
TDD slots, acknowledgements and ARQ would have nothing to run on.

The transmitter sends only wire-v5 bursts.  Each burst carries one PSDU: an
A-MPDU of one or more serialized frames, one datagram per frame, so several
small datagrams can share one preamble and SIG.  The receiver decodes v5 and
legacy v1-v4 bursts side by side, because older transmitters stay in use.

Loss is therefore real and visible.  :class:`MessageReceiver` reports it from
gaps in the PHY frame sequence number, which cannot distinguish a burst whose
preamble was never detected from one that failed its CRC; both are counted as
missing.  There is no mechanism here that could recover either.  Inside a v5
burst every frame keeps its own CRC-32 and sequence number, so a damaged
subframe costs only itself and shows up as a one-frame gap.
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
    Frame,
    FrameKind,
    normalized_evm,
)
from ofdm_link.phy.ampdu import ampdu_body_length, pack_ampdu, unpack_ampdu
from ofdm_link.phy.burst import DecodedBurst
from ofdm_link.phy.burst_v5 import AnyDecodedBurst, encode_burst_v5
from ofdm_link.phy.codec import (
    FrameDecodeError,
    deserialize_frame,
    map_symbols,
    serialize_frame,
)
from ofdm_link.phy.mcs_table import (
    V5_WIRE_VERSION,
    McsTableEntry,
    McsTableError,
    mcs_index_for_wire,
    v5_entries,
)
from ofdm_link.phy.streaming import PayloadFailure, StreamingBurstDecoder
from ofdm_link.runtime.airtime import v5_burst_samples_with_guards
from ofdm_link.runtime.factory import build_burst_config, select_frame_decoder

from . import datagram
from .aggregation import AggregationLimits, plan_aggregate
from .datagram import Datagram, ReassembledMessage, Reassembler

DEFAULT_FRAME_PAYLOAD_BYTES = 996
# v5 guards: the 160-sample STF absorbs the radio's leading transient, so 16
# zeros before a burst suffice, and 64 after it flush the transmit filters.
# Legacy bursts used 256 + 256.
DEFAULT_GUARD_PRE_SAMPLES = 16
DEFAULT_GUARD_POST_SAMPLES = 64
# Serialized frame overhead around a datagram: 7-byte header + CRC-32.
FRAME_OVERHEAD_BYTES = 11
# The streaming decoder rescans its whole buffer for each burst, so its cost
# grows with the chunk it is handed: 4 workers decoded 1139 burst/s from
# 8192-sample chunks but 751 from 65536.  Large transport chunks are split.
_DECODER_CHUNK_SAMPLES = 8192
_SEQUENCE_MODULUS = 1 << 16
# Non-zero 802.11 scrambler seeds; 0 would switch the scrambler off.
_SCRAMBLER_SEEDS = 127


def load_link_config(config_path: str | Path, overlays: Sequence[str | Path] = ()) -> LinkConfig:
    """Load and validate the base configuration with its overlays applied."""

    return load_config(config_path, overlays)


def default_v5_entry(modulation: MCS) -> McsTableEntry:
    """The most robust transmit-visible v5 entry of one modulation.

    The legacy ``--mcs qpsk|qam16`` option and the configuration's
    ``phy.mcs`` name only a constellation, while the transmitter needs a full
    v5 format.  The lowest rate of that constellation is the conservative
    reading: QPSK -> MCS 8 (QPSK 1/3), 16QAM -> MCS 12 (16QAM 1/2).
    """

    candidates = [
        entry for entry in v5_entries(include_hidden=True) if entry.modulation is modulation
    ]
    if not candidates:
        raise McsTableError(f"no v5 entry uses {modulation.name}")
    visible = [entry for entry in candidates if entry.tx_visible]
    return (visible or candidates)[0]


def require_v5_entry(entry: McsTableEntry) -> McsTableEntry:
    """Reject legacy formats on the transmit side, saying why."""

    if not isinstance(entry, McsTableEntry):
        raise TypeError("mcs_entry must be an McsTableEntry")
    if entry.family != "v5":
        raise ValueError(
            f"MCS {entry.index} is a legacy wire-v{entry.wire_version} format the "
            "receiver still decodes, but this transmitter sends only wire v5 "
            "(MCS 8-17)"
        )
    return entry


@dataclass(frozen=True, slots=True)
class PhyProfile:
    """Everything both halves need to agree on, resolved once from config."""

    config: LinkConfig
    mcs: MCS
    frame_payload_bytes: int
    guard_pre_samples: int = DEFAULT_GUARD_PRE_SAMPLES
    guard_post_samples: int = DEFAULT_GUARD_POST_SAMPLES

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
            "wire_version": V5_WIRE_VERSION,
            "legacy_rx_wire_version": selection.wire_version,
            "fec_backend": selection.decoder_backend,
            "frame_payload_bytes": self.frame_payload_bytes,
            "max_message_payload_bytes": self.max_message_payload_bytes,
            "guard_samples": (self.guard_pre_samples, self.guard_post_samples),
        }


def build_phy_profile(
    config: LinkConfig,
    *,
    mcs: MCS | None = None,
    mcs_entry: McsTableEntry | None = None,
    frame_payload_bytes: int = DEFAULT_FRAME_PAYLOAD_BYTES,
    guard_pre_samples: int = DEFAULT_GUARD_PRE_SAMPLES,
    guard_post_samples: int = DEFAULT_GUARD_POST_SAMPLES,
) -> PhyProfile:
    """Resolve the PHY working point, rejecting sizes the wire cannot carry.

    ``mcs_entry`` may be a legacy entry, because the receiver takes it only as
    a display hint; the transmitter enforces v5 itself.
    """

    if not isinstance(config, LinkConfig):
        raise TypeError("config must be a LinkConfig")
    if type(frame_payload_bytes) is not int or frame_payload_bytes <= datagram.HEADER_SIZE:
        raise ValueError(
            f"frame_payload_bytes must exceed the {datagram.HEADER_SIZE}-byte datagram header"
        )
    for name, value in (
        ("guard_pre_samples", guard_pre_samples),
        ("guard_post_samples", guard_post_samples),
    ):
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
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
        guard_pre_samples=guard_pre_samples,
        guard_post_samples=guard_post_samples,
    )
    tx_entry = (
        mcs_entry
        if mcs_entry is not None and mcs_entry.family == "v5"
        else default_v5_entry(resolved)
    )
    # Fail at construction rather than on the first typed message.
    MessageTransmitter(profile, mcs_entry=tx_entry).encode_message(
        b"x" * profile.max_message_payload_bytes
    )
    return profile


def mpdu_length(piece: Datagram) -> int:
    """Bytes one datagram occupies as a serialized frame, i.e. as one MPDU."""

    return FRAME_OVERHEAD_BYTES + datagram.HEADER_SIZE + len(piece.payload)


def aggregate_body_length(datagrams: Sequence[Datagram]) -> int:
    """A-MPDU body bytes (PSDU FCS excluded) for these datagrams as subframes."""

    return ampdu_body_length([mpdu_length(piece) for piece in datagrams])


@dataclass(frozen=True, slots=True)
class OutgoingBurst:
    """One v5 waveform transmission carrying one or more datagrams.

    ``samples`` include the guards.  ``sequences[i]`` is the PHY frame
    sequence number of ``datagrams[i]``; they are consecutive because numbers
    are assigned when the burst is encoded, in transmission order.
    """

    samples: NDArray[np.complex64]
    sequences: tuple[int, ...]
    datagrams: tuple[Datagram, ...]
    mcs_index: int
    modulation: MCS
    wire_version: int
    psdu_bytes: int

    @property
    def sequence(self) -> int:
        """Sequence number of the first frame: the burst's name in logs."""

        return self.sequences[0]

    @property
    def subframe_count(self) -> int:
        return len(self.datagrams)

    @property
    def payload_bytes(self) -> int:
        """Application bytes carried, datagram headers excluded."""

        return sum(len(piece.payload) for piece in self.datagrams)

    @property
    def sample_count(self) -> int:
        return int(self.samples.size)


class MessageTransmitter:
    """Turn application bytes into transmittable wire-v5 OFDM bursts.

    Fragmenting and encoding are separate steps so that the transmit worker
    can decide, burst by burst, how many queued datagrams to aggregate; that
    depends on what else is queued and on the radio, which this class does
    not see.
    """

    def __init__(
        self,
        profile: PhyProfile,
        *,
        mcs_entry: McsTableEntry | None = None,
        scrambler: bool = True,
    ) -> None:
        if not isinstance(profile, PhyProfile):
            raise TypeError("profile must be a PhyProfile")
        entry = default_v5_entry(profile.mcs) if mcs_entry is None else mcs_entry
        self._profile = profile
        self._burst_config = build_burst_config(profile.config)
        self._mcs_entry = require_v5_entry(entry)
        self._scrambler = bool(scrambler)
        self._codec_lock = threading.Lock()
        self._sequence = itertools.count()
        self._seed_counter = itertools.count()
        self._message_ids = itertools.count(1)
        self._origin = datagram.local_origin()
        self._guard_pre = np.zeros(profile.guard_pre_samples, dtype=np.complex64)
        self._guard_post = np.zeros(profile.guard_post_samples, dtype=np.complex64)

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
        """Apply one complete v5 format atomically to the next encoded burst."""

        require_v5_entry(entry)
        with self._codec_lock:
            self._mcs_entry = entry

    def fragment_message(self, message: bytes) -> tuple[Datagram, ...]:
        """Split one message into datagrams under a fresh message id."""

        if type(message) is not bytes:
            raise TypeError("message must be immutable bytes")
        return tuple(
            datagram.fragment(
                message,
                message_id=next(self._message_ids),
                max_payload_bytes=self._profile.max_message_payload_bytes,
                origin=self._origin,
            )
        )

    def burst_sample_count(self, entry: McsTableEntry, body_bytes: int) -> int:
        """On-air samples of a burst with this A-MPDU body, guards included."""

        return v5_burst_samples_with_guards(
            entry,
            body_bytes,
            self._profile.guard_pre_samples,
            self._profile.guard_post_samples,
        )

    def plan_burst(
        self,
        datagrams: Sequence[Datagram],
        limits: AggregationLimits,
        entry: McsTableEntry | None = None,
    ) -> int:
        """How many leading ``datagrams`` the next burst should carry (FIFO)."""

        chosen = self.active_mcs_entry if entry is None else entry
        return plan_aggregate(
            [mpdu_length(piece) for piece in datagrams],
            lambda body: self.burst_sample_count(chosen, body),
            self._profile.sample_rate,
            limits,
        )

    def encode_datagrams(
        self,
        datagrams: Sequence[Datagram],
        *,
        entry: McsTableEntry | None = None,
    ) -> OutgoingBurst:
        """Encode ``datagrams`` as the subframes of one v5 burst, in order.

        ``entry`` pins the format the caller planned the aggregate with, so an
        MCS change between planning and encoding cannot stretch a burst past
        its airtime cap; by default the active format is used.  Each burst
        also takes the next scrambler seed (1..127) so that constellation
        statistics, PAPR and the level after peak scaling do not depend on
        what the application sent.
        """

        pieces = tuple(datagrams)
        if not pieces:
            raise ValueError("a burst needs at least one datagram")
        chosen = self.active_mcs_entry if entry is None else require_v5_entry(entry)
        with self._codec_lock:
            sequences = tuple(next(self._sequence) % _SEQUENCE_MODULUS for _ in pieces)
            seed = next(self._seed_counter) % _SCRAMBLER_SEEDS + 1 if self._scrambler else 0
        mpdus = [
            serialize_frame(
                Frame(
                    protocol_version=CURRENT_PROTOCOL_VERSION,
                    kind=FrameKind.DATA,
                    mcs=chosen.modulation,
                    sequence=sequence,
                    payload=piece.encode(),
                )
            )
            for sequence, piece in zip(sequences, pieces, strict=True)
        ]
        encoded = encode_burst_v5(
            pack_ampdu(mpdus), chosen, self._burst_config, scrambler_seed=seed
        )
        samples = np.concatenate(
            (self._guard_pre, encoded.samples, self._guard_post)
        ).astype(np.complex64, copy=False)
        return OutgoingBurst(
            samples=samples,
            sequences=sequences,
            datagrams=pieces,
            mcs_index=chosen.index,
            modulation=chosen.modulation,
            wire_version=V5_WIRE_VERSION,
            psdu_bytes=encoded.header.psdu_length,
        )

    def encode_message(self, message: bytes) -> tuple[OutgoingBurst, ...]:
        """Fragment one message and encode each fragment as its own burst.

        No aggregation: this is the one-shot path for tests, offline runs and
        the profile check.  Live traffic goes through the transmit worker,
        which aggregates whatever is queued when the radio has room.
        """

        entry = self.active_mcs_entry
        return tuple(
            self.encode_datagrams((piece,), entry=entry)
            for piece in self.fragment_message(message)
        )


@dataclass(frozen=True, slots=True)
class ReceiveStats:
    """Counters the receiver can honestly derive from a one-way stream.

    ``bursts_decoded`` counts frames (datagrams), not radio bursts: a v5
    burst carries several, and loss is measured per frame from sequence
    gaps, so the loss ratio keeps one meaning for both families.

    The subframe counters describe v5 A-MPDUs only.  ``subframes_ok`` and
    ``subframes_bad`` count MPDUs whose CRC-32 passed or failed after a valid
    delimiter; ``bad_delimiters`` counts damaged stretches where no delimiter
    was found, and ``delimiter_resyncs`` those after which the parser found a
    later one.  Subframes lost inside a damaged stretch appear in none of
    them, only as sequence gaps.
    """

    samples_consumed: int = 0
    bursts_decoded: int = 0
    payload_bytes_decoded: int = 0
    messages_delivered: int = 0
    message_bytes_delivered: int = 0
    sequence_gaps: int = 0
    missing_bursts: int = 0
    foreign_bursts: int = 0
    incomplete_messages_dropped: int = 0
    psdus_received: int = 0
    psdus_fcs_failed: int = 0
    subframes_ok: int = 0
    subframes_bad: int = 0
    bad_delimiters: int = 0
    delimiter_resyncs: int = 0

    @property
    def expected_bursts(self) -> int:
        return self.bursts_decoded + self.missing_bursts

    @property
    def burst_loss_ratio(self) -> float:
        """Fraction of expected frames that never arrived intact.

        A frame counts as missing when the PHY sequence number skips it.  That
        covers a preamble the receiver never detected, a burst whose CRC
        failed and a v5 subframe whose own CRC failed; a one-way link cannot
        tell those apart.
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

    For a v5 burst ``sequence`` is that of its first CRC-valid subframe and
    ``subframes`` the number of CRC-valid subframes; a legacy burst has one.
    ``mcs_index`` is None only for a legacy header naming no table entry.
    """

    sequence: int
    wire_version: int
    modulation: MCS
    payload_symbols: NDArray[np.complex64]
    evm: float | None
    effective_snr_db: float | None
    is_ours: bool
    mcs_index: int | None = None
    subframes: int = 1


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
    mcs_index: int | None = None


@dataclass(frozen=True, slots=True)
class _BurstContext:
    """What every frame of one burst shares when it is accepted."""

    wire_version: int
    modulation: MCS
    mcs_index: int | None
    payload_symbols: NDArray[np.complex64]
    evm: float | None
    effective_snr_db: float | None


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

    def drain_link_snr_db(self) -> tuple[float, ...]:
        """Preamble SNR, in dB, of every burst acquired since the last call.

        Unlike a burst's EVM, this is measured whether or not the burst then
        decoded, so it keeps describing the channel while bursts are lost.
        """

        return self._decoder.drain_link_snr_db()

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
            burst = result.burst
            failure = result.failure
            if burst is not None and failure is None:
                # A v5 PSDU whose FCS failed still comes back as a burst, for
                # its CRC-valid subframes, but counts as a payload failure.
                failure = PayloadFailure.from_burst(burst)
            self._decoder.record_payload_outcome(failure, burst)
            if burst is not None:
                delivered.extend(self._accept(burst, result.annotation))
        return tuple(delivered)

    def _accept(self, burst: AnyDecodedBurst, quality) -> list[ReceivedMessage]:
        """Deliver what one decoded burst carries, legacy or v5."""

        if quality is None:
            quality = burst_quality(burst)
        if isinstance(burst, DecodedBurst):
            return self._accept_legacy(burst, quality)
        return self._accept_psdu(burst, quality)

    def _accept_legacy(self, burst: DecodedBurst, quality) -> list[ReceivedMessage]:
        frame = burst.frame
        wire_version = burst.diagnostics.wire_version
        try:
            index: int | None = mcs_index_for_wire(frame.mcs, wire_version)
        except McsTableError:
            index = None
        context = _BurstContext(
            wire_version=wire_version,
            modulation=frame.mcs,
            mcs_index=index,
            payload_symbols=burst.payload_symbols,
            evm=quality[0],
            effective_snr_db=quality[1],
        )
        delivered, is_ours = self._accept_frame(frame, context)
        self._observe(context, frame.sequence, is_ours=is_ours, subframes=1)
        return delivered

    def _accept_psdu(self, burst, quality) -> list[ReceivedMessage]:
        """Unpack a v5 A-MPDU and deliver every subframe whose CRC-32 passed.

        Delivery does not wait for the PSDU's FCS: a failed FCS means some
        bytes are wrong, and each frame's own CRC-32 says which frames those
        were.  Dropping the whole burst would turn one damaged code block
        into the loss of every datagram it shared a burst with.
        """

        parsed = unpack_ampdu(burst.body)
        context = _BurstContext(
            wire_version=burst.diagnostics.wire_version,
            modulation=burst.modulation,
            mcs_index=burst.mcs_index,
            payload_symbols=burst.payload_symbols,
            evm=quality[0],
            effective_snr_db=quality[1],
        )
        delivered: list[ReceivedMessage] = []
        good = bad = 0
        first_sequence: int | None = None
        any_ours = False
        for mpdu in parsed.mpdus:
            try:
                frame = deserialize_frame(mpdu)
            except FrameDecodeError:
                bad += 1
                continue
            good += 1
            if first_sequence is None:
                first_sequence = frame.sequence
            messages, is_ours = self._accept_frame(frame, context)
            any_ours = any_ours or is_ours
            delivered.extend(messages)
        self._stats = replace(
            self._stats,
            psdus_received=self._stats.psdus_received + 1,
            psdus_fcs_failed=self._stats.psdus_fcs_failed + (0 if burst.result.fcs_ok else 1),
            subframes_ok=self._stats.subframes_ok + good,
            subframes_bad=self._stats.subframes_bad + bad,
            bad_delimiters=self._stats.bad_delimiters + parsed.bad_delimiters,
            delimiter_resyncs=self._stats.delimiter_resyncs + parsed.resyncs,
        )
        if first_sequence is not None:
            self._observe(context, first_sequence, is_ours=any_ours, subframes=good)
        return delivered

    def _observe(
        self, context: _BurstContext, sequence: int, *, is_ours: bool, subframes: int
    ) -> None:
        self._observations.append(
            BurstObservation(
                sequence=sequence,
                wire_version=context.wire_version,
                modulation=context.modulation,
                payload_symbols=context.payload_symbols,
                evm=context.evm,
                effective_snr_db=context.effective_snr_db,
                is_ours=is_ours,
                mcs_index=context.mcs_index,
                subframes=subframes,
            )
        )

    def _accept_frame(
        self, frame: Frame, context: _BurstContext
    ) -> tuple[list[ReceivedMessage], bool]:
        """Account for one CRC-valid frame; returns (messages, is_ours)."""

        try:
            parsed = datagram.decode(frame.payload)
        except datagram.DatagramError:
            # A CRC-valid frame that is not one of ours: another
            # application on the same channel, not a link error.
            self._stats = replace(
                self._stats,
                foreign_bursts=self._stats.foreign_bursts + 1,
            )
            return [], False

        self._tracker.observe(frame.sequence)
        self._stats = replace(
            self._stats,
            bursts_decoded=self._stats.bursts_decoded + 1,
            payload_bytes_decoded=self._stats.payload_bytes_decoded + len(parsed.payload),
        )

        message = self._reassembler.accept(parsed)
        if message is None:
            return [], True
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
                wire_version=context.wire_version,
                modulation=context.modulation,
                payload_symbols=context.payload_symbols,
                evm=context.evm,
                effective_snr_db=context.effective_snr_db,
                latency_s=self._latency_for(message),
                same_clock_domain=message.origin == self._origin,
                mcs_index=context.mcs_index,
            )
        ], True

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


def burst_modulation(burst: AnyDecodedBurst) -> MCS:
    """Payload constellation of a decoded burst of either family."""

    return burst.frame.mcs if isinstance(burst, DecodedBurst) else burst.modulation


def burst_quality(burst: AnyDecodedBurst) -> tuple[float | None, float | None]:
    """:func:`measure_quality` for one decoded burst; runs in decode workers."""

    return measure_quality(burst.payload_symbols, burst_modulation(burst))


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
