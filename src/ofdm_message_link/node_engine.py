"""The radio side of ``node_app``: one child process transmits and receives at once.

A Pluto opens in one libiio context per process (a second one fails with
EBUSY), so both directions of a node live in one engine child.  The child
reuses everything the one-way apps run: :class:`~.tx_app.TransmitWorker`
and :class:`~.rx_app.ReceiveWorker`, the decode pool, UDP ingress and
egress, and :func:`~.engine._serve`.  What is new here:

* :class:`PlutoDuplexFactory` / :class:`UdpDuplexFactory`, picklable, build
  the sink and source in the child, on one
  :class:`~.pluto.SharedPlutoContext` (or as two UDP sample sockets, for a
  radio-free duplex link on one PC);
* :class:`NodeWorker` starts transmit first and receive second, and stops in
  the same order, so the TX attenuator is back at minimum output before the
  context goes away;
* the link controller (:class:`~.la.controller.LinkController`) is the
  transmit worker's burst policy, the receiver's control handler and the
  receive worker's observer;
* :class:`ProcessNodeWorker` is the parent's handle on the child.

engine.py itself does not change.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .engine import (
    _ATTACH_TIMEOUT_S,
    _DETACH_TIMEOUT_S,
    _bound_numeric_threads,
    _EngineProcess,
    _EventSink,
    _serve,
    _Signal,
)
from .transport import TransportError

SWITCH_INTERVAL_S = 0.002
_GAIN_TIMEOUT_S = 5.0
_SUBMIT_FAILURE_EVERY_S = 1.0


# --------------------------------------------------------------------------
# endpoints (built in the child)
# --------------------------------------------------------------------------


class DuplexEndpoint:
    """One node's sink and source, and the shared context under them if any."""

    def __init__(self, sink, source, *, shared=None, sample_rate: int | None = None,
                 description: str = "") -> None:
        self.sink = sink
        self.source = source
        self.shared = shared
        self._sample_rate = sample_rate
        self.description = description

    def open(self) -> None:
        """Set the shared sample rate once, before either transport starts."""

        if self.shared is not None:
            self.shared.configure(self._sample_rate)

    def close(self) -> None:
        if self.shared is not None:
            self.shared.close()

    def snapshot(self) -> dict[str, object] | None:
        return None if self.shared is None else self.shared.snapshot()


#: Highest TX duty at which a Pluto still receives 5 MS/s without overflows
#: (M0, experiments/duplex_fdd/results/usb*: 0.7 clean on both, 0.8 asked ->
#: TX reached 0.78 and one Pluto overflowed ~220 times a minute).
DEFAULT_PLUTO_DUTY_CAP = 0.7


