"""Fixed robust signaling header for dynamic payload-MCS acquisition.

The wire header is always carried by QPSK signaling, independently of the
payload MCS.  Its 96 bits are each repeated three times, producing exactly
three OFDM symbols when 48 data carriers are used.
"""

from __future__ import annotations

import itertools
import struct
import zlib
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .codec import (
    MAX_INFORMATION_FRAME_BITS,
    MCS,
    MIN_INFORMATION_FRAME_BITS,
)
from .turbo import turbo_coded_bit_count

BURST_HEADER_MAGIC = b"OF"
CURRENT_BURST_HEADER_VERSION = 1
TURBO_BURST_HEADER_VERSION = 2
CONVOLUTIONAL_R13_BURST_HEADER_VERSION = 3
UNCODED_BURST_HEADER_VERSION = 4

# Versions whose 32-bit field is a coded payload length.  They share one header
# class because the receiver derives the information length from the version's
# fixed rate; only the Turbo version carries an information length instead.
CODED_LENGTH_WIRE_VERSIONS = (
    CURRENT_BURST_HEADER_VERSION,
    CONVOLUTIONAL_R13_BURST_HEADER_VERSION,
    UNCODED_BURST_HEADER_VERSION,
)
SUPPORTED_WIRE_VERSIONS = tuple(
    sorted((*CODED_LENGTH_WIRE_VERSIONS, TURBO_BURST_HEADER_VERSION))
)
BURST_HEADER_REPETITION_FACTOR = 3
BURST_HEADER_SIGNALING_BITS_PER_SYMBOL = 2  # Fixed QPSK, not payload MCS.
OFDM_DATA_CARRIER_COUNT = 48
# Receiver-only soft-decision bounds; the wire format is unchanged.
SOFT_HEADER_FLIP_CANDIDATES = 6
SOFT_HEADER_MAX_FLIPPED_BITS = 2

_PREFIX = struct.Struct(">2sBBI")
_CRC = struct.Struct(">I")

BURST_HEADER_UNCODED_BIT_COUNT = (_PREFIX.size + _CRC.size) * 8
BURST_HEADER_CODED_BIT_COUNT = (
    BURST_HEADER_UNCODED_BIT_COUNT * BURST_HEADER_REPETITION_FACTOR
)
BURST_HEADER_SIGNALING_SYMBOL_COUNT = (
    BURST_HEADER_CODED_BIT_COUNT // BURST_HEADER_SIGNALING_BITS_PER_SYMBOL
)
BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT = (
    BURST_HEADER_SIGNALING_SYMBOL_COUNT // OFDM_DATA_CARRIER_COUNT
)


class BurstHeaderError(ValueError):
    """Base class for invalid burst-header inputs."""


class BurstHeaderValidationError(BurstHeaderError):
    """Raised when metadata cannot be represented by this wire version."""


class BurstHeaderDecodeError(BurstHeaderError):
    """Raised when signaling bits do not contain one valid header."""


class BurstHeaderIntegrityError(BurstHeaderDecodeError):
    """Raised when the decoded header fails CRC-32."""


@dataclass(frozen=True, slots=True)
class BurstHeader:
    """Metadata whose 32-bit field is a coded payload length.

    Used by every wire version except the Turbo one: v1 (convolutional rate
    1/2), v3 (convolutional rate 1/3), and v4 (uncoded rate 1).
    """

    wire_version: int
    mcs: MCS
    coded_payload_bit_count: int

    def __post_init__(self) -> None:
        if type(self.wire_version) is not int:
            raise BurstHeaderValidationError("wire_version must be an integer")
        if self.wire_version not in CODED_LENGTH_WIRE_VERSIONS:
            raise BurstHeaderValidationError(
                f"unsupported wire_version {self.wire_version}; "
                f"expected one of {list(CODED_LENGTH_WIRE_VERSIONS)}"
            )
        if not isinstance(self.mcs, MCS):
            raise BurstHeaderValidationError("mcs must be an MCS")
        if (
            type(self.coded_payload_bit_count) is not int
            or not 1 <= self.coded_payload_bit_count <= 0xFFFFFFFF
        ):
            raise BurstHeaderValidationError(
                "coded_payload_bit_count must be an integer in [1, 4294967295]"
            )

    @property
    def payload_symbol_count(self) -> int:
        """Number of payload-constellation symbols, including bit padding."""

        bits_per_symbol = self.mcs.bits_per_symbol
        return (self.coded_payload_bit_count + bits_per_symbol - 1) // bits_per_symbol

    @property
    def payload_bit_padding_count(self) -> int:
        """Zero bits required to complete the final payload symbol."""

        return self.payload_symbol_count * self.mcs.bits_per_symbol - self.coded_payload_bit_count

    @property
    def payload_ofdm_symbol_count(self) -> int:
        """Number of 48-data-carrier OFDM symbols required by the payload."""

        return (
            self.payload_symbol_count + OFDM_DATA_CARRIER_COUNT - 1
        ) // OFDM_DATA_CARRIER_COUNT

    @property
    def payload_carrier_padding_symbol_count(self) -> int:
        """Unused constellation positions in the final payload OFDM symbol."""

        return (
            self.payload_ofdm_symbol_count * OFDM_DATA_CARRIER_COUNT
            - self.payload_symbol_count
        )


