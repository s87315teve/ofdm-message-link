"""A transport send that raises must not lose the burst it was given.

The transmit worker keeps such a burst (same samples, sequence numbers and
MCS), backs off, and sends it first once the transport accepts again, the
way it already kept a burst interrupted by a detach.  A burst whose encoding
raises is still dropped, but counted.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from ofdm_link.phy.mcs_table import mcs_entry
from ofdm_message_link.aggregation import AggregationLimits
from ofdm_message_link.link import (
    MessageReceiver,
    MessageTransmitter,
    build_phy_profile,
    load_link_config,
)
from ofdm_message_link.transport import TransportError
from ofdm_message_link.tx_app import _ERROR_BACKOFF_S, ControlPlan, TransmitWorker
from ofdm_message_link.tx_health import tx_health

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
        self.sent = _Signal()
        self.failed = _Signal()


class _RecordingTransmitter(MessageTransmitter):
    """Records every burst it encodes; ``fail_encodes`` makes the next N encodes raise."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.encoded = []
        self.encode_calls = 0
        self.fail_encodes = 0

    def encode_datagrams(self, datagrams, **kwargs):
        self.encode_calls += 1
        if self.fail_encodes > 0:
            self.fail_encodes -= 1
            raise ValueError("cannot encode this burst")
        burst = super().encode_datagrams(datagrams, **kwargs)
        self.encoded.append(burst)
        return burst


class _FlakySink:
    """Raises on the sends ``fail(n)`` says should fail; records every attempt.

    A raising send transmits nothing, so it declares ``retry_failed_send``
    like the udp and UHD sinks; :class:`_NoRetrySink` is one that does not.
    """

    retry_failed_send = True

    def __init__(self, fail=lambda attempt: False, error=None):
        self._fail = fail
        self._error = error or TransportError("UDP send failed: [Errno 111] Connection refused")
        self.attempts = []  # (monotonic time, samples object, accepted)
        self.accepted = []
        self.stopped = False
        self._lock = threading.Lock()

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def send(self, samples):
        with self._lock:
            failing = self._fail(len(self.attempts))
            self.attempts.append((time.monotonic(), samples, not failing))
            if failing:
                raise self._error
            self.accepted.append(samples)
            return True


def _wait_for(condition, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)
    return condition()


def _worker(profile, *, entry=8, policy=None, limits=None):
    transmitter = _RecordingTransmitter(profile, mcs_entry=mcs_entry(entry))
    bridge = _Bridge()
    worker = TransmitWorker(
        transmitter, bridge, aggregation=limits or AggregationLimits(), policy=policy
    )
    return worker, transmitter, bridge


def _messages(count, size=120):
    return [f"message {index:03d} ".encode().ljust(size, b".") for index in range(count)]


def _burst_of(transmitter, samples):
    (burst,) = [burst for burst in transmitter.encoded if burst.samples is samples]
    return burst


def _decoded(profile, accepted):
    """Every message a receiver gets out of the accepted bursts, in order."""

    receiver = MessageReceiver(profile)
    gap = np.zeros(3000, np.complex64)
    stream = np.concatenate([gap, *[np.asarray(s, np.complex64) for s in accepted], gap])
    return [item.message.payload for item in receiver.feed(stream.astype(np.complex64))]


def _assert_each_burst_once_in_order(transmitter, sink):
    """Every encoded burst accepted exactly once, in encode order, as encoded."""

    assert [id(s) for s in sink.accepted] == [id(b.samples) for b in transmitter.encoded]
    sequences = [seq for burst in transmitter.encoded for seq in burst.sequences]
    assert sequences == list(range(len(sequences)))


