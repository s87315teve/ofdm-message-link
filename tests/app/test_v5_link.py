"""Wire-v5 bursts and A-MPDU aggregation through the message-link layer.

The transmit worker is driven with a recording sink instead of a radio, and
the receiver's v5 path is exercised both end to end and with hand-built
decoded bursts, so that one damaged subframe can be placed exactly.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from ofdm_link.phy.ampdu import DELIMITER_BYTES, pack_ampdu
from ofdm_link.phy.burst_v5 import encode_burst_v5
from ofdm_link.phy.codec import MCS, Frame, FrameKind, serialize_frame
from ofdm_link.phy.mcs_table import mcs_entry, v5_entries
from ofdm_link.runtime.airtime import v5_burst_samples_with_guards
from ofdm_link.runtime.factory import build_burst_config
from ofdm_message_link import transport
from ofdm_message_link.aggregation import AggregationLimits
from ofdm_message_link.link import (
    MessageReceiver,
    MessageTransmitter,
    aggregate_body_length,
    build_phy_profile,
    concatenate_bursts,
    load_link_config,
)
from ofdm_message_link.tx_app import TransmitWorker

CONFIG = "configs/default.yaml"
OVERLAY = "configs/profiles/b210_ota_3p8ghz.yaml"


@pytest.fixture(scope="module")
def profile():
    return build_phy_profile(load_link_config(CONFIG, [OVERLAY]))


def _awgn(samples, snr_db, seed):
    rng = np.random.default_rng(seed)
    power = float(np.mean(np.abs(samples) ** 2))
    variance = power / (10.0 ** (snr_db / 10.0))
    noise = rng.normal(scale=np.sqrt(variance / 2.0), size=(samples.size, 2))
    return (samples + (noise[:, 0] + 1j * noise[:, 1])).astype(np.complex64)


class _Signal:
    def __init__(self):
        self.values = []

    def emit(self, value):
        self.values.append(value)


class _Bridge:
    def __init__(self):
        self.sent = _Signal()
        self.failed = _Signal()


class _RecordingSink:
    """Accepts every burst at once and remembers when it arrived."""

    def __init__(self):
        self.bursts: list[np.ndarray] = []
        self.arrivals: list[float] = []
        self._lock = threading.Lock()

    def start(self):
        pass

    def stop(self):
        pass

    def send(self, samples):
        with self._lock:
            self.bursts.append(np.array(samples))
            self.arrivals.append(time.monotonic())
        return True


def _wait_for(condition, timeout_s=20.0):
    deadline = time.monotonic() + timeout_s
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


def _run_worker(profile, messages, limits, *, entry=None, attach_first=False, pause_s=0.0):
    """Submit ``messages`` to a worker and return (bridge, sink).

    Unless ``attach_first``, everything is queued before the radio starts, so
    the first burst sees the whole queue: aggregation becomes deterministic.
    """

    transmitter = MessageTransmitter(profile, mcs_entry=entry)
    bridge = _Bridge()
    worker = TransmitWorker(transmitter, bridge, aggregation=limits)
    sink = _RecordingSink()
    worker.start()
    try:
        if attach_first:
            worker.attach(sink)
        for message in messages:
            assert worker.submit("test", message)
            if pause_s:
                time.sleep(pause_s)
        if not attach_first:
            worker.attach(sink)
        assert _wait_for(
            lambda: sum(record.message_count for record in bridge.sent.values) == len(messages)
        ), bridge.failed.values
        assert worker.pending == 0
    finally:
        worker.stop()
    assert not bridge.failed.values
    return bridge, sink


def _receive(profile, sink, snr_db=None):
    receiver = MessageReceiver(profile)
    stream = np.concatenate(sink.bursts).astype(np.complex64)
    if snr_db is not None:
        stream = _awgn(stream, snr_db, seed=5)
    delivered = []
    for start in range(0, stream.size, 4096):
        delivered.extend(receiver.feed(stream[start : start + 4096]))
    return receiver, delivered


# -- encoding --------------------------------------------------------------


def test_aggregated_burst_length_matches_the_airtime_model(profile):
    transmitter = MessageTransmitter(profile)
    pieces = transmitter.fragment_message(b"a" * 30) + transmitter.fragment_message(b"b" * 1500)
    for entry in v5_entries(include_hidden=True):
        burst = transmitter.encode_datagrams(pieces, entry=entry)
        body = aggregate_body_length(pieces)
        assert burst.psdu_bytes == body + 4
        assert burst.sample_count == v5_burst_samples_with_guards(entry, body, 16, 64)
        assert burst.sample_count == transmitter.burst_sample_count(entry, body)
        assert burst.subframe_count == 3
        assert burst.sequences == tuple(range(burst.sequences[0], burst.sequences[0] + 3))


def test_guards_are_zero_and_the_v5_burst_sits_between_them(profile):
    transmitter = MessageTransmitter(profile)
    (piece,) = transmitter.fragment_message(b"guarded")
    burst = transmitter.encode_datagrams((piece,))
    assert not np.any(burst.samples[:16])
    assert not np.any(burst.samples[-64:])
    assert np.any(burst.samples[16:32])


def test_scrambler_seed_rotates_per_burst_and_never_switches_off(profile):
    transmitter = MessageTransmitter(profile)
    (piece,) = transmitter.fragment_message(b"same bytes every time")
    first = transmitter.encode_datagrams((piece,))
    second = transmitter.encode_datagrams((piece,))
    # Same datagram, different seed: the payload samples differ.
    assert not np.allclose(first.samples[-2000:-64], second.samples[-2000:-64])


def test_a_message_split_across_two_bursts_reassembles(profile):
    transmitter = MessageTransmitter(profile)
    message = bytes(range(256)) * 16  # 4096 B: five fragments
    pieces = transmitter.fragment_message(message)
    assert len(pieces) == 5
    bursts = (transmitter.encode_datagrams(pieces[:2]), transmitter.encode_datagrams(pieces[2:]))
    receiver = MessageReceiver(profile)
    delivered = receiver.feed(_awgn(concatenate_bursts(bursts), 18.0, seed=3))

    assert [item.message.payload for item in delivered] == [message]
    stats = receiver.stats
    assert (stats.psdus_received, stats.subframes_ok, stats.bursts_decoded) == (2, 5, 5)
    assert stats.missing_bursts == 0
    observations = receiver.drain_observations()
    assert [item.subframes for item in observations] == [2, 3]


# -- transmit worker ---------------------------------------------------------


def test_worker_loopback_through_the_simulated_transport(profile):
    """Mixed sizes, aggregated by the worker, over the noisy UDP sample link."""

    rng = np.random.default_rng(21)
    messages = [rng.bytes(int(size)) for size in (5, 900, 40, 2100, 11, 600, 300, 1)]
    _, sink = _run_worker(profile, messages, AggregationLimits())
    source = transport.UdpSampleSource(port=53190, snr_db=16.0, seed=22)
    udp = transport.UdpSampleSink(port=53190)
    source.start()
    udp.start()
    receiver = MessageReceiver(profile)
    delivered = []
    try:
        for burst in sink.bursts:
            assert udp.send(burst) is True
        deadline = time.monotonic() + 20.0
        while len(delivered) < len(messages) and time.monotonic() < deadline:
            chunk = source.recv(0.2)
            if chunk is not None:
                delivered.extend(receiver.feed(chunk))
    finally:
        udp.stop()
        source.stop()

    assert [item.message.payload for item in delivered] == messages
    assert len(sink.bursts) < len(messages), "the queued datagrams were aggregated"
    assert receiver.stats.missing_bursts == 0
    assert receiver.stats.subframes_ok == receiver.stats.bursts_decoded


def test_worker_reports_subframes_per_burst(profile):
    messages = [f"m{index}".encode() for index in range(6)]
    bridge, sink = _run_worker(profile, messages, AggregationLimits())
    records = bridge.sent.values
    assert len(records) == len(sink.bursts) == 1
    assert records[0].subframe_count == 6
    assert records[0].message_count == 6
    assert records[0].byte_count == sum(len(message) for message in messages)
    assert records[0].sample_count == sink.bursts[0].size
    assert records[0].mcs_index == 8


def test_worker_honours_the_subframe_cap_and_keeps_fifo_order(profile):
    messages = [f"message {index}".encode() for index in range(10)]
    bridge, sink = _run_worker(profile, messages, AggregationLimits(max_subframes=3))
    assert [record.subframe_count for record in bridge.sent.values] == [3, 3, 3, 1]
    _, delivered = _receive(profile, sink)
    assert [item.message.payload for item in delivered] == messages


def test_worker_honours_the_airtime_cap(profile):
    # A full frame at MCS 8 is 3.92 ms; a 6 ms cap fits one, never two.
    big = [bytes(900)] * 3
    bridge, sink = _run_worker(profile, big, AggregationLimits(max_burst_airtime_s=0.006))
    assert [record.subframe_count for record in bridge.sent.values] == [1, 1, 1]
    # Small datagrams still share a burst under the same cap.
    small = [bytes(20)] * 5
    bridge, sink = _run_worker(profile, small, AggregationLimits(max_burst_airtime_s=0.006))
    assert [record.subframe_count for record in bridge.sent.values] == [5]
    assert all(burst.size / profile.sample_rate <= 0.006 for burst in sink.bursts)


def test_worker_honours_the_psdu_byte_cap(profile):
    # 500 B payload -> 539 B MPDU; two, with delimiters, pad and FCS, are 1091 B.
    limits = AggregationLimits(max_psdu_bytes=1091, max_burst_airtime_s=0.1)
    bridge, _ = _run_worker(profile, [bytes(500)] * 4, limits, entry=mcs_entry(14))
    assert [record.subframe_count for record in bridge.sent.values] == [2, 2]
    limits = AggregationLimits(max_psdu_bytes=1090, max_burst_airtime_s=0.1)
    bridge, _ = _run_worker(profile, [bytes(500)] * 4, limits, entry=mcs_entry(14))
    assert [record.subframe_count for record in bridge.sent.values] == [1, 1, 1, 1]


def test_worker_with_aggregation_off_sends_one_datagram_per_burst(profile):
    messages = [b"a", b"b" * 2000, b"c"]
    bridge, _ = _run_worker(profile, messages, AggregationLimits(enabled=False))
    # 2000 bytes are three fragments of at most 968.
    assert [record.subframe_count for record in bridge.sent.values] == [1] * 5
    assert [record.message_count for record in bridge.sent.values] == [1, 0, 0, 1, 1]


def test_without_max_wait_a_lone_datagram_leaves_at_once(profile):
    bridge, sink = _run_worker(
        profile, [b"first", b"second"], AggregationLimits(), attach_first=True, pause_s=0.3
    )
    assert [record.subframe_count for record in bridge.sent.values] == [1, 1]


def test_max_wait_holds_a_burst_for_company_but_no_longer(profile):
    limits = AggregationLimits(max_wait_s=0.6)
    started = time.monotonic()
    bridge, sink = _run_worker(
        profile, [b"first", b"second"], limits, attach_first=True, pause_s=0.15
    )
    assert [record.subframe_count for record in bridge.sent.values] == [2]
    waited = sink.arrivals[0] - started
    assert 0.5 <= waited < 3.0, waited

    # Alone, the datagram waits out max_wait and then goes, not forever.
    started = time.monotonic()
    bridge, sink = _run_worker(profile, [b"alone"], limits, attach_first=True)
    assert [record.subframe_count for record in bridge.sent.values] == [1]
    assert 0.5 <= sink.arrivals[0] - started < 3.0


def test_max_wait_does_not_hold_a_burst_that_is_already_full(profile):
    limits = AggregationLimits(max_wait_s=5.0, max_subframes=2)
    started = time.monotonic()
    bridge, sink = _run_worker(profile, [b"a", b"b"], limits)
    assert [record.subframe_count for record in bridge.sent.values] == [2]
    assert sink.arrivals[0] - started < 3.0


def test_mcs_change_applies_to_the_next_burst(profile):
    transmitter = MessageTransmitter(profile)
    bridge = _Bridge()
    worker = TransmitWorker(transmitter, bridge)
    sink = _RecordingSink()
    worker.start()
    worker.attach(sink)
    try:
        assert worker.submit("t", b"at 8")
        assert _wait_for(lambda: len(bridge.sent.values) == 1)
        worker.select_mcs_entry(mcs_entry(14))
        assert worker.submit("t", b"at 14")
        assert _wait_for(lambda: len(bridge.sent.values) == 2)
    finally:
        worker.stop()
    assert [record.mcs_index for record in bridge.sent.values] == [8, 14]


# -- receiver: aggregate delivery semantics ---------------------------------


def _mpdus(transmitter, payloads, first_sequence=0):
    pieces = [transmitter.fragment_message(payload)[0] for payload in payloads]
    return [
        serialize_frame(Frame(1, FrameKind.DATA, MCS.QPSK, first_sequence + index, piece.encode()))
        for index, piece in enumerate(pieces)
    ]


def _fake_psdu_burst(body, *, fcs_ok, mcs_index=8):
    """A stand-in for DecodedPsduBurst with exactly the fields the receiver reads."""

    return SimpleNamespace(
        body=body,
        result=SimpleNamespace(fcs_ok=fcs_ok),
        fcs_ok=fcs_ok,
        mcs_index=mcs_index,
        modulation=MCS.QPSK,
        diagnostics=SimpleNamespace(wire_version=5),
        payload_symbols=np.ones(48, dtype=np.complex64),
    )


def test_one_corrupted_subframe_costs_only_itself(profile):
    transmitter = MessageTransmitter(profile)
    payloads = [f"datagram {index}".encode() for index in range(5)]
    mpdus = _mpdus(transmitter, payloads)
    body = bytearray(pack_ampdu(mpdus))
    # Flip one payload byte of the third MPDU (its delimiter stays intact).
    third = sum(DELIMITER_BYTES + len(m) + (-(DELIMITER_BYTES + len(m)) % 4) for m in mpdus[:2])
    body[third + DELIMITER_BYTES + 12] ^= 0xFF
    receiver = MessageReceiver(profile)

    delivered = receiver._accept(_fake_psdu_burst(bytes(body), fcs_ok=False), (None, None))

    assert [item.message.payload for item in delivered] == [
        payloads[0], payloads[1], payloads[3], payloads[4]
    ]
    stats = receiver.stats
    assert (stats.subframes_ok, stats.subframes_bad) == (4, 1)
    assert (stats.psdus_received, stats.psdus_fcs_failed) == (1, 1)
    assert (stats.missing_bursts, stats.sequence_gaps) == (1, 1)
    assert stats.bad_delimiters == 0
    (observation,) = receiver.drain_observations()
    assert observation.subframes == 4 and observation.mcs_index == 8


def test_a_corrupted_delimiter_is_resynchronized_past(profile):
    transmitter = MessageTransmitter(profile)
    payloads = [f"datagram {index}".encode() for index in range(4)]
    mpdus = _mpdus(transmitter, payloads, first_sequence=100)
    body = bytearray(pack_ampdu(mpdus))
    second = DELIMITER_BYTES + len(mpdus[0]) + (-(DELIMITER_BYTES + len(mpdus[0])) % 4)
    body[second + 2] ^= 0x5A  # the second delimiter's CRC-8
    receiver = MessageReceiver(profile)

    delivered = receiver._accept(_fake_psdu_burst(bytes(body), fcs_ok=False), (None, None))

    assert [item.message.payload for item in delivered] == [payloads[0], payloads[2], payloads[3]]
    stats = receiver.stats
    assert stats.bad_delimiters == 1 and stats.delimiter_resyncs == 1
    assert stats.subframes_ok == 3
    assert stats.missing_bursts == 1


def test_an_intact_aggregate_counts_no_errors(profile):
    transmitter = MessageTransmitter(profile)
    payloads = [b"x" * size for size in (1, 50, 700)]
    receiver = MessageReceiver(profile)
    body = pack_ampdu(_mpdus(transmitter, payloads))
    delivered = receiver._accept(_fake_psdu_burst(body, fcs_ok=True), (None, None))
    assert [item.message.payload for item in delivered] == payloads
    stats = receiver.stats
    assert (stats.subframes_ok, stats.subframes_bad, stats.psdus_fcs_failed) == (3, 0, 0)
    assert (stats.bad_delimiters, stats.delimiter_resyncs) == (0, 0)


def test_a_psdu_with_no_valid_subframe_is_counted_but_not_observed(profile):
    receiver = MessageReceiver(profile)
    assert receiver._accept(_fake_psdu_burst(bytes(64), fcs_ok=False), (None, None)) == []
    assert receiver.stats.psdus_received == 1
    assert receiver.stats.bad_delimiters == 1
    assert receiver.drain_observations() == ()


def test_a_foreign_frame_inside_an_aggregate_is_counted_as_foreign(profile):
    transmitter = MessageTransmitter(profile)
    ours = _mpdus(transmitter, [b"ours"], first_sequence=0)
    foreign = serialize_frame(Frame(1, FrameKind.DATA, MCS.QPSK, 1, b"not a datagram"))
    receiver = MessageReceiver(profile)
    body = pack_ampdu([*ours, foreign])
    delivered = receiver._accept(_fake_psdu_burst(body, fcs_ok=True), (None, None))
    assert [item.message.payload for item in delivered] == [b"ours"]
    assert receiver.stats.foreign_bursts == 1
    assert receiver.stats.subframes_ok == 2


def test_a_real_v5_burst_with_a_broken_code_block_delivers_the_rest(profile):
    """End to end: noise wipes part of the payload, the other subframes survive."""

    transmitter = MessageTransmitter(profile, mcs_entry=mcs_entry(12))
    payloads = [bytes([index]) * 900 for index in range(6)]
    pieces = [transmitter.fragment_message(payload)[0] for payload in payloads]
    burst = transmitter.encode_datagrams(pieces)
    samples = burst.samples.copy()
    # Bury the last fifth of the payload under strong noise.
    rng = np.random.default_rng(9)
    end = samples.size - 64
    start = end - (end - 16 - 560) // 5
    samples[start:end] += (rng.normal(size=end - start) * 3).astype(np.complex64)
    receiver = MessageReceiver(profile)
    delivered = receiver.feed(_awgn(samples, 25.0, seed=10))
    got = [item.message.payload for item in delivered]

    assert got and got == payloads[: len(got)], "a prefix survives, in order"
    assert len(got) < len(payloads)
    stats = receiver.stats
    assert stats.psdus_fcs_failed == 1
    assert receiver.decoder_snapshot().psdu_fcs_failures == 1


def test_encode_burst_v5_sample_count_has_no_guards(profile):
    entry = mcs_entry(8)
    body = bytes(100)
    encoded = encode_burst_v5(body, entry, build_burst_config(profile.config), scrambler_seed=1)
    assert v5_burst_samples_with_guards(entry, len(body), 0, 0) == encoded.sample_count


# -- configuration ---------------------------------------------------------


def test_guards_and_scrambler_follow_the_phy_v5_section(profile):
    from dataclasses import replace

    config = profile.config
    v5 = replace(config.phy.v5, guard_pre_samples=0, guard_post_samples=200, scrambler=False)
    custom = build_phy_profile(replace(config, phy=replace(config.phy, v5=v5)))
    assert (custom.guard_pre_samples, custom.guard_post_samples) == (0, 200)
    (piece,) = MessageTransmitter(custom).fragment_message(b"same bytes every time")
    # Fresh transmitters, so both bursts carry sequence number 0.
    first = MessageTransmitter(custom).encode_datagrams((piece,))
    body = aggregate_body_length((piece,))
    assert first.sample_count == v5_burst_samples_with_guards(mcs_entry(8), body, 0, 200)
    assert np.any(first.samples[:16]) and not np.any(first.samples[-200:])
    # Scrambler off is seed 0, which is what the encoder gets.
    expected = encode_burst_v5(
        pack_ampdu([serialize_frame(Frame(1, FrameKind.DATA, MCS.QPSK, 0, piece.encode()))]),
        mcs_entry(8),
        build_burst_config(custom.config),
        scrambler_seed=0,
    )
    np.testing.assert_array_equal(first.samples[:-200], expected.samples)
    scrambled = MessageTransmitter(custom, scrambler=True).encode_datagrams((piece,))
    assert not np.array_equal(scrambled.samples, first.samples)


def test_aggregation_caps_come_from_mac_aggregation_in_milliseconds(profile):
    import argparse
    from dataclasses import replace

    from ofdm_message_link import options
    from ofdm_message_link.tx_app import build_parser

    config = profile.config
    section = replace(
        config.mac.aggregation,
        enabled=False,
        max_wait_ms=3.0,
        max_subframes=5,
        max_psdu_bytes=4000,
        max_burst_airtime_ms=7.5,
        min_burst_interval_ms=6.0,
    )
    custom = replace(config, mac=replace(config.mac, aggregation=section))
    limits = options.aggregation_limits(build_parser().parse_args([]), custom)
    assert limits == AggregationLimits(
        enabled=False,
        max_wait_s=0.003,
        max_subframes=5,
        max_psdu_bytes=4000,
        max_burst_airtime_s=0.0075,
        min_burst_interval_s=0.006,
    )
    # Options override the file one by one.
    overridden = options.aggregation_limits(
        build_parser().parse_args(["--aggregation", "on", "--aggregation-max-subframes", "9"]),
        custom,
    )
    assert overridden.enabled is True and overridden.max_subframes == 9
    assert overridden.max_wait_s == 0.003 and overridden.max_psdu_bytes == 4000
    # A namespace without the transmit options (the receiver's) gets the file.
    assert options.aggregation_limits(argparse.Namespace(), custom) == limits


# -- minimum burst interval ----------------------------------------------------


def test_min_burst_interval_plumbs_through_config_and_cli(profile):
    import argparse
    from dataclasses import replace

    import pytest as _pytest

    from ofdm_link.config import ConfigError
    from ofdm_message_link import options
    from ofdm_message_link.tx_app import build_parser

    config = profile.config
    assert config.mac.aggregation.min_burst_interval_ms == 4.0
    default = options.aggregation_limits(build_parser().parse_args([]), config)
    assert default.min_burst_interval_s == 0.004
    cli = options.aggregation_limits(
        build_parser().parse_args(["--min-burst-interval-ms", "0"]), config
    )
    assert cli.min_burst_interval_s == 0.0
    assert options.aggregation_limits(argparse.Namespace(), config) == default
    with _pytest.raises(SystemExit):
        build_parser().parse_args(["--min-burst-interval-ms", "51"])
    with _pytest.raises(ValueError):
        AggregationLimits(min_burst_interval_s=-0.001)
    assert AggregationLimits().min_burst_interval_s == 0.0
    # Config validation: range 0-50 ms.
    from ofdm_link import config as config_module

    for bad in (-1, 50.5, "x"):
        with _pytest.raises(ConfigError):
            config_module._aggregation_section({"aggregation": {"min_burst_interval_ms": bad}})
    section = config_module._aggregation_section({"aggregation": {"min_burst_interval_ms": 50}})
    assert section.min_burst_interval_ms == 50.0
    assert replace(section, min_burst_interval_ms=0.0).min_burst_interval_ms == 0.0


def _offer(worker, rate_hz, count, size=20):
    period = 1.0 / rate_hz
    start = time.monotonic()
    for index in range(count):
        assert worker.submit("test", bytes([index % 256]) * size)
        wait = start + (index + 1) * period - time.monotonic()
        if wait > 0:
            time.sleep(wait)


def test_min_burst_interval_caps_the_burst_rate_and_fills_the_bursts(profile):
    interval = 0.01
    limits = AggregationLimits(min_burst_interval_s=interval)
    transmitter = MessageTransmitter(profile)
    bridge = _Bridge()
    worker = TransmitWorker(transmitter, bridge, aggregation=limits)
    sink = _RecordingSink()
    worker.attach(sink)
    worker.start()
    try:
        _offer(worker, 1000.0, 300)
        assert _wait_for(
            lambda: sum(record.message_count for record in bridge.sent.values) == 300
        ), bridge.failed.values
    finally:
        worker.stop()
    assert not bridge.failed.values
    gaps = np.diff(sink.arrivals)
    # Never faster than the cap (small slack for timer granularity) ...
    assert gaps.min() >= interval * 0.9
    assert len(sink.bursts) <= 0.3 / interval + 3
    # ... and the bursts carry several datagrams each.
    assert 300 / len(sink.bursts) > 1.5


class _SlowSink(_RecordingSink):
    """Blocks in send() like a Pluto pushing a long burst (about its airtime)."""

    def __init__(self, push_s):
        super().__init__()
        self.push_s = push_s
        self.returns: list[float] = []

    def send(self, samples):
        super().send(samples)
        time.sleep(self.push_s)
        self.returns.append(time.monotonic())
        return True


def test_the_interval_runs_start_to_start_so_long_bursts_leave_no_idle_air(profile):
    # A push that blocks longer than the interval must be followed at once by
    # the next one; counting the interval from the push's return would add
    # it as idle air after every long burst (a third of the air at MCS 8).
    interval, push = 0.02, 0.05
    limits = AggregationLimits(min_burst_interval_s=interval, max_subframes=1)
    worker = TransmitWorker(MessageTransmitter(profile), _Bridge(), aggregation=limits)
    sink = _SlowSink(push)
    worker.attach(sink)
    worker.start()
    try:
        for index in range(6):
            worker.submit("test", bytes([index]) * 100)
        assert _wait_for(lambda: len(sink.returns) == 6)
    finally:
        worker.stop()
    idle = [start - end for end, start in zip(sink.returns[:-1], sink.arrivals[1:], strict=True)]
    assert max(idle) < interval * 0.5, idle


def test_a_lone_datagram_after_idle_is_not_delayed(profile):
    limits = AggregationLimits(min_burst_interval_s=0.2)
    transmitter = MessageTransmitter(profile)
    bridge = _Bridge()
    worker = TransmitWorker(transmitter, bridge, aggregation=limits)
    sink = _RecordingSink()
    worker.attach(sink)
    worker.start()
    try:
        worker.submit("test", b"first")
        assert _wait_for(lambda: len(sink.bursts) == 1)
        time.sleep(0.25)  # longer than the interval: idle
        submitted = time.monotonic()
        worker.submit("test", b"second")
        assert _wait_for(lambda: len(sink.bursts) == 2)
    finally:
        worker.stop()
    assert sink.arrivals[1] - submitted < 0.1


def test_back_to_back_datagram_waits_for_the_interval(profile):
    limits = AggregationLimits(min_burst_interval_s=0.15)
    transmitter = MessageTransmitter(profile)
    bridge = _Bridge()
    worker = TransmitWorker(transmitter, bridge, aggregation=limits)
    sink = _RecordingSink()
    worker.attach(sink)
    worker.start()
    try:
        worker.submit("test", b"first")
        assert _wait_for(lambda: len(sink.bursts) == 1)
        worker.submit("test", b"second")
        assert _wait_for(lambda: len(sink.bursts) == 2)
    finally:
        worker.stop()
    assert sink.arrivals[1] - sink.arrivals[0] >= 0.14


def test_a_full_burst_is_not_held_for_the_interval(profile):
    limits = AggregationLimits(min_burst_interval_s=0.3, max_subframes=4)
    transmitter = MessageTransmitter(profile)
    bridge = _Bridge()
    worker = TransmitWorker(transmitter, bridge, aggregation=limits)
    sink = _RecordingSink()
    worker.attach(sink)
    worker.start()
    try:
        worker.submit("test", b"first")
        assert _wait_for(lambda: len(sink.bursts) == 1)
        for index in range(4):  # exactly max_subframes: the burst is full
            worker.submit("test", bytes([index]) * 20)
        assert _wait_for(lambda: len(sink.bursts) == 2)
        queued = time.monotonic()
    finally:
        worker.stop()
    assert queued - sink.arrivals[0] < 0.2
    assert [record.subframe_count for record in bridge.sent.values] == [1, 4]


def test_interval_zero_keeps_the_opportunistic_behaviour(profile):
    messages = [bytes([index]) * 50 for index in range(10)]
    bridge, sink = _run_worker(profile, messages, AggregationLimits(min_burst_interval_s=0.0))
    # Queued before the radio started: one burst, as before the interval existed.
    assert [record.subframe_count for record in bridge.sent.values] == [10]
    _, sink = _run_worker(
        profile,
        messages[:3],
        AggregationLimits(min_burst_interval_s=0.0),
        attach_first=True,
        pause_s=0.3,
    )
    assert len(sink.bursts) == 3
