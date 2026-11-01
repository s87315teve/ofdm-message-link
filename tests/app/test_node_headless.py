"""node_app's headless parent against a fake node worker: no child, no radio."""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ofdm_message_link import log_limits, node_app
from ofdm_message_link.link import ReceiveStats

ROOT = Path(__file__).resolve().parents[2]


def _child_stats(bursts=0, delivered=0):
    return {
        "tx": {
            "bursts": bursts,
            "control_bursts": 1,
            "subframes": bursts,
            "payload_bytes": 900 * bursts,
            "messages": bursts,
            "samples": 20_000 * bursts,
            "last_mcs": 8,
            "pending": 0,
            "ingress_datagrams": bursts,
            "ingress_dropped_queue_full": 0,
            "transport": {"transport": "fake", "readback": {"x": 1}},
        },
        "rx": {
            "receiver": ReceiveStats(
                bursts_decoded=delivered,
                messages_delivered=delivered,
                message_bytes_delivered=900 * delivered,
                control_frames=delivered,
            ),
            "decoder": None,
            "egress_datagrams": delivered,
            "transport": {"transport": "fake", "rx_overflow_events": 0},
            "decode_pool": None,
        },
        "la": {"mode": "auto", "mcs_index": 8, "feedback_sent": 3, "feedback_received": 2,
               "feedback_stale": 0, "decisions": 1, "stale": False},
        "device": None,
        "cpu": {"engine_pid": 1, "decode_worker_cpu_s": None, "max_lag_ms": None},
    }


class FakeWorker:
    def __init__(self, listener, *, fail_attach=False):
        self.listener = listener
        self.fail_attach = fail_attach
        self.calls = []
        self.gains = []
        self._stop = threading.Event()
        self._thread = None
        self.started = self.stopped = False
        self.attached = None

    def start(self):
        self.started = True

    def attach_from(self, factory):
        self.calls.append("attach")
        if self.fail_attach:
            raise RuntimeError("no radio")
        self.attached = factory()

        def run():
            count = 0
            while not self._stop.wait(0.05):
                count += 1
                self.listener("link_snr", (15.0,))
                self.listener("stats", _child_stats(count, count))

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return "fake radio"

    def set_gain(self, direction, gain_db):
        self.gains.append((time.monotonic(), direction, gain_db))
        return float(gain_db)

    def select_mcs_entry(self, entry):
        self.calls.append(("mcs", None if entry is None else entry.index))

    def set_la_mode(self, mode):
        self.calls.append(("la", mode))

    def mark(self, text):
        self.calls.append(("mark", text))

    def detach(self):
        self.calls.append("detach")
        self._stop.set()

    def stop(self):
        self.stopped = True
        self._stop.set()


def _free_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _node(tmp_path, *, fail_attach=False, **kwargs):
    monitor = node_app.NodeMonitor(1, "fdd", 5e6)
    worker = FakeWorker(monitor.on_event, fail_attach=fail_attach)
    stats = log_limits.RotatingLog(tmp_path / "stats.jsonl", max_bytes=None)
    node = node_app.HeadlessNode(
        worker,
        monitor,
        lambda: "endpoint",
        stats_log=stats,
        ready_file=str(tmp_path / "ready"),
        out=io.StringIO(),
        **kwargs,
    )
    return node, worker


def _lines(tmp_path):
    return [json.loads(line) for line in (tmp_path / "stats.jsonl").read_text().splitlines()]


