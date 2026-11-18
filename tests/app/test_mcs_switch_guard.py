"""Regression guard: a runtime MCS change never loses, reorders or mis-encodes data.

The transmit worker (``tx_app.TransmitWorker``) is driven exactly as tx_app
and node_app drive it, with a recording sink in place of the radio, and the
recorded bursts are then fed, noise-free, to a fresh ``MessageReceiver``:

* the tx_app path changes the MCS with ``TransmitWorker.select_mcs_entry``
  (the GUI combo and the engine's ``("mcs", entry)`` command);
* the node_app path runs the worker with the node's ``LinkController`` as its
  ``BurstPolicy`` (built like ``node_engine._node_main``) and changes the MCS
  with ``LinkController.set_fixed`` (node_app's ``set_mcs``) or through the
  link adaptation's own decision, which here moves on every burst.

The switches are made adversarial but deterministic by hooks on the worker
thread itself: right after an aggregate has been planned and before it is
encoded, or while a burst is in the transport and the queue behind it is
full.  Every test requires, for every datagram: delivered == sent, byte-exact,
in order, no FCS/CRC failure and no sequence gap; and for every burst: it is
encoded with the MCS it was planned with, that MCS is the one selected when
its planning began, it stays within the aggregation caps of that MCS, and the
receiver decodes it with that MCS (from its own SIG header).

``OFDM_MCS_SOAK=1`` enables a longer soak variant.
"""

from __future__ import annotations

import argparse
import itertools
import os
import random
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import pytest
from mcs_switch_helpers import every_transition, required_transitions, transitions

from ofdm_link.phy.burst_v5 import V5_MAX_TRACKED_BURST_SAMPLES
from ofdm_link.phy.mcs_table import mcs_entry, v5_entries
from ofdm_message_link import node_options, options
from ofdm_message_link.aggregation import AggregationLimits
from ofdm_message_link.la.api import Decision
from ofdm_message_link.la.controller import LinkController
from ofdm_message_link.link import (
    MessageReceiver,
    MessageTransmitter,
    OutgoingBurst,
    aggregate_body_length,
    build_phy_profile,
    load_link_config,
    node_seed_range,
)
from ofdm_message_link.node_engine import NodeSpec, build_algorithms
from ofdm_message_link.tx_app import TransmitWorker

CONFIG = "configs/default.yaml"
# tx_app accepts every v5 entry (MessageTransmitter.select_mcs_entry); node_app
# only the transmit-visible ones, which leaves out MCS 11.
TX_MCS = tuple(entry.index for entry in v5_entries(include_hidden=True))
NODE_MCS = tuple(entry.index for entry in v5_entries())
CONTROL_MCS = 8
# A 1.5 ms airtime cap makes the aggregate size depend strongly on the MCS
# (8 small datagrams fit at MCS 8, 32 at MCS 13 and above), so an aggregate
# planned at a high MCS and encoded at a low one would break the cap.
TIGHT = AggregationLimits(max_subframes=32, max_psdu_bytes=16384, max_burst_airtime_s=0.0015)
WAIT_S = 90.0
STALL_S = 10.0  # no message went on the air for this long: the worker lost some
SOAK = os.environ.get("OFDM_MCS_SOAK") == "1"

assert TX_MCS == tuple(range(8, 18)), TX_MCS


@pytest.fixture(scope="module", autouse=True)
def _one_numeric_thread():
    """One BLAS thread, as every engine child runs (engine._bound_numeric_threads).

    Unbounded, NumPy's pool spins a thread per core through these tests.
    """

    from ofdm_link.runtime.cpu_budget import apply_cpu_budget, numeric_thread_count

    before = numeric_thread_count()
    apply_cpu_budget(1)
    yield
    if before is not None:
        apply_cpu_budget(before)


@pytest.fixture(scope="module")
def tx_profile():
    return build_phy_profile(load_link_config(CONFIG))


@pytest.fixture(scope="module")
def node_profile():
    return build_phy_profile(load_link_config(CONFIG, [node_options.DEFAULT_NODE_OVERLAY]))