@dataclass(frozen=True, slots=True)
class TurboBurstHeader:
    """Wire-v2 metadata whose 32-bit field is an information-frame length."""

    wire_version: int
    mcs: MCS
    information_frame_bit_count: int

    def __post_init__(self) -> None:
        if type(self.wire_version) is not int:
            raise BurstHeaderValidationError("wire_version must be an integer")
        if self.wire_version != TURBO_BURST_HEADER_VERSION:
            raise BurstHeaderValidationError(
                f"unsupported Turbo wire_version {self.wire_version}; "
                f"expected {TURBO_BURST_HEADER_VERSION}"
            )
        if not isinstance(self.mcs, MCS):
            raise BurstHeaderValidationError("mcs must be an MCS")
        count = self.information_frame_bit_count
        if (
            type(count) is not int
            or not MIN_INFORMATION_FRAME_BITS <= count <= MAX_INFORMATION_FRAME_BITS
            or count % 8
        ):
            raise BurstHeaderValidationError(
                "information_frame_bit_count must be a byte-aligned integer in "
                f"[{MIN_INFORMATION_FRAME_BITS}, {MAX_INFORMATION_FRAME_BITS}]"
            )

    @property
    def coded_payload_bit_count(self) -> int:
        """Derive the transmitted length from the one fixed Turbo profile."""

        return turbo_coded_bit_count(self.information_frame_bit_count)

    @property
    def payload_symbol_count(self) -> int:
        bits_per_symbol = self.mcs.bits_per_symbol
        return (self.coded_payload_bit_count + bits_per_symbol - 1) // bits_per_symbol

    @property
    def payload_bit_padding_count(self) -> int:
        return self.payload_symbol_count * self.mcs.bits_per_symbol - self.coded_payload_bit_count

    @property
    def payload_ofdm_symbol_count(self) -> int:
        return (
            self.payload_symbol_count + OFDM_DATA_CARRIER_COUNT - 1
        ) // OFDM_DATA_CARRIER_COUNT

    @property
    def payload_carrier_padding_symbol_count(self) -> int:
        return (
            self.payload_ofdm_symbol_count * OFDM_DATA_CARRIER_COUNT
            - self.payload_symbol_count
        )


BurstHeaderDescriptor = BurstHeader | TurboBurstHeader


def encode_burst_header(header: BurstHeaderDescriptor) -> NDArray[np.uint8]:
    """Serialize, CRC-protect, and repeat every header bit three times."""

    if not isinstance(header, (BurstHeader, TurboBurstHeader)):
        raise BurstHeaderValidationError("header must be a versioned burst header")
    length_field = (
        header.coded_payload_bit_count
        if isinstance(header, BurstHeader)
        else header.information_frame_bit_count
    )
    prefix = _PREFIX.pack(
        BURST_HEADER_MAGIC,
        header.wire_version,
        int(header.mcs),
        length_field,
    )
    raw = prefix + _CRC.pack(zlib.crc32(prefix) & 0xFFFFFFFF)
    bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="big")
    return np.repeat(bits, BURST_HEADER_REPETITION_FACTOR)


