"""Application-layer datagram framing for the one-way message demo.

The PHY already carries a per-frame sequence number and a CRC, so this layer
adds only what a one-way link cannot recover on its own:

* ``message_id`` plus ``fragment_index``/``fragment_count`` so one application
  message larger than a single frame payload can be reassembled, and so an
  incomplete message is dropped rather than delivered truncated.
* ``origin`` plus ``tx_monotonic_ns`` so the receiver can report one-way
  latency **only** when transmitter and receiver share a clock, which on Linux
  means the same host.  Across two hosts ``CLOCK_MONOTONIC`` epochs are
  unrelated and any latency computed from them would be fiction, so the
  receiver reports none.

Nothing here is a MAC layer: there is no acknowledgement, no retransmission
and no ordering guarantee.
"""

from __future__ import annotations

import os
import socket
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass

_HEADER = struct.Struct(">HBBIHHQQ")

MAGIC = 0x4F4C
VERSION = 1
HEADER_SIZE = _HEADER.size

assert HEADER_SIZE == 28, "datagram header must stay a fixed 28 bytes"


class DatagramError(ValueError):
    """Raised when bytes do not contain one valid demo datagram."""


@dataclass(frozen=True, slots=True)
class Datagram:
    """One fragment of one application message."""

    message_id: int
    fragment_index: int
    fragment_count: int
    origin: int
    tx_monotonic_ns: int
    payload: bytes

    def __post_init__(self) -> None:
        if not 0 <= self.message_id <= 0xFFFFFFFF:
            raise DatagramError("message_id must fit in 32 bits")
        if not 1 <= self.fragment_count <= 0xFFFF:
            raise DatagramError("fragment_count must be in [1, 65535]")
        if not 0 <= self.fragment_index < self.fragment_count:
            raise DatagramError("fragment_index must be below fragment_count")
        if not 0 <= self.origin <= 0xFFFFFFFFFFFFFFFF:
            raise DatagramError("origin must fit in 64 bits")
        if not 0 <= self.tx_monotonic_ns <= 0xFFFFFFFFFFFFFFFF:
            raise DatagramError("tx_monotonic_ns must fit in 64 bits")
        if type(self.payload) is not bytes:
            raise DatagramError("payload must be immutable bytes")

    @property
    def is_complete_message(self) -> bool:
        return self.fragment_count == 1

    def encode(self) -> bytes:
        header = _HEADER.pack(
            MAGIC,
            VERSION,
            0,
            self.message_id,
            self.fragment_index,
            self.fragment_count,
            self.origin,
            self.tx_monotonic_ns,
        )
        return header + self.payload


def decode(raw: bytes) -> Datagram:
    """Parse one datagram, rejecting anything that is not this wire format."""

    if type(raw) is not bytes:
        raise DatagramError("raw must be immutable bytes")
    if len(raw) < HEADER_SIZE:
        raise DatagramError(f"need at least {HEADER_SIZE} bytes, got {len(raw)}")
    magic, version, _flags, message_id, index, count, origin, tx_ns = _HEADER.unpack(
        raw[:HEADER_SIZE]
    )
    if magic != MAGIC:
        raise DatagramError(f"bad magic 0x{magic:04X}")
    if version != VERSION:
        raise DatagramError(f"unsupported datagram version {version}")
    return Datagram(
        message_id=message_id,
        fragment_index=index,
        fragment_count=count,
        origin=origin,
        tx_monotonic_ns=tx_ns,
        payload=raw[HEADER_SIZE:],
    )


def local_origin() -> int:
    """Return a stable 64-bit id for this host's monotonic clock domain.

    Derived from the hostname and the kernel boot id where one is readable, so
    two processes on the same running kernel agree and a different host (or the
    same host after a reboot) does not.
    """

    parts = [socket.gethostname().encode("utf-8", "replace")]
    try:
        with open("/proc/sys/kernel/random/boot_id", "rb") as handle:
            parts.append(handle.read().strip())
    except OSError:
        parts.append(str(os.getpid()).encode("ascii"))
    digest = 0
    for part in parts:
        for byte in part:
            digest = (digest * 1099511628211 ^ byte) & 0xFFFFFFFFFFFFFFFF
    return digest


def fragment(
    message: bytes,
    *,
    message_id: int,
    max_payload_bytes: int,
    origin: int,
    tx_monotonic_ns: int | None = None,
) -> Iterator[Datagram]:
    """Split one application message into datagrams that fit one PHY frame."""

    if type(message) is not bytes:
        raise DatagramError("message must be immutable bytes")
    if type(max_payload_bytes) is not int or max_payload_bytes < 1:
        raise DatagramError("max_payload_bytes must be a positive integer")
    timestamp = time.monotonic_ns() if tx_monotonic_ns is None else tx_monotonic_ns

    chunks = [
        message[at : at + max_payload_bytes]
        for at in range(0, len(message), max_payload_bytes)
    ]
    if not chunks:
        chunks = [b""]
    if len(chunks) > 0xFFFF:
        raise DatagramError(
            f"message needs {len(chunks)} fragments, above the 65535 wire limit"
        )
    for index, chunk in enumerate(chunks):
        yield Datagram(
            message_id=message_id & 0xFFFFFFFF,
            fragment_index=index,
            fragment_count=len(chunks),
            origin=origin,
            tx_monotonic_ns=timestamp,
            payload=chunk,
        )


@dataclass(frozen=True, slots=True)
class ReassembledMessage:
    """One application message recovered from one or more datagrams."""

    message_id: int
    payload: bytes
    origin: int
    tx_monotonic_ns: int
    fragment_count: int


class Reassembler:
    """Bounded reassembly of fragmented messages on a lossy one-way link.

    Partial messages are held only until ``max_pending`` newer messages have
    started, then dropped.  Without a return path there is nothing to ask for
    a missing fragment, so an incomplete message is counted and discarded
    rather than kept forever.
    """

    def __init__(self, *, max_pending: int = 16) -> None:
        if type(max_pending) is not int or max_pending < 1:
            raise DatagramError("max_pending must be a positive integer")
        self._max_pending = max_pending
        self._pending: dict[int, dict[int, Datagram]] = {}
        self._incomplete_dropped = 0

    @property
    def incomplete_dropped(self) -> int:
        """Messages evicted before every fragment arrived."""

        return self._incomplete_dropped

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def accept(self, datagram: Datagram) -> ReassembledMessage | None:
        """Add one datagram, returning a message once it is complete."""

        if not isinstance(datagram, Datagram):
            raise DatagramError("datagram must be a Datagram")
        if datagram.is_complete_message:
            return ReassembledMessage(
                message_id=datagram.message_id,
                payload=datagram.payload,
                origin=datagram.origin,
                tx_monotonic_ns=datagram.tx_monotonic_ns,
                fragment_count=1,
            )

        fragments = self._pending.setdefault(datagram.message_id, {})
        fragments[datagram.fragment_index] = datagram
        if len(fragments) < datagram.fragment_count:
            self._evict()
            return None

        del self._pending[datagram.message_id]
        ordered = [fragments[index].payload for index in range(datagram.fragment_count)]
        first = fragments[0]
        return ReassembledMessage(
            message_id=datagram.message_id,
            payload=b"".join(ordered),
            origin=first.origin,
            tx_monotonic_ns=first.tx_monotonic_ns,
            fragment_count=datagram.fragment_count,
        )

    def _evict(self) -> None:
        while len(self._pending) > self._max_pending:
            oldest = next(iter(self._pending))
            del self._pending[oldest]
            self._incomplete_dropped += 1