@pytest.fixture(scope="module")
def configured_limits():
    """The configuration's caps (32 subframes, 16384 B, 8 ms, 4 ms burst interval)."""

    return options.aggregation_limits(argparse.Namespace(), load_link_config(CONFIG))


# --------------------------------------------------------------------------
# instrumentation
# --------------------------------------------------------------------------


class _Signal:
    def __init__(self):
        self.values = []

    def emit(self, value):
        self.values.append(value)


class _Bridge:
    def __init__(self):
        self.sent = _Signal()
        self.failed = _Signal()


@dataclass(frozen=True)
class _Plan:
    mcs_index: int
    take: int
    queued: int
    pinned: bool
    expected: int | None


@dataclass(frozen=True)
class _Encode:
    burst: OutgoingBurst


class _ProbeTransmitter(MessageTransmitter):
    """A MessageTransmitter that logs every plan and encode, with a hook in between.

    ``expected()`` is the test's own record of the MCS currently selected; it
    is sampled when a plan starts.  ``on_plan(pinned)`` runs right after the
    plan, on the worker thread, before the planned aggregate is encoded.
    """

    def __init__(self, profile, *, entry, seed_range=(1, 127)):
        super().__init__(profile, mcs_entry=entry, seed_range=seed_range)
        self.events: list[_Plan | _Encode] = []
        self.expected = None
        self.on_plan = None

    def plan_burst(self, datagrams, limits, entry=None, *, extra_mpdu_lengths=()):
        expected = None if self.expected is None else self.expected()
        chosen = self.active_mcs_entry if entry is None else entry
        take = super().plan_burst(
            datagrams, limits, entry, extra_mpdu_lengths=extra_mpdu_lengths
        )
        self.events.append(_Plan(chosen.index, take, len(datagrams), entry is not None, expected))
        if self.on_plan is not None:
            self.on_plan(entry is not None)
        return take

    def encode_datagrams(self, datagrams, *, entry=None, control_payloads=()):
        burst = super().encode_datagrams(
            datagrams, entry=entry, control_payloads=control_payloads
        )
        self.events.append(_Encode(burst))
        return burst


class _Sink:
    """Records every burst the worker hands over; ``on_send`` runs inside the send."""

    def __init__(self):
        self.samples: list[np.ndarray] = []
        self.on_send = None

    def start(self):
        pass

    def stop(self):
        pass

    def send(self, samples):
        self.samples.append(samples)
        if self.on_send is not None:
            self.on_send()
        return True


@dataclass
class _Run:
    transmitter: _ProbeTransmitter
    worker: TransmitWorker
    bridge: _Bridge
    sink: _Sink
    limits: AggregationLimits
    sent: list[bytes] = field(default_factory=list)
    current: list[int] = field(default_factory=list)  # the MCS the test selected last

    def messages_on_air(self) -> int:
        return sum(record.message_count for record in list(self.bridge.sent.values))

    def data_bursts_on_air(self) -> int:
        return sum(1 for record in list(self.bridge.sent.values) if record.subframe_count)

    def submit(self, payload: bytes) -> bool:
        if not self.worker.submit("test", payload):
            return False
        self.sent.append(payload)
        return True


def _tx_run(profile, limits, first_mcs):
    """tx_app: no policy; the MCS is changed with the worker's select_mcs_entry."""

    transmitter = _ProbeTransmitter(profile, entry=mcs_entry(first_mcs))
    bridge = _Bridge()
    worker = TransmitWorker(transmitter, bridge, aggregation=limits)
    run = _Run(transmitter, worker, bridge, _Sink(), limits, current=[first_mcs])
    transmitter.expected = lambda: run.current[-1]
    return run


def _tx_switch(run: _Run, index: int) -> None:
    run.current.append(index)
    run.worker.select_mcs_entry(mcs_entry(index))


