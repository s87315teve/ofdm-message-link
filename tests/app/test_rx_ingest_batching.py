"""UHD receive ingest batching: GNU Radio groups N samples before the Python sink.

The UHD source is replaced by a Python GNU Radio block that makes a ramp
(sample k has value k) in short work() calls and tags ``rx_time`` where a
B210 would, so the real :class:`UhdSampleSource` sink, queue and tag handling
run without a radio.  Nothing here transmits; none of it is RF evidence.
"""

from __future__ import annotations

import math
import queue
import time
from types import SimpleNamespace

import numpy as np
import pytest

from ofdm_message_link import transport
from ofdm_message_link.transport import (
    ReceiveFeed,
    TransportError,
    UhdSampleSource,
    rx_ingest_multiple,
    rx_queue_limits,
)

FS = 1_000_000.0
T0 = 7.0
N = 8192
FEED = ReceiveFeed(N, 0.015)  # 15 ms at 1 MS/s is 15000 samples: N = 8192


def _seconds(offset: int, lost_before: int = 0) -> float:
    return T0 + (offset + lost_before) / FS


def _device_class():
    gr = pytest.importorskip("gnuradio.gr")
    pmt = pytest.importorskip("pmt")

    class FakeUsrpSource(gr.sync_block):  # type: ignore[misc]
        """Ramp samples in short calls, rx_time tags at chosen offsets, then done."""

        def __init__(self, samples, tags, per_call=2040, rate=FS, pace_s=0.0):
            super().__init__(name="fake_usrp_source", in_sig=None, out_sig=[np.complex64])
            self.message_port_register_out(pmt.intern("async_msgs"))
            self._samples = np.asarray(samples, dtype=np.complex64)
            self._tags = sorted(tags.items())
            self._per_call = per_call
            self._rate = float(rate)
            self._pace_s = pace_s
            self.min_output_buffer = None

        def get_center_freq(self, channel=0):
            return 915e6

        def get_samp_rate(self):
            return self._rate

        def set_min_output_buffer(self, *args):
            self.min_output_buffer = args[-1]
            self.gateway.set_min_output_buffer(*args)

        def work(self, input_items, output_items):
            start = int(self.nitems_written(0))
            if start >= self._samples.size:
                return -1  # WORK_DONE
            if self._pace_s:
                time.sleep(self._pace_s)  # like a radio: samples arrive over time
            count = min(len(output_items[0]), self._per_call, self._samples.size - start)
            output_items[0][:count] = self._samples[start : start + count]
            for offset, seconds in self._tags:
                if start <= offset < start + count:
                    whole = int(seconds)
                    self.add_item_tag(
                        0,
                        offset,
                        pmt.intern("rx_time"),
                        pmt.make_tuple(pmt.from_uint64(whole), pmt.from_double(seconds - whole)),
                    )
            return count

    return FakeUsrpSource


@pytest.fixture
def fake_usrp(monkeypatch):
    """Install a fake ``create_b210_source``; returns a function that arms it."""

    pytest.importorskip("gnuradio.gr")
    import ofdm_link.radio.uhd as uhd

    device_class = _device_class()
    built = []
    armed = {}

    def create(settings, *, recv_frames=None):
        block = device_class(
            armed["samples"], armed["tags"], armed["per_call"],
            armed["device_rate"] or settings.sample_rate, armed["pace_s"],
        )
        built.append(block)
        return block

    monkeypatch.setattr(uhd, "create_b210_source", create)

    def arm(samples, tags, per_call=2040, pace_s=0.0, device_rate=None):
        armed.update(samples=samples, tags=tags, per_call=per_call, pace_s=pace_s,
                     device_rate=device_rate)
        return built

    return arm


def _settings(rate=FS):
    return SimpleNamespace(sample_rate=rate, center_frequency=915e6, device_args="")


def _ramp(total):
    return np.arange(total, dtype=np.float32).astype(np.complex64)


def _drain(source, total, *, check=None, timeout=10.0):
    chunks = []
    received = 0
    deadline = time.monotonic() + timeout
    while received < total and time.monotonic() < deadline:
        chunk = source.recv(0.2)
        if chunk is None:
            continue
        chunks.append(chunk)
        received += chunk.size
        if check is not None:
            check(received)
    return chunks


# The tags of a B210 stream: one at start, three around the first N boundary
# (re-tags with no gap), and one after an overflow that lost 2000 samples.
GAP_AT = 5 * N + 100