@pytest.mark.parametrize("failures", [1, 3])
def test_a_burst_whose_send_raises_goes_out_first_and_unchanged(profile, failures):
    worker, transmitter, bridge = _worker(profile)
    messages = _messages(30)
    for message in messages:
        assert worker.submit("t", message)
    sink = _FlakySink(fail=lambda attempt: attempt < failures)
    worker.start()
    worker.attach(sink)
    try:
        # The MCS changes while the first burst is failing: the kept burst must
        # still go at the MCS it was encoded with, not be re-encoded.
        assert _wait_for(lambda: len(sink.attempts) >= 1)
        worker.select_mcs_entry(mcs_entry(12))
        assert _wait_for(lambda: sum(r.message_count for r in bridge.sent.values) == 30)
    finally:
        worker.stop()
    failed = [samples for _, samples, ok in sink.attempts if not ok]
    assert len(failed) == failures
    # Every failed attempt and the first accepted one are the same burst object.
    first = sink.accepted[0]
    assert all(samples is first for samples in failed)
    kept = _burst_of(transmitter, first)
    assert kept.sequences[0] == 0 and kept.mcs_index == 8
    assert transmitter.encoded[0] is kept  # encoded once, never again
    assert len(transmitter.encoded) == transmitter.encode_calls
    _assert_each_burst_once_in_order(transmitter, sink)
    assert [r.first_sequence for r in bridge.sent.values] == [
        b.sequences[0] for b in transmitter.encoded
    ]
    assert worker.send_failures == failures
    assert len(bridge.failed.values) == 1 and "kept and sent again" in bridge.failed.values[0]
    # The receiver gets every message exactly once, in order.
    assert _decoded(profile, sink.accepted) == messages
    assert worker.pending == 0


def test_retries_are_paced_and_reported_once_per_streak(profile):
    worker, transmitter, bridge = _worker(profile)
    for message in _messages(5):
        assert worker.submit("t", message)
    sink = _FlakySink(fail=lambda attempt: True)
    worker.start()
    worker.attach(sink)
    # The worker thread's own CPU time (the process may run other threads).
    clock_id = time.pthread_getcpuclockid(worker._thread.ident)
    cpu_started = time.clock_gettime(clock_id)
    time.sleep(0.6)
    cpu = time.clock_gettime(clock_id) - cpu_started
    stop_started = time.monotonic()
    worker.stop()
    stop_s = time.monotonic() - stop_started
    times = [t for t, _, _ in sink.attempts]
    # One attempt per back-off, never a busy loop.
    assert 6 <= len(times) <= 0.6 / _ERROR_BACKOFF_S + 2
    assert min(np.diff(times)) >= 0.8 * _ERROR_BACKOFF_S
    assert cpu < 0.3
    # Always the same burst; the rest wait their turn.
    assert len({id(samples) for _, samples, _ in sink.attempts}) == 1
    assert len(transmitter.encoded) == 1
    assert worker.send_failures == len(times)
    assert len(bridge.failed.values) == 1  # once per streak, not once per second
    assert bridge.sent.values == []
    assert stop_s < 0.5 and sink.stopped


def test_a_persistent_failure_holds_a_bounded_queue(profile):
    worker, transmitter, bridge = _worker(profile)
    sink = _FlakySink(fail=lambda attempt: True)
    worker.start()
    worker.attach(sink)
    try:
        assert worker.submit("t", b"first")
        assert _wait_for(lambda: len(sink.attempts) >= 1)
        accepted = sum(worker.submit("t", b"x" * 50) for _ in range(1000))
        time.sleep(0.2)
    finally:
        worker.stop()
    # The send queue's own bound: the rest is refused (and counted by the ingress).
    assert accepted <= 256 + 1
    assert worker.pending <= 256 + 2
    assert len(transmitter.encoded) <= 1


