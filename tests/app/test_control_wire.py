"""The Link Control PDU wire format: sizes, round trips, and hostile input."""

from __future__ import annotations

import random
import struct

import pytest

from ofdm_link.phy.ampdu import DELIMITER_BYTES, ampdu_body_length
from ofdm_message_link import control
from ofdm_message_link.control import (
    FLAG_CONTROL_ONLY,
    LCP_VERSION,
    LcpHeader,
    PduType,
    decode_feedback,
    decode_feedback_lcp,
    decode_lcp,
    encode_feedback,
    encode_lcp,
    feedback_lcp,
    lcp_version,
    link_header,
)
from ofdm_message_link.la.api import (
    FLAG_CLIPPING_SEEN,
    FLAG_LAST_SEQ_VALID,
    BurstReport,
    FeedbackReport,
    counter_delta,
)
from ofdm_message_link.link import FRAME_OVERHEAD_BYTES, control_mpdu_length


def _entry(seq, *, mcs=12, snr=17.25, ok=5, lost=0):
    return BurstReport(
        t_s=0.0, first_sequence=seq, mcs_index=mcs, snr_db=snr, subframes_ok=ok, subframes_lost=lost
    )


def _report(**overrides) -> FeedbackReport:
    fields = dict(
        version=LCP_VERSION,
        reporter_id=0,
        report_seq=41,
        window_s=0.020,
        age_s=0.003,
        preambles=9,
        snr_mean_db=17.42,
        snr_p10_db=15.0,
        snr_last_db=18.01,
        data_ok_total=123456,
        data_lost_total=789,
        last_data_seq=65000,
        psdu_fcs_fail_total=3,
        header_fail_total=4,
        rx_overflow_total=1,
        last_mcs_index=12,
        flags=FLAG_LAST_SEQ_VALID,
        bursts=tuple(_entry(100 + 5 * index) for index in range(4)),
        extras=(),
    )
    fields.update(overrides)
    return FeedbackReport(**fields)


def _subframe_bytes(lcp: bytes) -> int:
    """Bytes the CONTROL subframe takes in front of data: delimiter, MPDU, padding."""

    mpdu = control_mpdu_length(lcp)
    return ampdu_body_length([mpdu, FRAME_OVERHEAD_BYTES]) - DELIMITER_BYTES - FRAME_OVERHEAD_BYTES


def test_link_header_is_eight_bytes_and_costs_24_bytes_per_burst():
    lcp = link_header(1, 2, FLAG_CONTROL_ONLY)
    assert len(lcp) == 8
    assert lcp[:2] == b"LC" and lcp[2] == 1 and lcp[3] == PduType.LINK_HEADER
    assert _subframe_bytes(lcp) == 24
    decoded = decode_lcp(lcp)
    assert decoded is not None
    assert decoded.header == LcpHeader(PduType.LINK_HEADER, 1, 2, FLAG_CONTROL_ONLY)
    assert decoded.body == b""


def test_feedback_with_four_entries_costs_84_bytes():
    lcp = feedback_lcp(_report(), src_node=2, dst_node=1)
    assert len(lcp) == 8 + 36 + 4 * 6
    assert _subframe_bytes(lcp) == 84


def test_feedback_round_trip_keeps_every_field_within_its_quantisation():
    report = _report(extras=((0x80, b"abc"), (0x01, b"")))
    lcp = decode_lcp(feedback_lcp(report, src_node=2, dst_node=1, flags=0))
    assert lcp is not None and lcp.is_feedback
    assert lcp.header.src_node == 2 and lcp.header.dst_node == 1
    decoded = decode_feedback_lcp(lcp)
    assert decoded is not None
    assert decoded.version == 1
    assert decoded.reporter_id == 0
    assert decoded.report_seq == 41
    assert decoded.window_s == pytest.approx(0.020)
    assert decoded.age_s == pytest.approx(0.003)
    assert decoded.preambles == 9
    assert decoded.snr_mean_db == pytest.approx(17.42, abs=0.005)
    assert decoded.snr_p10_db == pytest.approx(15.0)
    assert decoded.snr_last_db == pytest.approx(18.01, abs=0.005)
    assert decoded.data_ok_total == 123456
    assert decoded.data_lost_total == 789
    assert decoded.last_data_seq == 65000
    assert (decoded.psdu_fcs_fail_total, decoded.header_fail_total) == (3, 4)
    assert decoded.rx_overflow_total == 1
    assert decoded.last_mcs_index == 12
    assert decoded.flags == FLAG_LAST_SEQ_VALID
    assert decoded.bursts == report.bursts
    assert decoded.extras == ((0x80, b"abc"), (0x01, b""))
    assert decoded.received_t_s is None


