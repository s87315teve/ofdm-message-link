"""The transmit worker's BurstPolicy hook, and tx_app without one.

The worker drives a recording sink.  Without a policy its bursts must stay
bit-identical to what it sent before the hook existed: the hashes below were
recorded on the parent commit (duplex-fdd eb930aa) with the same fixed
datagram origin, timestamp and message set.
"""

from __future__ import annotations

import errno
import hashlib
import socket
import threading
import time

import numpy as np
import pytest

from ofdm_link.phy.mcs_table import mcs_entry
from ofdm_message_link import datagram
from ofdm_message_link.aggregation import AggregationLimits
from ofdm_message_link.control import decode_lcp, link_header
from ofdm_message_link.link import (
    MessageReceiver,
    MessageTransmitter,
    build_phy_profile,
    control_mpdu_length,
    load_link_config,
)
from ofdm_message_link.tx_app import ControlPlan, IngressListener, TransmitWorker

CONFIG = "configs/default.yaml"

GOLDEN_SHA256 = {
    8: (6, "c70fbc13b897c24b566b09294fa02c44234ad7051f11e4b36713de1d2fe37bab"),
    12: (3, "2474efd5dc264f0557c8c5461f682841d7bf3e1f52e6b4dba6eaa15dabfd33e3"),
    14: (3, "31243131326b691728a9deece8761c0e9968869433e81ee5b2a4b5811daa7f1a"),
}


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


class _Sink:
    def __init__(self):
        self.bursts: list[np.ndarray] = []
        self._lock = threading.Lock()

    def start(self):
        pass

    def stop(self):
        pass

    def send(self, samples):
        with self._lock:
            self.bursts.append(np.array(samples))
        return True


def _wait_for(condition, timeout_s=4.0):
    deadline = time.monotonic() + timeout_s
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.005)
    return condition()


class _Policy:
    """Scripted BurstPolicy that records every call."""

    def __init__(self, entry, *, plans=(), idle_plans=(), idle_waits=()):
        self.entry = entry
        self.plans = list(plans)
        self.idle_plans = list(idle_plans)
        self.idle_waits = list(idle_waits)
        self.sent = []
        self.calls = []

    def entry_for_next_burst(self, now):
        self.calls.append("entry")
        return self.entry

    def control_plan(self, now, *, idle):
        self.calls.append(f"plan idle={idle}")
        source = self.idle_plans if idle else self.plans
        return source.pop(0) if source else ControlPlan((link_header(1),))

    def control_entry(self):
        return mcs_entry(8)

    def idle_wait_s(self, now):
        return self.idle_waits.pop(0) if self.idle_waits else None

    def on_sent(self, burst, now):
        self.sent.append(burst)


def _start(profile, *, policy=None, limits=None, entry=None):
    transmitter = MessageTransmitter(profile, mcs_entry=entry)
    bridge = _Bridge()
    worker = TransmitWorker(
        transmitter, bridge, aggregation=limits or AggregationLimits(), policy=policy
    )
    return worker, bridge


def test_without_a_policy_the_bursts_are_bit_identical_to_before(profile, monkeypatch):
    monkeypatch.setattr(datagram, "local_origin", lambda: 0x0123456789ABCDEF)
    monkeypatch.setattr(datagram.time, "monotonic_ns", lambda: 1_000_000_000)
    messages = [bytes([index]) * (50 + 300 * index) for index in range(12)]
    for index, (count, digest) in GOLDEN_SHA256.items():
        worker, bridge = _start(profile, entry=mcs_entry(index))
        for message in messages:
            assert worker.submit("t", message)
        sink = _Sink()
        worker.start()
        worker.attach(sink)
        try:
            assert _wait_for(
                lambda bridge=bridge: sum(r.message_count for r in bridge.sent.values)
                == len(messages)
            )
        finally:
            worker.stop()
        assert not bridge.failed.values
        hasher = hashlib.sha256()
        for burst in sink.bursts:
            hasher.update(np.ascontiguousarray(burst, dtype=np.complex64).tobytes())
        assert (len(sink.bursts), hasher.hexdigest()) == (count, digest), index
        assert all(r.control_subframes == 0 and not r.control_only for r in bridge.sent.values)


