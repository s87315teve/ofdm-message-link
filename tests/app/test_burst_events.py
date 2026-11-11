"""Per-burst receive events (``node_app --events``): recorder, tap, writer and the engine."""

from __future__ import annotations

import json
import socket
import time

import numpy as np
import pytest
from stream_fixtures import profile as build_profile
from stream_fixtures import v5_stream

from ofdm_link.phy.streaming import link_snr_db  # noqa: F401  (the SNR the events repeat)
from ofdm_link.phy.sync_v5 import V5_FIXED_LENGTH, V5_PREAMBLE_LENGTH
from ofdm_message_link import node_options
from ofdm_message_link.burst_events import (
    BURST_FIELDS,
    CLIPPING_LEVEL,
    EVENTS_SCHEMA_VERSION,
    BurstEvent,
    BurstEventRecorder,
    BurstEventTap,
    EventLogWriter,
    read_events,
)
from ofdm_message_link.link import MessageReceiver
from ofdm_message_link.transport import RxTimeGapCounter

RATE = 5e6


@pytest.fixture(scope="module")
def profile():
    return build_profile()


@pytest.fixture(scope="module")
def stream(profile):
    # Scaled into a radio's full scale (the fixture's OFDM has unit power).
    samples, payloads = v5_stream(profile, bursts=10, snr_db=22.0)
    return (samples * 0.2).astype(np.complex64), payloads


def _record(profile, samples, *, workers=1, chunk=4096, sample_rate=RATE, timeout_s=1.0):
    """Feed ``samples`` through a receiver with events on; (events, locks, payloads, snrs)."""

    receiver = MessageReceiver(profile, decode_workers=workers)
    recorder = BurstEventRecorder(sample_rate, lock_timeout_s=timeout_s)
    receiver.enable_burst_events(recorder)
    events, locks, delivered, snrs = [], [], [], []
    try:
        for start in range(0, samples.size, chunk):
            delivered.extend(receiver.feed(samples[start : start + chunk]))
            snrs.extend(receiver.drain_link_snr_db())
            got, lock = recorder.drain()
            events.extend(got)
            locks.extend(lock)
        delivered.extend(receiver.flush(30.0))
        recorder.flush()
        got, lock = recorder.drain()
        events.extend(got)
        locks.extend(lock)
    finally:
        receiver.close()
    events.sort(key=lambda event: event.off)
    return events, locks, [m.message.payload for m in delivered], snrs


def test_every_preamble_is_one_event_paired_with_its_outcome(profile, stream):
    samples, payloads = stream
    events, locks, delivered, snrs = _record(profile, samples)
    assert delivered == payloads
    # One event per physical preamble, with the same SNR the link_snr events carry.
    assert len(events) == len(snrs) == len(payloads)
    assert [e.snr_db for e in events] == pytest.approx(snrs)
    offsets = [e.off for e in events]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    for event in events:
        assert event.hdr == "ok" and event.pl == "ok" and event.kind == "data"
        assert event.fcs is True and event.sf_ok == 1 and event.sf_bad == 0
        assert event.mcs in (8, 12, 16)
        assert event.noise_var > 0 and event.signal_power > 0
        # snr = signal power / noise variance, scaled to the payload samples.
        assert 10 * np.log10(event.signal_power / event.noise_var) > event.snr_db
        assert 0 < event.peak < CLIPPING_LEVEL and event.clip is False
        assert event.dev_t is None  # no device-time anchors given
    assert [e.mcs for e in events] == [(8, 12, 16)[i % 3] for i in range(len(events))]
    assert [lock[0] for lock in locks] == ["acquired"]


def test_pool_and_inline_decoding_give_the_same_events(profile, stream):
    samples, _ = stream
    inline, _, _, _ = _record(profile, samples, workers=1)
    pooled, _, _, _ = _record(profile, samples, workers=2)
    strip = [(e.off, e.snr_db, e.hdr, e.pl, e.kind, e.mcs, e.fcs) for e in inline]
    assert strip == [(e.off, e.snr_db, e.hdr, e.pl, e.kind, e.mcs, e.fcs) for e in pooled]


