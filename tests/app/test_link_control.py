"""CONTROL subframes through the message link: placement, numbering, handling.

A node puts one CONTROL frame first in each burst, numbered from its own
counter; the receiver hands it to a control handler, keeps DATA loss
accounting clean, and reports each DATA burst with its preamble SNR.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from ofdm_link.phy import CURRENT_PROTOCOL_VERSION
from ofdm_link.phy.ampdu import ampdu_body_length, pack_ampdu
from ofdm_link.phy.codec import MCS, Frame, FrameKind, serialize_frame
from ofdm_link.phy.mcs_table import mcs_entry
from ofdm_link.phy.streaming import StreamingBurstDecoder
from ofdm_message_link.aggregation import AggregationLimits
from ofdm_message_link.control import decode_lcp, link_header
from ofdm_message_link.link import (
    ControlContext,
    ControlVerdict,
    MessageReceiver,
    MessageTransmitter,
    build_phy_profile,
    control_mpdu_length,
    load_link_config,
    mpdu_length,
)

CONFIG = "configs/default.yaml"
SNR_DB = 22.0


@pytest.fixture(scope="module")
def profile():
    return build_phy_profile(load_link_config(CONFIG))


def _awgn(samples, snr_db, seed):
    rng = np.random.default_rng(seed)
    power = float(np.mean(np.abs(samples) ** 2))
    variance = power / (10.0 ** (snr_db / 10.0))
    noise = rng.normal(scale=np.sqrt(variance / 2.0), size=(samples.size, 2))
    return (samples + (noise[:, 0] + 1j * noise[:, 1])).astype(np.complex64)


def _stream(bursts, gap=600):
    blocks = []
    for burst in bursts:
        blocks.append(np.zeros(gap, dtype=np.complex64))
        blocks.append(burst.samples)
    blocks.append(np.zeros(4 * gap, dtype=np.complex64))
    return np.concatenate(blocks).astype(np.complex64)


def _feed(receiver, stream, chunk=8192):
    delivered = []
    for start in range(0, stream.size, chunk):
        delivered.extend(receiver.feed(stream[start : start + chunk]))
    delivered.extend(receiver.flush())
    return delivered


class _Handler:
    def __init__(self, verdict=ControlVerdict.ACCEPT):
        self.verdict = verdict
        self.calls: list[tuple[bytes, ControlContext]] = []

    def on_control(self, payload, context):
        self.calls.append((payload, context))
        return self.verdict


def _datagrams(transmitter, count, size=60):
    return [transmitter.fragment_message(bytes([index]) * size)[0] for index in range(count)]


# -- transmit side -----------------------------------------------------------


def test_control_frames_go_first_with_their_own_sequence(profile):
    transmitter = MessageTransmitter(profile)
    bursts = [
        transmitter.encode_datagrams(
            _datagrams(transmitter, 3), control_payloads=[link_header(1)]
        )
        for _ in range(3)
    ]
    assert [burst.control_sequences for burst in bursts] == [(0,), (1,), (2,)]
    assert [burst.sequences for burst in bursts] == [(0, 1, 2), (3, 4, 5), (6, 7, 8)]
    assert bursts[1].sequence == 3 and not bursts[1].control_only

    handler = _Handler()
    receiver = MessageReceiver(profile, control_handler=handler)
    delivered = _feed(receiver, _awgn(_stream(bursts), SNR_DB, seed=1))

    assert len(delivered) == 9
    assert [message.sequence for message in delivered] == list(range(9))
    assert [context.control_sequence for _, context in handler.calls] == [0, 1, 2]
    assert all(decode_lcp(payload).header.src_node == 1 for payload, _ in handler.calls)
    stats = receiver.stats
    assert stats.control_frames == 3
    assert stats.foreign_bursts == 0
    assert stats.missing_bursts == 0 and stats.sequence_gaps == 0
    assert stats.bursts_decoded == 9
    context = handler.calls[0][1]
    assert context.mcs_index == 8
    assert context.link_snr_db == pytest.approx(SNR_DB, abs=3.0)


def test_a_receiver_without_a_handler_counts_control_frames_not_foreign(profile):
    transmitter = MessageTransmitter(profile)
    bursts = [
        transmitter.encode_datagrams(_datagrams(transmitter, 2), control_payloads=[link_header(2)]),
        transmitter.encode_datagrams((), control_payloads=[link_header(2)]),
        transmitter.encode_datagrams(_datagrams(transmitter, 1)),
    ]
    receiver = MessageReceiver(profile)
    delivered = _feed(receiver, _awgn(_stream(bursts), SNR_DB, seed=2))
    assert [message.sequence for message in delivered] == [0, 1, 2]
    stats = receiver.stats
    assert stats.control_frames == 2
    assert stats.foreign_bursts == 0
    assert stats.missing_bursts == 0


def test_a_control_only_burst_carries_no_data_and_leaves_no_gap(profile):
    transmitter = MessageTransmitter(profile)
    first = transmitter.encode_datagrams(_datagrams(transmitter, 1))
    control_only = transmitter.encode_datagrams(
        (), entry=mcs_entry(8), control_payloads=[link_header(1, flags=1)]
    )
    last = transmitter.encode_datagrams(_datagrams(transmitter, 1))
    assert control_only.control_only
    assert control_only.sequences == () and control_only.datagrams == ()
    assert control_only.sequence == control_only.control_sequences[0] == 0
    assert control_only.payload_bytes == 0 and control_only.subframe_count == 0
    assert (first.sequence, last.sequence) == (0, 1)

    handler = _Handler()
    receiver = MessageReceiver(profile, control_handler=handler)
    delivered = _feed(receiver, _awgn(_stream([first, control_only, last]), SNR_DB, seed=3))
    assert len(delivered) == 2
    assert len(handler.calls) == 1
    assert receiver.stats.missing_bursts == 0
    reports = receiver.drain_burst_reports()
    assert [report.first_sequence for report in reports] == [0, 1]
    with pytest.raises(ValueError):
        transmitter.encode_datagrams(())


def test_seed_ranges_partition_the_scrambler_seeds(profile):
    (piece,) = _datagrams(MessageTransmitter(profile), 1, size=8)
    default = MessageTransmitter(profile)
    assert default.seed_range == (1, 127)
    seeds = [default.encode_datagrams((piece,)).scrambler_seed for _ in range(3)]
    assert seeds == [1, 2, 3]

    node2 = MessageTransmitter(profile, seed_range=(64, 127))
    seeds = [node2.encode_datagrams((piece,)).scrambler_seed for _ in range(66)]
    assert seeds[:3] == [64, 65, 66]
    assert seeds[63] == 127 and seeds[64] == 64
    node1 = MessageTransmitter(profile, seed_range=(1, 63))
    seeds = {node1.encode_datagrams((piece,)).scrambler_seed for _ in range(70)}
    assert seeds == set(range(1, 64))
    for bad in ((0, 63), (64, 128), (70, 64), (1.0, 5)):
        with pytest.raises(ValueError):
            MessageTransmitter(profile, seed_range=bad)


def test_extra_mpdu_lengths_count_against_every_cap(profile):
    transmitter = MessageTransmitter(profile)
    datagrams = _datagrams(transmitter, 10, size=200)
    extra = [control_mpdu_length(link_header(1))]
    by_count = AggregationLimits(max_subframes=4)
    assert transmitter.plan_burst(datagrams, by_count) == 4
    assert transmitter.plan_burst(datagrams, by_count, extra_mpdu_lengths=extra) == 3

    # A PSDU cap that fits exactly three data MPDUs plus the FCS.
    three = ampdu_body_length([mpdu_length(datagrams[0])] * 3) + 4
    by_bytes = AggregationLimits(max_psdu_bytes=three)
    assert transmitter.plan_burst(datagrams, by_bytes) == 3
    assert transmitter.plan_burst(datagrams, by_bytes, extra_mpdu_lengths=extra) == 2

    entry = mcs_entry(8)
    airtime = transmitter.burst_sample_count(entry, three - 4) / profile.sample_rate
    by_airtime = AggregationLimits(max_burst_airtime_s=airtime)
    assert transmitter.plan_burst(datagrams, by_airtime, entry) == 3
    assert transmitter.plan_burst(datagrams, by_airtime, entry, extra_mpdu_lengths=extra) == 2

    # A queue is never stalled by its control overhead, and nothing is invented.
    assert transmitter.plan_burst(datagrams, by_count, extra_mpdu_lengths=[4000] * 5) == 1
    assert transmitter.plan_burst([], by_count, extra_mpdu_lengths=extra) == 0
    single = AggregationLimits(enabled=False)
    assert transmitter.plan_burst(datagrams, single, extra_mpdu_lengths=extra) == 1


# -- receive side ------------------------------------------------------------


def test_drop_burst_drops_the_whole_psdu(profile):
    own = MessageTransmitter(profile)
    peer = MessageTransmitter(profile, seed_range=(64, 127))
    bursts = [
        peer.encode_datagrams(_datagrams(peer, 1), control_payloads=[link_header(2)]),
        own.encode_datagrams(_datagrams(own, 3), control_payloads=[link_header(1)]),
        peer.encode_datagrams(_datagrams(peer, 1), control_payloads=[link_header(2)]),
    ]

    class OwnLeakage(_Handler):
        def on_control(self, payload, context):
            super().on_control(payload, context)
            own_burst = decode_lcp(payload).header.src_node == 1
            return ControlVerdict.DROP_BURST if own_burst else ControlVerdict.ACCEPT

    handler = OwnLeakage()
    receiver = MessageReceiver(profile, control_handler=handler)
    delivered = _feed(receiver, _awgn(_stream(bursts), SNR_DB, seed=4))

    assert [message.sequence for message in delivered] == [0, 1]
    assert len(handler.calls) == 3
    stats = receiver.stats
    assert stats.dropped_bursts == 1
    assert stats.bursts_decoded == 2
    assert stats.missing_bursts == 0  # own sequences 0-2 were never tracked
    assert stats.psdus_received == 2
    assert [report.first_sequence for report in receiver.drain_burst_reports()] == [0, 1]
    assert len(receiver.drain_observations()) == 2


def _fake_psdu_burst(body, *, fcs_ok=True):
    return SimpleNamespace(
        body=body,
        result=SimpleNamespace(fcs_ok=fcs_ok),
        fcs_ok=fcs_ok,
        mcs_index=8,
        modulation=MCS.QPSK,
        burst_start=1000,
        diagnostics=SimpleNamespace(wire_version=5),
        payload_symbols=np.ones(48, dtype=np.complex64),
    )


def _control_mpdu(sequence, payload):
    return serialize_frame(
        Frame(CURRENT_PROTOCOL_VERSION, FrameKind.CONTROL, MCS.QPSK, sequence, payload)
    )


def test_drop_counts_only_for_the_first_crc_valid_subframe(profile):
    transmitter = MessageTransmitter(profile)
    (piece,) = _datagrams(transmitter, 1)
    data = serialize_frame(
        Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, 0, piece.encode())
    )
    receiver = MessageReceiver(profile, control_handler=_Handler(ControlVerdict.DROP_BURST))
    body = pack_ampdu([data, _control_mpdu(0, link_header(1))])
    delivered = receiver._accept(_fake_psdu_burst(body), (None, None))
    assert len(delivered) == 1
    assert receiver.stats.dropped_bursts == 0
    assert receiver.stats.control_frames == 1

    # A damaged first subframe makes the CONTROL frame behind it the first
    # CRC-valid one, so it may still drop the burst.
    broken = bytearray(_control_mpdu(1, link_header(1)))
    broken[-1] ^= 0xFF
    body = pack_ampdu([bytes(broken), _control_mpdu(2, link_header(1)), data])
    assert receiver._accept(_fake_psdu_burst(body), (None, None)) == []
    assert receiver.stats.dropped_bursts == 1


def test_a_legacy_control_frame_reaches_the_handler(profile):
    from ofdm_link.phy import encode_burst
    from ofdm_link.runtime.factory import build_burst_config, select_frame_decoder_for_entry

    entry = mcs_entry(0)
    selection = select_frame_decoder_for_entry(profile.config, entry)
    encoded = encode_burst(
        Frame(CURRENT_PROTOCOL_VERSION, FrameKind.CONTROL, entry.modulation, 5, link_header(3)),
        build_burst_config(profile.config),
        symbol_mapper=selection.symbol_mapper,
        wire_version=selection.wire_version,
        frame_encoder=selection.frame_encoder,
    )
    guard = np.zeros(512, dtype=np.complex64)
    stream = np.concatenate((guard, encoded.samples, guard, guard)).astype(np.complex64)
    handler = _Handler()
    receiver = MessageReceiver(profile, control_handler=handler)
    assert _feed(receiver, _awgn(stream, SNR_DB, seed=5)) == []
    assert [context.control_sequence for _, context in handler.calls] == [5]
    assert receiver.stats.control_frames == 1 and receiver.stats.foreign_bursts == 0
    assert receiver.drain_burst_reports() == ()


def _gapped_stream(profile):
    transmitter = MessageTransmitter(profile, mcs_entry=mcs_entry(12))
    bursts = [
        transmitter.encode_datagrams(_datagrams(transmitter, 2), control_payloads=[link_header(1)])
        for _ in range(4)
    ]
    # The second burst never arrives: its two DATA sequences (2, 3) go missing.
    return _awgn(_stream([bursts[0], bursts[2], bursts[3]]), SNR_DB, seed=6)


def test_burst_reports_count_ok_and_lost_subframes(profile):
    receiver = MessageReceiver(profile, clock=lambda: 12.5)
    _feed(receiver, _gapped_stream(profile))
    reports = receiver.drain_burst_reports()
    assert [(r.first_sequence, r.subframes_ok, r.subframes_lost) for r in reports] == [
        (0, 2, 0),
        (4, 2, 2),
        (6, 2, 0),
    ]
    assert all(r.mcs_index == 12 and r.fcs_ok is True and r.t_s == 12.5 for r in reports)
    assert all(r.snr_db == pytest.approx(SNR_DB, abs=3.0) for r in reports)
    assert receiver.drain_burst_reports() == ()


def test_snr_at_finds_the_same_preamble_inline_and_through_a_decode_pool(profile):
    stream = np.concatenate((np.zeros(50_000, dtype=np.complex64), _gapped_stream(profile)))
    inline = MessageReceiver(profile)
    _feed(inline, stream, chunk=6000)
    expected = inline.drain_burst_reports()
    pooled = MessageReceiver(profile, decode_workers=2)
    try:
        assert pooled.decode_workers == 2
        _feed(pooled, stream, chunk=6000)
        actual = pooled.drain_burst_reports()
        assert pooled.decode_pool_snapshot().submitted == 3
    finally:
        pooled.close()
    assert len(expected) == len(actual) == 3
    assert all(report.snr_db is not None for report in expected)
    assert [r.snr_db for r in actual] == [r.snr_db for r in expected]
    assert [r.subframes_lost for r in actual] == [0, 2, 0]


def test_snr_at_matches_within_one_preamble_and_forgets_old_bursts():
    decoder = StreamingBurstDecoder()
    decoder._snr_by_start.extend([(1000, 10.0), (5000, 20.0), (5300, 21.0)])
    assert decoder.snr_at(1000) == 10.0
    assert decoder.snr_at(1200) == 10.0
    assert decoder.snr_at(5100) == 20.0
    assert decoder.snr_at(5250) == 21.0
    assert decoder.snr_at(3000) is None
    assert decoder.snr_at(1000 + 321) is None
    assert decoder.snr_at(1000, tolerance=0) == 10.0
