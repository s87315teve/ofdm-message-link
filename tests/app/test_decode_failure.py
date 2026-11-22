"""A dead decode worker ends decoding once, loudly, and loses nothing already decoded."""

from __future__ import annotations

import functools
import os
import signal
import threading
import time

import numpy as np
import pytest
from stream_fixtures import MEASUREMENT, profile, v5_stream
from test_rx_chunk_merge import _Bridge, _QueueSource

from ofdm_link.runtime.segment_decode_pool import (
    DecodePoolClosed,
    DecodePoolError,
    SegmentDecodePool,
)
from ofdm_message_link.burst_events import EventLogWriter, read_events
from ofdm_message_link.link import MessageReceiver, burst_quality
from ofdm_message_link.rx_app import ReceiveWorker

# v5_stream cycles these; a burst at MCS 12 is the one that kills its worker.
_MCS = (8, 12, 16)
_KILLER_MCS = 12


def _annotate_kill_after(delay_s: float, burst):
    """In a worker: the MCS 12 bursts die late, so the others finish first."""

    if burst.mcs_index == _KILLER_MCS:
        time.sleep(delay_s)
        os.kill(os.getpid(), signal.SIGKILL)
    return burst_quality(burst)


def _annotate_slowly(delay_s: float, burst):
    time.sleep(delay_s)
    return burst_quality(burst)


def _receiver(link, annotate) -> MessageReceiver:
    receiver = MessageReceiver(link, decode_workers=2)
    receiver._pool.close()
    receiver._pool = SegmentDecodePool(link.config, workers=2, annotate=annotate, max_batch=1)
    return receiver


class _Streaming(_QueueSource):
    """Like a radio: idle samples keep coming after the stream."""

    def __init__(self, chunks):
        super().__init__(chunks)
        self.received = 0

    def recv(self, timeout):
        chunk = super().recv(0.0)
        if chunk is None:
            time.sleep(0.002)
            chunk = np.zeros(4096, np.complex64)
        self.received += 1
        return chunk


class _Stream(_QueueSource):
    """The stream, then nothing."""

    def recv(self, timeout):
        chunk = super().recv(0.0)
        if chunk is None:
            time.sleep(min(timeout, 0.01))
        return chunk


def _chunks(stream, size=8192):
    return [stream[start : start + size] for start in range(0, stream.size, size)]


def _wait_for(condition, timeout=60.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def test_what_was_decoded_before_a_worker_died_is_delivered_in_order():
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=89, bursts=9, mcs_indices=_MCS)
    receiver = _receiver(link, functools.partial(_annotate_kill_after, 0.5))
    delivered = []
    try:
        with pytest.raises(DecodePoolError) as caught:
            delivered.extend(receiver.feed(stream))
            delivered.extend(receiver.flush(30.0))
        delivered.extend(caught.value.delivered)
        pool = receiver.decode_pool_snapshot()
    finally:
        receiver.close()
    got = [message.message.payload for message in delivered]
    # The first burst, and those behind the task that died, which finished first.
    assert got[:3] == [payloads[0], payloads[2], payloads[3]]
    positions = [payloads.index(payload) for payload in got]
    assert positions == sorted(set(positions))
    assert all(_MCS[position % 3] != _KILLER_MCS for position in positions)
    # Every segment that was in flight got its one result.
    assert pool.in_flight == 0 and pool.completed == pool.submitted


def test_a_dead_worker_is_reported_once_and_decoding_stops():
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=97, bursts=9, mcs_indices=_MCS)
    receiver = _receiver(link, functools.partial(_annotate_kill_after, 0.3))
    feeds = []
    real_feed = receiver.feed

    def counting_feed(samples, **kwargs):
        feeds.append(1)
        return real_feed(samples, **kwargs)

    receiver.feed = counting_feed
    bridge = _Bridge()
    worker = ReceiveWorker(receiver, bridge)
    source = _Streaming(_chunks(stream))
    worker.start()
    worker.attach(source)
    try:
        _wait_for(lambda: worker.decode_failure is not None)
        fed = len(feeds)
        seen = source.received
        # Samples keep being taken from the transport, and none is decoded.
        _wait_for(lambda: source.received > seen + 20)
        assert len(feeds) == fed
    finally:
        worker.stop()
    assert "worker died" in worker.decode_failure
    assert len(bridge.failed.values) == 1
    assert bridge.failed.values[0].startswith("decoding stopped")
    got = [message.message.payload for message in bridge.message.values]
    positions = [payloads.index(payload) for payload in got]
    assert positions and positions == sorted(set(positions))


