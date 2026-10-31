"""Wire-v5 SIG header: 48-bit body, CRC-16, K=7 rate-1/2 code, BPSK.

The legacy header repeats 96 bits three times on QPSK; v5 spends the same
three OFDM symbols (144 BPSK carriers) on a real code instead.  The 802.11a
SIGNAL field's recipe -- the K=7 133/171 convolutional code that this project
already decodes natively, BPSK, a block interleaver -- reaches 1 % FER near
1-1.5 dB SNR, about 3 dB below the most robust v5 payload, so the header is
never what limits the link and needs no list-decoding fallback.

Body layout, MSB first (bit 47 first on the wire):

====== ================== ===== =========================================
Bits   Field              Width Values
====== ================== ===== =========================================
47:44  ``format_version`` 4     0; anything else is rejected
43:38  ``mcs_index``      6     a v5 MCS-table index
37:22  ``psdu_length``    16    PSDU bytes including the 4-byte FCS
21:15  ``scrambler_seed`` 7     1-127; 0 means the scrambler is off
14:13  ``payload_format`` 2     0 = A-MPDU; 1-3 reserved
12:0   reserved           13    0
====== ================== ===== =========================================

Info word (72 bits) = body ‖ CRC-16/CCITT-FALSE over the 6 body bytes ‖
``00`` ‖ six tail zeros.  The 144 coded bits are interleaved over all three
symbols: coded bit ``k`` goes to grid slot ``9 (k mod 16) + floor(k / 16)``,
so neighbouring coded bits sit nine data carriers (about 0.8 MHz) apart and
the code spans all three symbols.  BPSK maps bit 0 to +1.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .burst_header import BurstHeaderDecodeError, BurstHeaderIntegrityError
from .fec import _convolutional_encode

V5_HEADER_BODY_BITS = 48
V5_HEADER_CRC_BITS = 16
V5_HEADER_PAD_BITS = 2
V5_HEADER_TAIL_BITS = 6
V5_HEADER_INFO_BITS = (
    V5_HEADER_BODY_BITS + V5_HEADER_CRC_BITS + V5_HEADER_PAD_BITS + V5_HEADER_TAIL_BITS
)
V5_HEADER_CODED_BITS = 2 * V5_HEADER_INFO_BITS
V5_HEADER_OFDM_SYMBOL_COUNT = 3
V5_FORMAT_VERSION = 0
V5_PAYLOAD_FORMAT_AMPDU = 0
# Delimiter (4) + the shortest serialized frame (11) + PSDU FCS (4).
V5_MIN_PSDU_BYTES = 19
# Soft values handed to the Viterbi are clipped like every other LLR in the
# receiver, so one wildly mis-scaled carrier cannot dominate a path metric.
V5_HEADER_LLR_CLIP = 64.0


class V5HeaderError(ValueError):
    """Raised when a v5 header cannot be represented on the wire."""


class V5HeaderDecodeError(BurstHeaderDecodeError):
    """Raised when decoded SIG bits are not one acceptable v5 header."""


class V5HeaderIntegrityError(V5HeaderDecodeError, BurstHeaderIntegrityError):
    """Raised when the decoded SIG fails its CRC-16 or its zero pad."""


@dataclass(frozen=True, slots=True)
class V5Header:
    """The fields one v5 burst signals; ranges are the wire field widths.

    Semantic limits (which MCS indices exist, the shortest and longest PSDU
    the receiver accepts) are checked when decoding, because they are
    receiver policy rather than wire format.
    """

    mcs_index: int
    psdu_length: int
    scrambler_seed: int
    payload_format: int = V5_PAYLOAD_FORMAT_AMPDU
    format_version: int = V5_FORMAT_VERSION

    def __post_init__(self) -> None:
        for name, value, width in (
            ("mcs_index", self.mcs_index, 6),
            ("psdu_length", self.psdu_length, 16),
            ("scrambler_seed", self.scrambler_seed, 7),
            ("payload_format", self.payload_format, 2),
            ("format_version", self.format_version, 4),
        ):
            if type(value) is not int or not 0 <= value < (1 << width):
                raise V5HeaderError(f"{name} must be an integer in [0, {(1 << width) - 1}]")
        if self.psdu_length < 1:
            raise V5HeaderError("psdu_length must be positive")


def _build_interleaver() -> NDArray[np.int64]:
    coded = np.arange(V5_HEADER_CODED_BITS, dtype=np.int64)
    positions = 9 * (coded % 16) + coded // 16
    positions.setflags(write=False)
    return positions


#: Grid slot of each coded bit: ``V5_HEADER_INTERLEAVER[k] = 9 (k % 16) + k // 16``.
V5_HEADER_INTERLEAVER: NDArray[np.int64] = _build_interleaver()


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xor-out."""

    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def _body_value(header: V5Header) -> int:
    return (
        (header.format_version << 44)
        | (header.mcs_index << 38)
        | (header.psdu_length << 22)
        | (header.scrambler_seed << 15)
        | (header.payload_format << 13)
    )


def _int_to_bits(value: int, width: int) -> NDArray[np.uint8]:
    shifts = np.arange(width - 1, -1, -1, dtype=np.uint64)
    return ((np.uint64(value) >> shifts) & np.uint64(1)).astype(np.uint8)