def test_detach_during_a_failure_streak_sends_the_burst_on_the_next_radio(profile):
    worker, transmitter, bridge = _worker(profile)
    messages = _messages(12)
    for message in messages:
        assert worker.submit("t", message)
    broken = _FlakySink(fail=lambda attempt: True)
    worker.start()
    worker.attach(broken)
    try:
        assert _wait_for(lambda: len(broken.attempts) >= 3)
        worker.detach()
        assert broken.stopped
        attempts_at_detach = len(broken.attempts)
        time.sleep(0.15)
        assert len(broken.attempts) <= attempts_at_detach + 1  # no retries without a radio
        good = _FlakySink()
        worker.attach(good)
        assert _wait_for(lambda: sum(r.message_count for r in bridge.sent.values) == 12)
    finally:
        worker.stop()
    assert good.accepted[0] is broken.attempts[0][1]
    _assert_each_burst_once_in_order(transmitter, good)
    assert _decoded(profile, good.accepted) == messages
    assert len(bridge.failed.values) == 1


def test_a_new_radio_failing_is_reported_again(profile):
    worker, transmitter, bridge = _worker(profile)
    worker.submit("t", b"hello")
    first = _FlakySink(fail=lambda attempt: True)
    worker.start()
    worker.attach(first)
    try:
        assert _wait_for(lambda: len(first.attempts) >= 2)
        worker.detach()
        # At once: the first report of this streak falls inside the one-per-
        # second rate limit, so it must be retried, not marked as reported.
        second = _FlakySink(fail=lambda attempt: attempt < 30)  # about 1.5 s
        worker.attach(second)
        assert _wait_for(lambda: len(second.accepted) == 1)
    finally:
        worker.stop()
    assert len(bridge.failed.values) == 2
    assert second.accepted[0] is first.attempts[0][1]
    assert worker.dropped_send_failed == 0


def test_a_second_streak_on_the_same_radio_within_a_second_is_reported(profile):
    worker, transmitter, bridge = _worker(profile, limits=AggregationLimits(max_subframes=1))
    for message in _messages(3):
        worker.submit("t", message)
    # Fail, succeed, then fail again for about 1.5 s.
    sink = _FlakySink(fail=lambda attempt: attempt == 0 or 2 <= attempt < 32)
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: len(sink.accepted) == 3)
    finally:
        worker.stop()
    assert len(bridge.failed.values) == 2, bridge.failed.values
    assert worker.send_failures == 31 and worker.dropped_send_failed == 0
    _assert_each_burst_once_in_order(transmitter, sink)


def test_a_failing_report_never_costs_the_kept_burst(profile):
    worker, transmitter, bridge = _worker(profile)

    class Torn(_Signal):
        def emit(self, value):
            raise RuntimeError("wrapped C/C++ object of type _Bridge has been deleted")

    bridge.failed = Torn()
    messages = _messages(3, size=2000)
    for message in messages:
        worker.submit("t", message)
    sink = _FlakySink(fail=lambda attempt: attempt < 2)
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: worker.pending == 0 and len(sink.accepted) >= 1)
        time.sleep(0.1)
    finally:
        worker.stop()
    assert sink.accepted[0] is sink.attempts[0][1]
    _assert_each_burst_once_in_order(transmitter, sink)
    assert _decoded(profile, sink.accepted) == messages


def test_a_persistent_failure_gives_up_after_the_bound_and_the_queue_flows_on(profile):
    from ofdm_message_link import tx_app

    worker, transmitter, bridge = _worker(profile, limits=AggregationLimits(max_subframes=1))
    messages = _messages(6)
    for message in messages:
        worker.submit("t", message)
    # Fails until the worker has given up on the first burst, then recovers.
    sink = _FlakySink(fail=lambda attempt: worker.dropped_send_failed_bursts == 0)
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: len(sink.accepted) == 5, timeout_s=6.0)
        time.sleep(0.1)
    finally:
        worker.stop()
    failed = [(t, samples) for t, samples, ok in sink.attempts if not ok]
    # Every failed attempt was the first burst, for the whole retry bound.
    assert all(samples is failed[0][1] for _, samples in failed)
    assert failed[-1][0] - failed[0][0] >= tx_app._SEND_GIVE_UP_S - _ERROR_BACKOFF_S
    assert len(failed) >= tx_app._SEND_GIVE_UP_ATTEMPTS
    assert len(failed) <= tx_app._SEND_GIVE_UP_S / _ERROR_BACKOFF_S + 3
    assert (worker.dropped_send_failed_bursts, worker.dropped_send_failed) == (1, 1)
    assert worker.send_failures == len(failed)
    # The rest goes out, each once and in order, after the dropped one.
    assert _decoded(profile, sink.accepted) == messages[1:]
    assert [b.sequences[0] for b in transmitter.encoded] == list(range(6))
    assert [id(s) for s in sink.accepted] == [id(b.samples) for b in transmitter.encoded[1:]]
    assert len(bridge.failed.values) == 2  # the streak, and that it gave up
    assert "gave up: 1 burst(s), 1 datagram(s) dropped" in bridge.failed.values[1]


