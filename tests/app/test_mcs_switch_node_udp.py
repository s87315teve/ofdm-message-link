"""Regression guard over the real node_app engines: set_mcs mid-stream loses nothing.

Two node engines run in child processes exactly as node_app runs them and
exchange samples over localhost UDP (no added noise).  Node 1's datagrams
enter through its UDP ingress and leave node 2's egress; meanwhile the MCS of
node 1 is changed with ``ProcessNodeWorker.select_mcs_entry`` (node_app's
``set_mcs`` control command and the window's MCS combo).

* Sparse: one datagram at a time, the MCS changed before each one along a
  schedule that takes every ordered pair of transmit-visible MCS (every
  adjacent step and every cross-level jump).
* Bursts: rounds of back-to-back datagrams with the MCS changed in the middle
  of each round, so the change lands while datagrams are queued or planned.

Node 2 must deliver every datagram byte-exact and in order, with no FCS
failure and no sequence gap, and node 1's LA log must show every switch
applied to a data burst.

With ``--events`` on (``NodeSpec.burst_events``), node 1's ``sent`` lines and
node 2's ``burst`` lines must also join one to one: every data burst once,
its sequences contiguous with the previous burst's, at the MCS node 2 decoded.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest
from mcs_switch_helpers import (
    assert_la_lines_match_sent,
    assert_sent_joins_received,
    every_transition,
    free_udp_ports,
    required_transitions,
    transitions,
)

from ofdm_link.phy.mcs_table import mcs_entry, v5_entries
from ofdm_message_link import node_options
from ofdm_message_link.burst_events import EventLogWriter, read_events
from ofdm_message_link.link import build_phy_profile
from ofdm_message_link.node_engine import NodeSpec, ProcessNodeWorker, UdpDuplexFactory
from ofdm_message_link.options import load_link_config

NODE_MCS = tuple(entry.index for entry in v5_entries())
BURST_ROUNDS = ((8, 17), (17, 8), (9, 15), (15, 9), (10, 16), (16, 12))
BURST_SIZE = 48


class _Egress:
    def __init__(self, port):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.socket.bind(("127.0.0.1", port))
        self.socket.settimeout(0.1)
        self.payloads = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.payloads.append(self.socket.recvfrom(65536)[0])
            except TimeoutError:
                continue
            except OSError:
                return

    def close(self):
        self._stop.set()
        self._thread.join(1)
        self.socket.close()


class _Listener:
    """Keeps the newest raw stats dict of one engine and its failure messages."""

    def __init__(self):
        self.stats = None
        self.failed = []
        self.data_burst_mcs = []  # MCS of every data burst, as encoded (SentRecord)

    def __call__(self, kind, value):
        if kind == "stats":
            self.stats = value
        elif kind == "sent" and not getattr(value, "control_only", False):
            self.data_burst_mcs.append(int(value.mcs_index))
        elif kind == "failed":
            self.failed.append(str(value))


def _wait_for(condition, timeout_s):
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            return condition()
        time.sleep(0.005)
    return True


def _log_events(path, kind):
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a line still being written
        if event.get("event") == kind:
            events.append(event)
    return events


def _payload(number: int, size: int) -> bytes:
    head = f"#{number:06d}:".encode()
    return head + bytes((number * 7 + i) & 0xFF for i in range(size))


# Node 2 decodes inline (1) or on a decode pool (2), whose results come back
# out of step with the feeds that carried them.  ``events``: both nodes run
# with --events (NodeSpec.burst_events) and their lines must join exactly.
@pytest.mark.parametrize(("decode_workers", "events"), [(1, False), (2, False), (2, True)])
def test_node_engines_over_udp_keep_every_datagram_across_set_mcs(tmp_path, decode_workers,
                                                                   events):
    config = load_link_config("configs/default.yaml", [node_options.DEFAULT_NODE_OVERLAY])
    profile = build_phy_profile(config)
    ingress1, egress1, samples1, ingress2, egress2, samples2 = free_udp_ports(6)
    egress = {1: _Egress(egress1), 2: _Egress(egress2)}
    listeners = {1: _Listener(), 2: _Listener()}
    writers = {}
    events_path = {node_id: tmp_path / f"node{node_id}_events.jsonl" for node_id in (1, 2)}
    la_log = tmp_path / "la_1.jsonl"
    plan = {
        1: (ingress1, egress1, samples1, samples2),
        2: (ingress2, egress2, samples2, samples1),
    }
    workers = {}
    sent: list[bytes] = []
    schedule = every_transition(NODE_MCS)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def switch(index):
        before = len(_log_events(la_log, "mode"))
        workers[1].select_mcs_entry(mcs_entry(index))
        # The change is in force once the child's controller logged it.
        assert _wait_for(lambda: len(_log_events(la_log, "mode")) > before, 10.0)

    def send(payload):
        sent.append(payload)
        sender.sendto(payload, ("127.0.0.1", ingress1))

    try:
        for node_id, (ingress, out, _listen, _peer) in plan.items():
            spec = NodeSpec(
                node_id=node_id,
                peer_id=node_options.peer_id(node_id),
                mcs_index=8,
                control_mcs_index=8,
                seed_range=node_options.SEED_RANGES[node_id],
                ingress_port=ingress,
                egress=("127.0.0.1", out),
                la_spec="fixed",
                la_log_path=str(la_log) if node_id == 1 else None,
                burst_events=events,
            )
            listener = listeners[node_id]
            if events:
                writers[node_id] = EventLogWriter(
                    str(events_path[node_id]), node_id=node_id, sample_rate=profile.sample_rate
                )

                def listener(kind, value, _l=listeners[node_id], _w=writers[node_id]):
                    _l(kind, value)
                    _w.on_event(kind, value)

            workers[node_id] = ProcessNodeWorker(
                profile, spec, listener, decode_workers=decode_workers
            )
            workers[node_id].start()
        for node_id, (_, _, listen, peer) in plan.items():
            workers[node_id].attach_from(
                UdpDuplexFactory(host="127.0.0.1", tx_port=peer, rx_port=listen)
            )
        # Sparse: the queue is empty at every switch; wait for each datagram.
        for number, index in enumerate(schedule):
            switch(index)
            send(_payload(number, 20 + (number * 37) % 900))
            assert _wait_for(lambda: len(egress[2].payloads) >= len(sent), 10.0), (
                f"datagram #{number} at MCS {index} never left node 2"
            )
        sparse_changes = [event["mcs_index"] for event in _log_events(la_log, "la")]
        # Bursts: back-to-back datagrams, the MCS changed halfway through each round.
        number = len(sent)
        for first, second in BURST_ROUNDS:
            switch(first)
            for position in range(BURST_SIZE):
                if position == BURST_SIZE // 2:
                    workers[1].select_mcs_entry(mcs_entry(second))
                send(_payload(number, 1 + (number * 13) % 120))
                number += 1
            assert _wait_for(lambda: len(egress[2].payloads) >= len(sent), 15.0)
        # Let a stats tick report the receiver's counters after the last datagram.
        assert _wait_for(
            lambda: listeners[2].stats is not None
            and listeners[2].stats["rx"]["receiver"].messages_delivered >= len(sent),
            10.0,
        )
        stats = {node_id: listeners[node_id].stats for node_id in plan}
        # Taken before the stop below, which reports the engines' exit.
        failures = {node_id: list(listeners[node_id].failed) for node_id in plan}
    finally:
        sender.close()
        for worker in workers.values():
            try:
                worker.detach()
            finally:
                worker.stop()
        for writer in writers.values():
            writer.stop()
        for receiver in egress.values():
            receiver.close()

    got = egress[2].payloads
    assert len(got) == len(sent)
    assert got == sent  # byte-exact and in order
    assert egress[1].payloads == []
    receiver = stats[2]["rx"]["receiver"]
    assert receiver.psdus_fcs_failed == 0 and receiver.subframes_bad == 0
    assert receiver.sequence_gaps == 0 and receiver.missing_bursts == 0
    assert receiver.incomplete_messages_dropped == 0
    assert stats[2]["rx"]["decoder"].crc_failures == 0
    assert stats[1]["tx"]["ingress_dropped_queue_full"] == 0
    transport = stats[2]["rx"]["transport"]
    assert transport["bursts_incomplete"] == 0 and transport["chunks_dropped_overflow"] == 0
    assert failures == {1: [], 2: []}
    # Every switch of the sparse phase reached a data burst, in order: the MCS
    # changes node 1 logged are the schedule itself (8 is the start).
    assert sparse_changes == schedule
    assert transitions(sparse_changes) == required_transitions(NODE_MCS)
    # And on the wire: the sparse phase's data bursts, one per datagram, went
    # out at exactly the scheduled MCS (no "sent" event was dropped).
    assert stats[1]["dropped_display_events"] == 0
    assert listeners[1].data_burst_mcs[: len(schedule)] == schedule
    burst_changes = [event["mcs_index"] for event in _log_events(la_log, "la")]
    # In the burst phase whether a round's first datagrams beat the halfway
    # change is a race (ingress socket vs control queue); either MCS is right,
    # nothing else is, and rounds 2 -> 3 share no MCS, so something changed.
    burst_phase = burst_changes[len(sparse_changes):]
    assert burst_phase
    assert set(burst_phase) <= {index for pair in BURST_ROUNDS for index in pair}
    if not events:
        return
    rows = {node_id: read_events(str(events_path[node_id])) for node_id in plan}
    assert not any(row["k"] in ("gap", "tx_dropped") for node_id in plan for row in rows[node_id])
    sent_rows = [row for row in rows[1] if row["k"] == "sent"]
    data = assert_sent_joins_received(
        sent_rows, [row for row in rows[2] if row["k"] == "burst"], len(sent), waits=False
    )
    # The sparse phase: one datagram per burst, at the scheduled MCS.
    sparse = data[: len(schedule)]
    assert [row["mcs"] for row in sparse] == schedule
    assert all(row["subframes"] == 1 for row in sparse)
    # The LA log names the first DATA sequence of every switch.
    assert_la_lines_match_sent(_log_events(la_log, "la"), data)
    # Node 2 sent control bursts only.
    node2_sent = [row for row in rows[2] if row["k"] == "sent"]
    assert node2_sent and all(row["control_only"] and row["first_sequence"] is None
                              and isinstance(row["control_sequence"], int)
                              for row in node2_sent)