class _CyclingLa:
    """The node's configured LA, except that every decision takes the next MCS of a schedule."""

    def __init__(self, inner, schedule):
        self._inner = inner
        self._schedule = itertools.cycle(schedule)
        self.decisions: list[int] = []

    def decide(self, now_s):
        index = next(self._schedule)
        self.decisions.append(index)
        return Decision(index, reason="test schedule")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _node_run(profile, limits, first_mcs, *, la_schedule=None):
    """node_app: the worker runs with the node's LinkController as its policy.

    Built like ``node_engine._node_main`` with ``--la fixed``.  Without
    ``la_schedule`` the controller is put in fixed mode at ``first_mcs`` and
    the test changes it with ``set_fixed`` (node_app's ``set_mcs``); with one,
    the controller stays in auto mode and the LA moves every burst.
    """

    spec = NodeSpec(
        node_id=1,
        peer_id=2,
        mcs_index=first_mcs,
        control_mcs_index=CONTROL_MCS,
        seed_range=node_seed_range(1),
        ingress_port=0,
        egress=("127.0.0.1", 9),
        la_spec="fixed",
    )
    la, reporter, ladder = build_algorithms(spec, profile.sample_rate)
    if la_schedule is not None:
        la = _CyclingLa(la, la_schedule)
    controller = LinkController(
        node_id=spec.node_id,
        peer_id=spec.peer_id,
        la=la,
        reporter=reporter,
        ladder=ladder,
        control_entry=mcs_entry(spec.control_mcs_index),
        mode="auto",
        sample_rate=float(profile.sample_rate),
        own_seed_range=tuple(spec.seed_range),
    )
    transmitter = _ProbeTransmitter(
        profile, entry=mcs_entry(spec.mcs_index), seed_range=tuple(spec.seed_range)
    )
    bridge = _Bridge()
    worker = TransmitWorker(transmitter, bridge, aggregation=limits, policy=controller)
    run = _Run(transmitter, worker, bridge, _Sink(), limits, current=[first_mcs])
    run.controller = controller
    run.la = la
    if la_schedule is None:
        controller.set_fixed(mcs_entry(first_mcs))
        transmitter.expected = lambda: run.current[-1]
    else:
        transmitter.expected = lambda: la.decisions[-1]
    return run


def _node_switch(run: _Run, index: int) -> None:
    run.current.append(index)
    run.controller.set_fixed(mcs_entry(index))


# --------------------------------------------------------------------------
# traffic
# --------------------------------------------------------------------------


def _payload(rng: random.Random, number: int, *, small_only: bool = False) -> bytes:
    """Unique (numbered) payloads: mostly small, some medium, some fragmented."""

    roll = 1.0 if small_only else rng.random()
    if roll < 0.04:
        size = rng.randint(1000, 2600)  # 2-3 fragments, may straddle bursts
    elif roll < 0.15:
        size = rng.randint(200, 968)
    else:
        size = rng.randint(0, 76)
    return number.to_bytes(4, "big") + rng.randbytes(size)


