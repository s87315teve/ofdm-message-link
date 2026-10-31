"""A-MPDU aggregation of several MPDUs into one v5 PSDU body.

One v5 burst pays for a preamble, a SIG header and a PSDU FCS regardless of
how much it carries, so small datagrams are cheaper when several of them share
a burst.  This module is the byte-level half of that: it packs MPDUs into a
body (the PSDU without its trailing FCS-32, which the PHY adds) and splits a
received body back into MPDU candidates.  It deliberately knows nothing about
frames, CRC-32 or the PHY, so it can be tested without any of them.

Layout, following 802.11n practice::

    body = SF_1 | pad_1 | SF_2 | pad_2 | ... | SF_n          (no pad after SF_n)
    SF_i = delimiter (4 B) | MPDU_i
    delimiter = [reserved:2 | length:14] (big-endian) | CRC-8 | 0x4E

Every delimiter starts at a multiple of four bytes from the body start.  The
alignment is what makes resynchronization cheap and unambiguous: after a
damaged delimiter the parser only has to test every fourth byte for the
signature plus CRC-8 instead of every byte.  The CRC-8 (polynomial
x^8+x^2+x+1 = 0x07, init 0xFF, no reflection, no xor-out) protects the length,
the one field that, if wrong, would desynchronize every later subframe.  The
parser never trusts the length of an MPDU whose own CRC the caller finds bad:
the caller can drop that MPDU and the following ones are unaffected, because
the next delimiter position is derived from a delimiter that passed its CRC-8.

False delimiters (known limit, not fixed): during resynchronization a random
aligned word passes the signature byte and the CRC-8 with probability about
2^-16 (minus what the length bounds reject), so a damaged region of n words
yields a false match about n / 65536 times.  A false match only produces an
MPDU candidate whose own CRC-32 then fails -- the caller drops it -- but it
can make the scan skip a real delimiter behind it and lose that subframe
too.  Only a damaged body is scanned this way: in one whose FCS passed,
every delimiter is the packer's own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

DELIMITER_BYTES = 4
DELIMITER_SIGNATURE = 0x4E
MAX_MPDU_BYTES = 16383
# 7-byte frame header + 4-byte CRC-32: an empty-payload frame.  Anything shorter
# cannot be a frame, so rejecting it also lowers false delimiter matches.
MIN_MPDU_BYTES = 11
_ALIGNMENT = 4
_CRC8_POLYNOMIAL = 0x07
_CRC8_INIT = 0xFF


def delimiter_crc8(length_bytes: bytes) -> int:
    """CRC-8 (poly 0x07, init 0xFF, MSB first, no xor-out) of ``length_bytes``.

    Check value: ``delimiter_crc8(b"123456789") == 0xFB`` (CRC-8/ITU-style
    polynomial with the 802.11 all-ones initial value).
    """
    crc = _CRC8_INIT
    for byte in length_bytes:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ _CRC8_POLYNOMIAL) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def _padding(subframe_bytes: int) -> int:
    return -subframe_bytes % _ALIGNMENT


def _validate_length(length: int) -> None:
    if not MIN_MPDU_BYTES <= length <= MAX_MPDU_BYTES:
        raise ValueError(
            f"MPDU length must be {MIN_MPDU_BYTES}..{MAX_MPDU_BYTES} bytes, got {length}"
        )


def ampdu_body_length(mpdu_lengths: Sequence[int]) -> int:
    """Length of the packed body for MPDUs of the given lengths (no FCS)."""
    total = 0
    for index, length in enumerate(mpdu_lengths):
        _validate_length(length)
        total += DELIMITER_BYTES + length
        if index != len(mpdu_lengths) - 1:
            total += _padding(DELIMITER_BYTES + length)
    return total


def _delimiter(length: int) -> bytes:
    field = length.to_bytes(2, "big")
    return field + bytes((delimiter_crc8(field), DELIMITER_SIGNATURE))


def pack_ampdu(mpdus: Sequence[bytes]) -> bytes:
    """Pack ``mpdus`` into an aligned A-MPDU body (the PSDU without its FCS)."""
    if not mpdus:
        raise ValueError("an A-MPDU needs at least one MPDU")
    out = bytearray()
    for index, mpdu in enumerate(mpdus):
        _validate_length(len(mpdu))
        out += _delimiter(len(mpdu))
        out += mpdu
        if index != len(mpdus) - 1:
            out += bytes(_padding(DELIMITER_BYTES + len(mpdu)))
    return bytes(out)


@dataclass(frozen=True, slots=True)
class AmpduParseResult:
    """MPDU candidates found in a body.

    ``offsets[i]`` is the body offset of ``mpdus[i]`` itself (just after its
    delimiter), so callers can map an MPDU onto the code blocks that carried
    it.  ``bad_delimiters`` counts positions where the parser expected a
    delimiter and found none (one per damaged stretch, not per probed byte);
    ``resyncs`` counts those after which a later valid delimiter was found.
    The MPDUs are candidates: the frame CRC-32 is the caller's job.
    """

    mpdus: tuple[bytes, ...]
    offsets: tuple[int, ...]
    bad_delimiters: int
    resyncs: int


def _delimiter_length_at(body: bytes, position: int) -> int | None:
    """MPDU length of a valid delimiter at ``position``, else ``None``."""
    if position + DELIMITER_BYTES > len(body):
        return None
    if body[position + 3] != DELIMITER_SIGNATURE:
        return None
    field = body[position : position + 2]
    if delimiter_crc8(field) != body[position + 2]:
        return None
    length = int.from_bytes(field, "big")
    if length & 0xC000 or not MIN_MPDU_BYTES <= length:
        return None
    if position + DELIMITER_BYTES + length > len(body):
        return None
    return length


def unpack_ampdu(body: bytes) -> AmpduParseResult:
    """Split ``body`` into MPDU candidates, resynchronizing after bad delimiters.

    Never raises and never returns a slice that extends beyond ``body``.  After
    a position that holds no valid delimiter (bad CRC-8, signature or length,
    or a length running past the end), the scan advances in 4-byte steps to
    the next valid delimiter; a truncated tail simply ends the scan.
    """
    body = bytes(body)
    mpdus: list[bytes] = []
    offsets: list[int] = []
    bad = 0
    resyncs = 0
    position = 0
    scanning = False
    while position + DELIMITER_BYTES <= len(body):
        length = _delimiter_length_at(body, position)
        if length is None:
            if not scanning:
                bad += 1
                scanning = True
            position += _ALIGNMENT
            continue
        if scanning:
            resyncs += 1
            scanning = False
        start = position + DELIMITER_BYTES
        mpdus.append(body[start : start + length])
        offsets.append(start)
        position = start + length
        position += _padding(position)
    return AmpduParseResult(tuple(mpdus), tuple(offsets), bad, resyncs)