def test_after_giving_up_each_burst_gets_one_attempt_until_a_send_succeeds(profile):
    from ofdm_message_link import tx_app

    worker, transmitter, bridge = _worker(profile, limits=AggregationLimits(max_subframes=1))
    for message in _messages(20):
        worker.submit("t", message)
    sink = _FlakySink(fail=lambda attempt: True)
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: worker.dropped_send_failed_bursts >= 4, timeout_s=6.0)
    finally:
        worker.stop()
    failed = [samples for _, samples, _ in sink.attempts]
    first_count = sum(samples is failed[0] for samples in failed)
    assert first_count >= tx_app._SEND_GIVE_UP_ATTEMPTS
    later = failed[first_count:]
    assert len({id(samples) for samples in later}) == len(later)  # one attempt each
    assert worker.dropped_send_failed == worker.dropped_send_failed_bursts


def test_a_lone_failure_after_a_quiet_spell_is_retried_again(profile):
    from ofdm_message_link import tx_app

    worker, transmitter, bridge = _worker(profile)
    state = {"dead": True, "blip_at": None}
    sink = _FlakySink(fail=lambda n: state["dead"] or n == state["blip_at"])
    worker.submit("t", b"during the outage")
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: worker.dropped_send_failed_bursts == 1)
        state["dead"] = False  # healthy again; nothing to send for a while
        time.sleep(tx_app._SEND_STREAK_GAP_S + 0.3)
        state["blip_at"] = len(sink.attempts)  # one transient failure
        assert worker.submit("t", b"after the quiet spell")
        assert _wait_for(lambda: len(sink.accepted) == 1)
    finally:
        worker.stop()
    # Retried, not dropped as part of the old, given-up streak.
    assert worker.dropped_send_failed_bursts == 1
    assert sink.attempts[-2][1] is sink.attempts[-1][1] is sink.accepted[0]
    assert _decoded(profile, sink.accepted) == [b"after the quiet spell"]
    # A fresh streak: one attempt (the elapsed time may round above 0.0 s on a slow machine).
    assert bridge.failed.values[-1].endswith(
        " s, 1 attempt(s); the burst is kept and sent again)"
    )


class _NoRetrySink(_FlakySink):
    """A sink whose raising send may already have transmitted (like a Pluto)."""

    retry_failed_send = False


@pytest.mark.parametrize("sink_class", [_NoRetrySink, type("Bare", (_FlakySink,), {
    "retry_failed_send": property(lambda self: (_ for _ in ()).throw(AttributeError()))})])
def test_a_sink_that_cannot_retry_keeps_the_old_drop(profile, sink_class):
    worker, transmitter, bridge = _worker(profile, limits=AggregationLimits(max_subframes=1))
    messages = _messages(4)
    for message in messages:
        worker.submit("t", message)
    sink = sink_class(fail=lambda attempt: attempt == 0)
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: len(sink.accepted) == 3)
        time.sleep(0.1)
    finally:
        worker.stop()
    # The first burst is dropped, as before the retry existed, and counted.
    assert len(sink.attempts) == 4 and sink.attempts[0][1] is not sink.accepted[0]
    assert _decoded(profile, sink.accepted) == messages[1:]
    assert bridge.failed.values == [
        "TransportError: UDP send failed: [Errno 111] Connection refused"
    ]
    assert (worker.send_failures, worker.dropped_send_failed) == (1, 1)
    assert worker.dropped_send_failed_bursts == 1