def _wait_for(condition, timeout_s=WAIT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            return condition()
        time.sleep(0.002)
    return True


def _wait_for_progress(count, target: int, stall_s: float = STALL_S) -> bool:
    """Wait until ``count() == target``; give up once it has not moved for ``stall_s``."""

    last, moved_at = count(), time.monotonic()
    while last != target:
        time.sleep(0.002)
        now_count = count()
        if now_count != last:
            last, moved_at = now_count, time.monotonic()
        elif time.monotonic() - moved_at > stall_s:
            return False
    return True


def _finish(run: _Run) -> None:
    try:
        assert _wait_for_progress(run.messages_on_air, len(run.sent)), (
            f"only {run.messages_on_air()} of {len(run.sent)} messages went on the air"
        )
    finally:
        run.worker.stop()
    assert run.bridge.failed.values == []
    assert run.worker.policy_errors == 0
    assert run.worker.pending == 0


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def _bursts_on_air(run: _Run) -> list[OutgoingBurst]:
    """Encoded bursts in the order the sink took them (each one exactly once)."""

    encoded = {id(event.burst.samples): event.burst for event in run.transmitter.events
               if isinstance(event, _Encode)}
    on_air = [encoded[id(samples)] for samples in run.sink.samples]
    assert len({id(burst) for burst in on_air}) == len(on_air), "a burst went out twice"
    encoded_data = [burst for burst in encoded.values() if burst.datagrams]
    on_air_data = [burst for burst in on_air if burst.datagrams]
    assert len(on_air_data) == len(encoded_data), "a data burst was encoded but never sent"
    return on_air


def _check_transmit_side(run: _Run, profile, *, check_expected: bool = True) -> list:
    """Every data burst: planned entry == encoded entry == selected entry, within caps."""

    limits = run.limits
    last_plan: _Plan | None = None
    for event in run.transmitter.events:
        if isinstance(event, _Plan):
            last_plan = event
            continue
        burst = event.burst
        if not burst.datagrams:
            assert burst.mcs_index == CONTROL_MCS, "a control-only burst left the control MCS"
            continue
        assert last_plan is not None and last_plan.pinned, "a data burst was encoded unplanned"
        assert burst.mcs_index == last_plan.mcs_index, (
            f"planned at MCS {last_plan.mcs_index} but encoded at MCS {burst.mcs_index}"
        )
        assert burst.subframe_count == last_plan.take
        if check_expected and last_plan.expected is not None:
            assert burst.mcs_index == last_plan.expected, (
                f"MCS {last_plan.expected} was selected when the burst was planned, "
                f"but it went out at MCS {burst.mcs_index}"
            )
        assert burst.modulation is mcs_entry(burst.mcs_index).modulation
        if limits.enabled:
            assert burst.subframe_count <= limits.max_subframes
        if burst.subframe_count > 1:
            # Several datagrams were aggregated, so the planner promised the caps.
            assert burst.psdu_bytes <= limits.max_psdu_bytes
            assert burst.sample_count / profile.sample_rate <= limits.max_burst_airtime_s
            assert burst.sample_count <= V5_MAX_TRACKED_BURST_SAMPLES
        last_plan = None
    on_air = _bursts_on_air(run)
    # Sequence numbers are assigned at encode; none was burned by a lost burst.
    data_sequences = [sequence for burst in on_air for sequence in burst.sequences]
    assert data_sequences == list(range(len(data_sequences)))
    return on_air


@dataclass
class _Received:
    payloads: list[bytes]
    message_mcs: list[tuple[int, int | None]]
    burst_mcs: list[tuple[int | None, int]]
    stats: object
    decoder: object


def _receive(profile, bursts: list[OutgoingBurst], *, workers: int = 1, seed: int = 0):
    rng = np.random.default_rng(seed)
    pieces = [np.zeros(800, np.complex64)]
    for burst in bursts:
        pieces.append(np.asarray(burst.samples, dtype=np.complex64))
        pieces.append(np.zeros(int(rng.integers(0, 400)), np.complex64))
    pieces.append(np.zeros(20000, np.complex64))
    stream = np.concatenate(pieces)
    receiver = MessageReceiver(profile, decode_workers=workers)
    messages, observations = [], []
    try:
        for start in range(0, stream.size, 8192):
            messages.extend(receiver.feed(stream[start : start + 8192]))
            observations.extend(receiver.drain_observations())
        messages.extend(receiver.flush(timeout=60.0))
        observations.extend(receiver.drain_observations())
        stats, decoder = receiver.stats, receiver.decoder_snapshot()
    finally:
        receiver.close()
    return _Received(
        payloads=[item.message.payload for item in messages],
        message_mcs=[(item.sequence, item.mcs_index) for item in messages],
        burst_mcs=[(item.mcs_index, item.subframes) for item in observations],
        stats=stats,
        decoder=decoder,
    )


def _check_delivery(run: _Run, profile, bursts, *, workers: int = 1) -> _Received:
    got = _receive(profile, bursts, workers=workers)
    sent = run.sent
    if got.payloads != sent:
        first = next(
            (i for i, pair in enumerate(zip(got.payloads, sent, strict=False))
             if pair[0] != pair[1]),
            min(len(got.payloads), len(sent)),
        )
        pytest.fail(f"delivered {len(got.payloads)} of {len(sent)}; first mismatch at #{first}")
    stats = got.stats
    assert stats.psdus_received == len(bursts)
    assert stats.psdus_fcs_failed == 0
    assert stats.subframes_bad == 0 and stats.bad_delimiters == 0
    assert stats.sequence_gaps == 0 and stats.missing_bursts == 0
    assert stats.incomplete_messages_dropped == 0 and stats.foreign_bursts == 0
    assert got.decoder.psdu_fcs_failures == 0 and got.decoder.crc_failures == 0
    assert got.decoder.payload_decode_failure == 0
    # The receiver took each burst's MCS from its own SIG header ...
    assert got.burst_mcs == [
        (burst.mcs_index, burst.subframe_count + len(burst.control_sequences)) for burst in bursts
    ]
    # ... and so delivered each message at the MCS of the burst that completed it.
    carried_by = {seq: burst.mcs_index for burst in bursts for seq in burst.sequences}
    assert all(mcs == carried_by[sequence] for sequence, mcs in got.message_mcs)
    return got


def _data_mcs(bursts) -> list[int]:
    return [burst.mcs_index for burst in bursts if burst.datagrams]


# --------------------------------------------------------------------------
# empty queue: one message at a time, a switch before every message
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["tx_app", "node_app"])
def test_sparse_traffic_switching_before_every_message(path, tx_profile, node_profile,
                                                        configured_limits):
    """Every ordered MCS pair, with the queue empty when each switch happens."""

    rng = random.Random(1)
    if path == "tx_app":
        profile, indices = tx_profile, TX_MCS
        schedule = every_transition(indices)
        run, switch = _tx_run(profile, configured_limits, schedule[0]), _tx_switch
    else:
        profile, indices = node_profile, NODE_MCS
        schedule = every_transition(indices)
        run, switch = _node_run(profile, configured_limits, schedule[0]), _node_switch
    run.worker.start()
    run.worker.attach(run.sink)
    try:
        for number, index in enumerate(schedule):
            switch(run, index)
            assert run.submit(_payload(rng, number))
            assert _wait_for(lambda n=number: run.messages_on_air() == n + 1, 20.0)
    finally:
        _finish(run)
    bursts = _check_transmit_side(run, profile)
    # One burst per message, each at the MCS selected just before it.
    assert _data_mcs(bursts) == schedule
    assert transitions(_data_mcs(bursts)) == required_transitions(indices)
    _check_delivery(run, profile, bursts)