def test_damaged_header_payload_and_a_clipped_burst(profile, stream):
    samples, _ = stream
    clean, _, _, _ = _record(profile, samples)
    damaged = samples.copy()
    rng = np.random.default_rng(3)

    def smash(start, stop):
        damaged[start:stop] = (rng.normal(size=stop - start) + 1j * rng.normal(size=stop - start)
                               ).astype(np.complex64) * 0.06

    header, payload, loud = clean[2].off, clean[5].off, clean[7].off
    smash(header + V5_PREAMBLE_LENGTH, header + V5_FIXED_LENGTH)
    # The middle of the payload: past the SIG, short of the next preamble.
    end = clean[6].off
    smash(payload + V5_FIXED_LENGTH + 200, min(payload + V5_FIXED_LENGTH + 2000, end - 64))
    damaged[loud : clean[8].off - 64] *= 4.0
    events, _, _, _ = _record(profile, damaged)
    by_off = {e.off: e for e in events}
    assert by_off[header].hdr in ("fail", "invalid") and by_off[header].pl is None
    assert by_off[payload].hdr == "ok" and by_off[payload].pl in ("fail", "fcs_fail")
    assert by_off[loud].clip is True and by_off[loud].peak >= CLIPPING_LEVEL
    untouched = [e for e in events if e.off not in (header, payload, loud)]
    assert untouched and all(e.pl == "ok" for e in untouched)


def test_off_by_default_and_delivery_is_unchanged(profile, stream):
    samples, payloads = stream
    receiver = MessageReceiver(profile)
    try:
        assert receiver._burst_recorder is None
        assert receiver._decoder.burst_observer is None
        out = []
        for start in range(0, samples.size, 4096):
            out.extend(receiver.feed(samples[start : start + 4096]))
    finally:
        receiver.close()
    assert [m.message.payload for m in out] == payloads
    _, _, with_events, _ = _record(profile, samples)
    assert with_events == payloads


def test_lock_is_lost_after_the_timeout_and_regained_at_the_next_header(profile, stream):
    samples, _ = stream
    rate = 1e5  # a fake rate, so that the 0.5 s timeout is 50 000 samples
    silence = np.zeros(120_000, dtype=np.complex64)
    events, locks, _, _ = _record(profile, np.concatenate([samples, silence, samples]),
                                  sample_rate=rate, timeout_s=0.5)
    states = [lock[0] for lock in locks]
    assert states == ["acquired", "lost", "acquired"]
    last_before = max(e.off for e in events if e.off < samples.size)
    assert locks[1][1] == last_before + 50_000
    assert locks[2][1] == min(e.off for e in events if e.off >= samples.size)


def _peer_burst(recorder, off):
    """One acquired preamble whose header decoded and whose payload carried our data."""

    recorder.acquired(off, 20.0, 1e-3, 0.1)
    recorder.header_ok()
    recorder.payload(None, True)
    recorder.link(off, "data", 16, 1, 1, 0, 0, True)


def _run_feeds(recorder, feeds, anchors_at):
    """Feed (offset, count, burst offsets) chunks; ``anchors_at(offset)`` gives the anchors
    the receive stream has once that chunk arrived.  Returns every lock transition."""

    locks = []
    for offset, count, bursts in feeds:
        recorder.on_feed(offset, count)
        for off in bursts:
            _peer_burst(recorder, off)
        locks.extend(recorder.drain(anchors_at(offset))[1])
    return locks


_LOCK_RATE = 1e5  # a fake rate: the 0.5 s timeout is 50 000 samples


def _steady_feeds(until):
    """10 000-sample chunks, one peer burst at the start of each."""

    return [(off, 10_000, (off,)) for off in range(0, until, 10_000)]


