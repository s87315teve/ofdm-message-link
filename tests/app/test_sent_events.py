"""``node_app --events`` on the transmit side: one ``sent`` line per accepted burst.

Each line is stamped by the transmit worker in the engine child right after
the transport accepted the burst, and names the burst's DATA sequences, MCS,
the MCS the aggregation wait sized it with and the send attempt, so a
measurement can join every transmitted burst to the peer's ``burst`` line.
Dropped bursts become ``tx_dropped`` lines with their sequences.

* Two real headless ``node_app`` processes over the udp transport, with the
  MCS changed through the control socket (and by the LA) mid-stream.
* The transmit worker alone: the accept time, retries (``attempt``), drops
  on send and encode failures, and nothing at all without ``events``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from mcs_switch_helpers import (
    assert_la_lines_match_sent,
    assert_sent_joins_received,
    free_udp_ports,
)

from ofdm_link.phy.mcs_table import mcs_entry
from ofdm_message_link import link
from ofdm_message_link.aggregation import AggregationLimits
from ofdm_message_link.burst_events import EventLogWriter, read_events
from ofdm_message_link.link import MessageTransmitter, build_phy_profile, load_link_config
from ofdm_message_link.transport import TransportError
from ofdm_message_link.tx_app import ControlPlan, SentRecord, TransmitWorker

ROOT = Path(__file__).resolve().parents[2]
SCHEDULE = (12, 8, 17, 9, 16, 10, 15)


# --------------------------------------------------------------------------
# two node_app processes
# --------------------------------------------------------------------------


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


def _wait_for(condition, timeout_s):
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            return condition()
        time.sleep(0.01)
    return True


def _payload(number: int) -> bytes:
    return f"#{number:06d}:".encode() + bytes((number * 11 + i) & 0xFF for i in range(number % 180))


def _status_subframes(path):
    """``tx.subframes`` of the newest ``status`` line written so far (None before one)."""

    try:
        rows = read_events(str(path), kinds=["status"])
    except (FileNotFoundError, ValueError):
        return None
    return rows[-1]["tx"]["subframes"] if rows else None


def _node(node_id, tmp_path, ports):
    ingress, egress, listen, peer, control = ports
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "OMP_NUM_THREADS": "1",
           "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    command = [
        sys.executable, "-m", "ofdm_message_link.node_app", "--node-id", str(node_id),
        "--headless", "--transport", "udp", "--decode-workers", "1",
        "--ready-file", str(tmp_path / f"ready{node_id}"),
        "--events", str(tmp_path / f"node{node_id}_events.jsonl"),
        "--la-log", str(tmp_path / f"la{node_id}.jsonl"),
        "--control-port", str(control), "--ingress-port", str(ingress),
        "--egress-port", str(egress), "--udp-listen-port", str(listen),
        "--udp-peer-port", str(peer),
    ]
    return subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)


def test_two_node_apps_log_one_sent_line_per_burst_that_joins_the_peer_burst(tmp_path):
    (ingress1, egress1, samples1, control1,
     ingress2, egress2, samples2, control2) = free_udp_ports(8)
    egress = _Egress(egress1), _Egress(egress2)
    processes = []
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(5.0)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sent: list[bytes] = []

    def ask(port, request):
        client.sendto(json.dumps(request).encode(), ("127.0.0.1", port))
        return json.loads(client.recvfrom(65536)[0])

    def send(count, gap_s):
        for _ in range(count):
            payload = _payload(len(sent))
            sent.append(payload)
            sender.sendto(payload, ("127.0.0.1", ingress1))
            time.sleep(gap_s)

    def delivered():
        return _wait_for(lambda: len(egress[1].payloads) >= len(sent), 20.0)

    try:
        processes.append(_node(1, tmp_path, (ingress1, egress1, samples1, samples2, control1)))
        processes.append(_node(2, tmp_path, (ingress2, egress2, samples2, samples1, control2)))
        for node_id, process in enumerate(processes, start=1):
            assert _wait_for(lambda n=node_id: (tmp_path / f"ready{n}").exists(), 60.0), (
                process.stderr.read1().decode() if process.poll() is not None else "no ready")
        time.sleep(0.5)  # a few idle control bursts first
        send(60, 0.003)  # the LA (olla) picks the MCS
        assert delivered()
        for index in SCHEDULE:
            send(20, 0.0005)
            # Back to back: the change lands mid-queue, often during a wait.
            assert ask(control1, {"cmd": "set_mcs", "mcs": index}) == {"ok": True}
            send(20, 0.0005)
            assert delivered()
        assert ask(control1, {"cmd": "set_mcs", "mcs": None}) == {"ok": True}
        send(40, 0.001)
        assert delivered()
        # Wait for a status line that counts every datagram: the parent
        # writes one every 0.5 s from child stats that arrive every 0.25 s
        # (plus batching), so a fixed sleep can catch pre-final counters.
        assert _wait_for(lambda: _status_subframes(tmp_path / "node1_events.jsonl")
                         == len(sent), 30.0)
        for port in (control1, control2):
            assert ask(port, {"cmd": "stop"}) == {"ok": True}
        for process in processes:
            assert process.wait(timeout=30) == 0, process.stderr.read().decode()
    finally:
        client.close()
        sender.close()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        for receiver in egress:
            receiver.close()

    assert egress[1].payloads == sent  # byte-exact and in order
    rows = {n: read_events(str(tmp_path / f"node{n}_events.jsonl")) for n in (1, 2)}
    assert rows[1][-1]["k"] == "stop" and rows[2][-1]["k"] == "stop"
    assert not any(row["k"] in ("gap", "tx_dropped") for n in (1, 2) for row in rows[n])
    sent_rows = [row for row in rows[1] if row["k"] == "sent"]
    # The analyzer's fields, all present on every line.
    fields = {"k", "node", "mono", "wall", "n", "first_sequence", "subframes",
              "control_subframes", "control_sequence", "mcs", "planned_mcs", "sample_count",
              "control_only", "standalone", "attempt"}
    assert all(set(row) == fields and row["node"] == 1 for row in sent_rows)
    data = assert_sent_joins_received(
        sent_rows, [row for row in rows[2] if row["k"] == "burst"], len(sent), waits=True
    )
    # The MCS really changed under the queue, and some bursts aggregated.
    assert set(SCHEDULE) <= {row["mcs"] for row in data}
    assert any(row["subframes"] > 1 for row in data)
    # The last status line (after the last data burst; idle control bursts
    # still followed it) counts what the sent lines say.
    status = [row for row in rows[1] if row["k"] == "status"][-1]
    assert status["tx"]["bursts"] <= len(sent_rows)
    assert status["tx"]["subframes"] == sum(row["subframes"] for row in data)
    # The LA log's "la" lines name the first DATA sequence at the new MCS,
    # and a burst sized before a switch carries the MCS it was sized at.
    la = [json.loads(line) for line in (tmp_path / "la1.jsonl").read_text().splitlines()]
    moved = assert_la_lines_match_sent([event for event in la if event.get("event") == "la"],
                                       data)
    assert moved >= 1  # each set_mcs above lands mid-queue (7 of 7 in local runs)
    # Node 2 had nothing to send but control bursts.
    node2 = [row for row in rows[2] if row["k"] == "sent"]
    assert node2 and all(row["control_only"] and row["first_sequence"] is None
                         and row["subframes"] == 0 for row in node2)


# --------------------------------------------------------------------------
# the transmit worker alone
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def profile():
    return build_phy_profile(load_link_config("configs/default.yaml"))


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
    """Accepts every send unless ``fail(attempt)``; records when each send returned."""

    def __init__(self, fail=lambda attempt: False, *, retry=True):
        self.retry_failed_send = retry
        self._fail = fail
        self.calls = 0
        self.accepted_at = []  # time.monotonic() inside each accepted send

    def start(self):
        pass

    def stop(self):
        pass

    def send(self, samples):
        attempt, self.calls = self.calls, self.calls + 1
        if self._fail(attempt):
            raise TransportError("UDP send failed: [Errno 111] Connection refused")
        self.accepted_at.append(time.monotonic())
        return True


def _run(profile, sink, count, *, events=True, limits=None, transmitter=None, policy=None,
         timeout_s=10.0):
    transmitter = transmitter or MessageTransmitter(profile, mcs_entry=mcs_entry(8))
    bridge = _Bridge()
    log: list[tuple[str, dict]] = []
    worker = TransmitWorker(
        transmitter, bridge,
        aggregation=limits or AggregationLimits(max_subframes=4, min_burst_interval_s=0.004),
        events=(lambda kind, value: log.append((kind, value))) if events else None,
        policy=policy,
    )
    for index in range(count):
        assert worker.submit("t", f"message {index:03d} ".encode().ljust(100, b"."))
    worker.attach(sink)
    worker.start()
    try:
        assert _wait_for(lambda: worker.pending == 0, timeout_s)
    finally:
        worker.stop()
    return worker, bridge.sent.values, log


def test_a_sent_record_is_stamped_when_the_transport_accepted_it(profile):
    sink = _Sink()
    worker, records, log = _run(profile, sink, 40)
    assert log == []
    data = [record for record in records if not record.control_only]
    assert [record.n for record in records] == list(range(1, len(records) + 1))
    assert sum(record.subframe_count for record in data) == 40
    assert len(records) == len(sink.accepted_at) > 4
    expected = 0
    for i, record in enumerate(records):
        assert record.first_sequence == expected
        expected += record.subframe_count
        # Stamped after the send that accepted it, before the next send.
        assert record.mono >= sink.accepted_at[i] and record.wall > 1e9
        if i + 1 < len(records):
            assert record.mono <= sink.accepted_at[i + 1]
        assert record.attempt == 1 and record.mcs_index == 8
    # Sized at the active entry by the minimum-interval wait, but the first
    # burst (no previous burst to wait after).
    assert records[0].planned_mcs is None
    assert all(record.planned_mcs == 8 for record in records[1:])


def test_a_burst_kept_after_a_raising_send_is_logged_once_with_its_attempt(profile):
    sink = _Sink(fail=lambda attempt: attempt in (0, 1, 5))
    worker, records, log = _run(profile, sink, 30)
    assert worker.send_failures == 3 and log == []
    assert [record.attempt for record in records][:2] == [3, 1]
    assert sorted(record.attempt for record in records)[-2:] == [2, 3]
    expected = 0
    for record in records:
        assert record.first_sequence == expected
        expected += record.subframe_count
    assert expected == 30


def test_a_dropped_send_names_the_sequences_it_lost(profile):
    sink = _Sink(fail=lambda attempt: attempt == 1, retry=False)
    worker, records, log = _run(profile, sink, 30)
    assert worker.dropped_send_failed_bursts == 1
    ((kind, drop),) = log
    assert kind == "tx_dropped" and drop["reason"] == "send_failed"
    first, lost = records[0].subframe_count, worker.dropped_send_failed
    assert (drop["first_sequence"], drop["subframes"], drop["attempt"]) == (first, lost, 1)
    assert drop["mcs"] == 8 and drop["mono"] > 0 and drop["wall"] > 1e9
    # Sent and dropped together tile the sequences without a hole.
    assert records[1].first_sequence == first + lost
    assert sum(record.subframe_count for record in records) + lost == 30


def test_an_encode_failure_after_numbering_names_the_sequences_it_used(profile, monkeypatch):
    original = link.encode_burst_v5
    calls = {"n": 0}

    def fail_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("cannot encode this burst")
        return original(*args, **kwargs)

    monkeypatch.setattr(link, "encode_burst_v5", fail_second)
    worker, records, log = _run(profile, _Sink(), 30)
    ((kind, drop),) = log
    assert kind == "tx_dropped" and drop["reason"] == "encode_failed"
    lost = worker.dropped_encode_datagrams
    first = records[0].subframe_count
    assert (drop["first_sequence"], drop["subframes"], drop["attempt"]) == (first, lost, 0)
    assert records[1].first_sequence == first + lost  # the receiver sees this gap


def test_an_encode_failure_before_numbering_has_no_sequences(profile):
    class Failing(MessageTransmitter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = 0

        def encode_datagrams(self, datagrams, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("refused before numbering")
            return super().encode_datagrams(datagrams, **kwargs)

    worker, records, log = _run(profile, _Sink(), 20,
                                transmitter=Failing(profile, mcs_entry=mcs_entry(8)))
    ((_, drop),) = log
    assert drop["first_sequence"] is None and drop["subframes"] == worker.dropped_encode_datagrams
    assert records[0].first_sequence == 0  # nothing was used up


def test_without_events_nothing_is_stamped_or_logged(profile):
    worker, records, log = _run(profile, _Sink(fail=lambda attempt: attempt == 1, retry=False),
                                30, events=False)
    assert log == [] and worker.dropped_send_failed_bursts == 1
    assert all(record.mono is None and record.wall is None and record.planned_mcs is None
               for record in records)


class _DisagreeingPolicy:
    """``peek_next_entry`` says MCS 8, the decision says 12: a switch during the wait."""

    def peek_next_entry(self):
        return mcs_entry(8)

    def entry_for_next_burst(self, now):
        return mcs_entry(12)

    def control_plan(self, now, *, idle):
        return ControlPlan()

    def control_entry(self):
        return mcs_entry(8)

    def idle_wait_s(self, now):
        return None

    def on_sent(self, burst, now):
        pass


@pytest.mark.parametrize("limits", [AggregationLimits(max_subframes=4, max_wait_s=0.003),
                                    AggregationLimits(max_subframes=4,
                                                      min_burst_interval_s=0.004)])
def test_planned_mcs_is_the_peeked_entry_when_the_decision_differs(profile, limits):
    worker, records, log = _run(profile, _Sink(), 20, policy=_DisagreeingPolicy(),
                                limits=limits)
    assert log == [] and sum(record.subframe_count for record in records) == 20
    waited = records if limits.max_wait_s > 0 else records[1:]  # interval: from the 2nd
    assert all((record.planned_mcs, record.mcs_index) == (8, 12) for record in waited)
    assert all(record.mcs_index == 12 for record in records)


def test_a_burst_kept_across_a_detach_is_sent_once_with_attempt_2(profile):
    bridge = _Bridge()
    worker = TransmitWorker(MessageTransmitter(profile, mcs_entry=mcs_entry(8)), bridge,
                            aggregation=AggregationLimits(max_subframes=4),
                            events=lambda kind, value: None)

    class Detaching(_Sink):
        def send(self, samples):
            worker.detach()  # the radio goes away while the burst waits for room
            return False

    for _index in range(3):
        assert worker.submit("t", b"y" * 50)
    worker.attach(Detaching())
    worker.start()
    try:
        assert _wait_for(lambda: worker.sink is None, 5.0)
        time.sleep(0.2)
        assert bridge.sent.values == []
        worker.attach(_Sink())
        assert _wait_for(lambda: worker.pending == 0, 5.0)
    finally:
        worker.stop()
    (record,) = bridge.sent.values
    assert (record.first_sequence, record.subframe_count, record.attempt, record.n) == (0, 3, 2, 1)


def test_a_retrying_sink_that_gives_up_logs_the_burst_with_its_attempts(profile):
    state = {}
    sink = _Sink(fail=lambda attempt: state["worker"].dropped_send_failed_bursts == 0)
    bridge = _Bridge()
    log = []
    worker = state["worker"] = TransmitWorker(
        MessageTransmitter(profile, mcs_entry=mcs_entry(8)), bridge,
        aggregation=AggregationLimits(max_subframes=4),
        events=lambda kind, value: log.append((kind, value)))
    for _index in range(8):
        assert worker.submit("t", b"z" * 60)
    worker.attach(sink)
    worker.start()
    try:
        assert _wait_for(lambda: worker.pending == 0, 10.0)
    finally:
        worker.stop()
    ((kind, drop),) = log
    assert kind == "tx_dropped" and drop["reason"] == "send_failed"
    assert drop["attempt"] >= 5 and drop["attempt"] == sink.calls - len(bridge.sent.values)
    assert (drop["first_sequence"], drop["subframes"]) == (0, 4)
    assert [record.first_sequence for record in bridge.sent.values] == [4]
    assert bridge.sent.values[0].attempt == 1


@pytest.mark.parametrize("fail_first", [False, True])
def test_sequences_wrap_at_16_bits_in_sent_and_dropped_lines(profile, monkeypatch, fail_first):
    import itertools

    transmitter = MessageTransmitter(profile, mcs_entry=mcs_entry(8))
    transmitter._sequence = itertools.count(65534)
    transmitter._next_sequence = 65534  # as if 65534 datagrams had gone before
    if fail_first:
        original, calls = link.encode_burst_v5, []

        def fail_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("cannot encode this burst")
            return original(*args, **kwargs)

        monkeypatch.setattr(link, "encode_burst_v5", fail_once)
    worker, records, log = _run(profile, _Sink(), 12, transmitter=transmitter)
    sequences = [(record.first_sequence, record.subframe_count) for record in records]
    if fail_first:
        ((_, drop),) = log
        assert (drop["first_sequence"], drop["subframes"]) == (65534, 4)  # 65534 .. 1
        assert sequences == [(2, 4), (6, 4)]
    else:
        assert log == []
        assert sequences == [(65534, 4), (2, 4), (6, 4)]
    assert transmitter.next_sequence == 10


def test_the_writer_writes_the_childs_stamp_and_skips_unstamped_records(tmp_path):
    path = tmp_path / "events.jsonl"
    writer = EventLogWriter(str(path), node_id=1, sample_rate=5e6)
    common = dict(source="udp", text="", byte_count=300, message_count=3, subframe_count=3,
                  sample_count=8240, first_sequence=4093, mcs_index=16, mcs_label="MCS 16")
    writer.on_event("sent", SentRecord(**common))  # not stamped: --events is off in the child
    writer.on_event("sent", SentRecord(**common, control_subframes=1, control_sequence=77,
                                       planned_mcs=15, attempt=2, n=9, mono=12.5,
                                       wall=1763520000.25))
    writer.on_event("sent", SentRecord(**{**common, "subframe_count": 0, "first_sequence": 78},
                                       control_subframes=1, control_sequence=78,
                                       control_only=True, standalone=True, n=10, mono=12.6,
                                       wall=1763520000.35))
    writer.on_event("tx_dropped", {"mono": 13.0, "wall": 1763520001.0, "reason": "send_failed",
                                   "first_sequence": 4096, "subframes": 2,
                                   "control_subframes": 1, "control_sequence": 79, "mcs": 16,
                                   "attempt": 1})
    writer.stop()
    rows = read_events(str(path))
    assert rows[0] == {"k": "sent", "node": 1, "mono": 12.5, "wall": 1763520000.25, "n": 9,
                       "first_sequence": 4093, "subframes": 3, "control_subframes": 1,
                       "control_sequence": 77, "mcs": 16, "planned_mcs": 15,
                       "sample_count": 8240, "control_only": False, "standalone": False,
                       "attempt": 2}
    assert rows[1]["first_sequence"] is None and rows[1]["standalone"] is True
    assert rows[2] == {"k": "tx_dropped", "node": 1, "mono": 13.0, "wall": 1763520001.0,
                       "reason": "send_failed", "first_sequence": 4096, "subframes": 2,
                       "control_subframes": 1, "control_sequence": 79, "mcs": 16, "attempt": 1}
    assert rows[3]["counts"] == {"sent": 2, "tx_dropped": 1}