def _tags():
    return {
        0: _seconds(0),
        N - 1: _seconds(N - 1),
        N: _seconds(N),
        N + 1: _seconds(N + 1),
        GAP_AT: _seconds(GAP_AT, 2000),
    }


@pytest.mark.gnuradio
def test_batched_stream_is_exact_and_tags_arrive_with_their_samples(fake_usrp):
    total = 40 * N  # GNU Radio drops a final tail shorter than N
    tags = _tags()
    fake_usrp(_ramp(total), tags)
    source = UhdSampleSource(_settings())
    source.set_receive_feed(FEED)
    source.start()
    assert source.ingest_multiple == N
    cap = source.snapshot()["chunk_samples"]
    seen = []

    def anchors_match(received):
        # Exactly the tags inside the samples handed on so far: none early, none late.
        count, _ = source.time_anchors()
        seen.append((received, count))
        assert count == sum(1 for offset in tags if offset < received)

    try:
        chunks = _drain(source, total, check=anchors_match)
    finally:
        source.stop()

    stream = np.concatenate(chunks)
    np.testing.assert_array_equal(stream, _ramp(total))
    assert all(chunk.size % N == 0 and chunk.size <= cap for chunk in chunks)
    count, anchors = source.time_anchors()
    assert count == len(tags)
    assert anchors == (
        (0, _seconds(0), 0),
        (N - 1, _seconds(N - 1), 0),
        (N, _seconds(N), 0),
        (N + 1, _seconds(N + 1), 0),
        (GAP_AT, _seconds(GAP_AT, 2000), 2000),
    )
    snapshot = source.snapshot()
    assert snapshot["rx_samples_lost"] == 2000
    assert snapshot["rx_time_discontinuities"] == 1
    assert snapshot["chunks_received"] == len(chunks)
    assert total // cap <= len(chunks) <= total // N
    assert snapshot["samples_received"] == total
    assert snapshot["ingest_multiple"] == N
    assert snapshot["queue_depth_chunks"] is None
    assert snapshot["queue_budget_samples"] == 100_000  # 0.1 s at the device's 1 MS/s


@pytest.mark.gnuradio
def test_back_pressure_loses_and_repeats_nothing(fake_usrp):
    total = 120 * N
    # Tags all through the stream, so some sit in windows the sink offers twice.
    tags = {offset: _seconds(offset) for offset in range(0, total, 7 * N + 13)}
    lost_at = 61 * N + 5
    tags[lost_at] = _seconds(lost_at, 2000)
    fake_usrp(_ramp(total), tags)
    source = UhdSampleSource(_settings(), queue_depth=2)
    source.set_receive_feed(FEED)
    source.start()
    try:
        time.sleep(0.3)  # nobody drains: the sink waits and returns 0
        assert source.snapshot()["queue_full_backpressure_events"] > 0
        chunks = _drain(source, total)
    finally:
        source.stop()
    np.testing.assert_array_equal(np.concatenate(chunks), _ramp(total))
    count, anchors = source.time_anchors()
    assert count == len(tags)
    assert [anchor[0] for anchor in anchors] == sorted(tags)
    assert source.snapshot()["rx_samples_lost"] == 2000
    assert source.snapshot()["rx_time_discontinuities"] == 1


@pytest.mark.gnuradio
def test_without_a_feed_the_sink_does_not_batch(fake_usrp):
    total = 30_001  # any length: nothing waits for a multiple
    fake_usrp(_ramp(total), {0: T0})
    source = UhdSampleSource(_settings())
    source.start()
    try:
        chunks = _drain(source, total)
    finally:
        source.stop()
    assert source.ingest_multiple == 1
    assert source.snapshot()["queue_depth_chunks"] == 16
    np.testing.assert_array_equal(np.concatenate(chunks), _ramp(total))
    assert source.time_anchors() == (1, ((0, T0, 0),))


