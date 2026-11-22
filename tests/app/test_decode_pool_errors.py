"""A decode worker that raises fails its own burst; later bursts still flow, in order.

Before the fix, a task whose future held an exception other than
``BrokenExecutor`` stayed at the head of the pool's queue: every later feed
re-raised it, so every following burst was lost, and the results already
collected in the same ``submit_many`` call were dropped with it.
"""

from __future__ import annotations

import functools
import os
import threading
from concurrent.futures.process import BrokenProcessPool

import numpy as np
import pytest
from stream_fixtures import MEASUREMENT, profile, v5_stream

from ofdm_link.phy.streaming import BurstSegment, PayloadFailure
from ofdm_link.runtime.overload import OFF as OVERLOAD_OFF
from ofdm_link.runtime.segment_decode_pool import (
    DecodePoolError,
    SegmentDecodePool,
    SegmentDecodeResult,
)
from ofdm_message_link.link import MessageReceiver

# v5_stream cycles these; the bursts at MCS 12 are the ones made to fail.
_MCS = (8, 12, 16)
_BAD_MCS = 12


def _annotate_or_raise(bad_mcs: int, burst):
    """Runs in the worker, after the payload decoded: raise for one MCS."""

    from ofdm_message_link.link import burst_quality

    if burst.mcs_index == bad_mcs:
        raise ValueError("injected worker failure")
    return burst_quality(burst)


def _annotate_unpicklable(bad_mcs: int, burst):
    """Return something the worker cannot send back: the whole task fails."""

    from ofdm_message_link.link import burst_quality

    if burst.mcs_index == bad_mcs:
        return threading.Lock()
    return burst_quality(burst)


def _receiver(link, annotate, *, max_batch: int | None = None) -> MessageReceiver:
    receiver = MessageReceiver(link, decode_workers=2)
    receiver._pool.close()
    kwargs = {} if max_batch is None else {"max_batch": max_batch}
    receiver._pool = SegmentDecodePool(link.config, workers=2, annotate=annotate, **kwargs)
    return receiver


def _feed(receiver, stream, chunk: int | None) -> list[bytes]:
    if chunk is None:
        delivered = list(receiver.feed(stream))
    else:
        delivered = []
        for start in range(0, stream.size, chunk):
            delivered.extend(receiver.feed(stream[start : start + chunk]))
    delivered.extend(receiver.flush(120.0))
    return [m.message.payload for m in delivered]


@pytest.mark.parametrize("chunk", [None, 4096], ids=["one-feed", "chunked"])
def test_a_worker_exception_fails_only_its_own_burst(chunk):
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=53, bursts=12, mcs_indices=_MCS)
    bad = [index % len(_MCS) == _MCS.index(_BAD_MCS) for index in range(len(payloads))]
    expected = [p for p, failed in zip(payloads, bad, strict=True) if not failed]
    receiver = _receiver(link, functools.partial(_annotate_or_raise, _BAD_MCS))
    try:
        first = _feed(receiver, stream, chunk)
        # The same receiver keeps decoding: nothing is stuck at the head.
        second = _feed(receiver, stream, 4096)
        pool = receiver.decode_pool_snapshot()
        counts = receiver.decoder_snapshot()
        errors = receiver.drain_decode_errors()
    finally:
        receiver.close()
    assert first == expected
    assert second == expected
    assert pool.in_flight == 0 and pool.completed == pool.submitted == 2 * len(payloads)
    assert pool.worker_errors == 2 * sum(bad)
    # Counted like an undiagnosed payload failure on the inline path.
    assert counts.payload_decode_attempts == 2 * len(payloads)
    assert counts.payload_decode_failure == 2 * sum(bad)
    assert counts.unclassified_decode_failures == 2 * sum(bad)
    assert len(errors) == 2 * sum(bad)
    assert all(error == "ValueError: injected worker failure" for error in errors)
    assert receiver.drain_decode_errors() == ()


