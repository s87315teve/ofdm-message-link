"""Two node engines over the UDP sample transport: a radio-free duplex link.

Each engine runs in its own child process, exactly as node_app runs it; the
two exchange samples over localhost UDP.  Datagrams must arrive byte-exact in
both directions, feedback must flow both ways, and the LA log must record
decisions.
"""

from __future__ import annotations

import json
import socket
import threading
import time

from ofdm_message_link import node_options
from ofdm_message_link.link import build_phy_profile
from ofdm_message_link.node_app import NodeMonitor
from ofdm_message_link.node_engine import NodeSpec, ProcessNodeWorker, UdpDuplexFactory
from ofdm_message_link.options import load_link_config

SECONDS = 3.0


def _free_ports(count):
    sockets = []
    for _ in range(count):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        sockets.append(probe)
    ports = [probe.getsockname()[1] for probe in sockets]
    for probe in sockets:
        probe.close()
    return ports


class _Egress:
    def __init__(self, port):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
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


def test_two_engines_exchange_datagrams_and_feedback(tmp_path):
    config = load_link_config("configs/default.yaml", [node_options.DEFAULT_NODE_OVERLAY])
    profile = build_phy_profile(config)
    ingress1, egress1, samples1, ingress2, egress2, samples2 = _free_ports(6)
    egress = {1: _Egress(egress1), 2: _Egress(egress2)}
    workers, monitors = {}, {}
    plan = {
        1: (ingress1, egress1, samples1, samples2),
        2: (ingress2, egress2, samples2, samples1),
    }
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
                la_spec="olla",
                la_log_path=str(tmp_path / f"la_{node_id}.jsonl"),
            )
            monitors[node_id] = NodeMonitor(node_id, "udp", profile.sample_rate)
            workers[node_id] = ProcessNodeWorker(
                profile, spec, monitors[node_id].on_event, decode_workers=1
            )
            workers[node_id].start()
        for node_id, (_, _, listen, peer) in plan.items():
            workers[node_id].attach_from(
                UdpDuplexFactory(host="127.0.0.1", tx_port=peer, rx_port=listen)
            )
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sent = {1: [], 2: []}
        started = time.monotonic()
        index = 0
        while time.monotonic() - started < SECONDS:
            for node_id, (ingress, *_rest) in plan.items():
                payload = f"node {node_id} datagram {index:05d} ".encode() * 20
                sent[node_id].append(payload)
                sender.sendto(payload, ("127.0.0.1", ingress))
            index += 1
            time.sleep(0.02)
        sender.close()
        time.sleep(1.5)
        lines = {node_id: monitors[node_id].line() for node_id in plan}
    finally:
        for worker in workers.values():
            try:
                worker.detach()
            finally:
                worker.stop()
        for receiver in egress.values():
            receiver.close()

    # Node 1's datagrams come out of node 2's egress, byte-exact, and the reverse.
    for source, sink in ((1, 2), (2, 1)):
        got = egress[sink].payloads
        assert got, f"nothing arrived from node {source}"
        assert set(got) <= set(sent[source])
        assert len(got) >= 0.9 * len(sent[source])
    for node_id, line in lines.items():
        assert line is not None
        assert line["feedback"]["sent"] > 0 and line["feedback"]["received"] > 0
        assert line["rx"]["own_bursts_heard"] == 0
        assert line["rx"]["control_frames"] > 0
        # Over UDP a node never hears itself, and nothing in the LA path failed.
        assert line["rx"]["own_seed_drops"] == 0
        assert line["rx"]["control_handler_errors"] == 0
        assert line["la"]["la_errors"] == 0 and line["la"]["reporter_errors"] == 0
        assert line["la"]["control_unsupported"] == 0
        events = [
            json.loads(row)
            for row in (tmp_path / f"la_{node_id}.jsonl").read_text().splitlines()
        ]
        assert any(event.get("event") == "la" for event in events)
        assert any(event.get("event") == "feedback" for event in events)