def test_a_sink_that_cannot_retry_drains_as_before_under_a_persistent_failure(profile):
    worker, transmitter, bridge = _worker(profile, limits=AggregationLimits(max_subframes=1))
    for message in _messages(8):
        worker.submit("t", message)
    sink = _NoRetrySink(fail=lambda attempt: True)
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: worker.pending == 0, timeout_s=3.0)
    finally:
        worker.stop()
    assert len({id(samples) for _, samples, _ in sink.attempts}) == 8  # one attempt each
    assert worker.dropped_send_failed == 8 and len(bridge.failed.values) == 1


def test_which_sinks_retry():
    from ofdm_message_link import pluto
    from ofdm_message_link.node_engine import DutyCappedSink
    from ofdm_message_link.transport import UdpSampleSink, UhdSampleSink

    assert UdpSampleSink.retry_failed_send is True
    assert UhdSampleSink.retry_failed_send is True
    assert getattr(pluto.PlutoSampleSink, "retry_failed_send", False) is False
    # The duty-cap wrapper is what its wrapped sink is.
    assert DutyCappedSink(UdpSampleSink(), 0.5, 5e6).retry_failed_send is True
    assert DutyCappedSink(_NoRetrySink(), 0.5, 5e6).retry_failed_send is False
    with pytest.raises(AttributeError):
        DutyCappedSink(object(), 0.5, 5e6).retry_failed_send  # noqa: B018


def test_a_udp_retry_reaches_the_receiver_once(profile):
    """A send that fails mid-burst leaves an incomplete burst the source drops."""

    import socket as socket_module

    from ofdm_message_link.transport import UdpSampleSink, UdpSampleSource

    with socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    source = UdpSampleSource(bind_host="127.0.0.1", port=port)
    source.start()
    sink = UdpSampleSink(host="127.0.0.1", port=port)
    sink.start()

    class FailSecondFragment:
        def __init__(self, inner):
            self.inner, self.calls = inner, 0

        def sendto(self, data, address):
            self.calls += 1
            if self.calls == 2:
                raise OSError(105, "No buffer space available")
            return self.inner.sendto(data, address)

        def close(self):
            self.inner.close()

    sink._socket = FailSecondFragment(sink._socket)
    worker, transmitter, bridge = _worker(profile, limits=AggregationLimits(max_subframes=1))
    messages = _messages(12, size=900)  # each burst is several UDP fragments
    for message in messages:
        worker.submit("t", message)
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: worker.pending == 0)
        deadline = time.monotonic() + 3.0
        while source.snapshot()["bursts_received"] < 12 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()
        snapshot = source.snapshot()
        orphans = len(source._pending)
        source.stop()
    assert worker.send_failures == 1 and worker.dropped_send_failed == 0
    assert snapshot["bursts_received"] == 12  # the retried burst exactly once
    # The failed attempt's lone fragment: still held, or evicted and counted.
    assert orphans + snapshot["bursts_incomplete"] == 1


def test_a_send_that_raises_because_the_radio_was_stopped_is_a_detach(profile):
    worker, transmitter, bridge = _worker(profile)
    messages = _messages(4)
    for message in messages:
        worker.submit("t", message)

    class StoppedUnderneath(_FlakySink):
        def send(self, samples):
            # The window stops the radio while the worker is in send: the
            # sink is detached first, then its send fails.
            worker.detach()
            self.attempts.append((time.monotonic(), samples, False))
            raise TransportError("sink is not started")

    stopping = StoppedUnderneath()
    worker.start()
    worker.attach(stopping)
    try:
        assert _wait_for(lambda: len(stopping.attempts) == 1 and worker.sink is None)
        good = _FlakySink()
        worker.attach(good)
        assert _wait_for(lambda: sum(r.message_count for r in bridge.sent.values) == 4)
    finally:
        worker.stop()
    assert good.accepted[0] is stopping.attempts[0][1]
    _assert_each_burst_once_in_order(transmitter, good)
    assert worker.send_failures == 0 and bridge.failed.values == []