# --------------------------------------------------------------------------
# full queue: a switch for every burst while the queue stays full
# --------------------------------------------------------------------------


def _full_queue(run: _Run, schedule, switch, *, switch_at: str, seed: int,
                small_only: bool = False, extra_rounds: int = 0) -> None:
    """Keep the 256-message send queue full while the MCS moves along ``schedule``.

    ``switch_at="plan"`` switches right after each aggregate was planned and
    before it is encoded; ``"send"`` while each burst is in the transport,
    with the full queue waiting behind it.  Either way burst k must carry
    schedule[k].  The queue is topped up from inside the send until enough
    bursts went out to cover the schedule ``extra_rounds + 1`` times.
    """

    rng = random.Random(seed)
    numbers = itertools.count()
    steps = itertools.cycle(schedule[1:])
    bursts_wanted = (extra_rounds + 1) * len(schedule)

    def top_up():
        if run.data_bursts_on_air() >= bursts_wanted:
            return
        while run.submit(_payload(rng, next(numbers), small_only=small_only)):
            pass
        # The failed submit's number is skipped; numbers only need to be unique.

    def on_plan(pinned):
        if pinned and switch_at == "plan":
            switch(run, next(steps))

    def on_send():
        top_up()
        # The worker encodes and then sends, so the last event is this burst.
        last = run.transmitter.events[-1]
        if switch_at == "send" and isinstance(last, _Encode) and last.burst.datagrams:
            switch(run, next(steps))

    run.transmitter.on_plan = on_plan
    run.sink.on_send = on_send
    run.worker.start()
    top_up()  # held: no sink yet, so the queue is full before the first plan
    assert len(run.sent) == 256
    run.worker.attach(run.sink)
    try:
        assert _wait_for(lambda: run.data_bursts_on_air() >= bursts_wanted)
    finally:
        _finish(run)


