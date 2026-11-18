"""Engine->parent events travel in batches: same events, same order, none stranded.

Every thread of an engine child puts on one ``_EventSink``; it sends its
buffer as one queue item at the end of each unit of work, at ``_MAX_BATCH``
events, before every reply and at least every ``_serve`` pass.  The parent's
pump hands the events on one by one.  Drops are counted in events.
"""

from __future__ import annotations

import itertools
import queue
import sys
import threading
import time

import numpy as np
import pytest

from ofdm_link.runtime.segment_decode_pool import _safe_context
from ofdm_message_link import engine
from ofdm_message_link.rx_app import ReceiveWorker


class _Channel:
    """Records every batch the sink sends; ``accept`` decides whether it fits."""

    def __init__(self, accept=lambda batch: True):
        self.batches: list[list] = []
        self.refused: list[list] = []
        self._accept = accept
        self._lock = threading.Lock()

    def put_batch(self, batch):
        with self._lock:
            if not self._accept(batch):
                self.refused.append(list(batch))
                return False
            self.batches.append(list(batch))
            return True

    def events(self):
        with self._lock:
            return [event for batch in self.batches for event in batch]


def _sink(channel, **kwargs):
    return engine._EventSink(channel, samples_per_second=10.0, **kwargs)


def test_batches_keep_the_global_put_order_of_every_producer():
    channel = _Channel()
    sink = _sink(channel, max_batch=8)
    order: list[tuple[str, int]] = []
    recording = threading.Lock()
    stop = threading.Event()

    def producer(name, flush_every):
        for number in range(600):
            # The outer lock fixes the order puts happen in, so the order the
            # parent must see is exactly ``order``.
            with recording:
                order.append((name, number))
                sink.put(name, number)
            if number % flush_every == 0:
                sink.flush_events()  # outside the lock: flushes race each other
        sink.flush_events()

    def timer():  # like the _serve loop's flush every pass
        while not stop.is_set():
            sink.flush_events()

    ticker = threading.Thread(target=timer)
    ticker.start()
    threads = [threading.Thread(target=producer, args=(f"p{i}", every))
               for i, every in enumerate((1, 3, 7, 50))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stop.set()
    ticker.join()
    sink.flush_events()
    assert channel.events() == order
    assert all(1 <= len(batch) <= 8 for batch in channel.batches)
    assert sink.dropped == 0


def test_a_full_buffer_is_sent_at_the_batch_cap():
    channel = _Channel()
    sink = _sink(channel)
    cap = engine._MAX_BATCH
    for number in range(2 * cap + 22):
        sink.put("message", number)
    assert [len(batch) for batch in channel.batches] == [cap, cap]
    sink.flush_events()
    assert [len(batch) for batch in channel.batches] == [cap, cap, 22]
    assert [value for _, value in channel.events()] == list(range(2 * cap + 22))
    sink.flush_events()  # nothing buffered: nothing sent
    assert len(channel.batches) == 3


def test_drops_are_counted_in_events_not_batches():
    channel = _Channel(accept=lambda batch: len(batch) < 5)
    sink = _sink(channel)
    for size in (3, 7, 1, 5):
        for number in range(size):
            sink.put("symbols", number)
        sink.flush_events()
    assert [len(batch) for batch in channel.batches] == [3, 1]
    assert sink.dropped == 7 + 5


def test_the_queue_is_bounded_in_events_however_they_are_batched():
    channel = engine._EventQueue(_safe_context(), depth=10)
    sink = _sink(channel)
    try:
        def send(count):
            for number in range(count):
                sink.put("link_snr", number)
            sink.flush_events()

        send(4)
        send(4)
        assert sink.dropped == 0
        send(4)  # 12 events would be outstanding: the whole batch is dropped
        assert sink.dropped == 4
        send(2)  # exactly at the bound
        assert sink.dropped == 4
        send(1)
        assert sink.dropped == 5
        taken = channel.get(5.0)  # the parent takes one batch: its room is back
        assert len(taken) == 4
        send(4)
        assert sink.dropped == 5
        sizes = [len(channel.get(5.0)) for _ in range(3)]
        assert sizes == [4, 2, 4]
        with pytest.raises(queue.Empty):
            channel.get(0.2)
    finally:
        channel.close()
        channel.join_thread()


# --------------------------------------------------------------- _serve


class _Replies:
    """Snapshots what the sink has sent at the moment of every reply."""

    def __init__(self, channel):
        self.channel = channel
        self.seen: list[tuple[tuple, list]] = []

    def put(self, reply):
        self.seen.append((reply, self.channel.events()))


class _Worker:
    lag_monitor = None

    def __init__(self, sink):
        self.sink = sink

    def attach(self, endpoint):
        self.sink.put("attached", endpoint.description)

    def detach(self):
        # Like NodeWorker.after_detach writing the last --events records.
        self.sink.put("bursts", "final")


class _Endpoint:
    description = "fake radio"


@pytest.fixture
def _restore_switch_interval():
    saved = sys.getswitchinterval()
    yield
    sys.setswitchinterval(saved)


def test_serve_flushes_before_every_reply_and_when_it_stops(_restore_switch_interval):
    channel = _Channel()
    sink = _sink(channel)
    worker = _Worker(sink)
    control: queue.Queue = queue.Queue()
    replies = _Replies(channel)
    for command in (("attach", _Endpoint), ("mark", 1), ("detach",), ("mark", 2), ("stop",)):
        control.put(command)

    def handle(command):
        sink.put("mark", command[1])

    ticks = itertools.count()
    engine._serve(worker, control, replies, lambda: sink.put("stats", next(ticks)),
                  handle, switch_interval_s=0.005, flush=sink.flush_events)
    attach_reply, at_attach = replies.seen[0]
    detach_reply, at_detach = replies.seen[1]
    assert attach_reply == ("ok", "fake radio")
    assert ("attached", "fake radio") in at_attach
    assert detach_reply == ("ok", "")
    assert at_detach[-1] == ("bursts", "final")
    # The mark handled just before "stop" is not stranded in the buffer.
    events = channel.events()
    assert [kind for kind, _ in events if kind != "stats"] == [
        "attached", "mark", "bursts", "mark"
    ]


def test_serve_flushes_other_threads_events_within_a_pass(_restore_switch_interval):
    """The timed flush: an event no producer flushes still leaves within ~50 ms."""

    channel = _Channel()
    sink = _sink(channel)
    control: queue.Queue = queue.Queue()
    arrived = {}

    def stray():
        time.sleep(0.2)
        put_at = time.monotonic()
        sink.put("la", "from a thread that never flushes")
        while not channel.events():
            time.sleep(0.001)
        arrived["delay"] = time.monotonic() - put_at
        control.put(("stop",))

    thread = threading.Thread(target=stray)
    thread.start()
    engine._serve(_Worker(sink), control, _Replies(channel), lambda: None,
                  switch_interval_s=0.005, flush=sink.flush_events)
    thread.join()
    assert channel.events() == [("la", "from a thread that never flushes")]
    assert arrived["delay"] < 0.5


# ------------------------------------------------------- receive worker


class _RaisingReceiver:
    def feed(self, samples, **_kwargs):
        raise ValueError("decoder broke")


class _OneChunk:
    description = "one chunk"

    def __init__(self):
        self._chunks = [np.ones(64, np.complex64)]

    def start(self):
        pass

    def stop(self):
        pass

    def recv(self, timeout):
        if self._chunks:
            return self._chunks.pop()
        time.sleep(timeout)
        return None


def test_the_receive_worker_flushes_an_iteration_that_ends_in_continue():
    channel = _Channel()
    sink = _sink(channel)
    worker = ReceiveWorker(_RaisingReceiver(), sink, sink, min_feed_samples=0)
    worker.start()
    worker.attach(_OneChunk())
    try:
        deadline = time.monotonic() + 5.0
        while not channel.events() and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        worker.stop()
    kinds = [kind for kind, _ in channel.events()]
    # Nothing but the worker flushes here: no _serve loop is running.
    assert kinds[-1] == "failed"
    assert channel.events()[-1][1] == "ValueError: decoder broke"


# ------------------------------------------------ through the processes


def _child_main(events, control, replies):
    """An engine child: four producers and the real _serve loop."""

    sink = engine._EventSink(events, samples_per_second=10.0)
    numbering = itertools.count()
    recording = threading.Lock()
    go = threading.Event()

    def put(kind):
        with recording:
            sink.put(kind, next(numbering))

    class Worker:
        lag_monitor = None

        def attach(self, endpoint):
            put("attached")
            go.set()

        def detach(self):
            for _ in range(5):
                put("bursts")

    def producer(kind, count, flush_every):
        go.wait()
        for number in range(count):
            put(kind)
            if flush_every and number % flush_every == 0:
                sink.flush_events()
        if flush_every:
            sink.flush_events()

    threads = [threading.Thread(target=producer, args=args, daemon=True)
               for args in (("message", 400, 1), ("sent", 300, 1), ("la", 200, 0))]
    for thread in threads:
        thread.start()

    def handle(command):
        if command[0] == "mark":
            put("mark")

    try:
        engine._serve(Worker(), control, replies, lambda: put("stats"), handle,
                      switch_interval_s=0.005, flush=sink.flush_events)
    finally:
        for thread in threads:
            thread.join(5.0)
        put("final")
        sink.flush_events()


def _endpoint():
    return _Endpoint()


def test_the_parent_gets_every_event_in_order_through_the_real_queue():
    got: list[tuple[str, int]] = []
    process = engine._EngineProcess(_child_main, (), lambda kind, value: got.append((kind, value)))
    process.start()
    try:
        assert process.request(("attach", _endpoint), 60.0) == ("ok", "fake radio")
        for _ in range(20):
            assert process.post(("mark",))
        deadline = time.monotonic() + 30.0
        while sum(kind == "message" for kind, _ in got) < 400 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert process.request(("detach",), 30.0) == ("ok", "")
    finally:
        process.stop()
    # The pump reports the child's exit after stop, as it always has.
    got = [event for event in got if event != ("failed", "radio engine process exited")]
    numbers = [number for _, number in got]
    # Every event, once, in the order the child put them: the numbers count up.
    assert numbers == list(range(len(numbers)))
    counts = {kind: sum(1 for k, _ in got if k == kind) for kind, _ in got}
    assert counts["message"] == 400 and counts["sent"] == 300 and counts["la"] == 200
    assert counts["mark"] == 20 and counts["bursts"] == 5 and counts["attached"] == 1
    # Put after the loop stopped, in the child's finally: still delivered.
    assert got[-1][0] == "final"


def test_a_message_signal_strips_the_constellation_before_batching():
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Message:
        payload_symbols: np.ndarray

    channel = _Channel()
    sink = _sink(channel)
    sink.message.emit(Message(np.ones(8, np.complex64)))
    sink.flush_events()
    (kind, value), = channel.events()
    assert kind == "message" and value.payload_symbols.size == 0


@pytest.mark.parametrize("max_batch", [1, 2, 64])
def test_batch_size_never_changes_what_arrives(max_batch):
    channel = _Channel()
    sink = _sink(channel, max_batch=max_batch)
    for number in range(100):
        sink.put("message", number)
        if number % 9 == 0:
            sink.flush_events()
    sink.flush_events()
    assert [value for _, value in channel.events()] == list(range(100))
    assert max(len(batch) for batch in channel.batches) <= max_batch



# ------------------------------------------------------ transmit worker


class _AcceptingRadio:
    description = "accepting radio"

    def start(self):
        pass

    def stop(self):
        pass

    def send(self, samples):
        return True


def test_the_transmit_worker_flushes_after_each_burst():
    from stream_fixtures import MEASUREMENT, profile

    from ofdm_message_link.link import MessageTransmitter
    from ofdm_message_link.tx_app import TransmitWorker

    channel = _Channel()
    sink = _sink(channel)
    worker = TransmitWorker(MessageTransmitter(profile(MEASUREMENT)), sink, sink)
    worker.start()
    worker.attach(_AcceptingRadio())
    try:
        for number in range(3):
            assert worker.submit("test", bytes([number]) * 40)
            deadline = time.monotonic() + 10.0
            # No _serve loop runs here: only the worker's own flush sends it.
            while (sum(kind == "sent" for kind, _ in channel.events()) <= number
                   and time.monotonic() < deadline):
                time.sleep(0.005)
    finally:
        worker.stop()
    sent = [value for kind, value in channel.events() if kind == "sent"]
    assert [record.message_count for record in sent] == [1, 1, 1]
