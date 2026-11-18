"""The aggregation wait is sized at the MCS the burst will actually get.

node_app's bursts take their MCS from the burst policy (the link
adaptation), while the transmitter's active entry stays at the startup
``--mcs``.  ``TransmitWorker._fill`` must judge "this burst is full, stop
waiting" at the policy's entry, which it reads with the side-effect-free
``peek_next_entry``; the stateful ``entry_for_next_burst`` is still called
once per burst.  Without a policy (tx_app) nothing changes.
"""

from __future__ import annotations

import io
import threading
import time
from collections import deque

import pytest
from test_la_controller import _controller

from ofdm_link.phy.mcs_table import mcs_entry
from ofdm_message_link.aggregation import AggregationLimits
from ofdm_message_link.link import MessageTransmitter, build_phy_profile, load_link_config
from ofdm_message_link.tx_app import ControlPlan, TransmitWorker

CONFIG = "configs/default.yaml"
SIZE = 300
# At 10 MS/s, 300-byte datagrams in a 1.5 ms burst: 2 fit at MCS 8, 9 at MCS 14.
LIMITS = AggregationLimits(max_burst_airtime_s=0.0015, max_wait_s=0.15)


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


class _Policy:
    """A BurstPolicy whose entry differs from the transmitter's active one."""

    def __init__(self, entry, *, peek=True):
        self.entry = entry
        self.decisions = 0
        self.peeks = 0
        self.sent = []
        if not peek:
            self.peek_next_entry = None

    def peek_next_entry(self):
        self.peeks += 1
        return self.entry

    def entry_for_next_burst(self, now):
        self.decisions += 1
        return self.entry

    def control_plan(self, now, *, idle):
        return ControlPlan()

    def control_entry(self):
        return mcs_entry(8)

    def idle_wait_s(self, now):
        return None

    def on_sent(self, burst, now):
        self.sent.append(burst)


class _NoPeekPolicy(_Policy):
    """A policy written before ``peek_next_entry`` existed."""

    def __getattribute__(self, name):
        if name == "peek_next_entry":
            raise AttributeError(name)
        return super().__getattribute__(name)


def _fits(profile, entry_index, limits=LIMITS):
    transmitter = MessageTransmitter(profile)
    queue = [transmitter.fragment_message(bytes([i]) * SIZE)[0] for i in range(32)]
    return transmitter.plan_burst(queue, limits, mcs_entry(entry_index))


def _idle_worker(profile, policy, *, active=8, limits=LIMITS):
    transmitter = MessageTransmitter(profile, mcs_entry=mcs_entry(active))
    return TransmitWorker(transmitter, _Bridge(), aggregation=limits, policy=policy)


def _fill_with(worker, preload, *, feed_every_s=None, feed=0):
    """Run ``_fill`` on ``preload`` queued datagrams (and ``feed`` more arriving)."""

    for index in range(preload):
        assert worker.submit("t", bytes([index]) * SIZE)
    started = time.monotonic()
    if feed:
        def feeder():
            for index in range(feed):
                time.sleep(feed_every_s)
                worker.submit("t", bytes([100 + index]) * SIZE)

        threading.Thread(target=feeder, daemon=True).start()
    pending = deque()
    worker._fill(pending)
    return len(pending), time.monotonic() - started


def test_the_limits_make_the_two_entries_differ(profile):
    assert (_fits(profile, 8), _fits(profile, 14)) == (2, 9)


def test_a_burst_full_at_the_policy_entry_stops_the_wait_at_once(profile):
    fit = _fits(profile, 14)
    policy = _Policy(mcs_entry(14))
    count, elapsed = _fill_with(_idle_worker(profile, policy), fit + 1)
    assert count == fit + 1 and elapsed < 0.05
    assert policy.decisions == 0  # _fill never makes the decision


def test_room_at_the_policy_entry_keeps_waiting_even_if_the_active_entry_is_full(profile):
    fit, active_fit = _fits(profile, 14), _fits(profile, 8)
    policy = _Policy(mcs_entry(14))
    count, elapsed = _fill_with(_idle_worker(profile, policy), active_fit + 1)
    # The burst at MCS 14 still had room: the wait runs to max_wait (0.15 s),
    # which is all it may ever add.
    assert count == active_fit + 1 < fit
    assert 0.13 <= elapsed < 0.3
    assert policy.decisions == 0 and policy.peeks == 1