def _bits_to_int(bits: NDArray[np.uint8]) -> int:
    value = 0
    for bit in bits.tolist():
        value = (value << 1) | int(bit)
    return value


def encode_v5_header(header: V5Header, *, interleave: bool = True) -> NDArray[np.uint8]:
    """Return the 144 coded SIG bits in transmission (grid) order.

    ``interleave=False`` exists only so simulations can measure what the
    interleaver buys; the wire always interleaves.
    """

    if not isinstance(header, V5Header):
        raise V5HeaderError("header must be a V5Header")
    body = _body_value(header)
    crc = crc16_ccitt_false(body.to_bytes(6, "big"))
    information = np.concatenate(
        (
            _int_to_bits(body, V5_HEADER_BODY_BITS),
            _int_to_bits(crc, V5_HEADER_CRC_BITS),
            np.zeros(V5_HEADER_PAD_BITS, dtype=np.uint8),
        )
    )
    # The encoder appends the six terminating tail zeros itself.
    coded = _convolutional_encode(information)
    if coded.size != V5_HEADER_CODED_BITS:
        raise V5HeaderError("SIG encoder produced an unexpected length")
    if not interleave:
        return coded.astype(np.uint8, copy=False)
    grid = np.empty_like(coded)
    grid[V5_HEADER_INTERLEAVER] = coded
    return grid


def v5_header_symbols(header: V5Header, *, interleave: bool = True) -> NDArray[np.complex64]:
    """The 144 BPSK SIG symbols in data-carrier order (bit 0 -> +1)."""

    bits = encode_v5_header(header, interleave=interleave)
    return (1.0 - 2.0 * bits.astype(np.float32)).astype(np.complex64)


@lru_cache(maxsize=1)
def _viterbi():
    from .turbo import native_convolutional_soft_decoder

    return native_convolutional_soft_decoder(2)


def decode_v5_header(
    llrs: ArrayLike,
    *,
    valid_mcs: frozenset[int],
    max_psdu_bytes: int,
    interleave: bool = True,
) -> V5Header:
    """Decode 144 SIG LLRs (grid order, positive = bit 0) into a v5 header.

    Every check is a hard rejection; there is no fallback list decoding,
    because the code plus CRC-16 already leaves the SIG about 3 dB more
    robust than the most robust payload.  The CRC is 16 bits, so roughly one
    in 65 536 random words passes it; the zero pad, the version, the payload
    format, the 13 reserved zeros, the MCS set and the length range reject
    almost all of those as well.
    """

    values = np.asarray(llrs, dtype=np.float32)
    if values.shape != (V5_HEADER_CODED_BITS,):
        raise V5HeaderDecodeError(f"SIG needs exactly {V5_HEADER_CODED_BITS} LLRs")
    if not np.all(np.isfinite(values)):
        raise V5HeaderDecodeError("SIG LLRs must be finite")
    coded = values[V5_HEADER_INTERLEAVER] if interleave else values
    coded = np.clip(coded, -V5_HEADER_LLR_CLIP, V5_HEADER_LLR_CLIP)
    try:
        decoded = _viterbi()(coded)
    except (ValueError, RuntimeError) as error:
        raise V5HeaderDecodeError(f"SIG Viterbi failed: {error}") from error
    if decoded.size != V5_HEADER_INFO_BITS:
        raise V5HeaderDecodeError("SIG Viterbi returned an unexpected length")
    body_end = V5_HEADER_BODY_BITS
    crc_end = body_end + V5_HEADER_CRC_BITS
    pad_end = crc_end + V5_HEADER_PAD_BITS
    body = _bits_to_int(decoded[:body_end])
    if np.any(decoded[crc_end:pad_end]):
        raise V5HeaderIntegrityError("SIG pad bits are not zero")
    if _bits_to_int(decoded[body_end:crc_end]) != crc16_ccitt_false(body.to_bytes(6, "big")):
        raise V5HeaderIntegrityError("SIG CRC-16 failed")
    format_version = (body >> 44) & 0xF
    mcs_index = (body >> 38) & 0x3F
    psdu_length = (body >> 22) & 0xFFFF
    seed = (body >> 15) & 0x7F
    payload_format = (body >> 13) & 0x3
    reserved = body & 0x1FFF
    if format_version != V5_FORMAT_VERSION:
        raise V5HeaderDecodeError(f"unsupported SIG format_version {format_version}")
    if payload_format != V5_PAYLOAD_FORMAT_AMPDU:
        raise V5HeaderDecodeError(f"unsupported SIG payload_format {payload_format}")
    if reserved:
        raise V5HeaderDecodeError("SIG reserved bits are not zero")
    if mcs_index not in valid_mcs:
        raise V5HeaderDecodeError(f"SIG mcs_index {mcs_index} is not a v5 entry")
    if not V5_MIN_PSDU_BYTES <= psdu_length <= max_psdu_bytes:
        raise V5HeaderDecodeError(
            f"SIG psdu_length {psdu_length} outside [{V5_MIN_PSDU_BYTES}, {max_psdu_bytes}]"
        )
    return V5Header(
        mcs_index=mcs_index,
        psdu_length=psdu_length,
        scrambler_seed=seed,
        payload_format=payload_format,
        format_version=format_version,
    )