def test_a_task_whose_result_cannot_come_back_fails_its_segments_and_no_others():
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=59, bursts=9, mcs_indices=_MCS)
    bad = [index % len(_MCS) == _MCS.index(_BAD_MCS) for index in range(len(payloads))]
    receiver = _receiver(link, functools.partial(_annotate_unpicklable, _BAD_MCS), max_batch=1)
    try:
        delivered = _feed(receiver, stream, None)
        delivered += _feed(receiver, stream, 4096)
        pool = receiver.decode_pool_snapshot()
        counts = receiver.decoder_snapshot()
        errors = receiver.drain_decode_errors()
    finally:
        receiver.close()
    expected = [p for p, failed in zip(payloads, bad, strict=True) if not failed]
    assert delivered == expected + expected
    assert pool.in_flight == 0 and pool.worker_errors == 2 * sum(bad)
    assert counts.payload_decode_attempts == 2 * len(payloads)
    assert counts.unclassified_decode_failures == 2 * sum(bad)
    assert len(errors) == 2 * sum(bad)


def test_a_failed_task_still_yields_one_result_per_segment_in_order():
    """A batch of four with one bad segment: four results, the order kept."""

    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=61, bursts=9, mcs_indices=_MCS)
    receiver = _receiver(link, functools.partial(_annotate_unpicklable, _BAD_MCS))
    try:
        delivered = _feed(receiver, stream, None)
        pool = receiver.decode_pool_snapshot()
        counts = receiver.decoder_snapshot()
    finally:
        receiver.close()
    # Whole tasks fail here, so good bursts that shared a task are lost too,
    # but nothing is duplicated or reordered, and every segment is accounted.
    positions = [payloads.index(payload) for payload in delivered]
    assert positions == sorted(set(positions))
    assert pool.in_flight == 0 and pool.completed == pool.submitted == len(payloads)
    assert counts.payload_decode_attempts == len(payloads)
    assert counts.payload_decode_failure == len(payloads) - len(delivered)


def test_a_failed_submit_closes_the_pool_instead_of_losing_segments_quietly():
    link = profile(MEASUREMENT)
    stream, _ = v5_stream(link, seed=67, bursts=6, mcs_indices=_MCS)
    receiver = MessageReceiver(link, decode_workers=2)
    pool = receiver._pool

    def refuse(*_args, **_kwargs):
        raise RuntimeError("cannot schedule new futures")

    original = pool._executor.submit
    pool._executor.submit = refuse
    try:
        with pytest.raises(DecodePoolError):
            receiver.feed(stream)
        snapshot = pool.snapshot()
        assert snapshot.in_flight == 0
        with pytest.raises(DecodePoolError, match="closed"):
            receiver.feed(stream[:4096])
    finally:
        pool._executor.submit = original
        receiver.close()


def test_the_failure_is_an_undiagnosed_payload_failure():
    from ofdm_link.runtime.segment_decode_pool import SegmentDecodeResult, failed_result

    result = failed_result(ValueError("boom"))
    assert isinstance(result, SegmentDecodeResult)
    assert result.burst is None
    assert result.failure == PayloadFailure(diagnosed=False, crc_failed=False)
    assert result.error == "ValueError: boom"