@pytest.mark.parametrize(
    ("min_samples", "wait_s", "rate", "expected"),
    [
        (8192, 0.002, 10e6, 8192),        # PC defaults
        (8192, 0.002, 5e6, 8192),
        (65536, 0.015, 5e6, 65536),       # Jetson
        (65536, 0.015, 1e6, 15000),       # limited by the wait
        (0, 0.015, 5e6, 1),               # merging off
        (65536, 0.0, 5e6, 1),             # no wait
        (65536, 1e-6, 1e6, 1),            # a wait under two samples
        (131072, 0.020, 20e6, 131072),    # 20 MS/s: the cap is 4 x 65536
        (131072, 0.030, 5e6, 65536),      # limited by the chunk cap
    ],
)
def test_the_ingest_multiple_table(min_samples, wait_s, rate, expected):
    cap = transport._RX_CHUNK_SAMPLES * transport._rx_rate_scale(rate)
    assert rx_ingest_multiple(ReceiveFeed(min_samples, wait_s), rate, cap) == expected


def test_the_default_feed_switch_turns_batching_off_for_the_pc_defaults_only(monkeypatch):
    monkeypatch.setattr(transport, "RX_BATCH_DEFAULT_FEED", False)
    assert rx_ingest_multiple(ReceiveFeed(), 10e6, 131072) == 1
    assert rx_ingest_multiple(ReceiveFeed(65536, 0.015), 5e6, 65536) == 65536


@pytest.mark.parametrize(
    ("rate", "multiple", "fixed", "expected"),
    [
        (5e6, 1, None, (16, None)),           # unbatched: 16 chunks, as always
        (5e6, 1, 3, (3, None)),
        (10e6, 8192, None, (None, 1_000_000)),  # batched: 0.1 s of samples
        (5e6, 65536, None, (None, 500_000)),
        (1e6, 15000, None, (None, 100_000)),
        (5e6, 65536, 2, (2, 500_000)),        # an explicit depth also caps chunks
    ],
)
def test_the_queue_limits_table(rate, multiple, fixed, expected):
    assert rx_queue_limits(rate, multiple, fixed) == expected


@pytest.mark.gnuradio
def test_n_the_queue_and_the_gap_counter_use_the_rate_the_device_reports(fake_usrp):
    # Asked for 2 MS/s, the device runs at 1 MS/s: rx_time follows 1 MS/s.
    total = 20 * 10_000  # a multiple of N
    fake_usrp(_ramp(total), {0: T0, 50_000: T0 + 50_000 / 1e6}, device_rate=1e6)
    source = UhdSampleSource(_settings(2e6))
    source.set_receive_feed(ReceiveFeed(65536, 0.01))
    source.start()
    try:
        chunks = _drain(source, total)
    finally:
        source.stop()
    np.testing.assert_array_equal(np.concatenate(chunks), _ramp(total))
    assert source.ingest_multiple == 10_000                    # 10 ms at 1 MS/s
    assert source.snapshot()["queue_budget_samples"] == 100_000
    assert source.snapshot()["rx_samples_lost"] == 0
    assert source.snapshot()["rx_time_discontinuities"] == 0


@pytest.mark.parametrize("depth", [0, -1, 2.5])
def test_a_queue_depth_that_could_never_hold_a_chunk_is_refused(depth):
    with pytest.raises(TransportError, match="queue_depth"):
        UhdSampleSource(_settings(), queue_depth=depth)


def test_the_queue_is_bounded_in_samples_but_takes_one_chunk_when_empty():
    from ofdm_message_link.transport import _RxChunkQueue

    chunks = _RxChunkQueue(None, 100_000)
    assert chunks.put("big", 262_144, 0.0)            # empty: always room for one
    assert not chunks.put("next", 8192, 0.01)         # over the budget: refused
    assert chunks.get(0.0) == "big"
    for k in range(12):
        assert chunks.put(k, 8192, 0.0)               # 12 x 8192 = 98304
    assert not chunks.put("13th", 8192, 0.0)
    assert chunks.queued_samples == 12 * 8192
    assert [chunks.get(0.0) for _ in range(12)] == list(range(12))
    assert chunks.get(0.0) is None and chunks.queued_samples == 0


@pytest.mark.gnuradio
@pytest.mark.parametrize(("rate", "feed"), [(10e6, ReceiveFeed()), (5e6, ReceiveFeed(1024, 0.015))])
def test_a_stalled_consumer_queues_no_more_than_the_budget(fake_usrp, rate, feed):
    """After back-pressure the sink takes up to its cap per call; the queue still holds <= 0.1 s."""

    budget = math.ceil(0.1 * rate)
    total = 3 * budget + 64 * 1024
    total -= total % 8192
    fake_usrp(_ramp(total), {0: T0}, per_call=8160)
    source = UhdSampleSource(_settings(rate))
    source.set_receive_feed(feed)
    source.start()
    try:
        deadline = time.monotonic() + 5.0
        while (source.snapshot()["queue_full_backpressure_events"] < 5
               and time.monotonic() < deadline):
            time.sleep(0.01)
        queued = source.snapshot()["queued_samples"]
        assert source.snapshot()["queue_full_backpressure_events"] >= 5
        assert budget - source.snapshot()["chunk_samples"] < queued <= budget
        chunks = _drain(source, total)
    finally:
        source.stop()
    np.testing.assert_array_equal(np.concatenate(chunks), _ramp(total))