def test_lock_without_overflow_is_the_same_with_or_without_device_time():
    # A peer that stops: 'lost' exactly timeout after its last header, in both clocks.
    feeds = _steady_feeds(110_000) + [(off, 10_000, ()) for off in range(110_000, 200_000, 10_000)]
    feeds += [(200_000, 10_000, (203_000,))]
    plain = BurstEventRecorder(_LOCK_RATE, lock_timeout_s=0.5)
    timed = BurstEventRecorder(_LOCK_RATE, lock_timeout_s=0.5)
    without = _run_feeds(plain, feeds, lambda off: [])
    with_anchor = _run_feeds(timed, feeds, lambda off: [(0, 10.0)])
    assert [lock[:2] for lock in without] == [("acquired", 0), ("lost", 150_000),
                                               ("acquired", 203_000)]
    assert [lock[:2] for lock in with_anchor] == [lock[:2] for lock in without]
    # The device time of each transition rides along when there is one.
    assert [lock[4] for lock in without] == [None, None, None]
    assert [lock[4] for lock in with_anchor] == pytest.approx([10.0, 11.5, 12.03])


@pytest.mark.parametrize("resumes_in_gap_chunk", [False, True])
def test_a_receive_overflow_longer_than_the_timeout_is_a_lost_lock(resumes_in_gap_chunk):
    # The receiver's own overflow loses 2 s of samples between offsets 109 999 and
    # 110 000: the stream offsets run on, but the device clock jumps.  The peer never
    # stopped, so its bursts carry on the moment samples flow again.
    before = [(0, 10.0)]
    after = [(0, 10.0), (110_000, 13.1)]  # 1.1 s of samples, then 2.0 s lost
    feeds = _steady_feeds(110_000)
    first_after = 110_000 + (2_000 if resumes_in_gap_chunk else 10_000)
    feeds += [(110_000, 10_000, (first_after,) if resumes_in_gap_chunk else ())]
    feeds += [(120_000, 10_000, (120_000,))]
    recorder = BurstEventRecorder(_LOCK_RATE, lock_timeout_s=0.5)
    locks = _run_feeds(recorder, feeds, lambda off: after if off >= 110_000 else before)
    assert [lock[0] for lock in locks] == ["acquired", "lost", "acquired"]
    lost, regained = locks[1], locks[2]
    # Lost 0.5 s of device time after the last header (100 000 -> 11.0 s), which falls
    # inside the gap: the first sample after the gap is where the stream knows it.
    assert lost[1] == 110_000
    assert lost[4] == pytest.approx(11.5)
    assert regained[1] == first_after
    assert regained[4] == pytest.approx(13.1 + (first_after - 110_000) / _LOCK_RATE)


def test_a_short_receive_overflow_is_not_a_lost_lock():
    after = [(0, 10.0), (110_000, 11.3)]  # 0.2 s lost: under the 0.5 s timeout
    feeds = _steady_feeds(110_000) + [(110_000, 10_000, (110_000,))]
    recorder = BurstEventRecorder(_LOCK_RATE, lock_timeout_s=0.5)
    locks = _run_feeds(recorder, feeds,
                       lambda off: after if off >= 110_000 else [(0, 10.0)])
    assert [lock[0] for lock in locks] == ["acquired"]


def test_device_time_comes_from_the_last_anchor_at_or_before_the_burst():
    recorder = BurstEventRecorder(RATE)
    for off in (1000, 3_000_000):
        recorder.acquired(off, 20.0, 1e-3, 0.1)
        recorder.header_failed("fail", 0.5)
    recorder.flush()
    events, _ = recorder.drain([(0, 100.0), (2_000_000, 100.5)])
    assert events[0].dev_t == pytest.approx(100.0 + 1000 / RATE)
    # After an overflow the stream re-anchors: 2e6 samples later is 0.5 s, not 0.4 s.
    assert events[1].dev_t == pytest.approx(100.5 + 1_000_000 / RATE)