def test_an_error_after_the_send_does_not_send_the_burst_twice(profile):
    worker, transmitter, bridge = _worker(profile)

    class BrokenSent(_Signal):
        raised = False

        def emit(self, value):
            if not BrokenSent.raised:
                BrokenSent.raised = True
                raise RuntimeError("display gone")
            super().emit(value)

    bridge.sent = BrokenSent()
    for message in _messages(3, size=2000):  # several bursts
        worker.submit("t", message)
    sink = _FlakySink()
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: len(sink.accepted) == len(transmitter.encoded)
                         and worker.pending == 0)
        time.sleep(0.1)
    finally:
        worker.stop()
    _assert_each_burst_once_in_order(transmitter, sink)
    assert worker.send_failures == 0


def test_a_burst_whose_encode_raises_is_dropped_counted_and_not_retried(profile):
    worker, transmitter, bridge = _worker(profile, limits=AggregationLimits(max_subframes=4))
    transmitter.fail_encodes = 1
    messages = _messages(10)
    for message in messages:
        assert worker.submit("t", message)
    sink = _FlakySink()
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: worker.pending == 0 and worker.encode_failures == 1)
        time.sleep(0.1)
    finally:
        worker.stop()
    assert worker.encode_failures == 1 and worker.dropped_encode_datagrams == 4
    assert transmitter.encode_calls == len(transmitter.encoded) + 1  # no retry loop
    (failure,) = bridge.failed.values
    assert failure.startswith("encode failed, 4 datagram(s) dropped (4 so far): ValueError")
    # The rest goes out, and the drop shows in the TX health record.
    assert _decoded(profile, sink.accepted) == messages[4:]
    health = tx_health(None, queue_datagrams=worker.pending, ingress_dropped_queue_full=0,
                       send_failures=worker.send_failures,
                       dropped_encode_failed=worker.dropped_encode_datagrams)
    assert health["dropped_encode_failed"] == 4 and health["send_failures"] == 0


class _CountingPolicy:
    """A BurstPolicy (node_app's hook) that counts its stateful calls."""

    def __init__(self, entry):
        self.entry = entry
        self.decisions = 0
        self.sent = []

    def entry_for_next_burst(self, now):
        self.decisions += 1
        return self.entry

    def control_plan(self, now, *, idle):
        return ControlPlan(() if idle else (b"link header",))

    def control_entry(self):
        return mcs_entry(8)

    def idle_wait_s(self, now):
        return None

    def on_sent(self, burst, now):
        self.sent.append(burst)


def test_with_a_policy_a_failed_send_neither_decides_again_nor_reports_sent(profile):
    policy = _CountingPolicy(mcs_entry(14))
    worker, transmitter, bridge = _worker(
        profile, policy=policy, limits=AggregationLimits(max_subframes=4)
    )
    messages = _messages(20)
    for message in messages:
        assert worker.submit("t", message)
    sink = _FlakySink(fail=lambda attempt: attempt in (0, 1, 4))
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: sum(r.message_count for r in bridge.sent.values) == 20)
    finally:
        worker.stop()
    # One decision and one on_sent per burst, failed attempts included.
    assert policy.decisions == len(transmitter.encoded) == len(policy.sent)
    assert [id(b.samples) for b in policy.sent] == [id(b.samples) for b in transmitter.encoded]
    assert all(b.mcs_index == 14 for b in policy.sent)
    _assert_each_burst_once_in_order(transmitter, sink)
    assert worker.send_failures == 3
    assert _decoded(profile, sink.accepted) == messages