@pytest.mark.gnuradio
def test_losses_count_when_queued_and_anchors_publish_when_handed_on(fake_usrp):
    """A stop with chunks still queued keeps the overflow counted (rx_samples_lost)."""

    rate = 2.5e6  # a 250000-sample queue against a 65536-sample chunk cap
    total = 40 * N
    lost_at = 12 * N + 5
    fake_usrp(_ramp(total), {0: T0, lost_at: T0 + (lost_at + 2000) / rate})
    source = UhdSampleSource(_settings(rate))
    source.set_receive_feed(FEED)
    source.start()
    try:
        handed = sum(chunk.size for chunk in _drain(source, 4 * N))  # < 4N + one cap
        deadline = time.monotonic() + 5.0
        while (source.snapshot()["samples_received"] <= lost_at + N
               and time.monotonic() < deadline):
            time.sleep(0.01)
    finally:
        source.stop()
    assert handed < lost_at < source.snapshot()["samples_received"]
    assert source.snapshot()["rx_samples_lost"] == 2000
    assert source.snapshot()["rx_time_discontinuities"] == 1
    # Not handed on: not yet an anchor.
    assert source.time_anchors() == (1, ((0, T0, 0),))


@pytest.mark.gnuradio
def test_set_output_multiple_really_groups_the_work_calls():
    """Fails (within a timeout, never hangs) if GNU Radio stops honouring the multiple."""

    import threading

    gr = pytest.importorskip("gnuradio.gr")
    from ofdm_message_link.transport import _build_rx_sink_block

    total = 20 * N
    sizes = []
    calls = [0]
    base = _build_rx_sink_block(65536, lambda values, tags: sizes.append(values.size) or True,
                                ingest_multiple=N)

    class CountingSink(base):
        def work(self, input_items, output_items):
            calls[0] += 1
            return super().work(input_items, output_items)

    device = _device_class()(_ramp(total), {}, 2040, FS, 0.0005)
    sink = CountingSink()
    sink.set_output_multiple(N)
    top = gr.top_block("output_multiple_test")
    top.connect(device, sink)
    top.start()
    done = threading.Thread(target=top.wait, daemon=True)
    done.start()
    done.join(10.0)
    if done.is_alive():
        top.stop()
        done.join(2.0)
        pytest.fail(f"flowgraph did not finish: {calls[0]} work calls, {sum(sizes)} samples")
    assert sum(sizes) == total
    assert all(size % N == 0 for size in sizes)
    assert calls[0] <= total // N + 3


# -- attach order and restarts ----------------------------------------------


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


class _RecordingSource:
    description = "recording"

    def __init__(self):
        self.calls = []

    def set_receive_feed(self, feed):
        self.calls.append(("feed", feed))

    def start(self):
        self.calls.append(("start",))

    def stop(self):
        self.calls.append(("stop",))

    def recv(self, timeout):
        return None


def test_attach_hands_the_feed_to_the_source_before_it_starts():
    from ofdm_message_link.rx_app import ReceiveWorker

    worker = ReceiveWorker(object(), _Bridge(), min_feed_samples=65536, max_merge_wait_s=0.015)
    source = _RecordingSource()
    worker.attach(source)
    worker.detach()
    worker.attach(source)
    feed = ("feed", ReceiveFeed(65536, 0.015))
    assert source.calls == [feed, ("start",), ("stop",), feed, ("start",)]


def test_a_source_without_a_feed_setter_still_attaches():
    from ofdm_message_link.rx_app import ReceiveWorker

    class Plain:
        description = "plain"
        started = False

        def start(self):
            self.started = True

        def stop(self):
            pass

    worker = ReceiveWorker(object(), _Bridge())
    source = Plain()
    worker.attach(source)
    assert source.started and worker.source is source