def test_the_policy_entry_is_used_for_both_plan_and_encode(profile):
    # Airtime cap fits several 300-byte datagrams at MCS 14 but few at MCS 8.
    limits = AggregationLimits(max_burst_airtime_s=0.0015)
    policy = _Policy(mcs_entry(14))
    worker, bridge = _start(profile, policy=policy, limits=limits, entry=mcs_entry(8))
    for index in range(12):
        assert worker.submit("t", bytes([index]) * 300)
    sink = _Sink()
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: sum(r.message_count for r in bridge.sent.values) == 12)
    finally:
        worker.stop()
    assert not bridge.failed.values
    first = policy.sent[0]
    assert first.mcs_index == 14
    reference = MessageTransmitter(profile)
    queue = [reference.fragment_message(bytes([i]) * 300)[0] for i in range(12)]
    expected = reference.plan_burst(
        queue,
        limits,
        mcs_entry(14),
        extra_mpdu_lengths=[control_mpdu_length(link_header(1))],
    )
    assert first.subframe_count == expected
    assert expected > reference.plan_burst(queue, limits, mcs_entry(8))
    assert len(sink.bursts) == len(policy.sent)
    assert all(burst.control_sequences for burst in policy.sent)
    assert sample_seconds(first, profile) <= limits.max_burst_airtime_s
    assert [r.control_subframes for r in bridge.sent.values] == [1] * len(policy.sent)


def sample_seconds(burst, profile):
    return burst.sample_count / profile.sample_rate


def test_control_payloads_go_first_and_reach_the_peer(profile):
    policy = _Policy(mcs_entry(12), plans=[ControlPlan((link_header(1, 2),))])
    worker, bridge = _start(profile, policy=policy)
    sink = _Sink()
    worker.start()
    worker.attach(sink)
    try:
        assert worker.submit("t", b"hello peer")
        assert _wait_for(lambda: len(policy.sent) == 1)
    finally:
        worker.stop()
    payloads = []

    class Handler:
        def on_control(self, payload, context):
            payloads.append(payload)
            from ofdm_message_link.link import ControlVerdict

            return ControlVerdict.ACCEPT

    receiver = MessageReceiver(profile, control_handler=Handler())
    gap = np.zeros(3000, np.complex64)
    stream = np.concatenate([gap, *sink.bursts, gap])
    delivered = receiver.feed(stream.astype(np.complex64))
    assert [item.message.payload for item in delivered] == [b"hello peer"]
    assert decode_lcp(payloads[0]).header.dst_node == 2
    assert policy.calls[:2] == ["entry", "plan idle=False"]


def test_an_idle_control_burst_goes_out_when_due(profile):
    policy = _Policy(
        mcs_entry(14),
        idle_waits=[0.0, 0.0],
        idle_plans=[ControlPlan((link_header(1, flags=1),))],
    )
    worker, bridge = _start(profile, policy=policy)
    sink = _Sink()
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: len(policy.sent) == 1)
        time.sleep(0.15)  # idle_wait_s is None now: nothing more is sent
    finally:
        worker.stop()
    assert len(policy.sent) == 1
    burst = policy.sent[0]
    assert burst.control_only and burst.mcs_index == 8
    (record,) = bridge.sent.values
    assert record.control_only and record.source == "control"
    assert (record.message_count, record.subframe_count, record.byte_count) == (0, 0, 0)
    assert record.control_subframes == 1
    assert "plan idle=True" in policy.calls and "entry" not in policy.calls


def test_an_empty_idle_plan_sends_nothing(profile):
    policy = _Policy(mcs_entry(8), idle_waits=[0.0] * 4, idle_plans=[ControlPlan(), ControlPlan()])
    worker, bridge = _start(profile, policy=policy)
    worker.start()
    worker.attach(_Sink())
    try:
        assert _wait_for(lambda: policy.calls.count("plan idle=True") == 2)
    finally:
        worker.stop()
    assert policy.sent == [] and not bridge.failed.values


def test_a_standalone_plan_sends_a_control_burst_before_the_data(profile):
    feedback = b"pretend feedback LCP"
    policy = _Policy(
        mcs_entry(14),
        plans=[ControlPlan((feedback,), standalone=True), ControlPlan((link_header(1),))],
    )
    worker, bridge = _start(profile, policy=policy)
    assert worker.submit("t", b"data after control")
    sink = _Sink()
    worker.start()
    worker.attach(sink)
    try:
        assert _wait_for(lambda: len(policy.sent) == 2)
    finally:
        worker.stop()
    control, data = policy.sent
    assert control.control_only and control.mcs_index == 8
    assert control.control_sequences == (0,)
    assert data.mcs_index == 14 and data.sequences == (0,)
    assert data.control_sequences == (1,)
    assert [r.message_count for r in bridge.sent.values] == [0, 1]
    assert worker.pending == 0