class DutyCappedSink:
    """A sink that keeps its transmit duty at or below ``cap``.

    A Pluto's USB 2.0 link carries its 20 MB/s receive stream and transmit
    pushes up to a duty of about 0.7; above that the receiver overflows.  A
    burst may start no earlier than the previous start plus its airtime /
    ``cap``.  Idle time banks at most ``credit_s`` of earlier starts, so a
    short run of bursts after a pause is not held back.
    """

    def __init__(self, sink, cap: float, sample_rate: float, *, credit_s: float = 0.05,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if not 0.0 < cap <= 1.0:
            raise ValueError("the duty cap must be in (0, 1]")
        self._sink = sink
        self._cap = float(cap)
        self._rate = float(sample_rate)
        self._credit_s = float(credit_s)
        self._clock = clock
        self._sleep = sleep
        self._next_allowed = -1e18
        self.waits = 0
        self.waited_s = 0.0

    def __getattr__(self, name: str):
        return getattr(self._sink, name)

    def send(self, samples) -> bool:
        now = self._clock()
        start = max(self._next_allowed, now - self._credit_s)
        if start > now:
            self.waits += 1
            self.waited_s += start - now
            self._sleep(start - now)
        accepted = self._sink.send(samples)
        if accepted:
            self._next_allowed = start + len(samples) / self._rate / self._cap
        return accepted

    def snapshot(self) -> dict[str, object]:
        return {
            **self._sink.snapshot(),
            "tx_duty_cap": self._cap,
            "duty_cap_waits": self.waits,
            "duty_cap_waited_s": round(self.waited_s, 3),
        }


@dataclass(frozen=True)
class PlutoDuplexFactory:
    """Builds a node's Pluto sink and source on one shared context, in the child."""

    uri: str
    serial: str
    sample_rate: int
    tx_frequency: float
    rx_frequency: float
    tx_gain_db: float
    rx_gain_db: float
    acknowledgement: str
    rx_bandwidth: float | None = None
    rx_kernel_buffers: int = 4
    peak_amplitude: float = 0.7
    tx_duty_cap: float | None = DEFAULT_PLUTO_DUTY_CAP

    def __call__(self) -> DuplexEndpoint:
        from ofdm_link.radio import acknowledge_rf_transmission

        from . import pluto

        token = acknowledge_rf_transmission(self.acknowledgement)
        shared = pluto.SharedPlutoContext(
            self.uri, self.serial, rx_kernel_buffers=self.rx_kernel_buffers
        )
        sink = pluto.PlutoSampleSink(
            pluto.PlutoSettings(
                self.uri, self.serial, self.sample_rate, self.tx_frequency, self.tx_gain_db
            ),
            token,
            peak_amplitude=self.peak_amplitude,
            open_context=shared.factory(),
        )
        source = pluto.PlutoSampleSource(
            pluto.PlutoSettings(
                self.uri,
                self.serial,
                self.sample_rate,
                self.rx_frequency,
                self.rx_gain_db,
                rf_bandwidth=self.rx_bandwidth,
            ),
            open_context=shared.factory(),
        )
        if self.tx_duty_cap is not None and self.tx_duty_cap < 1.0:
            sink = DutyCappedSink(sink, self.tx_duty_cap, self.sample_rate)
        return DuplexEndpoint(
            sink,
            source,
            shared=shared,
            sample_rate=self.sample_rate,
            description=(
                f"pluto {self.serial[-6:]} TX {self.tx_frequency / 1e6:.3f} MHz "
                f"RX {self.rx_frequency / 1e6:.3f} MHz"
            ),
        )


@dataclass(frozen=True)
class UdpDuplexFactory:
    """Two UDP sample sockets: send to the peer's port, listen on our own."""

    host: str
    tx_port: int
    rx_port: int
    peak_amplitude: float = 0.7
    snr_db: float | None = None

    def __call__(self) -> DuplexEndpoint:
        from .transport import UdpSampleSink, UdpSampleSource

        sink = UdpSampleSink(host=self.host, port=self.tx_port, peak_amplitude=self.peak_amplitude)
        source = UdpSampleSource(bind_host=self.host, port=self.rx_port, snr_db=self.snr_db)
        return DuplexEndpoint(
            sink, source, description=f"udp TX :{self.tx_port} RX :{self.rx_port}"
        )


# --------------------------------------------------------------------------
# what the child runs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeSpec:
    """Everything the child needs that is not the PHY profile; picklable."""

    node_id: int
    peer_id: int
    mcs_index: int
    control_mcs_index: int
    seed_range: tuple[int, int]
    ingress_port: int
    egress: tuple[str, int]
    la_spec: str = "fixed"
    la_args: tuple[str, ...] = ()
    reporter_spec: str = "default"
    reporter_args: tuple[str, ...] = ()
    thresholds_path: str | None = None
    la_log_path: str | None = None
    tx_gain_db: float | None = None
    mode: str = "fdd"
    ingress_host: str = "127.0.0.1"


def la_context(spec: NodeSpec, sample_rate: float):
    """The :class:`~.la.api.LaContext` both the parent check and the child use."""

    from .la.api import LaContext
    from .la.thresholds import build_ladder, load_thresholds

    thresholds = load_thresholds(spec.thresholds_path)
    return LaContext(
        ladder=build_ladder(thresholds),
        thresholds_db=thresholds,
        initial_mcs_index=spec.mcs_index,
        sample_rate=float(sample_rate),
        node_id=spec.node_id,
    )


def build_algorithms(spec: NodeSpec, sample_rate: float):
    """(la, reporter, ladder): built from the spec strings; SystemExit on a bad spec."""

    from .la.api import ReporterContext
    from .la.loader import build_link_adaptation, build_reporter

    context = la_context(spec, sample_rate)
    la = build_link_adaptation(spec.la_spec, context, spec.la_args)
    reporter = build_reporter(
        spec.reporter_spec,
        ReporterContext(node_id=spec.node_id, peer_node_id=spec.peer_id),
        spec.reporter_args,
    )
    return la, reporter, context.ladder


class _NullPlots:
    """The transmit worker's plot buffers: a node shows the receive spectrum only."""

    def set_samples(self, samples) -> None:
        pass

    def add_symbols(self, symbols, *, context) -> None:
        pass


@dataclass
class TxCounters:
    bursts: int = 0
    control_bursts: int = 0
    subframes: int = 0
    payload_bytes: int = 0
    messages: int = 0
    samples: int = 0
    last_mcs: int | None = None


class _CountingSent:
    """``bridge.sent`` that counts every burst before the display event may drop."""

    def __init__(self, sink: _EventSink, counters: TxCounters, lock: threading.Lock) -> None:
        self._signal = _Signal(sink, "sent")
        self._counters = counters
        self._lock = lock

    def emit(self, record) -> None:
        with self._lock:
            counters = self._counters
            counters.bursts += 1
            counters.samples += int(record.sample_count)
            counters.last_mcs = int(record.mcs_index)
            if getattr(record, "control_only", False):
                counters.control_bursts += 1
            else:
                counters.subframes += int(record.subframe_count)
                counters.payload_bytes += int(record.byte_count)
                counters.messages += int(record.message_count)
        self._signal.emit(record)


class _TxBridge:
    def __init__(self, sink: _EventSink, counters: TxCounters, lock: threading.Lock) -> None:
        self.sent = _CountingSent(sink, counters, lock)
        self.failed = sink.failed


class NodeWorker:
    """Both directions of one node behind the one worker surface ``_serve`` drives."""

    def __init__(self, tx_worker, rx_worker, *, controller=None, ingress_error: str | None = None,
                 failed: Callable[[str], None] | None = None) -> None:
        self.tx = tx_worker
        self.rx = rx_worker
        self.controller = controller
        self.ingress_error = ingress_error
        self.endpoint: DuplexEndpoint | None = None
        self.lag_monitor = None
        self._failed = failed or (lambda message: None)

    def start(self) -> None:
        self.tx.start()
        self.rx.start()

    def attach(self, endpoint: DuplexEndpoint) -> None:
        """Transmit first (attenuator quiet, TX LO), then receive."""

        if self.ingress_error is not None:
            raise TransportError(self.ingress_error)
        endpoint.open()
        try:
            self.tx.attach(endpoint.sink)
            self.rx.attach(endpoint.source)
        except BaseException:
            self.tx.detach()
            self.rx.detach()
            endpoint.close()
            raise
        self.endpoint = endpoint
        snapshot = endpoint.snapshot()
        if snapshot is not None and snapshot.get("ensm_mode") not in (None, "", "fdd"):
            self._failed(f"the Pluto's ensm_mode is {snapshot['ensm_mode']!r}, not 'fdd'")

    def detach(self) -> None:
        """Transmit first, so its attenuator returns to minimum output, then receive."""

        endpoint, self.endpoint = self.endpoint, None
        self.tx.detach()
        self.rx.detach()
        if endpoint is not None:
            endpoint.close()

    def set_gain(self, direction: str, gain_db: float) -> float:
        if direction == "tx":
            readback = float(self.tx.set_gain(gain_db))
            if self.controller is not None:
                self.controller.set_tx_gain(readback)
            return readback
        if direction == "rx":
            return float(self.rx.set_gain(gain_db))
        raise ValueError("direction must be 'tx' or 'rx'")

    def stop(self) -> None:
        self.tx.stop()
        self.rx.stop()


def _node_main(profile, spec: NodeSpec, ui_fps, decode_workers, aggregation,
               events, control, replies) -> None:
    _bound_numeric_threads()
    from ofdm_link.phy.mcs_table import mcs_entry

    from .la.controller import LinkController
    from .link import MessageReceiver, MessageTransmitter
    from .rx_app import EgressForwarder, ReceiveWorker
    from .tx_app import IngressListener, TransmitWorker

    sink = _EventSink(events, samples_per_second=ui_fps)
    la_log = open(spec.la_log_path, "a", encoding="utf-8") if spec.la_log_path else None  # noqa: SIM115
    la, reporter, ladder = build_algorithms(spec, profile.sample_rate)
    controller = LinkController(
        node_id=spec.node_id,
        peer_id=spec.peer_id,
        la=la,
        reporter=reporter,
        ladder=ladder,
        control_entry=mcs_entry(spec.control_mcs_index),
        mode="auto",
        la_log=la_log,
        sample_rate=float(profile.sample_rate),
        on_event=lambda kind, record: sink.put(kind, dict(record)),
        # The node's own leakage is recognised by its scrambler seeds (node_app only:
        # rx_app's peer is a tx_app that uses every seed).
        own_seed_range=tuple(spec.seed_range),
    )
    controller.set_tx_gain(spec.tx_gain_db)
    counters = TxCounters()
    counters_lock = threading.Lock()
    transmitter = MessageTransmitter(
        profile, mcs_entry=mcs_entry(spec.mcs_index), seed_range=tuple(spec.seed_range)
    )
    tx_worker = TransmitWorker(
        transmitter,
        _TxBridge(sink, counters, counters_lock),
        _NullPlots(),
        aggregation=aggregation,
        policy=controller,
    )
    receiver = MessageReceiver(
        profile,
        decode_workers=decode_workers,
        control_handler=controller,
        own_seed_range=tuple(spec.seed_range),
    )
    egress = EgressForwarder(*spec.egress)
    rx_worker = ReceiveWorker(receiver, sink, sink, egress=egress, observer=controller)
    submit_failures = {"count": 0, "last_report": 0.0}

    def submit_counted(source: str, payload: bytes) -> bool:
        """The ingress path: a full send queue drops the datagram, and that is counted."""

        accepted = tx_worker.submit(source, payload)
        if not accepted:
            submit_failures["count"] += 1
        return accepted

    ingress = IngressListener(
        spec.ingress_host, spec.ingress_port, submit_counted, reuse_address=False
    )
    ingress_error = None
    try:
        ingress.start()
    except OSError as error:
        ingress_error = f"cannot open UDP ingress port {spec.ingress_port}: {error}"
        sink.failed.emit(ingress_error)
    worker = NodeWorker(tx_worker, rx_worker, controller=controller,
                        ingress_error=ingress_error, failed=sink.failed.emit)
    worker.start()

    def apply_gain(direction: str, gain_db: float) -> float:
        readback = worker.set_gain(direction, gain_db)
        sink.put("gain", {"dir": direction, "readback": readback, "t": time.monotonic()})
        return readback

    def handle(command: tuple) -> None:
        kind = command[0]
        try:
            if kind == "submit":
                if not tx_worker.submit(command[1], command[2]):
                    submit_failures["count"] += 1
                    now = time.monotonic()
                    if now - submit_failures["last_report"] >= _SUBMIT_FAILURE_EVERY_S:
                        submit_failures["last_report"] = now
                        sink.failed.emit("send queue is full; the transport is not keeping up")
            elif kind == "gain":
                apply_gain(command[1], float(command[2]))
            elif kind == "gain_sync":
                try:
                    replies.put(("ok", apply_gain(command[1], float(command[2]))))
                except Exception as error:
                    replies.put(("error", f"{type(error).__name__}: {error}"))
            elif kind == "mcs":
                controller.set_fixed(command[1])
            elif kind == "la":
                controller.set_la_mode(command[1])
            elif kind == "mark":
                controller.mark(command[1])
                sink.put("mark", {"t": time.monotonic(), "text": command[1]})
        except Exception as error:  # shown, never swallowed
            sink.failed.emit(f"{kind} failed: {type(error).__name__}: {error}")

    started_cpu = os.times()

    def tick() -> None:
        tx_sink = tx_worker.sink
        rx_source = rx_worker.source
        endpoint = worker.endpoint
        with counters_lock:
            tx_counts = asdict(counters)
        times = os.times()
        lag = worker.lag_monitor
        sink.put(
            "stats",
            {
                "tx": {
                    **tx_counts,
                    "pending": tx_worker.pending,
                    "ingress_datagrams": ingress.datagrams,
                    "ingress_dropped_queue_full": submit_failures["count"],
                    "transport": None if tx_sink is None else tx_sink.snapshot(),
                },
                "rx": {
                    "receiver": receiver.stats,
                    "decoder": receiver.decoder_snapshot(),
                    "egress_datagrams": egress.datagrams,
                    "transport": None if rx_source is None else rx_source.snapshot(),
                    "decode_pool": receiver.decode_pool_snapshot(),
                },
                "la": controller.snapshot(),
                "device": None if endpoint is None else endpoint.snapshot(),
                "cpu": {
                    "engine_pid": os.getpid(),
                    "engine_cpu_s": round(
                        (times.user - started_cpu.user) + (times.system - started_cpu.system), 2
                    ),
                    "decode_worker_cpu_s": receiver.decode_worker_cpu_seconds(),
                    "max_lag_ms": None if lag is None else round(lag.max_lag_s * 1e3, 1),
                },
                "dropped_display_events": sink.dropped,
            },
        )

    try:
        _serve(worker, control, replies, tick, handle, switch_interval_s=SWITCH_INTERVAL_S)
    finally:
        worker.detach()
        ingress.stop()
        worker.stop()
        if la_log is not None:
            la_log.close()


# --------------------------------------------------------------------------
# the parent's handle
# --------------------------------------------------------------------------


Listener = Callable[[str, Any], None]


@dataclass
class NodeState:
    """The latest picture of the child, as the parent sees it."""

    stats: dict[str, Any] | None = None
    events: dict[str, int] = field(default_factory=dict)


class ProcessNodeWorker:
    """Both directions of one node, run in a child process.

    ``listener(kind, value)`` receives every event on the engine's pump
    thread: ``message``, ``sent``, ``level``, ``failed``, ``link_snr``,
    ``samples``, ``symbols``, ``stats``, ``la``, ``feedback``, ``gain``,
    ``mark``.
    """

    def __init__(
        self,
        profile,
        spec: NodeSpec,
        listener: Listener,
        *,
        ui_fps: int = 10,
        decode_workers: int = 3,
        aggregation=None,
        target: Callable[..., None] = _node_main,
    ) -> None:
        self._spec = spec
        self._listener = listener
        self._state = NodeState()
        self._attached: str | None = None
        self._engine = _EngineProcess(
            target,
            (profile, spec, ui_fps, decode_workers, aggregation),
            self._dispatch,
        )

    def _dispatch(self, kind: str, value) -> None:
        self._state.events[kind] = self._state.events.get(kind, 0) + 1
        if kind == "stats":
            self._state.stats = value
        self._listener(kind, value)

    @property
    def pid(self) -> int | None:
        return self._engine.pid

    @property
    def stats(self) -> dict[str, Any] | None:
        return self._state.stats

    @property
    def alive(self) -> bool:
        """False once the engine child has exited (crashed or stopped)."""

        process = self._engine._process
        return process.pid is not None and process.is_alive()

    @property
    def attached(self) -> str | None:
        return self._attached

    def start(self) -> None:
        self._engine.start()

    def attach_from(self, factory: Callable[[], Any]) -> str:
        status, detail = self._engine.request(("attach", factory), _ATTACH_TIMEOUT_S)
        if status != "ok":
            raise TransportError(detail)
        self._attached = detail
        return detail

    def detach(self) -> None:
        if self._attached is None:
            return
        self._attached = None
        self._engine.request(("detach",), _DETACH_TIMEOUT_S)

    def set_gain(self, direction: str, gain_db: float) -> float:
        """Change one direction's gain now; returns the device readback."""

        if direction not in ("tx", "rx"):
            raise ValueError("direction must be 'tx' or 'rx'")
        if self._attached is None:
            raise TransportError("the radio is not running")
        status, detail = self._engine.request(
            ("gain_sync", direction, float(gain_db)), _GAIN_TIMEOUT_S
        )
        if status != "ok":
            raise TransportError(detail)
        return float(detail)

    def _post(self, command: tuple) -> None:
        if not self._engine.post(command):
            raise TransportError("radio engine control queue is full")

    def select_mcs_entry(self, entry) -> None:
        """Fix the MCS (fixed mode); None hands it back to the LA."""

        self._post(("mcs", entry))

    def set_la_mode(self, mode: str) -> None:
        if mode not in ("auto", "fixed"):
            raise ValueError("mode must be 'auto' or 'fixed'")
        self._post(("la", mode))

    def mark(self, text: str) -> None:
        self._post(("mark", str(text)))

    def submit(self, source: str, payload: bytes) -> bool:
        return self._engine.post(("submit", source, payload))

    def stop(self) -> None:
        self._attached = None
        self._engine.stop()


def describe_spec(spec: NodeSpec) -> dict[str, object]:
    return asdict(replace(spec))


__all__ = [
    "DuplexEndpoint",
    "NodeSpec",
    "NodeWorker",
    "PlutoDuplexFactory",
    "ProcessNodeWorker",
    "UdpDuplexFactory",
    "build_algorithms",
    "describe_spec",
    "la_context",
]