@pytest.mark.gnuradio
def test_a_restart_reapplies_the_feed_and_counts_offsets_from_zero(fake_usrp):
    total = 8 * N
    built = fake_usrp(_ramp(total), {0: T0, 3 * N: _seconds(3 * N)})
    source = UhdSampleSource(_settings())
    source.set_receive_feed(FEED)
    source.start()
    with pytest.raises(TransportError, match="before the source is started"):
        source.set_receive_feed(ReceiveFeed(4096, 0.015))
    try:
        first = _drain(source, total)
    finally:
        source.stop()
    assert source.time_anchors()[0] == 2
    source.set_receive_feed(ReceiveFeed(4096, 0.015))
    source.start()
    assert source.ingest_multiple == 4096
    try:
        second = _drain(source, total)
    finally:
        source.stop()
    assert len(built) == 2
    np.testing.assert_array_equal(np.concatenate(first), _ramp(total))
    np.testing.assert_array_equal(np.concatenate(second), _ramp(total))
    assert all(chunk.size % 4096 == 0 for chunk in second)
    assert source.time_anchors() == (2, ((0, T0, 0), (3 * N, _seconds(3 * N), 0)))
    # Two chunks of room plus the next N being collected.
    assert built[-1].min_output_buffer == 2 * source.snapshot()["chunk_samples"] + 4096


# -- the receive worker's own merge ----------------------------------------


class _QueuedSource:
    description = "queued"

    def __init__(self, chunks, ingest_multiple=None, late=None):
        self._queue = queue.Queue()
        for chunk in chunks:
            self._queue.put(chunk)
        if ingest_multiple is not None:
            self.ingest_multiple = ingest_multiple
        if late is not None:  # (delay, chunk): arrives while a worker would wait
            import threading

            threading.Timer(late[0], self._queue.put, (late[1],)).start()

    def recv(self, timeout):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


def _worker(wait_s=0.1, min_feed=65536):
    from ofdm_message_link.rx_app import ReceiveWorker

    return ReceiveWorker(object(), _Bridge(), min_feed_samples=min_feed, max_merge_wait_s=wait_s)


@pytest.mark.parametrize(
    ("min_feed", "multiple"),
    [
        (65536, 15000),    # N limited by the wait (15 ms at 1 MS/s)
        (131072, 65536),   # N limited by the chunk cap (131072 / 20 ms at 5 MS/s)
    ],
)
def test_a_batching_source_is_never_waited_on(min_feed, multiple):
    """Feeds from a batching source are N (plus what is queued); a later chunk is not awaited."""

    worker = _worker(wait_s=0.1, min_feed=min_feed)
    first = np.zeros(multiple, np.complex64)
    source = _QueuedSource([first], ingest_multiple=multiple,
                           late=(0.02, np.ones(multiple, np.complex64)))
    started = time.monotonic()
    chunk = worker._next_chunk(source)
    assert time.monotonic() - started < 0.015
    assert chunk is first
    assert worker._next_chunk(source).size == multiple  # the late one is the next feed


def test_a_batching_source_still_merges_what_is_already_queued():
    worker = _worker(wait_s=0.015)
    parts = [np.full(15000, k, np.complex64) for k in range(3)]
    source = _QueuedSource(parts, ingest_multiple=15000)
    started = time.monotonic()
    chunk = worker._next_chunk(source)
    np.testing.assert_array_equal(chunk, np.concatenate(parts))
    assert time.monotonic() - started < 0.05


def test_a_source_that_does_not_batch_keeps_the_merge_wait():
    worker = _worker()
    for source in (
        _QueuedSource([np.zeros(15000, np.complex64)]),
        _QueuedSource([np.zeros(15000, np.complex64)], ingest_multiple=1),
    ):
        started = time.monotonic()
        assert worker._next_chunk(source).size == 15000
        assert time.monotonic() - started >= 0.09


# -- end to end: bursts across N, device time in --events ------------------