def _free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize("first_reuse", [True, False])
def test_an_ingress_port_clash_fails_without_reuse_address(first_reuse):
    port = _free_udp_port()
    first = IngressListener("127.0.0.1", port, lambda *_: None, reuse_address=first_reuse)
    first.start()
    try:
        second = IngressListener("127.0.0.1", port, lambda *_: None, reuse_address=False)
        with pytest.raises(OSError) as raised:
            second.start()
        assert raised.value.errno == errno.EADDRINUSE
        second.stop()
    finally:
        first.stop()


def test_ingress_keeps_reuse_address_by_default():
    port = _free_udp_port()
    received = []
    first = IngressListener("127.0.0.1", port, lambda source, data: received.append(data))
    first.start()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.sendto(b"ping", ("127.0.0.1", port))
        assert _wait_for(lambda: received == [b"ping"])
        assert first._socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
    finally:
        first.stop()


# -- a raising policy (review finding 3) ------------------------------------------


class _RaisingPolicy(_Policy):
    def __init__(self, entry, *, fail=("entry",)):
        super().__init__(entry)
        self.fail = set(fail)
        self.counts = {}

    def _maybe_raise(self, name):
        self.counts[name] = self.counts.get(name, 0) + 1
        if name in self.fail:
            raise RuntimeError(f"buggy {name}")

    def entry_for_next_burst(self, now):
        self._maybe_raise("entry")
        return super().entry_for_next_burst(now)

    def control_plan(self, now, *, idle):
        self._maybe_raise("plan")
        return super().control_plan(now, idle=idle)

    def idle_wait_s(self, now):
        self._maybe_raise("idle")
        return 0.0

    def on_sent(self, burst, now):
        self._maybe_raise("sent")
        super().on_sent(burst, now)


def _run_briefly(worker, sink, seconds=0.3):
    worker.start()
    worker.attach(sink)
    started = time.process_time()
    time.sleep(seconds)
    cpu = time.process_time() - started
    worker.stop()
    return cpu


@pytest.mark.parametrize("fail", [("entry",), ("entry", "plan"), ("plan", "sent")])
def test_a_raising_policy_falls_back_and_keeps_the_data_flowing(profile, fail):
    policy = _RaisingPolicy(mcs_entry(12), fail=fail)
    worker, bridge = _start(profile, policy=policy, entry=mcs_entry(8))
    for index in range(3):
        assert worker.submit("t", bytes([index]) * 100)
    sink = _Sink()
    _run_briefly(worker, sink)
    data = [r for r in bridge.sent.values if not r.control_only]
    assert sum(r.message_count for r in data) == 3
    if "entry" in fail:
        assert all(r.mcs_index == 8 for r in data)  # the active entry
    if "plan" in fail:
        assert all(r.control_subframes == 0 for r in data)
    assert worker.policy_errors >= 1
    # One report per failing method, then rate-limited.
    assert 1 <= len(bridge.failed.values) <= len(fail)


@pytest.mark.parametrize("fail", [("idle",), ("plan",), ("idle", "plan")])
def test_a_raising_policy_does_not_spin_the_idle_worker(profile, fail):
    policy = _RaisingPolicy(mcs_entry(12), fail=fail)
    worker, bridge = _start(profile, policy=policy)
    cpu = _run_briefly(worker, _Sink())
    assert len(bridge.failed.values) <= 1
    assert sum(policy.counts.values()) < 40  # 50 ms back-off after each failure
    assert cpu < 0.25


def test_a_worker_error_backs_off_and_is_reported_once_per_second(profile):
    class BrokenSink(_Sink):
        calls = 0

        def send(self, samples):
            BrokenSink.calls += 1
            raise OSError("radio gone")

    worker, bridge = _start(profile)
    for index in range(50):
        worker.submit("t", bytes([index]) * 100)
    _run_briefly(worker, BrokenSink())
    assert BrokenSink.calls <= 10  # 50 ms back-off
    assert len(bridge.failed.values) == 1