def test_the_receive_worker_reports_the_error_and_keeps_delivering():
    import time

    from test_rx_chunk_merge import _Bridge, _QueueSource

    from ofdm_message_link.rx_app import ReceiveWorker

    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=71, bursts=12, mcs_indices=_MCS)
    bad = {p for index, p in enumerate(payloads) if index % len(_MCS) == _MCS.index(_BAD_MCS)}
    expected = [p for p in payloads if p not in bad]
    receiver = _receiver(link, functools.partial(_annotate_or_raise, _BAD_MCS))
    chunks = [stream[start : start + 8192] for start in range(0, stream.size, 8192)]

    class _Streaming(_QueueSource):
        # Like a radio, idle samples keep coming after the stream, so the
        # last decodes have later feeds to land in.
        def recv(self, timeout):
            chunk = super().recv(0.0)
            if chunk is None:
                time.sleep(0.002)
                chunk = np.zeros(8192, np.complex64)
            return chunk

    bridge = _Bridge()
    worker = ReceiveWorker(receiver, bridge)
    worker.start()
    worker.attach(_Streaming(chunks))
    deadline = time.monotonic() + 60.0
    try:
        while len(bridge.message.values) < len(expected) and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        worker.stop()
    assert [m.message.payload for m in bridge.message.values] == expected
    failed = bridge.failed.values
    assert failed
    assert all(text.startswith("decode worker raised on ") for text in failed)
    assert all(text.endswith("ValueError: injected worker failure") for text in failed)


# ------------------------------------------- a wait that times out


class _BarePool(SegmentDecodePool):
    """Just the queue of a pool, around fake futures (no workers)."""

    def __init__(self, future, count):
        from collections import deque

        self._pending = deque([(future, count)])
        self._in_flight = count
        self._completed = 0
        self._worker_errors = 0
        self._closed = False
        self._taken = []


class _FinishesJustAfterTheTimeout:
    """result(timeout) times out; the worker is done by the time anyone looks."""

    def result(self, timeout=None):
        raise TimeoutError()

    def done(self):
        return True

    def exception(self, timeout=None):
        return None  # it succeeded: its results are good


class _TaskRaisedTimeoutError:
    """The task itself raised TimeoutError: that is its outcome."""

    def __init__(self):
        self.error = TimeoutError("the task's own timeout")

    def result(self, timeout=None):
        raise self.error

    def done(self):
        return True

    def exception(self, timeout=None):
        return self.error


def test_a_wait_that_times_out_never_turns_good_bursts_into_failures():
    pool = _BarePool(_FinishesJustAfterTheTimeout(), 3)
    with pytest.raises(TimeoutError):
        pool._take_oldest(timeout=0.5)
    # Still at the head, still in flight: a later call takes its results.
    assert len(pool._pending) == 1 and pool._in_flight == 3 and pool._completed == 0
    assert pool._worker_errors == 0


def test_a_timeout_the_task_raised_itself_fails_its_segments_and_moves_on():
    future = _TaskRaisedTimeoutError()
    pool = _BarePool(future, 2)
    results = pool._take_oldest(timeout=0.5)
    assert [result.error for result in results] == ["TimeoutError: the task's own timeout"] * 2
    assert not pool._pending and pool._in_flight == 0 and pool._worker_errors == 2


# ---------------------------------- results taken before a wait fails


def _numbered(number):
    return SegmentDecodeResult(burst=None, failure=None, annotation=number)


class _Finished:
    def __init__(self, number):
        self.number = number

    def result(self, timeout=None):
        return (_numbered(self.number),)

    def done(self):
        return True

    def exception(self, timeout=None):
        return None


class _DecodingUntilReleased(_Finished):
    """Times out until ``released``; then a finished task like any other."""

    released = False

    def result(self, timeout=None):
        if not self.released:
            raise TimeoutError()
        return super().result()

    def done(self):
        return self.released


class _WorkerDied(_Finished):
    def result(self, timeout=None):
        raise BrokenProcessPool("a worker died")


class _Executor:
    def shutdown(self, **_kwargs):
        pass


def _queued(*futures):
    pool = _BarePool(futures[0], 1)
    pool._pending.extend((future, 1) for future in futures[1:])
    pool._in_flight = len(futures)
    pool._max_in_flight = 8
    pool._max_batch = 4
    pool._backpressure_waits = 0
    pool._submitted = len(futures)
    pool._overload = OVERLOAD_OFF
    pool._executor = _Executor()
    return pool


def _numbers(results):
    return [result.annotation for result in results]


