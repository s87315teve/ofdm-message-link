"""Link Control PDU (LCP): the bytes a CONTROL subframe carries.

A node puts one CONTROL frame first in every burst it sends.  Its payload is
one LCP: a fixed 8-byte header that names the sending node, so a receiver can
recognise its own transmitter's leakage, and an optional body.  The FEEDBACK
body carries what the receiver measured on the peer's bursts back to the
peer's link adaptation.  All fields are big-endian::

    header (8 B)
      0 magic u16 = 0x4C43 ("LC")   2 version u8 = 1   3 pdu_type u8
      4 src_node u8 (1-254)         5 dst_node u8 (0 = any)
      6 flags u8                    7 body_length u8

    FEEDBACK body v1 (36-byte fixed part)
      0 report_seq u16    2 window_ms u16    4 age_ms u16    6 preambles u16
      8 snr_mean_cdb i16  10 snr_p10_cdb i16 12 snr_last_cdb i16   (0x8000 = none)
      14 data_ok_total u32   18 data_lost_total u32
      22 last_data_seq u16   24 psdu_fcs_fail_total u16
      26 header_fail_total u16   28 rx_overflow_total u16
      30 last_mcs_index u8 (0xFF = none)   31 fb_flags u8
      32 reporter_id u8   33 n_entries u8 (<= 16)   34 tlv_length u16
      36 entries, 6 B each: seq_first u16 | mcs u8 | snr_qdb u8 | ok u8 | lost u8
      then tlv_length bytes of TLVs: type u8 | len u8 | value

The receiver of an LCP must never fail on it: :func:`decode_lcp` and
:func:`decode_feedback` return None for anything they cannot read, and a
version other than :data:`LCP_VERSION` is ignored.  A body longer than the
known layout is accepted and its tail ignored, so a later minor revision can
append fields.  The counters are cumulative and wrap, so a receiver that
missed reports differences the ones it got (``la.api.counter_delta``).
"""

from __future__ import annotations

import enum
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass

from .la.api import (
    FLAG_LAST_SEQ_VALID,
    MAX_REPORT_ENTRIES,
    BurstReport,
    FeedbackReport,
)

LCP_MAGIC = 0x4C43
LCP_VERSION = 1
LCP_HEADER_BYTES = 8
MAX_BODY_BYTES = 0xFF

# LcpHeader.flags bits.
FLAG_CONTROL_ONLY = 0x01
FLAG_LBT_MODE = 0x02
FLAG_TX_QUEUE_NONEMPTY = 0x04

FEEDBACK_FIXED_BYTES = 36
FEEDBACK_ENTRY_BYTES = 6
# Custom reporters use TLV types 0x80-0xFF; lower types are reserved.
CUSTOM_TLV_FIRST = 0x80

_HEADER = struct.Struct(">HBBBBBB")
_FEEDBACK = struct.Struct(">HHHHhhhIIHHHHBBBBH")
_ENTRY = struct.Struct(">HBBBB")
_SNR_NONE = -0x8000
_U8_NONE = 0xFF
_SNR_QDB_MAX = 0xFE  # 63.5 dB; 0xFF means "none"

assert _HEADER.size == LCP_HEADER_BYTES
assert _FEEDBACK.size == FEEDBACK_FIXED_BYTES
assert _ENTRY.size == FEEDBACK_ENTRY_BYTES


class PduType(enum.IntEnum):
    """LCP body kinds; a receiver ignores types it does not know."""

    LINK_HEADER = 1
    FEEDBACK = 2
    STATUS = 3  # reserved for ARQ status reports


PDU_LINK_HEADER = PduType.LINK_HEADER
PDU_FEEDBACK = PduType.FEEDBACK
PDU_STATUS = PduType.STATUS


@dataclass(frozen=True, slots=True)
class LcpHeader:
    """The fixed LCP header without magic and body length."""

    pdu_type: int
    src_node: int
    dst_node: int = 0
    flags: int = 0
    version: int = LCP_VERSION