@pytest.mark.parametrize("switch_at", ["plan", "send"])
@pytest.mark.parametrize("limits_name", ["tight", "configured"])
def test_tx_app_full_queue_switching_every_burst(switch_at, limits_name, tx_profile,
                                                 configured_limits):
    limits = TIGHT if limits_name == "tight" else configured_limits
    schedule = every_transition(TX_MCS)
    run = _tx_run(tx_profile, limits, schedule[0])
    _full_queue(run, schedule, _tx_switch, switch_at=switch_at, seed=2)
    bursts = _check_transmit_side(run, tx_profile)
    data_mcs = _data_mcs(bursts)
    assert data_mcs == list(itertools.islice(itertools.cycle(schedule[:-1]), len(data_mcs)))
    assert transitions(data_mcs) == required_transitions(TX_MCS)
    # Aggregation really happened (a full queue, not one datagram per burst).
    assert max(burst.subframe_count for burst in bursts) > 1
    _check_delivery(run, tx_profile, bursts)


@pytest.mark.parametrize("switch_at", ["plan", "send"])
def test_node_app_full_queue_switching_every_burst_with_set_mcs(switch_at, node_profile):
    schedule = every_transition(NODE_MCS)
    run = _node_run(node_profile, TIGHT, schedule[0])
    _full_queue(run, schedule, _node_switch, switch_at=switch_at, seed=3)
    bursts = _check_transmit_side(run, node_profile)
    data_mcs = _data_mcs(bursts)
    assert data_mcs == list(itertools.islice(itertools.cycle(schedule[:-1]), len(data_mcs)))
    assert transitions(data_mcs) == required_transitions(NODE_MCS)
    assert max(burst.subframe_count for burst in bursts) > 1
    # Node bursts carry their CONTROL frames first, ahead of the data.
    assert all(burst.control_sequences for burst in bursts if burst.datagrams)
    _check_delivery(run, node_profile, bursts)


def test_node_app_full_queue_with_the_la_changing_every_burst(node_profile, configured_limits):
    """Auto mode: the link adaptation itself picks a new MCS for every burst."""

    schedule = every_transition(NODE_MCS)
    run = _node_run(node_profile, configured_limits, schedule[0], la_schedule=schedule[:-1])
    _full_queue(run, schedule, lambda run, index: None, switch_at="plan", seed=4)
    bursts = _check_transmit_side(run, node_profile)
    assert run.controller.mode == "auto"
    # A standalone robust-feedback burst takes a decision without data, so the
    # data bursts follow the schedule with gaps; each still went out at the
    # decision taken for it (checked above), and the MCS kept moving.
    data_mcs = _data_mcs(bursts)
    assert set(data_mcs) == set(NODE_MCS)
    assert sum(a != b for a, b in itertools.pairwise(data_mcs)) >= len(schedule) // 2
    assert max(burst.subframe_count for burst in bursts) > 1
    _check_delivery(run, node_profile, bursts)


# --------------------------------------------------------------------------
# the hazard itself: plan at one MCS, switch to a far one before the encode
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["tx_app", "node_app"])
def test_a_switch_between_plan_and_encode_cannot_stretch_the_planned_aggregate(
    path, tx_profile, node_profile
):
    """An aggregate planned at MCS 17 would overrun the cap at MCS 8, and the reverse.

    The switch lands between plan and encode every time; the burst must still
    go out at the MCS it was planned with, whole, within that MCS's caps.
    """

    schedule = [17, 8] * 12 + [17]
    if path == "tx_app":
        profile = tx_profile
        run, switch = _tx_run(profile, TIGHT, 17), _tx_switch
    else:
        profile = node_profile
        run, switch = _node_run(profile, TIGHT, 17), _node_switch
    _full_queue(run, schedule, switch, switch_at="plan", seed=5, small_only=True)
    bursts = _check_transmit_side(run, profile)
    assert _data_mcs(bursts)[: len(schedule) - 1] == schedule[:-1]
    # The hazard was real: some aggregate planned at 17 would not fit at 8.
    overrun = [
        burst for burst in bursts
        if burst.mcs_index == 17 and burst.subframe_count > 1
        and run.transmitter.burst_sample_count(
            mcs_entry(8), aggregate_body_length(burst.datagrams)
        ) / profile.sample_rate > TIGHT.max_burst_airtime_s
    ]
    assert overrun, "no aggregate exercised the plan/encode window"
    _check_delivery(run, profile, bursts)