def test_a_drain_that_times_out_keeps_the_results_it_already_took():
    late = _DecodingUntilReleased(2)
    pool = _queued(_Finished(0), _Finished(1), late, _Finished(3))
    with pytest.raises(TimeoutError):
        pool.drain(timeout=0.01)
    late.released = True
    assert _numbers(pool.drain(timeout=0.01)) == [0, 1, 2, 3]
    assert pool.drain() == ()


@pytest.mark.parametrize("next_call", ["collect", "submit_many"])
def test_results_kept_by_a_timed_out_drain_come_first_from_the_next_call(next_call):
    late = _DecodingUntilReleased(1)
    pool = _queued(_Finished(0), late)
    with pytest.raises(TimeoutError):
        pool.drain(timeout=0.01)
    # Still decoding: what was kept is returned, once, and the task stays.
    got = pool.collect() if next_call == "collect" else pool.submit_many(())
    assert _numbers(got) == [0]
    late.released = True
    assert _numbers(pool.collect()) == [1]


def test_a_worker_death_during_a_drain_keeps_the_results_before_it():
    pool = _queued(_Finished(0), _Finished(1), _WorkerDied(2))
    with pytest.raises(DecodePoolError, match="died"):
        pool.drain(timeout=0.01)
    assert _numbers(pool.drain(timeout=0.01)) == [0, 1]


def test_a_worker_death_under_backpressure_keeps_the_results_before_it():
    pool = _queued(_Finished(0), _WorkerDied(1))
    pool._max_in_flight = 1  # so the wait goes on to the task that died
    segment = object.__new__(BurstSegment)
    with pytest.raises(DecodePoolError, match="died"):
        pool.submit_many((segment,))
    assert _numbers(pool.collect()) == [0]


def _annotate_slowly(slow_mcs: int, burst):
    """Runs in the worker: a burst at this MCS is still decoding at the timeout."""

    import time

    from ofdm_message_link.link import burst_quality

    if burst.mcs_index == slow_mcs:
        time.sleep(0.4)
    return burst_quality(burst)


def test_a_receiver_flush_that_times_out_loses_no_message():
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=79, bursts=9, mcs_indices=_MCS)
    receiver = _receiver(link, functools.partial(_annotate_slowly, _BAD_MCS), max_batch=1)
    timeouts = 0
    try:
        delivered = list(receiver.feed(stream))
        while True:
            try:
                delivered.extend(receiver.flush(0.05))
                break
            except TimeoutError:
                timeouts += 1
    finally:
        receiver.close()
    assert timeouts > 0
    assert [message.message.payload for message in delivered] == payloads


# ------------------------------------------------------------ Ctrl-C


def _ignores_sigint(pid: int) -> bool:
    import signal

    with open(f"/proc/{pid}/status", encoding="ascii") as handle:
        for line in handle:
            if line.startswith("SigIgn:"):
                return bool(int(line.split()[1], 16) & (1 << (signal.SIGINT - 1)))
    return False


@pytest.mark.skipif(not os.path.exists("/proc/self/status"), reason="Linux /proc")
def test_ctrl_c_on_the_process_group_does_not_reach_the_decode_workers():
    import os
    import signal
    import time

    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=73, bursts=9, mcs_indices=_MCS)
    receiver = MessageReceiver(link, decode_workers=2)
    try:
        workers = list(receiver._pool._executor._processes)
        assert len(workers) == 2
        # The ready check can be answered by one worker while the other is
        # still starting; wait until both have run their initializer.
        deadline = time.monotonic() + 30.0
        while not all(_ignores_sigint(pid) for pid in workers):
            assert time.monotonic() < deadline, "a worker never ignored SIGINT"
            time.sleep(0.02)
        for pid in workers:  # what a terminal Ctrl-C does to every process
            os.kill(pid, signal.SIGINT)
        time.sleep(0.5)
        delivered = _feed(receiver, stream, 4096)
        assert sorted(receiver._pool._executor._processes) == sorted(workers)
    finally:
        receiver.close()
    assert delivered == payloads