def test_a_header_failure_is_final_only_once_the_next_preamble_arrives():
    recorder = BurstEventRecorder(RATE)
    recorder.acquired(500, 10.0, 1e-2, 0.1)
    recorder.header_failed("fail", 0.2)
    assert recorder.drain() == ([], [])
    # The same preamble found again a few samples on and decoded this time.
    recorder.header_ok()
    recorder.payload(None, False)
    events, _ = recorder.drain()
    assert [(e.off, e.hdr, e.pl) for e in events] == [(500, "ok", "ok")]


def test_pool_drops_are_recorded_as_dropped():
    recorder = BurstEventRecorder(RATE)
    for off in (100, 9000, 20000):
        recorder.acquired(off, 15.0, 1e-2, 0.1)
        recorder.header_ok()
        recorder.segment(off, 0.4)
    recorder.payload(None, False)
    recorder.dropped(2)
    events, _ = recorder.drain()
    assert [(e.off, e.pl) for e in events] == [(100, "ok"), (9000, "dropped"), (20000, "dropped")]


class _Source:
    def __init__(self, anchors):
        self.anchors = anchors

    def time_anchors(self):
        return len(self.anchors), tuple(self.anchors)


class _Inner:
    def __init__(self):
        self.calls = 0

    def on_receive(self, now, **kwargs):
        self.calls += 1


def test_tap_forwards_then_emits_numbered_batches_with_anchors_and_overflows():
    recorder = BurstEventRecorder(RATE)
    source = _Source([(0, 50.0, 0)])
    inner, out = _Inner(), []
    tap = BurstEventTap(inner, recorder, lambda kind, value: out.append((kind, value)),
                        lambda: source)
    recorder.acquired(250, 12.0, 1e-2, 0.1)
    recorder.header_ok()
    recorder.payload(None, False)
    tap.on_receive(0.0, snrs=(), bursts=(), health=None)
    source.anchors.append((10_000, 50.01, 5000))
    tap.on_receive(0.0, snrs=(), bursts=(), health=None)
    tap.on_receive(0.0, snrs=(), bursts=(), health=None)  # nothing new: no batch
    assert inner.calls == 3
    assert [value["seq"] for _, value in out] == [0, 1]
    first, second = out[0][1], out[1][1]
    assert first["anchors"] == [(0, 50.0)]
    event = BurstEvent(*first["events"][0])
    assert event.dev_t == pytest.approx(50.0 + 250 / RATE)
    assert second["overflows"] == [(10_000, 50.01, 5000)]


def test_tap_never_raises_into_the_receive_thread():
    recorder = BurstEventRecorder(RATE)

    def broken():
        raise RuntimeError("no source")

    tap = BurstEventTap(None, recorder, lambda kind, value: None, broken)
    tap.on_receive(0.0)
    assert tap.errors == 1


def test_rx_time_gap_counter_keeps_anchors_with_the_samples_lost():
    counter = RxTimeGapCounter(1000.0)
    counter.observe(0, 10.0)
    counter.observe(500, 10.5)        # on time: nothing lost
    counter.observe(1000, 11.2)       # 0.2 s ahead: 200 samples lost
    total, anchors = counter.anchors()
    assert total == 3
    assert anchors == ((0, 10.0, 0), (500, 10.5, 0), (1000, 11.2, 200))
    assert counter.samples_lost == 200 and counter.discontinuities == 1


