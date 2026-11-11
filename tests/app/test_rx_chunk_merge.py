"""ReceiveWorker merges short transport chunks without changing what it delivers."""

from __future__ import annotations

import queue
import threading
import time

import numpy as np
from stream_fixtures import MEASUREMENT, deliver, profile, v5_stream

from ofdm_message_link.link import MessageReceiver
from ofdm_message_link.rx_app import MIN_FEED_SAMPLES, ReceiveWorker


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


class _QueueSource:
    """A transport whose chunks are all queued before the worker starts."""

    description = "queued chunks"

    def __init__(self, chunks):
        self._queue: queue.Queue = queue.Queue()
        for chunk in chunks:
            self._queue.put(chunk)
        self.sizes: list[int] = []

    def start(self):
        pass

    def stop(self):
        pass

    def recv(self, timeout):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def empty(self):
        return self._queue.empty()


class _CountingReceiver(MessageReceiver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.feed_sizes: list[int] = []

    def feed(self, samples):
        self.feed_sizes.append(int(np.asarray(samples).size))
        return super().feed(samples)


def _run(worker, source, receiver, total):
    worker.start()
    worker.attach(source)
    deadline = time.monotonic() + 20.0
    try:
        while sum(receiver.feed_sizes) < total and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()


def test_short_chunks_are_merged_and_deliver_the_same_payloads():
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=21, bursts=10)
    chunks = [stream[start : start + 2560] for start in range(0, stream.size, 2560)]
    results = {}
    for minimum in (0, MIN_FEED_SAMPLES):
        receiver = _CountingReceiver(link)
        bridge = _Bridge()
        worker = ReceiveWorker(receiver, bridge, min_feed_samples=minimum)
        _run(worker, _QueueSource(chunks), receiver, stream.size)
        assert not bridge.failed.values
        assert sum(receiver.feed_sizes) == stream.size
        results[minimum] = (
            [message.message.payload for message in bridge.message.values],
            receiver.feed_sizes,
        )
    unmerged, merged = results[0], results[MIN_FEED_SAMPLES]
    assert set(unmerged[1]) <= {2560, stream.size % 2560}
    assert all(size >= MIN_FEED_SAMPLES for size in merged[1][:-1])
    assert merged[0] == unmerged[0] == deliver(link, stream)
    assert len(merged[0]) == len(payloads)


def test_a_short_chunk_waits_only_briefly_for_more():
    link = profile(MEASUREMENT)
    receiver = _CountingReceiver(link)
    worker = ReceiveWorker(receiver, _Bridge(), max_merge_wait_s=0.002)
    lonely = _QueueSource([np.zeros(100, np.complex64)])
    started = time.monotonic()
    chunk = worker._next_chunk(lonely)
    assert chunk.size == 100
    assert time.monotonic() - started < 0.1


def test_large_chunks_pass_through_untouched():
    link = profile(MEASUREMENT)
    worker = ReceiveWorker(MessageReceiver(link), _Bridge())
    big = np.ones(MIN_FEED_SAMPLES, np.complex64)
    source = _QueueSource([big, np.zeros(10, np.complex64)])
    assert worker._next_chunk(source) is big
    assert not source.empty()


def test_the_merge_stops_with_the_worker():
    link = profile(MEASUREMENT)
    worker = ReceiveWorker(MessageReceiver(link), _Bridge(), max_merge_wait_s=0.1)

    class Slow(_QueueSource):
        def recv(self, timeout):
            time.sleep(min(timeout, 0.01))
            return np.zeros(10, np.complex64)

    stopper = threading.Timer(0.02, worker._stop.set)
    stopper.start()
    started = time.monotonic()
    worker._next_chunk(Slow([]))
    assert time.monotonic() - started < 0.5