def test_ready_file_stats_lines_and_cleanup(tmp_path):
    node, worker = _node(tmp_path, duration_s=1.3)
    seen_ready = []
    watcher = threading.Thread(
        target=lambda: seen_ready.append(_wait_for(tmp_path / "ready")), daemon=True
    )
    watcher.start()
    assert node.run() == 0
    watcher.join(2)
    assert seen_ready == [True]
    assert not (tmp_path / "ready").exists()
    assert worker.calls[0] == "attach" and worker.calls[-1] == "detach" and worker.stopped
    lines = [line for line in _lines(tmp_path) if "tx" in line]
    assert 2 <= len(lines) <= 4
    last = lines[-1]
    assert last["node_id"] == 1 and last["mode"] == "fdd"
    assert last["rx"]["control_frames"] > 0 and last["rx"]["snr_2s"] == pytest.approx(15.0)
    assert last["feedback"] == {"sent": 3, "received": 2, "stale": 0}
    assert "readback" not in last["tx"]["transport"]


def _wait_for(path, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False


def test_attach_failure_exits_2_and_leaves_no_ready_file(tmp_path):
    node, worker = _node(tmp_path, fail_attach=True, duration_s=1.0)
    assert node.run() == node_app.EXIT_ATTACH_FAILED
    assert not (tmp_path / "ready").exists() and worker.stopped


def test_gain_profile_steps_happen_on_time(tmp_path):
    node, worker = _node(tmp_path, duration_s=1.0, gain_profile=((0.0, -5.0), (0.5, -20.0)))
    started = time.monotonic()
    node.run()
    assert [gain for _, _, gain in worker.gains] == [-5.0, -20.0]
    first, second = (when - started for when, _, _ in worker.gains)
    assert first < 0.2 and 0.45 <= second - first <= 0.65
    steps = [line["gain_step"] for line in _lines(tmp_path) if "gain_step" in line]
    assert [step["db"] for step in steps] == [-5.0, -20.0]


def test_control_socket_commands(tmp_path):
    port = _free_port()
    node, worker = _node(tmp_path, control_port=port, duration_s=5.0)
    runner = threading.Thread(target=node.run, daemon=True)
    runner.start()
    assert _wait_for(tmp_path / "ready")
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(2.0)

    def ask(request):
        client.sendto(json.dumps(request).encode(), ("127.0.0.1", port))
        return json.loads(client.recvfrom(65536)[0])

    assert ask({"cmd": "set_gain", "dir": "tx", "db": -20}) == {"ok": True, "readback": -20.0}
    assert ask({"cmd": "set_mcs", "mcs": 12}) == {"ok": True}
    assert ask({"cmd": "set_mcs", "mcs": 11})["ok"] is False
    assert ask({"cmd": "set_la", "mode": "auto"}) == {"ok": True}
    assert ask({"cmd": "mark", "text": "phase 2"}) == {"ok": True}
    time.sleep(0.6)
    assert ask({"cmd": "stats"})["stats"]["node_id"] == 1
    assert ask({"cmd": "bogus"})["ok"] is False
    assert ask({"cmd": "stop"}) == {"ok": True}
    runner.join(3)
    assert not runner.is_alive()
    client.close()
    assert ("mcs", 12) in worker.calls and ("mark", "phase 2") in worker.calls
    assert any(line.get("mark") == "phase 2" for line in _lines(tmp_path))
    assert not (tmp_path / "ready").exists()


def test_sigterm_cleans_up_a_real_headless_process(tmp_path):
    """A headless udp node: ready file, stats lines, and SIGTERM -> exit 0, file gone."""

    ready = tmp_path / "ready"
    stats = tmp_path / "stats.jsonl"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "OMP_NUM_THREADS": "1",
           "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
    port_base = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "ofdm_message_link.node_app", "--node-id", "1", "--headless",
         "--decode-workers", "1", "--ready-file", str(ready), "--stats-log", str(stats),
         "--control-port", "0", "--ingress-port", str(port_base),
         "--egress-port", str(_free_port()), "--udp-listen-port", str(_free_port()),
         "--udp-peer-port", str(_free_port())],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert _wait_for(ready, timeout=30.0), process.stderr.read1().decode()
        time.sleep(1.2)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=20) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert not ready.exists()
    lines = [json.loads(line) for line in stats.read_text().splitlines()]
    assert lines and lines[-1]["tx"]["control_bursts"] > 0