def test_none_values_use_their_sentinels():
    report = _report(
        snr_mean_db=None,
        snr_p10_db=None,
        snr_last_db=None,
        last_data_seq=None,
        last_mcs_index=None,
        flags=FLAG_LAST_SEQ_VALID | FLAG_CLIPPING_SEEN,
        bursts=(_entry(7, mcs=None, snr=None),),
    )
    body = encode_feedback(report)
    assert struct.unpack_from(">hhh", body, 8) == (-0x8000,) * 3
    assert body[30] == 0xFF
    decoded = decode_feedback(body)
    assert decoded is not None
    assert decoded.snr_mean_db is None and decoded.snr_p10_db is None
    assert decoded.snr_last_db is None
    assert decoded.last_data_seq is None
    assert decoded.last_mcs_index is None
    # The valid bit follows last_data_seq; other flags pass through.
    assert decoded.flags == FLAG_CLIPPING_SEEN
    assert decoded.bursts[0].mcs_index is None and decoded.bursts[0].snr_db is None


def test_entry_snr_is_quarter_db_and_saturates():
    bursts = (
        _entry(1, snr=17.3),
        _entry(2, snr=80.0),
        _entry(3, snr=-4.0),
        _entry(4, ok=999, lost=300),
    )
    decoded = decode_feedback(encode_feedback(_report(bursts=bursts)))
    assert decoded is not None
    assert [entry.snr_db for entry in decoded.bursts[:3]] == [17.25, 63.5, 0.0]
    assert (decoded.bursts[3].subframes_ok, decoded.bursts[3].subframes_lost) == (255, 255)


def test_fixed_fields_saturate_instead_of_wrapping():
    decoded = decode_feedback(
        encode_feedback(
            _report(window_s=100.0, age_s=-1.0, preambles=70000, snr_mean_db=400.0)
        )
    )
    assert decoded is not None
    assert decoded.window_s == pytest.approx(65.535)
    assert decoded.age_s == 0.0
    assert decoded.preambles == 0xFFFF
    assert decoded.snr_mean_db == pytest.approx(327.67)


def test_cumulative_counters_wrap_at_their_width():
    first = decode_feedback(encode_feedback(_report(data_ok_total=(1 << 32) - 2)))
    second = decode_feedback(
        encode_feedback(_report(data_ok_total=(1 << 32) + 5, header_fail_total=70000))
    )
    assert first is not None and second is not None
    assert first.data_ok_total == (1 << 32) - 2
    assert second.data_ok_total == 5
    assert counter_delta(second.data_ok_total, first.data_ok_total) == 7
    assert second.header_fail_total == 70000 % (1 << 16)


def test_only_the_most_recent_sixteen_entries_are_sent():
    bursts = tuple(_entry(index) for index in range(20))
    decoded = decode_feedback(encode_feedback(_report(bursts=bursts)))
    assert decoded is not None
    assert [entry.first_sequence for entry in decoded.bursts] == list(range(4, 20))


def test_an_unsupported_version_is_ignored_but_recognisable():
    lcp = bytearray(link_header(1))
    lcp[2] = 2
    assert decode_lcp(bytes(lcp)) is None
    assert lcp_version(bytes(lcp)) == 2
    assert lcp_version(b"not an lcp") is None
    assert decode_feedback(encode_feedback(_report()), version=2) is None


def test_an_unknown_pdu_type_still_names_its_sender():
    lcp = decode_lcp(encode_lcp(LcpHeader(pdu_type=9, src_node=1), b"\x01\x02"))
    assert lcp is not None
    assert lcp.header.pdu_type == 9 and lcp.header.src_node == 1
    assert lcp.body == b"\x01\x02"
    assert decode_feedback_lcp(lcp) is None