def _burst_stream(profile, n, *, seed=5):
    """Two bursts, the first across the N boundary, in a stream a multiple of N long."""

    from ofdm_link.phy.mcs_table import mcs_entry
    from ofdm_message_link.link import MessageTransmitter

    rng = np.random.default_rng(seed)
    payloads = [rng.integers(0, 256, 200, dtype=np.uint8).tobytes() for _ in range(2)]
    bursts = []
    for payload in payloads:
        (burst,) = MessageTransmitter(profile, mcs_entry=mcs_entry(8)).encode_message(payload)
        bursts.append(np.asarray(burst.samples, dtype=np.complex64) * np.complex64(0.2))
    places = [n - bursts[0].size // 2, 3 * n + 777]
    assert places[0] < n < places[0] + bursts[0].size
    total = 6 * n
    stream = np.zeros(total, dtype=np.complex64)
    for place, burst in zip(places, bursts, strict=True):
        stream[place : place + burst.size] = burst
    noise = rng.normal(scale=0.002, size=(total, 2))
    stream = (stream + (noise[:, 0] + 1j * noise[:, 1])).astype(np.complex64)
    return stream, payloads, places


def _reference_offsets(profile, stream):
    from ofdm_message_link.burst_events import BurstEventRecorder
    from ofdm_message_link.link import MessageReceiver

    receiver = MessageReceiver(profile)
    recorder = BurstEventRecorder(float(profile.sample_rate))
    receiver.enable_burst_events(recorder)
    try:
        for start in range(0, stream.size, 4096):
            receiver.feed(stream[start : start + 4096])
        receiver.flush(30.0)
    finally:
        receiver.close()
    recorder.flush()
    events, _ = recorder.drain()
    return sorted(e.off for e in events if e.pl == "ok")


@pytest.mark.gnuradio
@pytest.mark.parametrize("gap_before_second", [False, True])
def test_bursts_across_n_decode_with_exact_offsets_and_device_time(
    fake_usrp, gap_before_second
):
    from stream_fixtures import profile as build_profile

    from ofdm_message_link.burst_events import BurstEvent, BurstEventRecorder, BurstEventTap
    from ofdm_message_link.link import MessageReceiver
    from ofdm_message_link.rx_app import ReceiveWorker

    profile = build_profile()
    rate = float(profile.sample_rate)
    stream, payloads, places = _burst_stream(profile, 8192)
    t0 = 1234.5
    tags = {0: t0}
    gap_at = 2 * 8192 + 500
    if gap_before_second:
        tags[gap_at] = t0 + (gap_at + 2000) / rate
    fake_usrp(stream, tags, pace_s=0.002)
    settings = _settings(rate)
    source = UhdSampleSource(settings)
    boundaries = []
    recv = source.recv

    def recording_recv(timeout):
        chunk = recv(timeout)
        if chunk is not None:
            boundaries.append((boundaries[-1] if boundaries else 0) + chunk.size)
        return chunk

    source.recv = recording_recv
    receiver = MessageReceiver(profile)
    recorder = BurstEventRecorder(rate)
    receiver.enable_burst_events(recorder)
    batches = []
    tap = BurstEventTap(None, recorder, lambda kind, value: batches.append(value), lambda: source)
    bridge = _Bridge()
    # PC defaults: 8192 samples, 2 ms.
    worker = ReceiveWorker(receiver, bridge, observer=tap)
    worker.start()
    worker.attach(source)
    assert source.ingest_multiple == 8192
    deadline = time.monotonic() + 20.0
    try:
        while len(bridge.message.values) < len(payloads) and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()
    tap.emit(final=True)

    assert not bridge.failed.values
    # The first burst really was cut by a chunk boundary.
    assert 8192 in boundaries and all(b % 8192 == 0 for b in boundaries)
    assert [value.message.payload for value in bridge.message.values] == payloads
    events = [BurstEvent(*row) for batch in batches for row in batch["events"]]
    data = sorted((e for e in events if e.pl == "ok"), key=lambda e: e.off)
    # The offset is where the decoder puts the preamble in the stream: the
    # same as for the stream fed straight from memory, and the same distance
    # past where each burst's samples were placed.
    assert [e.off for e in data] == _reference_offsets(profile, stream)
    assert len({e.off - place for e, place in zip(data, places, strict=True)}) == 1
    assert 0 <= data[0].off - places[0] < 64
    anchors = sorted(tags.items())
    for event in data:
        anchor_off, anchor_sec = max((a for a in anchors if a[0] <= event.off))
        assert event.dev_t == pytest.approx(anchor_sec + (event.off - anchor_off) / rate,
                                            abs=1e-12)
    overflows = [o for batch in batches for o in batch["overflows"]]
    assert overflows == ([(gap_at, tags[gap_at], 2000)] if gap_before_second else [])
    assert source.snapshot()["rx_samples_lost"] == (2000 if gap_before_second else 0)
