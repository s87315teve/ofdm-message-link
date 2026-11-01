"""The receive worker's ReceiveObserver hook."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from ofdm_message_link.control import link_header
from ofdm_message_link.la.api import BurstReport, RxHealth
from ofdm_message_link.link import (
    MessageReceiver,
    MessageTransmitter,
    build_phy_profile,
    load_link_config,
)
from ofdm_message_link.rx_app import ReceiveWorker

CONFIG = "configs/default.yaml"


@pytest.fixture(scope="module")
def profile():
    return build_phy_profile(load_link_config(CONFIG))


class _Signal:
    def __init__(self):
        self.values = []

    def emit(self, value):
        self.values.append(value)


class _Bridge:
    def __init__(self):
        self.message = _Signal()
        self.level = _Signal()
        self.failed = _Signal()
        self.link_snr = _Signal()


class _Source:
    """Hands out prepared chunks once, then reports nothing more."""

    description = "test source"

    def __init__(self, chunks, overflows=0):
        self._chunks = list(chunks)
        self._lock = threading.Lock()
        self.overflows = overflows

    def start(self):
        pass

    def stop(self):
        pass

    def recv(self, timeout):
        with self._lock:
            if self._chunks:
                return self._chunks.pop(0)
        time.sleep(0.01)
        return None

    def snapshot(self):
        return {"rx_overflow_events": self.overflows, "chunks_dropped_overflow": 1}


class _Observer:
    def __init__(self):
        self.calls = []

    def on_receive(self, now, *, snrs, bursts, health):
        self.calls.append((now, snrs, bursts, health))


def _awgn(samples, snr_db, seed):
    rng = np.random.default_rng(seed)
    power = float(np.mean(np.abs(samples) ** 2))
    variance = power / (10.0 ** (snr_db / 10.0))
    noise = rng.normal(scale=np.sqrt(variance / 2.0), size=(samples.size, 2))
    return (samples + (noise[:, 0] + 1j * noise[:, 1])).astype(np.complex64)


def _chunks(profile, *, chunk=8192):
    transmitter = MessageTransmitter(profile)
    bursts = []
    for index in range(3):
        pieces = [transmitter.fragment_message(bytes([index]) * 100)[0] for _ in range(2)]
        bursts.append(transmitter.encode_datagrams(pieces, control_payloads=[link_header(2)]))
    bursts.append(transmitter.encode_datagrams((), control_payloads=[link_header(2)]))
    blocks = []
    for burst in bursts:
        blocks += [np.zeros(700, np.complex64), burst.samples]
    blocks.append(np.zeros(3 * chunk, np.complex64))
    stream = _awgn(np.concatenate(blocks), 20.0, seed=9) * np.float32(0.3)
    return [stream[start : start + chunk] for start in range(0, stream.size, chunk)]


def _run(worker, source, until, timeout_s=4.0):
    worker.start()
    worker.attach(source)
    deadline = time.monotonic() + timeout_s
    try:
        while not until() and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()


def test_the_observer_sees_every_preamble_burst_report_and_health(profile):
    chunks = _chunks(profile)
    observer = _Observer()
    bridge = _Bridge()
    worker = ReceiveWorker(MessageReceiver(profile), bridge, observer=observer)
    _run(worker, _Source(chunks, overflows=2), lambda: len(observer.calls) >= len(chunks))

    assert not bridge.failed.values
    assert len(observer.calls) == len(chunks)  # once per chunk
    snrs = [snr for _, snrs, _, _ in observer.calls for snr in snrs]
    bursts = [burst for _, _, reports, _ in observer.calls for burst in reports]
    assert len(snrs) == 4  # three data bursts and the control-only one
    assert all(snr > 15.0 for snr in snrs)  # noise was scaled to the whole stream
    assert all(isinstance(burst, BurstReport) for burst in bursts)
    assert [(b.first_sequence, b.subframes_ok, b.subframes_lost) for b in bursts] == [
        (0, 2, 0),
        (2, 2, 0),
        (4, 2, 0),
    ]
    times = [now for now, *_ in observer.calls]
    assert times == sorted(times)
    health = observer.calls[-1][3]
    assert isinstance(health, RxHealth)
    assert health.overflow_total == 3
    assert health.header_fail_total == 0 and health.fcs_fail_total == 0
    assert health.clipping_seen is False
    assert health.decode_backlog is False
    assert sum(len(item) for item in bridge.link_snr.values) == 4  # the window still gets them
    assert len(bridge.message.values) == 6


def test_clipping_is_reported_for_the_chunk_that_clipped(profile):
    quiet = np.zeros(4096, np.complex64)
    loud = quiet.copy()
    loud[100] = 0.95 + 0.0j
    loud_q = quiet.copy()
    loud_q[7] = 0.1 - 0.94j
    observer = _Observer()
    worker = ReceiveWorker(MessageReceiver(profile), _Bridge(), observer=observer)
    _run(worker, _Source([quiet, loud, quiet, loud_q]), lambda: len(observer.calls) >= 4)
    assert [call[3].clipping_seen for call in observer.calls] == [False, True, False, True]


def test_without_an_observer_burst_reports_are_left_alone(profile):
    chunks = _chunks(profile)
    receiver = MessageReceiver(profile)
    bridge = _Bridge()
    worker = ReceiveWorker(receiver, bridge)
    source = _Source(chunks)
    _run(worker, source, lambda: len(bridge.message.values) >= 6 and not source._chunks)
    assert len(bridge.message.values) == 6
    # Nobody drained them; the receiver's bounded queue still holds them.
    assert len(receiver.drain_burst_reports()) == 3


def test_an_observer_error_is_surfaced_and_the_worker_keeps_going(profile):
    class Broken:
        calls = 0

        def on_receive(self, now, *, snrs, bursts, health):
            Broken.calls += 1
            raise RuntimeError("observer bug")

    bridge = _Bridge()
    worker = ReceiveWorker(MessageReceiver(profile), bridge, observer=Broken())
    chunks = [np.zeros(1024, np.complex64)] * 3
    _run(worker, _Source(chunks), lambda: Broken.calls >= 3)
    assert Broken.calls == 3
    assert bridge.failed.values == ["observer RuntimeError: observer bug"] * 3


def test_decode_backlog_follows_the_pool(profile):
    decoder_snapshot = MessageReceiver(profile).decoder_snapshot()

    class PoolReceiver:
        """Just enough of a MessageReceiver for the health computation."""

        def __init__(self, snapshots):
            self._snapshots = list(snapshots)

        def feed(self, chunk):
            return ()

        def drain_observations(self):
            return ()

        def drain_link_snr_db(self):
            return ()

        def drain_burst_reports(self):
            return ()

        def decoder_snapshot(self):
            return decoder_snapshot

        def decode_pool_snapshot(self):
            from ofdm_link.runtime.segment_decode_pool import SegmentDecodePoolSnapshot

            in_flight, waits = self._snapshots.pop(0)
            return SegmentDecodePoolSnapshot(
                workers=2,
                max_in_flight=4,
                in_flight=in_flight,
                submitted=0,
                completed=0,
                backpressure_waits=waits,
            )

    observer = _Observer()
    receiver = PoolReceiver([(0, 0), (4, 0), (1, 2), (1, 2)])
    worker = ReceiveWorker(receiver, _Bridge(), observer=observer)
    chunks = [np.zeros(256, np.complex64)] * 4
    _run(worker, _Source(chunks), lambda: len(observer.calls) >= 4)
    assert [call[3].decode_backlog for call in observer.calls] == [False, True, True, False]


def test_own_leakage_never_reaches_the_snr_reported_to_the_peer(profile):
    from la_fakes import LADDER, context

    from ofdm_link.phy.mcs_table import mcs_entry
    from ofdm_message_link.la.api import ReporterContext
    from ofdm_message_link.la.controller import LinkController
    from ofdm_message_link.la.olla import OllaLinkAdaptation
    from ofdm_message_link.la.reporter import DefaultReporter

    reporter = DefaultReporter(ReporterContext(1, 2))
    controller = LinkController(
        node_id=1, peer_id=2, la=OllaLinkAdaptation(context()), reporter=reporter,
        ladder=LADDER, control_entry=mcs_entry(8),
    )
    own = MessageTransmitter(profile, seed_range=(1, 63))
    peer = MessageTransmitter(profile, seed_range=(64, 127))
    loud = own.encode_datagrams(own.fragment_message(b"own")[:1],
                                control_payloads=[link_header(1, 2)])
    weak = peer.encode_datagrams(peer.fragment_message(b"peer")[:1],
                                 control_payloads=[link_header(2, 1)])
    gap = np.zeros(3000, np.complex64)
    stream = np.concatenate([gap, loud.samples, gap, weak.samples * np.float32(0.1), gap,
                             np.zeros(3 * 8192, np.complex64)])
    stream = (stream + 0.002 * np.random.default_rng(3).standard_normal(stream.size)).astype(
        np.complex64
    )
    chunks = [stream[start : start + 8192] for start in range(0, stream.size, 8192)]
    receiver = MessageReceiver(profile, control_handler=controller)
    bridge = _Bridge()
    worker = ReceiveWorker(receiver, bridge, observer=controller)
    source = _Source(chunks)
    _run(worker, source, lambda: len(bridge.message.values) >= 1 and not source._chunks)
    time.sleep(0.05)

    acquired = [snr for item in bridge.link_snr.values for snr in item]
    assert len(acquired) == 2  # the window still shows every preamble
    report = reporter.build(1.0)
    assert receiver.stats.dropped_bursts == 1
    assert report.preambles == 1  # the peer's burst only
    assert report.snr_mean_db == pytest.approx(min(acquired), abs=0.01)
    assert report.snr_mean_db < max(acquired) - 10.0