def test_truncation_never_raises_and_yields_none():
    full = feedback_lcp(_report(extras=((0x81, b"xyz"),)), src_node=1)
    for cut in range(len(full)):
        assert decode_lcp(full[:cut]) is None
    body = encode_feedback(_report(extras=((0x81, b"xyz"),)))
    for cut in range(len(body)):
        assert decode_feedback(body[:cut]) is None


def test_garbage_never_raises():
    rng = random.Random(7)
    for _ in range(2000):
        raw = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 80)))
        if rng.random() < 0.5:
            raw = b"LC\x01" + raw
        lcp = decode_lcp(raw)
        if lcp is not None:
            decode_feedback_lcp(lcp)
        decode_feedback(raw)
    assert decode_lcp(None) is None  # type: ignore[arg-type]
    assert decode_feedback(12) is None  # type: ignore[arg-type]


def test_tlvs_are_kept_in_order_and_a_truncated_one_ends_the_list():
    body = bytearray(encode_feedback(_report(bursts=())))
    tlvs = bytes((0x05, 2, 0xAA, 0xBB, 0x90, 0)) + bytes((0x91, 9, 1))  # last one cut short
    struct.pack_into(">H", body, 34, len(tlvs))
    decoded = decode_feedback(bytes(body) + tlvs)
    assert decoded is not None
    assert decoded.extras == ((0x05, b"\xaa\xbb"), (0x90, b""))


def test_a_longer_body_from_a_later_revision_is_accepted():
    body = encode_feedback(_report()) + b"\x00future fields\x00"
    lcp = decode_lcp(encode_lcp(LcpHeader(PduType.FEEDBACK, 2), body))
    assert lcp is not None
    decoded = decode_feedback_lcp(lcp)
    assert decoded is not None
    assert decoded.bursts == _report().bursts
    # Bytes after the signalled LCP body (frame padding, say) are ignored too.
    assert decode_lcp(link_header(1) + b"\x00\x00\x00") is not None


def test_encoding_rejects_what_the_wire_cannot_carry():
    with pytest.raises(ValueError):
        encode_lcp(LcpHeader(PduType.LINK_HEADER, src_node=0))
    with pytest.raises(ValueError):
        encode_lcp(LcpHeader(PduType.LINK_HEADER, src_node=255))
    with pytest.raises(ValueError):
        encode_lcp(LcpHeader(PduType.FEEDBACK, src_node=1), bytes(256))
    with pytest.raises(ValueError):
        encode_feedback(_report(extras=((0x80, bytes(200)),)))
    with pytest.raises(ValueError):
        encode_feedback(_report(extras=((0x80, bytes(256)),)))


def test_module_exports_the_documented_names():
    for name in control.__all__:
        assert hasattr(control, name)


def test_session_id_travels_as_a_tlv_and_custom_tlvs_are_kept():
    report = _report(session_id=0xDEADBEEF, extras=((0x80, b"abc"),))
    body = encode_feedback(report)
    assert len(body) == len(encode_feedback(_report(extras=((0x80, b"abc"),)))) + 6
    decoded = decode_feedback(body)
    assert decoded.session_id == 0xDEADBEEF
    assert decoded.extras == ((0x80, b"abc"),)
    assert decode_feedback(encode_feedback(_report())).session_id is None
    # The fixed v1 layout is unchanged: a reader that knows no TLVs still parses it.
    assert body[:control.FEEDBACK_FIXED_BYTES - 2] == encode_feedback(
        _report(extras=((0x80, b"abc"),))
    )[:control.FEEDBACK_FIXED_BYTES - 2]


def test_a_session_tlv_of_the_wrong_length_stays_an_extra():
    body = bytearray(encode_feedback(_report()))
    tlv = bytes((control.TLV_SESSION_ID, 2, 1, 2))
    struct.pack_into(">H", body, 34, len(tlv))
    decoded = decode_feedback(bytes(body) + tlv)
    assert decoded.session_id is None and decoded.extras == ((control.TLV_SESSION_ID, b"\x01\x02"),)