@dataclass(frozen=True, slots=True)
class Lcp:
    """One decoded LCP.  ``header.pdu_type`` may be a type this code does not know."""

    header: LcpHeader
    body: bytes = b""

    @property
    def is_feedback(self) -> bool:
        return self.header.pdu_type == PduType.FEEDBACK


def encode_lcp(header: LcpHeader, body: bytes = b"") -> bytes:
    """Serialize one LCP; raises ValueError for fields the wire cannot carry."""

    if type(body) is not bytes:
        raise TypeError("body must be bytes")
    if len(body) > MAX_BODY_BYTES:
        raise ValueError(f"LCP body is {len(body)} bytes, the limit is {MAX_BODY_BYTES}")
    for name, value, low in (
        ("pdu_type", header.pdu_type, 0),
        ("src_node", header.src_node, 1),
        ("dst_node", header.dst_node, 0),
        ("flags", header.flags, 0),
        ("version", header.version, 0),
    ):
        if not isinstance(value, int) or not low <= int(value) <= 0xFF:
            raise ValueError(f"{name} must be an integer in [{low}, 255]")
    if header.src_node == 0xFF:
        raise ValueError("src_node 255 is reserved")
    return (
        _HEADER.pack(
            LCP_MAGIC,
            int(header.version),
            int(header.pdu_type),
            int(header.src_node),
            int(header.dst_node),
            int(header.flags),
            len(body),
        )
        + body
    )


def decode_lcp(data: bytes) -> Lcp | None:
    """Parse one LCP, or return None if it is not a readable version-1 LCP.

    Never raises.  Bytes after the signalled body are ignored.
    """

    try:
        raw = bytes(data)
    except (TypeError, ValueError):
        return None
    if len(raw) < LCP_HEADER_BYTES:
        return None
    magic, version, pdu_type, src, dst, flags, body_length = _HEADER.unpack_from(raw)
    if magic != LCP_MAGIC or version != LCP_VERSION:
        return None
    if len(raw) < LCP_HEADER_BYTES + body_length:
        return None
    return Lcp(
        header=LcpHeader(
            pdu_type=pdu_type, src_node=src, dst_node=dst, flags=flags, version=version
        ),
        body=raw[LCP_HEADER_BYTES : LCP_HEADER_BYTES + body_length],
    )


def lcp_version(data: bytes) -> int | None:
    """The version byte of something that starts with the LCP magic, else None.

    Lets a caller count an LCP of an unsupported version separately from
    bytes that are not an LCP at all.
    """

    raw = bytes(data[:3]) if isinstance(data, (bytes, bytearray, memoryview)) else b""
    if len(raw) < 3 or int.from_bytes(raw[:2], "big") != LCP_MAGIC:
        return None
    return raw[2]


def link_header(src_node: int, dst_node: int = 0, flags: int = 0) -> bytes:
    """A LINK_HEADER LCP: the 8-byte header alone, for bursts with no report."""

    return encode_lcp(LcpHeader(PduType.LINK_HEADER, src_node, dst_node, flags))


def feedback_lcp(
    report: FeedbackReport, src_node: int, dst_node: int = 0, flags: int = 0
) -> bytes:
    """A FEEDBACK LCP carrying ``report``."""

    return encode_lcp(
        LcpHeader(PduType.FEEDBACK, src_node, dst_node, flags), encode_feedback(report)
    )


# -- FEEDBACK body ------------------------------------------------------------


def _saturate(value: float, high: int) -> int:
    if not math.isfinite(value):
        return high if value > 0 else 0
    return max(0, min(high, round(value)))


def _ms(seconds: float) -> int:
    return _saturate(float(seconds) * 1e3, 0xFFFF)