def test_stop_delivers_the_bursts_still_being_decoded():
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=101, bursts=8, mcs_indices=_MCS)
    receiver = _receiver(link, functools.partial(_annotate_slowly, 0.08))
    bridge = _Bridge()
    worker = ReceiveWorker(receiver, bridge)
    worker.start()
    worker.attach(_Stream(_chunks(stream)))
    _wait_for(lambda: receiver.stats.samples_consumed >= stream.size)
    worker.stop()
    assert [m.message.payload for m in bridge.message.values] == payloads
    assert bridge.failed.values == []


def test_detach_delivers_the_bursts_still_being_decoded():
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=103, bursts=8, mcs_indices=_MCS)
    receiver = _receiver(link, functools.partial(_annotate_slowly, 0.08))
    bridge = _Bridge()
    worker = ReceiveWorker(receiver, bridge)
    worker.start()
    worker.attach(_Stream(_chunks(stream)))
    try:
        _wait_for(lambda: receiver.stats.samples_consumed >= stream.size)
        worker.detach()
        # Already there when detach returns: --events writes its last records next.
        assert [m.message.payload for m in bridge.message.values] == payloads
    finally:
        worker.stop()


def test_a_stop_under_a_feed_waiting_on_a_slow_worker_reports_no_failure():
    """Stop ends the wait; neither that nor the slow worker it stops is a failure."""

    link = profile(MEASUREMENT)
    stream, _ = v5_stream(link, seed=5, bursts=8, mcs_indices=_MCS)
    receiver = MessageReceiver(link, decode_workers=2)
    receiver._pool.close()
    receiver._pool = SegmentDecodePool(link.config, workers=2, max_in_flight=2, max_batch=1,
                                       annotate=functools.partial(_annotate_slowly, 3.0))
    bridge = _Bridge()
    worker = ReceiveWorker(receiver, bridge)
    worker.start()
    worker.attach(_QueueSource([stream]))
    thread = worker._thread
    time.sleep(0.5)  # in back-pressure, behind two decodes of 3 s
    started = time.monotonic()
    worker.stop()
    assert time.monotonic() - started < 8.0
    thread.join(5.0)
    assert not thread.is_alive()
    assert worker.decode_failure is None
    assert bridge.failed.values == []


def test_a_real_worker_death_is_still_reported_after_a_close_race_fix():
    from test_decode_pool_errors import _Finished, _queued, _WorkerDied

    pool = _queued(_Finished(0), _WorkerDied(1))
    with pytest.raises(DecodePoolError, match="died") as caught:
        pool.drain(timeout=0.01)
    assert not isinstance(caught.value, DecodePoolClosed)


def test_a_wait_ended_by_close_is_not_a_dead_worker():
    from concurrent.futures import CancelledError
    from concurrent.futures.process import BrokenProcessPool

    from test_decode_pool_errors import _Finished, _queued

    class Raises(_Finished):
        def __init__(self, error):
            super().__init__(0)
            self.error = error

        def result(self, timeout=None):
            raise self.error

    for error in (BrokenProcessPool("killed by close"), CancelledError()):
        pool = _queued(Raises(error))
        pool._closing = True
        with pytest.raises(DecodePoolClosed):
            pool.drain(timeout=0.01)


def test_a_result_that_lands_after_close_emptied_the_queue_is_kept():
    from test_decode_pool_errors import _Finished, _numbers, _queued

    class ClosedDuringTheWait(_Finished):
        def result(self, timeout=None):
            pool.close()
            return super().result()

    pool = _queued(ClosedDuringTheWait(7))
    with pytest.raises(DecodePoolClosed):
        pool.drain()
    assert _numbers(pool.collect()) == [7]


def test_a_stop_that_times_out_delivers_what_finished_before_it_reports():
    from types import SimpleNamespace

    first = SimpleNamespace(message=SimpleNamespace(payload=b"in order"))
    second = SimpleNamespace(message=SimpleNamespace(payload=b"behind the unfinished one"))

    class Receiver:
        def flush(self, _timeout):
            raise TimeoutError

        def collect(self):
            return (first,)

        def give_up_decoding(self):
            return (second,), 1

    class BrokenReport:
        def emit(self, _text):
            raise RuntimeError("the window is gone")

    bridge = _Bridge()
    bridge.failed = BrokenReport()
    worker = ReceiveWorker(Receiver(), bridge)
    worker._finish_decoding(final=True)  # a report that fails does not end the thread
    assert bridge.message.values == [first, second]
    assert worker._finished.is_set()


def _annotate_mcs8_is_slow(delay_s: float, burst):
    if burst.mcs_index == 8:
        time.sleep(delay_s)
    return burst_quality(burst)


