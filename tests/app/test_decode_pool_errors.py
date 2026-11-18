"""A decode worker that raises fails its own burst; later bursts still flow, in order.

Before the fix, a task whose future held an exception other than
``BrokenExecutor`` stayed at the head of the pool's queue: every later feed
re-raised it, so every following burst was lost, and the results already
collected in the same ``submit_many`` call were dropped with it.
"""

from __future__ import annotations

import functools
import threading

import numpy as np
import pytest
from stream_fixtures import MEASUREMENT, profile, v5_stream

from ofdm_link.phy.streaming import PayloadFailure
from ofdm_link.runtime.segment_decode_pool import DecodePoolError, SegmentDecodePool
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
