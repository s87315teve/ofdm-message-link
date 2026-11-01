"""Bidirectional demo node: one process per Pluto, transmitting and receiving at once.

Two nodes make an FDD link: node 1 transmits on one frequency and listens on
the other, node 2 the reverse (``configs/profiles/duplex_fdd_2420_2460.yaml``).
Each node delivers what it receives to its egress port and sends what
arrives on its ingress port; every burst carries a small CONTROL frame with
feedback for the peer's link adaptation.  Semantics stay UDP: no ACK, no ARQ.

GUI (default)::

    python -m ofdm_message_link.node_app --node-id 1 --transport pluto \\
        --enable-rf --acknowledgement '...'

Headless, for scripted measurements::

    python -m ofdm_message_link.node_app --node-id 1 --transport pluto --headless \\
        --serial 1044... --tx-gain -5 --duration 90 --gain-profile 0:-5,30:-20,60:-5 \\
        --stats-log node1_stats.jsonl --la-log la_1.jsonl --ready-file node1.ready \\
        --link-adaptation olla --enable-rf --acknowledgement '...'

Radio-free duplex on one PC: ``--transport udp`` on both nodes (each sends
its samples to the other's sample port).

Control socket (headless and GUI; localhost UDP, one JSON object per
datagram, one answer)::

    {"cmd": "set_gain", "dir": "tx", "db": -20}   -> {"ok": true, "readback": -20.0}
    {"cmd": "set_mcs", "mcs": 12}                 fixed MCS (12); "mcs": null = back to LA
    {"cmd": "set_la", "mode": "auto" | "fixed"}
    {"cmd": "mark", "text": "phase 2"}            copied into the stats and LA logs
    {"cmd": "stats"}                              the latest stats line
    {"cmd": "stop"}
"""

from __future__ import annotations

import json
import select
import signal
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from typing import Any

from . import log_limits, node_options, options
from .dashboard import RxDashboard, TxDashboard
from .node_engine import NodeSpec, PlutoDuplexFactory, ProcessNodeWorker, UdpDuplexFactory

STATS_INTERVAL_S = 0.5
LA_HISTORY_S = 60.0
EXIT_ATTACH_FAILED = 2
EXIT_ENGINE_DIED = 3
BANNER = (
    "DEMO ONLY - bidirectional FDD link, UDP semantics: no ACK, no ARQ, lost bursts stay "
    "lost. Not throughput, reliability or RF acceptance evidence."
)


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def _strip_readback(snapshot: dict | None) -> dict | None:
    if snapshot is None:
        return None
    return {key: value for key, value in snapshot.items() if key != "readback"}