def decode_burst_header(coded_bits: ArrayLike) -> BurstHeaderDescriptor:
    """Majority-decode and validate one exact fixed-length signaling header."""

    coded = np.asarray(coded_bits)
    if coded.ndim != 1:
        raise BurstHeaderDecodeError("coded_bits must be one-dimensional")
    if coded.size != BURST_HEADER_CODED_BIT_COUNT:
        raise BurstHeaderDecodeError(
            f"coded header has {coded.size} bits; expected {BURST_HEADER_CODED_BIT_COUNT}"
        )
    if not np.issubdtype(coded.dtype, np.number) and coded.dtype != np.bool_:
        raise BurstHeaderDecodeError("coded_bits must contain only 0 and 1")
    if not np.all((coded == 0) | (coded == 1)):
        raise BurstHeaderDecodeError("coded_bits must contain only 0 and 1")

    votes = coded.astype(np.uint8, copy=False).reshape(
        BURST_HEADER_UNCODED_BIT_COUNT, BURST_HEADER_REPETITION_FACTOR
    )
    return _parse_header_bits(votes.sum(axis=1) > BURST_HEADER_REPETITION_FACTOR // 2)


def decode_burst_header_soft(
    coded_llrs: ArrayLike,
    *,
    flip_candidates: int = SOFT_HEADER_FLIP_CANDIDATES,
    max_flipped_bits: int = SOFT_HEADER_MAX_FLIPPED_BITS,
) -> BurstHeaderDescriptor:
    """Soft-combine the repetitions, then CRC-check a bounded flip list.

    ``coded_llrs`` uses the project convention: positive selects bit 0.  The
    wire format repeats each bit on consecutive coded positions, so one QPSK
    symbol carries two copies of the same bit and a single noisy symbol can
    defeat a hard majority vote.  Summing the three LLRs keeps the confidence
    of the clean copy.  If the CRC still fails, at most ``max_flipped_bits`` of
    the ``flip_candidates`` least reliable information bits are flipped
    (22 CRC checks with the defaults); CRC-32 plus magic and metadata checks
    keep the false-accept probability negligible.
    """

    values = np.asarray(coded_llrs)
    if values.ndim != 1:
        raise BurstHeaderDecodeError("coded_llrs must be one-dimensional")
    if values.size != BURST_HEADER_CODED_BIT_COUNT:
        raise BurstHeaderDecodeError(
            f"coded header has {values.size} LLRs; expected {BURST_HEADER_CODED_BIT_COUNT}"
        )
    if not np.issubdtype(values.dtype, np.number) or not np.all(np.isfinite(values)):
        raise BurstHeaderDecodeError("coded_llrs must contain only finite numbers")

    combined = values.astype(np.float64, copy=False).reshape(
        BURST_HEADER_UNCODED_BIT_COUNT, BURST_HEADER_REPETITION_FACTOR
    ).sum(axis=1)
    decided = combined < 0.0
    least_reliable = np.argsort(np.abs(combined), kind="stable")[:flip_candidates]
    last_error: BurstHeaderDecodeError | None = None
    for flip_count in range(max_flipped_bits + 1):
        for flipped in itertools.combinations(least_reliable, flip_count):
            bits = decided.copy()
            bits[list(flipped)] ^= True
            try:
                return _parse_header_bits(bits)
            except BurstHeaderIntegrityError as error:
                last_error = error
            except BurstHeaderDecodeError:
                # A CRC-valid pattern with invalid metadata is rejected, and
                # never accepted by flipping further bits around it.
                raise
    assert last_error is not None
    raise last_error


def _parse_header_bits(information_bits: NDArray[np.bool_]) -> BurstHeaderDescriptor:
    raw = np.packbits(information_bits).tobytes()
    prefix, received_crc = raw[:-_CRC.size], raw[-_CRC.size :]
    expected_crc = _CRC.pack(zlib.crc32(prefix) & 0xFFFFFFFF)
    if received_crc != expected_crc:
        raise BurstHeaderIntegrityError("burst header CRC-32 mismatch")

    magic, version, mcs_value, length_field = _PREFIX.unpack(prefix)
    if magic != BURST_HEADER_MAGIC:
        raise BurstHeaderDecodeError("burst header magic mismatch")
    try:
        mcs = MCS(mcs_value)
    except ValueError as error:
        raise BurstHeaderDecodeError(f"unknown MCS {mcs_value}") from error
    try:
        if version in CODED_LENGTH_WIRE_VERSIONS:
            return BurstHeader(version, mcs, length_field)
        if version == TURBO_BURST_HEADER_VERSION:
            return TurboBurstHeader(version, mcs, length_field)
        raise BurstHeaderValidationError(f"unsupported wire_version {version}")
    except BurstHeaderValidationError as error:
        raise BurstHeaderDecodeError(str(error)) from error