def test_the_wait_ends_exactly_when_the_policy_entry_burst_is_full(profile):
    fit, active_fit = _fits(profile, 14), _fits(profile, 8)
    limits = AggregationLimits(max_burst_airtime_s=0.0015, max_wait_s=2.0)
    policy = _Policy(mcs_entry(14))
    worker = _idle_worker(profile, policy, limits=limits)
    count, elapsed = _fill_with(worker, active_fit + 1, feed_every_s=0.02, feed=20)
    assert count == fit + 1  # one more than a burst at MCS 14 holds: stop
    assert elapsed < 1.0  # far from the 2 s deadline
    worker._stop.set()


@pytest.mark.parametrize("policy", [None, _NoPeekPolicy(mcs_entry(14)),
                                    _Policy(mcs_entry(14), peek=False)])
def test_without_a_peek_the_wait_is_sized_at_the_active_entry_as_before(profile, policy):
    # tx_app (no policy) and a policy without peek_next_entry: unchanged.
    active_fit = _fits(profile, 8)
    count, elapsed = _fill_with(_idle_worker(profile, policy), active_fit + 1)
    assert count == active_fit + 1 and elapsed < 0.05


def test_a_peek_that_fails_or_returns_junk_falls_back_to_the_active_entry(profile):
    class Raising(_Policy):
        def peek_next_entry(self):
            raise RuntimeError("buggy peek")

    class Junk(_Policy):
        def peek_next_entry(self):
            return "MCS 14"

    class Legacy(_Policy):
        def peek_next_entry(self):
            return mcs_entry(0)  # a wire v1 entry: plan_burst must never see it

    active_fit = _fits(profile, 8)
    for policy in (Raising(mcs_entry(14)), Legacy(mcs_entry(14)), Junk(mcs_entry(14))):
        worker = _idle_worker(profile, policy)
        count, elapsed = _fill_with(worker, active_fit + 1)
        assert count == active_fit + 1 and elapsed < 0.05
    assert worker.policy_errors == 0  # junk is ignored, not an error


def test_the_decision_is_made_once_per_burst(profile):
    # A running worker with a burst-rate cap, so _fill waits before most bursts.
    limits = AggregationLimits(max_burst_airtime_s=0.003, min_burst_interval_s=0.02)
    policy = _Policy(mcs_entry(14))
    worker = _idle_worker(profile, policy, limits=limits)

    class Sink:
        def __init__(self):
            self.count = 0

        def start(self):
            pass

        def stop(self):
            pass

        def send(self, samples):
            self.count += 1
            return True

    sink = Sink()
    worker.start()
    worker.attach(sink)
    try:
        for index in range(60):
            assert worker.submit("t", bytes([index]) * SIZE)
            time.sleep(0.004)
        deadline = time.monotonic() + 4.0
        while worker.pending and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()
    assert worker.pending == 0
    assert policy.decisions == len(policy.sent) == sink.count
    assert policy.peeks >= 1
    assert all(burst.mcs_index == 14 for burst in policy.sent)
    # Bursts were filled at MCS 14's capacity, not MCS 8's (2 per burst).
    assert max(len(burst.datagrams) for burst in policy.sent) > _fits(profile, 8, limits)


# -- LinkController.peek_next_entry ------------------------------------------------


def test_peek_has_no_side_effects_and_matches_the_next_decision():
    controller, clock, la, reporter, codec, log, events = _controller()
    decide_calls = []
    original = la.decide
    la.decide = lambda now: decide_calls.append(now) or original(now)
    before = controller.snapshot()
    assert controller.peek_next_entry() is None  # nothing decided yet
    assert controller.snapshot() == before and decide_calls == [] and events == []
    assert log.getvalue() == ""
    decided = controller.entry_for_next_burst(1.0)
    assert len(decide_calls) == 1
    after = controller.snapshot()
    log_after = log.getvalue()
    for _ in range(5):
        assert controller.peek_next_entry() == decided
    assert controller.snapshot() == after and len(decide_calls) == 1
    assert log.getvalue() == log_after
    # Fixed mode: exactly what the next decision returns.
    controller.set_fixed(mcs_entry(10))
    assert controller.peek_next_entry() == mcs_entry(10) == controller.entry_for_next_burst(2.0)


def test_peek_in_a_fresh_fixed_mode_controller():
    controller, *_ = _controller(mode="fixed", la_log=io.StringIO())
    assert controller.peek_next_entry() == controller.entry_for_next_burst(0.0)