def test_stop_delivers_the_bursts_that_finished_behind_one_that_did_not():
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=107, bursts=6, mcs_indices=(8, 12, 16, 12, 16, 12))
    receiver = _receiver(link, functools.partial(_annotate_mcs8_is_slow, 6.0))
    bridge = _Bridge()
    worker = ReceiveWorker(receiver, bridge)
    worker.start()
    worker.attach(_Stream(_chunks(stream)))
    _wait_for(lambda: receiver.stats.samples_consumed >= stream.size)
    time.sleep(0.5)  # the other worker has decoded everything behind the slow burst
    started = time.monotonic()
    worker.stop()
    assert time.monotonic() - started < 6.0
    assert [m.message.payload for m in bridge.message.values] == payloads[1:]
    assert bridge.failed.values == ["1 burst(s) still decoding at stop were dropped"]
    assert worker.decode_failure is None


def test_a_detach_that_times_out_keeps_the_unfinished_bursts_for_later():
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=109, bursts=6, mcs_indices=(8, 12, 16, 12, 16, 12))
    receiver = _receiver(link, functools.partial(_annotate_mcs8_is_slow, 2.0))
    bridge = _Bridge()
    worker = ReceiveWorker(receiver, bridge)
    worker.start()
    worker.attach(_Stream(_chunks(stream)))
    _wait_for(lambda: receiver.stats.samples_consumed >= stream.size)
    worker.detach()  # 1.5 s, less than the slow burst: nothing is given up yet
    assert bridge.failed.values == []
    worker.stop()  # by now it has finished
    assert [m.message.payload for m in bridge.message.values] == payloads
    assert bridge.failed.values == []


@pytest.mark.parametrize("decode_workers", [1, 2], ids=["inline", "pool"])
def test_a_session_cut_mid_burst_does_not_cost_the_next_its_first_burst(decode_workers):
    link = profile(MEASUREMENT)
    first, first_payloads = v5_stream(link, seed=11, bursts=6, mcs_indices=_MCS)
    second, second_payloads = v5_stream(link, seed=12, bursts=6, mcs_indices=(9, 14, 17))
    first = first[: first.size - 1500]  # the radio stopped inside the last burst
    receiver = MessageReceiver(link, decode_workers=decode_workers)
    bridge = _Bridge()
    worker = ReceiveWorker(receiver, bridge)
    worker.start()
    try:
        worker.attach(_Stream(_chunks(first)))
        _wait_for(lambda: receiver.stats.samples_consumed >= first.size)
        worker.detach()
        worker.attach(_Stream(_chunks(second)))
        _wait_for(lambda: receiver.stats.samples_consumed >= first.size + second.size)
        worker.detach()
    finally:
        worker.stop()
    got = [message.message.payload for message in bridge.message.values]
    assert got == first_payloads[:-1] + second_payloads


def test_resetting_the_stream_keeps_absolute_offsets():
    link = profile(MEASUREMENT)
    stream, _ = v5_stream(link, seed=13, bursts=2, mcs_indices=(8,))
    receiver = MessageReceiver(link)
    receiver.feed(stream[: stream.size - 1500])
    decoder = receiver._decoder
    position = decoder._buffer_start + decoder.buffered_sample_count
    receiver.end_of_stream()
    assert decoder.buffered_sample_count == 0
    assert decoder._buffer_start == position == receiver.stats.samples_consumed
    assert decoder._pending_acquisition is None and decoder._required_sample_count is None


def test_a_drain_timeout_bounds_the_whole_wait():
    from test_decode_pool_errors import _numbered, _queued

    class _TakesAWhile:
        def result(self, timeout=None):
            if timeout is not None and timeout < 0.1:
                time.sleep(timeout)
                raise TimeoutError()
            time.sleep(0.1)
            return (_numbered(0),)

        def done(self):
            return False

        def exception(self, timeout=None):
            return None

    pool = _queued(*[_TakesAWhile() for _ in range(8)])
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        pool.drain(timeout=0.25)
    assert time.monotonic() - started < 0.5


def test_results_behind_the_task_that_died_are_kept_in_order():
    from test_decode_pool_errors import _Finished, _numbers, _queued, _WorkerDied

    pool = _queued(_Finished(0), _WorkerDied(1), _Finished(2), _WorkerDied(3), _Finished(4))
    with pytest.raises(DecodePoolError, match="died"):
        pool.drain(timeout=0.01)
    results = pool.drain(timeout=0.01)
    assert _numbers(results) == [0, None, 2, None, 4]
    assert [result.error is not None for result in results] == [False, True, False, True, False]
    assert pool._completed == 5 and pool._in_flight == 0 and pool._worker_errors == 2