# --------------------------------------------------------------------------
# a concurrent switcher thread, and the decode pool
# --------------------------------------------------------------------------


def _hammer(run: _Run, switch, indices, stop: threading.Event, seed: int) -> threading.Thread:
    rng = random.Random(seed)

    def loop():
        while not stop.is_set():
            switch(run, rng.choice(indices))
            time.sleep(0.0002)

    thread = threading.Thread(target=loop, name="mcs-hammer", daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("path", ["tx_app", "node_app"])
def test_a_concurrent_switcher_thread_never_breaks_the_stream(path, tx_profile, node_profile):
    """Another thread changes the MCS every 0.2 ms while a full queue drains."""

    if path == "tx_app":
        profile, indices = tx_profile, TX_MCS
        run, switch = _tx_run(profile, TIGHT, 8), _tx_switch
    else:
        profile, indices = node_profile, NODE_MCS
        run, switch = _node_run(profile, TIGHT, 8), _node_switch
    stop = threading.Event()
    thread = _hammer(run, switch, indices, stop, seed=6)
    try:
        _full_queue(run, [8, 9], lambda run, index: None, switch_at="plan", seed=7,
                    extra_rounds=40)
    finally:
        stop.set()
        thread.join(2.0)
    # Which switch a burst sees is a race; plan == encode and the caps are not.
    bursts = _check_transmit_side(run, profile, check_expected=False)
    assert len(set(_data_mcs(bursts))) > 3
    _check_delivery(run, profile, bursts)


def test_the_decode_pool_decodes_each_burst_at_its_own_mcs_in_order(tx_profile):
    """Two decode workers: results come back in stream order, each at its burst's MCS."""

    schedule = every_transition(TX_MCS)
    run = _tx_run(tx_profile, TIGHT, schedule[0])
    _full_queue(run, schedule, _tx_switch, switch_at="plan", seed=8)
    bursts = _check_transmit_side(run, tx_profile)
    _check_delivery(run, tx_profile, bursts, workers=2)


# --------------------------------------------------------------------------
# soak (opt-in)
# --------------------------------------------------------------------------


@pytest.mark.skipif(not SOAK, reason="set OFDM_MCS_SOAK=1 for the soak variant")
@pytest.mark.parametrize("path", ["tx_app", "node_app"])
@pytest.mark.parametrize("limits_name", ["tight", "configured"])
def test_soak_switching_every_burst_and_from_a_thread(path, limits_name, tx_profile,
                                                       node_profile, configured_limits):
    limits = TIGHT if limits_name == "tight" else configured_limits
    if path == "tx_app":
        profile, indices = tx_profile, TX_MCS
        schedule = every_transition(indices)
        run, switch = _tx_run(profile, limits, schedule[0]), _tx_switch
    else:
        profile, indices = node_profile, NODE_MCS
        schedule = every_transition(indices)
        run, switch = _node_run(profile, limits, schedule[0]), _node_switch
    stop = threading.Event()
    thread = _hammer(run, switch, indices, stop, seed=9)
    try:
        _full_queue(run, schedule, switch, switch_at="plan", seed=10, extra_rounds=10)
    finally:
        stop.set()
        thread.join(2.0)
    bursts = _check_transmit_side(run, profile, check_expected=False)
    _check_delivery(run, profile, bursts, workers=2)