class NodeMonitor:
    """Turns the child's events into dashboards, LA history and one stats line.

    Thread-safe: events arrive on the engine pump thread, the stats line is
    asked for on the main (or GUI) thread.
    """

    def __init__(self, node_id: int, mode: str, sample_rate: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.node_id = node_id
        self.mode = mode
        self._clock = clock
        self._lock = threading.Lock()
        self.rx_dashboard = RxDashboard()
        self.tx_dashboard = TxDashboard(sample_rate)
        self.started = clock()
        self.ready_at: float | None = None
        self.stats: dict[str, Any] | None = None
        self.la_events: deque[dict] = deque(maxlen=200)
        self.la_history: deque[tuple[float, int | None, float | None, bool]] = deque()
        self.failures: deque[str] = deque(maxlen=50)
        self.marks: list[dict] = []
        self.gains: list[dict] = []
        self.last_line: dict[str, Any] | None = None
        self.last_views: tuple[Any, Any] = (None, None)
        self.messages = 0

    def la_snapshot(self) -> tuple[tuple[float, int | None, float | None, bool], ...]:
        with self._lock:
            return tuple(self.la_history)

    def on_event(self, kind: str, value: Any) -> None:
        now = self._clock()
        with self._lock:
            if kind == "stats":
                self.stats = value
                la = value.get("la") or {}
                self.la_history.append(
                    (now, la.get("mcs_index"), la.get("snr_f_db"), bool(la.get("stale")))
                )
                while self.la_history and now - self.la_history[0][0] > LA_HISTORY_S:
                    self.la_history.popleft()
            elif kind == "link_snr":
                for snr in value:
                    self.rx_dashboard.observe_snr(now, float(snr))
            elif kind == "la":
                self.la_events.append(dict(value))
            elif kind == "failed":
                self.failures.append(str(value))
            elif kind == "mark":
                self.marks.append(dict(value))
            elif kind == "gain":
                self.gains.append(dict(value))
            elif kind == "message":
                self.messages += 1

    def line(self, now: float | None = None) -> dict[str, Any] | None:
        """One stats line (design section 2.5), or None before the first child stats."""

        now = self._clock() if now is None else now
        with self._lock:
            stats = self.stats
            if stats is None:
                return None
            tx, rx, la = stats["tx"], stats["rx"], stats.get("la") or {}
            receiver = _plain(rx["receiver"])
            decoder = _plain(rx["decoder"])
            rx_view = self.rx_dashboard.refresh(now, receiver, mcs_index=la.get("mcs_index"))
            tx_view = self.tx_dashboard.refresh(
                now,
                radio_on=tx.get("transport") is not None,
                sent_bytes=int(tx["payload_bytes"]),
                samples=int(tx["samples"]),
                pending=int(tx["pending"]),
                faults=None,
            )
            self.last_views = (rx_view, tx_view)
            transport = _strip_readback(rx.get("transport"))
            line = {
                "monotonic_s": now,
                "elapsed_s": now - (self.ready_at if self.ready_at is not None else self.started),
                "node_id": self.node_id,
                "mode": self.mode,
                "tx": {
                    "bursts": tx["bursts"],
                    "control_bursts": tx["control_bursts"],
                    "subframes": tx["subframes"],
                    "payload_bytes": tx["payload_bytes"],
                    "pending": tx["pending"],
                    "ingress_datagrams": tx["ingress_datagrams"],
                    "ingress_dropped_queue_full": tx["ingress_dropped_queue_full"],
                    "active_mcs": (
                        la.get("mcs_index") if la.get("mcs_index") is not None
                        else tx.get("last_mcs")
                    ),
                    "airtime_frac": round(tx_view.airtime, 4),
                    "goodput_bps": round(tx_view.goodput_bps, 1),
                    "transport": _strip_readback(tx.get("transport")),
                },
                "rx": {
                    "receiver": receiver,
                    "decoder": decoder,
                    "goodput_now_bps": round(rx_view.goodput_bps, 1),
                    "snr_2s": rx_view.snr_2s,
                    "loss_10s": rx_view.loss_10s,
                    "egress_datagrams": rx["egress_datagrams"],
                    "own_bursts_heard": receiver.get("dropped_bursts", 0),
                    "control_frames": receiver.get("control_frames", 0),
                    "own_seed_drops": receiver.get("own_seed_drops", 0),
                    "sequence_resyncs": receiver.get("sequence_resyncs", 0),
                    "control_handler_errors": receiver.get("control_handler_errors", 0),
                    "transport": transport,
                },
                "la": {
                    key: la.get(key)
                    for key in (
                        "mode", "mcs_index", "reason", "snr_f_db", "offset_db", "snr_eff_db",
                        "feedback_age_s", "stale", "peer_loss_10s", "decisions",
                        "feedback_ignored", "peer_rebaselines", "control_unsupported",
                        "own_bursts_heard", "la_errors", "reporter_errors", "log_errors",
                        "event_errors",
                    )
                },
                "feedback": {
                    "sent": la.get("feedback_sent"),
                    "received": la.get("feedback_received"),
                    "stale": la.get("feedback_stale"),
                },
                "cpu": stats.get("cpu"),
                "device": stats.get("device"),
            }
            if self.marks:
                line["marks"], self.marks = self.marks, []
            if self.gains:
                line["gain_changes"], self.gains = self.gains, []
            self.last_line = line
            return line


class ControlServer:
    """Localhost UDP JSON control socket: one request per datagram, one answer."""

    def __init__(self, port: int, handle: Callable[[dict], dict], host: str = "127.0.0.1") -> None:
        self._handle = handle
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # No SO_REUSEADDR: a second node on the same port must fail, not share.
        try:
            self._socket.bind((host, int(port)))
        except OSError:
            self._socket.close()
            raise
        self.address = self._socket.getsockname()

    def poll(self, timeout: float) -> bool:
        """Serve at most one request within ``timeout``; True if one was served."""

        ready, _, _ = select.select([self._socket], [], [], max(0.0, timeout))
        if not ready:
            return False
        try:
            data, sender = self._socket.recvfrom(65536)
        except OSError:
            return False
        try:
            request = json.loads(data.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("a request is one JSON object")
            answer = self._handle(request)
        except Exception as error:  # every request gets an answer
            answer = {"ok": False, "error": f"{type(error).__name__}: {error}"}
        try:
            self._socket.sendto(json.dumps(answer, default=str).encode("utf-8"), sender)
        except OSError:
            pass
        return True

    def close(self) -> None:
        self._socket.close()


def control_handler(worker, monitor: NodeMonitor, *, log_mark: Callable[[str], None],
                    request_stop: Callable[[], None]) -> Callable[[dict], dict]:
    """The commands of the control socket, against a ProcessNodeWorker-like object."""

    from ofdm_link.phy.mcs_table import mcs_entry

    def handle(request: dict) -> dict:
        command = request.get("cmd")
        if command == "set_gain":
            direction = request.get("dir", "tx")
            readback = worker.set_gain(direction, float(request["db"]))
            return {"ok": True, "readback": readback}
        if command == "set_mcs":
            value = request.get("mcs")
            entry = None if value is None else mcs_entry(int(value))
            if entry is not None and (entry.family != "v5" or not entry.tx_visible):
                return {"ok": False, "error": f"MCS {value} is not a transmit-visible v5 entry"}
            worker.select_mcs_entry(entry)
            return {"ok": True}
        if command == "set_la":
            worker.set_la_mode(str(request.get("mode")))
            return {"ok": True}
        if command == "mark":
            text = str(request.get("text", ""))
            worker.mark(text)
            log_mark(text)
            return {"ok": True}
        if command == "stats":
            return {"ok": True, "stats": monitor.last_line}
        if command == "stop":
            request_stop()
            return {"ok": True}
        return {"ok": False, "error": f"unknown cmd {command!r}"}

    return handle


class HeadlessNode:
    """The headless parent loop: attach, mark ready, log, serve control, stop cleanly."""

    def __init__(
        self,
        worker,
        monitor: NodeMonitor,
        factory: Callable[[], Any],
        *,
        stats_log=None,
        ready_file: str | None = None,
        control_port: int = 0,
        duration_s: float | None = None,
        gain_profile: tuple[tuple[float, float], ...] = (),
        clock: Callable[[], float] = time.monotonic,
        out=sys.stderr,
    ) -> None:
        self.worker = worker
        self.monitor = monitor
        self.factory = factory
        self.stats_log = stats_log
        self.ready_file = ready_file
        self.control_port = control_port
        self.duration_s = duration_s
        self.schedule = node_options.GainSchedule(gain_profile)
        self.clock = clock
        self.out = out
        self.stop_requested = threading.Event()
        self.control: ControlServer | None = None

    def request_stop(self, *_: object) -> None:
        self.stop_requested.set()

    def _write(self, record: dict) -> None:
        if self.stats_log is not None:
            self.stats_log.write(json.dumps(record, default=str) + "\n")
            self.stats_log.flush()

    def _log_mark(self, text: str) -> None:
        self._write({"monotonic_s": self.clock(), "node_id": self.monitor.node_id,
                     "mark": text})

    def _apply_gain_steps(self, elapsed: float) -> None:
        for when, gain in self.schedule.due(elapsed):
            try:
                readback = self.worker.set_gain("tx", gain)
                print(f"node_app: t={elapsed:.2f} s TX gain {gain:g} dB (readback "
                      f"{readback:g}), profile step at {when:g} s", file=self.out)
                self._write({"monotonic_s": self.clock(), "node_id": self.monitor.node_id,
                             "gain_step": {"t": when, "db": gain, "readback": readback,
                                           "elapsed_s": elapsed}})
            except Exception as error:
                print(f"node_app: gain step at {when:g} s failed: {error}", file=self.out)

    def run(self) -> int:
        self.worker.start()
        try:
            try:
                description = self.worker.attach_from(self.factory)
            except Exception as error:
                print(f"node_app: the radio did not start: {error}", file=self.out)
                return EXIT_ATTACH_FAILED
            if self.control_port:
                try:
                    self.control = ControlServer(
                        self.control_port,
                        control_handler(self.worker, self.monitor, log_mark=self._log_mark,
                                        request_stop=self.request_stop),
                    )
                except OSError as error:
                    print(f"node_app: cannot open control port {self.control_port}: {error}",
                          file=self.out)
                    return EXIT_ATTACH_FAILED
            ready = self.clock()
            self.monitor.ready_at = ready
            options.mark_ready(self.ready_file, description)
            print(f"node_app: node {self.monitor.node_id} ready: {description}", file=self.out)
            next_line = ready
            while not self.stop_requested.is_set():
                if not getattr(self.worker, "alive", True):
                    print("node_app: the radio engine process exited", file=self.out)
                    return EXIT_ENGINE_DIED
                now = self.clock()
                elapsed = now - ready
                self._apply_gain_steps(elapsed)
                if self.duration_s is not None and elapsed >= self.duration_s:
                    break
                if now >= next_line:
                    next_line += STATS_INTERVAL_S
                    line = self.monitor.line(now)
                    if line is not None:
                        self._write(line)
                wait = max(0.0, min(next_line - self.clock(), 0.05))
                if self.control is not None:
                    self.control.poll(wait)
                else:
                    self.stop_requested.wait(wait)
            return 0
        finally:
            options.clear_ready(self.ready_file)
            try:
                self.worker.detach()
            except Exception as error:
                print(f"node_app: detach: {error}", file=self.out)
            self.worker.stop()
            if self.control is not None:
                self.control.close()
            if self.stats_log is not None:
                self.stats_log.close()


def build_spec(args, setup: node_options.NodeSetup) -> NodeSpec:
    return NodeSpec(
        node_id=setup.node_id,
        peer_id=setup.peer_id,
        mcs_index=setup.resolved.mcs_entry.index,
        control_mcs_index=setup.control_entry.index,
        seed_range=setup.seed_range,
        ingress_port=setup.ingress_port,
        egress=setup.egress,
        la_spec=args.link_adaptation,
        la_args=tuple(args.link_adaptation_arg),
        reporter_spec=args.feedback_reporter,
        reporter_args=tuple(args.feedback_reporter_arg),
        la_log_path=args.la_log,
        tx_gain_db=args.tx_gain,
        mode=setup.mode,
    )


def udp_factory(args, setup: node_options.NodeSetup) -> UdpDuplexFactory:
    return UdpDuplexFactory(
        host=args.udp_host,
        tx_port=setup.udp_peer_port,
        rx_port=setup.udp_listen_port,
        peak_amplitude=args.peak_amplitude,
        snr_db=args.snr_db,
    )


def pluto_factory(args, setup: node_options.NodeSetup, *, uri: str, serial: str,
                  tx_gain_db: float, rx_gain_db: float) -> PlutoDuplexFactory:
    return PlutoDuplexFactory(
        uri=uri,
        serial=serial,
        sample_rate=int(setup.resolved.profile.sample_rate),
        tx_frequency=setup.tx_frequency,
        rx_frequency=setup.rx_frequency,
        tx_gain_db=tx_gain_db,
        rx_gain_db=rx_gain_db,
        acknowledgement=args.acknowledgement,
        rx_bandwidth=args.rx_analog_bw,
        rx_kernel_buffers=args.rx_kernel_buffers,
        peak_amplitude=args.peak_amplitude,
        tx_duty_cap=None if args.tx_duty_cap >= 1.0 else args.tx_duty_cap,
    )


def find_pluto(serial: str):
    from . import pluto

    for device in pluto.discover():
        if device.serial == serial:
            return device
    raise SystemExit(f"Pluto {serial} not found")


def validate_algorithms(spec: NodeSpec, sample_rate: float) -> None:
    """Build throw-away LA and reporter instances so a bad spec fails before any process."""

    from .node_engine import build_algorithms

    build_algorithms(spec, sample_rate)


def main(argv: list[str] | None = None) -> int:
    args = node_options.parse_args(argv)
    from ofdm_link.runtime.cpu_budget import apply_cpu_budget

    apply_cpu_budget(1)
    node_options.check_paths(args)
    setup = node_options.resolve_node(args)
    node_options.warn(setup)
    if setup.resolved.uses_radio:
        # Before any window or child exists: an unauthorised process never transmits.
        options.require_rf_capability(args)
    spec = build_spec(args, setup)
    validate_algorithms(spec, setup.resolved.profile.sample_rate)
    if args.headless:
        return run_headless(args, setup, spec)
    from .node_window import run_gui

    return run_gui(args, setup, spec)


def run_headless(args, setup: node_options.NodeSetup, spec: NodeSpec) -> int:
    if setup.resolved.uses_radio:
        device = find_pluto(args.serial)
        factory = pluto_factory(args, setup, uri=device.address, serial=device.serial,
                                tx_gain_db=args.tx_gain, rx_gain_db=args.rx_gain)
    else:
        factory = udp_factory(args, setup)
    monitor = NodeMonitor(setup.node_id, setup.mode if setup.resolved.uses_radio else "udp",
                          setup.resolved.profile.sample_rate)
    worker = ProcessNodeWorker(
        setup.resolved.profile,
        spec,
        monitor.on_event,
        ui_fps=args.ui_fps,
        decode_workers=args.decode_workers,
        aggregation=setup.resolved.aggregation,
    )
    node = HeadlessNode(
        worker,
        monitor,
        factory,
        stats_log=log_limits.open_stats_log(args),
        ready_file=args.ready_file,
        control_port=setup.control_port,
        duration_s=args.duration,
        gain_profile=setup.gain_profile,
    )
    signal.signal(signal.SIGTERM, node.request_stop)
    signal.signal(signal.SIGINT, node.request_stop)
    return node.run()


if __name__ == "__main__":
    raise SystemExit(main())