def _cdb(value: float | None) -> int:
    if value is None or not math.isfinite(value):
        return _SNR_NONE
    return max(-0x7FFF, min(0x7FFF, round(float(value) * 100.0)))


def _from_cdb(value: int) -> float | None:
    return None if value == _SNR_NONE else value / 100.0


def _qdb(value: float | None) -> int:
    if value is None or not math.isfinite(value):
        return _U8_NONE
    return _saturate(float(value) * 4.0, _SNR_QDB_MAX)


def _u8_or_none(value: int | None) -> int:
    if value is None or not 0 <= int(value) < _U8_NONE:
        return _U8_NONE
    return int(value)


def _encode_entries(bursts: Sequence[BurstReport]) -> bytes:
    return b"".join(
        _ENTRY.pack(
            0 if burst.first_sequence is None else int(burst.first_sequence) & 0xFFFF,
            _u8_or_none(burst.mcs_index),
            _qdb(burst.snr_db),
            _saturate(burst.subframes_ok, 0xFF),
            _saturate(burst.subframes_lost, 0xFF),
        )
        for burst in bursts
    )


def _encode_tlvs(extras: Sequence[tuple[int, bytes]]) -> bytes:
    parts = []
    for kind, value in extras:
        if not 0 <= int(kind) <= 0xFF:
            raise ValueError("TLV type must be in [0, 255]")
        if type(value) is not bytes or len(value) > 0xFF:
            raise ValueError("TLV value must be bytes of at most 255")
        parts.append(bytes((int(kind), len(value))) + value)
    return b"".join(parts)


def encode_feedback(report: FeedbackReport) -> bytes:
    """Serialize a FeedbackReport as a version-1 FEEDBACK body.

    Values are quantised and saturated to their fields; only the most recent
    :data:`~ofdm_message_link.la.api.MAX_REPORT_ENTRIES` bursts are kept.
    ``fb_flags`` bit 0 is derived from ``last_data_seq``.  Raises ValueError
    if the TLVs do not fit the 255-byte body.
    """

    bursts = tuple(report.bursts)[-MAX_REPORT_ENTRIES:]
    tlvs = _encode_tlvs(report.extras)
    flags = int(report.flags) & 0xFF & ~FLAG_LAST_SEQ_VALID
    if report.last_data_seq is not None:
        flags |= FLAG_LAST_SEQ_VALID
    fixed = _FEEDBACK.pack(
        int(report.report_seq) & 0xFFFF,
        _ms(report.window_s),
        _ms(report.age_s),
        _saturate(report.preambles, 0xFFFF),
        _cdb(report.snr_mean_db),
        _cdb(report.snr_p10_db),
        _cdb(report.snr_last_db),
        int(report.data_ok_total) & 0xFFFFFFFF,
        int(report.data_lost_total) & 0xFFFFFFFF,
        0 if report.last_data_seq is None else int(report.last_data_seq) & 0xFFFF,
        int(report.psdu_fcs_fail_total) & 0xFFFF,
        int(report.header_fail_total) & 0xFFFF,
        int(report.rx_overflow_total) & 0xFFFF,
        _u8_or_none(report.last_mcs_index),
        flags,
        int(report.reporter_id) & 0xFF,
        len(bursts),
        len(tlvs),
    )
    body = fixed + _encode_entries(bursts) + tlvs
    if len(body) > MAX_BODY_BYTES:
        raise ValueError(
            f"FEEDBACK body is {len(body)} bytes, the limit is {MAX_BODY_BYTES}; "
            "send fewer entries or smaller TLVs"
        )
    return body


def _decode_tlvs(raw: bytes) -> tuple[tuple[int, bytes], ...]:
    extras = []
    at = 0
    while at + 2 <= len(raw):
        kind, length = raw[at], raw[at + 1]
        if at + 2 + length > len(raw):
            break  # a truncated TLV ends the list
        extras.append((kind, raw[at + 2 : at + 2 + length]))
        at += 2 + length
    return tuple(extras)