def _stats_line(*, tx_freq=2.42e9, rx_freq=2.46e9, overflow=0, mono=1.0):
    return {
        "monotonic_s": mono, "wall_time": 1000.0 + mono, "elapsed_s": mono,
        "tx": {"bursts": 10, "control_bursts": 2, "subframes": 30, "payload_bytes": 9000,
               "active_mcs": 16, "bandwidth_hz": 25e6,
               "transport": {"center_frequency_hz": tx_freq, "tx_gain_db": 80.0}},
        "rx": {"decoder": {"header_decode_success": 8}, "receiver": {"psdus_received": 8},
               "bandwidth_hz": 5e6,
               "transport": {"center_frequency_hz": rx_freq, "rx_gain_db": 30.0,
                             "rx_samples_lost": 0, "rx_time_discontinuities": 0,
                             "uhd_faults": {"rx_overflow": overflow}}},
        "la": {"mode": "fixed", "mcs_index": 16},
        "dropped_events": 0,
    }


def test_writer_lines_round_trip_and_mark_lost_batches(tmp_path):
    path = tmp_path / "node2_events.jsonl"
    writer = EventLogWriter(str(path), node_id=2, sample_rate=RATE)
    writer.start({"transport": "uhd"})
    row = tuple(BurstEvent(800, 12.5, 3.0, 1003.0, 25.1234, 1e-4, 0.03, 0.5, False, "ok", "ok",
                           "data", 16, 7, 12, 0, 0, True))
    writer.on_event("bursts", {"seq": 0, "events": [row],
                               "locks": [("acquired", 800, 3.0, 1003.0, 12.5)],
                               "anchors": [(0, 12.5)], "overflows": []})
    writer.on_event("bursts", {"seq": 3, "events": [], "locks": [], "anchors": [],
                               "overflows": [(5000, 12.6, 40)]})
    writer.on_event("gain", {"dir": "tx", "db": 0.0, "readback": 0.0, "t": 4.0})
    writer.on_event("stats", {"tx": {"transport": {"readback": {"gain_db": 80.0}}},
                              "rx": {"transport": {"readback": {"gain_db": 30.0}}}})
    writer.status(_stats_line(mono=1.0))
    writer.status(_stats_line(mono=1.5, rx_freq=2.47e9, overflow=2))
    writer.on_event("message", object())  # not written
    writer.stop()
    rows = read_events(str(path))
    kinds = [r["k"] for r in rows]
    assert kinds[0] == "start" and kinds[-1] == "stop"
    assert rows[0]["version"] == EVENTS_SCHEMA_VERSION
    assert rows[0]["burst_fields"] == list(BURST_FIELDS)
    burst = next(r for r in rows if r["k"] == "burst")
    assert (burst["off"], burst["dev_t"], burst["mono"], burst["wall"]) == (800, 12.5, 3.0, 1003.0)
    assert burst["snr_db"] == 25.123 and burst["kind"] == "data" and burst["fcs"] is True
    gap = next(r for r in rows if r["k"] == "gap")
    assert gap["lost_batches"] == 2
    lock = next(r for r in rows if r["k"] == "lock")
    assert lock["stream_t"] == pytest.approx(800 / RATE) and lock["dev_t"] == 12.5
    assert next(r for r in rows if r["k"] == "gain")["mono"] == 4.0
    assert next(r for r in rows if r["k"] == "readback")["rx"] == {"gain_db": 30.0}
    assert next(r for r in rows if r["k"] == "radio")["rx_freq_hz"] == 2.46e9
    assert next(r for r in rows if r["k"] == "freq") == pytest.approx(
        {**next(r for r in rows if r["k"] == "freq"), "rx_freq_hz": 2.47e9})
    overflows = [r for r in rows if r["k"] == "overflow"]
    assert [o["source"] for o in overflows] == ["rx_time", "counter"]
    assert overflows[1]["delta"] == 2
    assert sum(1 for r in rows if r["k"] == "status") == 2
    assert rows[-1]["lost_batches"] == 2
    # A writer killed mid-line leaves a torn last line; readers skip it.
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"k":"burst","off":')
    assert len(read_events(str(path))) == len(rows)
    assert len(read_events(str(path), kinds=["burst"])) == 1