def test_the_status_line_carries_the_decode_failure(tmp_path):
    from test_node_headless import _child_stats

    from ofdm_message_link import node_app

    monitor = node_app.NodeMonitor(1, "fdd", 5e6)
    writer = EventLogWriter(str(tmp_path / "events.jsonl"), node_id=1, sample_rate=5e6)
    assert monitor.decode_failure is None
    monitor.on_event("stats", _child_stats())
    writer.status(monitor.line())
    stats = _child_stats()
    stats["rx"]["decode_failure"] = "a segment decode worker died"
    monitor.on_event("stats", stats)
    assert monitor.decode_failure == "a segment decode worker died"
    line = monitor.line()
    assert line["rx"]["decode_failure"] == "a segment decode worker died"
    writer.status(line)
    writer.stop()
    rows = read_events(str(tmp_path / "events.jsonl"), kinds=("status",))
    assert [row["rx"]["decode_failure"] for row in rows] == [None, "a segment decode worker died"]


def test_a_headless_node_exits_non_zero_when_decoding_stops(tmp_path):
    from test_node_headless import FakeWorker, _child_stats, _lines

    from ofdm_message_link import log_limits, node_app

    class Failing(FakeWorker):
        def attach_from(self, factory):
            description = super().attach_from(factory)

            def fail():
                stats = _child_stats(5, 5)
                stats["rx"]["decode_failure"] = "a segment decode worker died"
                self.listener("stats", stats)

            threading.Timer(0.2, fail).start()
            return description

    import io

    out = io.StringIO()
    monitor = node_app.NodeMonitor(1, "fdd", 5e6)
    # The fake's own stats would overwrite the failure: only the one above is sent.
    worker = Failing(monitor.on_event)
    worker._stop.set()
    stats_log = log_limits.RotatingLog(tmp_path / "stats.jsonl", max_bytes=None)
    node = node_app.HeadlessNode(worker, monitor, lambda: "endpoint", stats_log=stats_log,
                                 ready_file=str(tmp_path / "ready"), duration_s=10.0, out=out)
    started = time.monotonic()
    assert node.run() == node_app.EXIT_DECODE_FAILED != 0
    assert time.monotonic() - started < 5.0
    assert "decoding stopped: a segment decode worker died" in out.getvalue()
    assert worker.stopped and not (tmp_path / "ready").exists()
    lines = [line for line in _lines(tmp_path) if "tx" in line]
    assert lines[-1]["rx"]["decode_failure"] == "a segment decode worker died"


@pytest.fixture(scope="module")
def application():
    """Held for the whole module: collecting it under a live widget aborts the process."""

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from ofdm_message_link import qt_runtime

    return qt_runtime.create_application()


@pytest.mark.gui
def test_the_node_window_keeps_showing_the_decode_failure(application, tmp_path):
    from test_node_window import FakeWorker as WindowWorker
    from test_node_window import _stats

    from ofdm_message_link import node_app, node_options
    from ofdm_message_link.node_window import NodeWindow

    args = node_options.parse_args(["--node-id", "1"])
    setup = node_options.resolve_node(args)
    window = NodeWindow(args, setup, node_app.build_spec(args, setup), worker_class=WindowWorker,
                        ready_file=str(tmp_path / "ready"))
    try:
        stats = _stats()
        stats["rx"]["decode_failure"] = "a segment decode worker died"
        window._on_event("stats", stats)
        window._status.setText("something else")
        window._refresh_stats()
        assert "a segment decode worker died" in window._status.text()
        application.processEvents()
    finally:
        window.close()


@pytest.mark.gui
def test_the_receive_window_shows_and_logs_the_decode_failure(application, tmp_path):
    import json

    from ofdm_message_link import options
    from ofdm_message_link.rx_app import ReceiveWindow, build_parser

    args = build_parser().parse_args(["--udp-port", "53311", "--egress-port", "53312"])
    resolved = options.resolve(args)
    log = open(tmp_path / "rx.jsonl", "w", encoding="utf-8")  # noqa: SIM115 - the window closes it
    window = ReceiveWindow(
        resolved, ("127.0.0.1", 53312),
        build_source=lambda selection: options.build_source(args, resolved, selection),
        stats_log=log,
    )
    try:
        window._worker._decode_failure = "a segment decode worker died"
        window._status.setText("something else")
        window._refresh_stats()
        assert "a segment decode worker died" in window._status.text()
    finally:
        window.close()
    last = json.loads((tmp_path / "rx.jsonl").read_text().splitlines()[-1])
    assert last["decode_failure"] == "a segment decode worker died"