def decode_feedback(body: bytes, *, version: int = LCP_VERSION) -> FeedbackReport | None:
    """Parse a FEEDBACK body, or return None if it is truncated.  Never raises.

    ``BurstReport.t_s`` and ``fcs_ok`` are not on the wire: entries come back
    with ``t_s = 0.0`` and ``fcs_ok = None``.  Every TLV is returned in
    ``extras``; this codec defines no TLV types, so none is interpreted.
    """

    try:
        raw = bytes(body)
    except (TypeError, ValueError):
        return None
    if version != LCP_VERSION or len(raw) < FEEDBACK_FIXED_BYTES:
        return None
    (
        report_seq,
        window_ms,
        age_ms,
        preambles,
        snr_mean,
        snr_p10,
        snr_last,
        data_ok,
        data_lost,
        last_seq,
        fcs_fail,
        header_fail,
        overflow,
        last_mcs,
        fb_flags,
        reporter_id,
        n_entries,
        tlv_length,
    ) = _FEEDBACK.unpack_from(raw)
    entries_end = FEEDBACK_FIXED_BYTES + n_entries * FEEDBACK_ENTRY_BYTES
    if n_entries > MAX_REPORT_ENTRIES or len(raw) < entries_end + tlv_length:
        return None
    bursts = []
    for offset in range(FEEDBACK_FIXED_BYTES, entries_end, FEEDBACK_ENTRY_BYTES):
        seq, mcs, qdb, ok, lost = _ENTRY.unpack_from(raw, offset)
        bursts.append(
            BurstReport(
                t_s=0.0,
                first_sequence=seq,
                mcs_index=None if mcs == _U8_NONE else mcs,
                snr_db=None if qdb == _U8_NONE else qdb / 4.0,
                subframes_ok=ok,
                subframes_lost=lost,
            )
        )
    return FeedbackReport(
        version=version,
        reporter_id=reporter_id,
        report_seq=report_seq,
        window_s=window_ms / 1e3,
        age_s=age_ms / 1e3,
        preambles=preambles,
        snr_mean_db=_from_cdb(snr_mean),
        snr_p10_db=_from_cdb(snr_p10),
        snr_last_db=_from_cdb(snr_last),
        data_ok_total=data_ok,
        data_lost_total=data_lost,
        last_data_seq=last_seq if fb_flags & FLAG_LAST_SEQ_VALID else None,
        psdu_fcs_fail_total=fcs_fail,
        header_fail_total=header_fail,
        rx_overflow_total=overflow,
        last_mcs_index=None if last_mcs == _U8_NONE else last_mcs,
        flags=fb_flags,
        bursts=tuple(bursts),
        extras=_decode_tlvs(raw[entries_end : entries_end + tlv_length]),
    )


def decode_feedback_lcp(lcp: Lcp) -> FeedbackReport | None:
    """The report in a FEEDBACK LCP, or None for any other or unreadable LCP."""

    if not lcp.is_feedback:
        return None
    return decode_feedback(lcp.body, version=lcp.header.version)


__all__ = [
    "CUSTOM_TLV_FIRST",
    "FEEDBACK_ENTRY_BYTES",
    "FEEDBACK_FIXED_BYTES",
    "FLAG_CONTROL_ONLY",
    "FLAG_LBT_MODE",
    "FLAG_TX_QUEUE_NONEMPTY",
    "LCP_HEADER_BYTES",
    "LCP_MAGIC",
    "LCP_VERSION",
    "MAX_BODY_BYTES",
    "PDU_FEEDBACK",
    "PDU_LINK_HEADER",
    "PDU_STATUS",
    "Lcp",
    "LcpHeader",
    "PduType",
    "decode_feedback",
    "decode_feedback_lcp",
    "decode_lcp",
    "encode_feedback",
    "encode_lcp",
    "feedback_lcp",
    "lcp_version",
    "link_header",
]