def test_events_option_needs_headless_and_an_existing_directory(tmp_path):
    with pytest.raises(SystemExit):
        node_options.parse_args(["--node-id", "1", "--transport", "udp", "--events", "x.jsonl"])
    args = node_options.parse_args(["--node-id", "1", "--transport", "udp", "--headless",
                                    "--events", str(tmp_path / "missing" / "e.jsonl")])
    assert args.events_lock_timeout == 1.0
    with pytest.raises(SystemExit):
        node_options.check_paths(args)


def _free_ports(count):
    probes = []
    for _ in range(count):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        probes.append(probe)
    ports = [probe.getsockname()[1] for probe in probes]
    for probe in probes:
        probe.close()
    return ports


def test_two_udp_engines_write_burst_events(tmp_path):
    from ofdm_message_link.link import build_phy_profile
    from ofdm_message_link.node_app import NodeMonitor
    from ofdm_message_link.node_engine import NodeSpec, ProcessNodeWorker, UdpDuplexFactory
    from ofdm_message_link.options import load_link_config

    config = load_link_config("configs/default.yaml", [node_options.DEFAULT_NODE_OVERLAY])
    duplex = build_phy_profile(config)
    ingress1, egress1, samples1, ingress2, egress2, samples2 = _free_ports(6)
    plan = {1: (ingress1, egress1, samples1, samples2), 2: (ingress2, egress2, samples2, samples1)}
    workers, writers, monitors = {}, {}, {}
    try:
        for node_id, (ingress, out, _listen, _peer) in plan.items():
            spec = NodeSpec(node_id=node_id, peer_id=node_options.peer_id(node_id), mcs_index=12,
                            control_mcs_index=8, seed_range=node_options.SEED_RANGES[node_id],
                            ingress_port=ingress, egress=("127.0.0.1", out), burst_events=True)
            writers[node_id] = EventLogWriter(str(tmp_path / f"node{node_id}_events.jsonl"),
                                              node_id=node_id, sample_rate=duplex.sample_rate)
            monitors[node_id] = NodeMonitor(node_id, "udp", duplex.sample_rate)

            def listener(kind, value, _m=monitors[node_id], _w=writers[node_id]):
                _m.on_event(kind, value)
                _w.on_event(kind, value)

            workers[node_id] = ProcessNodeWorker(duplex, spec, listener, decode_workers=1)
            workers[node_id].start()
        for node_id, (_, _, listen, peer) in plan.items():
            workers[node_id].attach_from(
                UdpDuplexFactory(host="127.0.0.1", tx_port=peer, rx_port=listen))
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        started = time.monotonic()
        while time.monotonic() - started < 2.0:
            for ingress, *_ in plan.values():
                sender.sendto(b"z" * 500, ("127.0.0.1", ingress))
            time.sleep(0.02)
        sender.close()
        time.sleep(1.0)
        for node_id in plan:
            writers[node_id].status(monitors[node_id].line())
    finally:
        for worker in workers.values():
            try:
                worker.detach()
            finally:
                worker.stop()
        for writer in writers.values():
            writer.stop()
    for node_id in plan:
        rows = read_events(str(tmp_path / f"node{node_id}_events.jsonl"))
        bursts = [r for r in rows if r["k"] == "burst"]
        data = [r for r in bursts if r["kind"] == "data"]
        assert len(data) >= 50, f"node {node_id}: {len(data)} data bursts"
        assert all(r["hdr"] == "ok" and r["pl"] == "ok" and r["mcs"] == 12 for r in data)
        assert any(r["kind"] == "control" for r in bursts)
        assert all(r["snr_db"] is not None and r["noise_var"] > 0 for r in bursts)
        assert [r["state"] for r in rows if r["k"] == "lock"][0] == "acquired"
        assert not any(r["k"] == "gap" for r in rows)
        assert json.loads(json.dumps(rows[-1]))["k"] == "stop"
