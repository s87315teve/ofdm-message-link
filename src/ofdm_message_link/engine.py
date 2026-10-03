"""Run each app's radio path in a child process, away from the GUI's GIL.

OTA with the Qt windows on the same interpreter, the receive sink block, the
decoder and the transmit source block all waited on the GIL behind the plots:
with a 0.1 s UHD receive buffer the receiver still overflowed while its sample
queue reported tens of thousands of back-pressure events, and the transmitter
underflowed mid-burst and then sent the following bursts late.  A window cannot
be made cheap enough to rule that out, so the sample path does not share an
interpreter with one.

The child runs the unchanged :class:`~.rx_app.ReceiveWorker` or
:class:`~.tx_app.TransmitWorker` loop.  UDP egress and ingress live in the
child too, so application datagrams never wait for the GUI event loop.  What
the window needs flows back over one bounded queue; display events are dropped
when it is full rather than ever stalling the radio path, and the drops are
counted.
"""

from __future__ import annotations

import atexit
import os
import queue
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import numpy as np

from ofdm_link.runtime.segment_decode_pool import _safe_context

from .transport import TransportError

_EVENT_DEPTH = 4096
_CONTROL_DEPTH = 1024
_STATS_INTERVAL_S = 0.25
_ATTACH_TIMEOUT_S = 60.0
_DETACH_TIMEOUT_S = 15.0
_STOP_TIMEOUT_S = 10.0
_DISPLAY_SYMBOLS = 512
# Set to "1" to report interpreter stalls over 20 ms on stderr.
LAG_MONITOR_ENV = "OFDM_MESSAGE_ENGINE_LAG_MONITOR"


class _Signal:
    def __init__(self, sink: _EventSink, kind: str) -> None:
        self._sink = sink
        self._kind = kind

    def emit(self, value: object) -> None:
        if self._kind == "message":
            # The constellation reaches the window as its own decimated event.
            value = replace(value, payload_symbols=np.zeros(0, dtype=np.complex64))
        self._sink.put(self._kind, value)


class _EventSink:
    """Child-side stand-in for the Qt bridge and the plot buffers."""

    def __init__(self, events: Any, *, samples_per_second: float) -> None:
        self._events = events
        self._samples_interval = 1.0 / samples_per_second
        self._last_samples = 0.0
        self.dropped = 0
        self.message = _Signal(self, "message")
        self.sent = _Signal(self, "sent")
        self.level = _Signal(self, "level")
        self.failed = _Signal(self, "failed")

    def put(self, kind: str, value: object) -> None:
        try:
            self._events.put_nowait((kind, value))
        except queue.Full:
            self.dropped += 1

    # PlotBuffers surface -------------------------------------------------

    def set_samples(self, samples) -> None:
        now = time.monotonic()
        if now - self._last_samples < self._samples_interval:
            return
        self._last_samples = now
        self.put("samples", np.array(samples[:8192], dtype=np.complex64))

    def add_symbols(self, symbols, *, context) -> None:
        values = np.asarray(symbols, dtype=np.complex64)
        if values.size > _DISPLAY_SYMBOLS:
            values = values[:: -(-values.size // _DISPLAY_SYMBOLS)]
        self.put("symbols", (values, replace(context, payload_symbols=values)))


class _EngineProcess:
    """Parent-side lifecycle of one engine child and its event pump."""

    def __init__(self, target: Callable[..., None], arguments: tuple, dispatch) -> None:
        context = _safe_context()
        self._events = context.Queue(maxsize=_EVENT_DEPTH)
        self._control = context.Queue(maxsize=_CONTROL_DEPTH)
        self._replies = context.Queue()
        self._process = context.Process(
            target=target,
            args=(*arguments, self._events, self._control, self._replies),
            name="ofdm-message-engine",
            # Not daemonic: the receive engine owns its own decode worker
            # processes, which a daemonic process may not start.  atexit and
            # the child's parent-PID check stand in for daemon cleanup.
            daemon=False,
        )
        self._dispatch = dispatch
        self._pump: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()

    @property
    def pid(self) -> int | None:
        return self._process.pid

    def start(self) -> None:
        self._process.start()
        # Registered after multiprocessing's own exit handler, so it runs first
        # and the join that handler does on non-daemonic children cannot hang.
        atexit.register(self.stop)
        self._pump = threading.Thread(target=self._run_pump, name="engine-pump", daemon=True)
        self._pump.start()

    def _run_pump(self) -> None:
        while not self._stopping.is_set():
            try:
                kind, value = self._events.get(timeout=0.1)
            except queue.Empty:
                if not self._process.is_alive():
                    self._dispatch("failed", "radio engine process exited")
                    return
                continue
            except (EOFError, OSError):
                return
            self._dispatch(kind, value)

    def post(self, command: tuple) -> bool:
        try:
            self._control.put_nowait(command)
            return True
        except queue.Full:
            return False

    def request(self, command: tuple, timeout: float) -> tuple:
        """Send one lifecycle command and wait for the child's reply."""

        with self._lock:
            if not self._process.is_alive():
                raise TransportError("radio engine process is not running")
            self._control.put(command, timeout=timeout)
            deadline = time.monotonic() + timeout
            while True:
                try:
                    return self._replies.get(timeout=0.2)
                except queue.Empty:
                    if not self._process.is_alive():
                        raise TransportError("radio engine process exited") from None
                    if time.monotonic() >= deadline:
                        raise TransportError(
                            f"radio engine did not answer {command[0]!r}"
                        ) from None

    def stop(self) -> None:
        if self._process.pid is None or self._stopping.is_set():
            return
        if self._process.is_alive():
            try:
                self._control.put(("stop",), timeout=1.0)
            except (queue.Full, OSError, ValueError):
                pass
            self._process.join(_STOP_TIMEOUT_S)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(2.0)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(2.0)
        self._stopping.set()
        if self._pump is not None:
            self._pump.join(1.0)
            self._pump = None
        for channel in (self._events, self._control, self._replies):
            channel.close()
            channel.join_thread()


class _TransportView:
    """What the window reads from a transport that lives in the child."""

    def __init__(self, description: str) -> None:
        self.description = description
        self._snapshot: dict[str, object] = {}

    def snapshot(self) -> dict[str, object]:
        return dict(self._snapshot)


class _LagMonitor:
    """Report when this interpreter could not run a thread for a while.

    A sleeping thread that wakes late was waiting for the GIL (or the CPU);
    the sample-path Python blocks wait for exactly the same thing.  Spikes go
    to stderr with a timestamp so they can be lined up with UHD's own 'L',
    'U' and 'O' reports in the same log.
    """

    _PERIOD_S = 0.002
    _REPORT_S = 0.02

    def __init__(self) -> None:
        self.max_lag_s = 0.0
        self.spikes = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="engine-lag", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            time.sleep(self._PERIOD_S)
            lag = time.monotonic() - started - self._PERIOD_S
            if lag > self.max_lag_s:
                self.max_lag_s = lag
            if lag > self._REPORT_S:
                self.spikes += 1
                print(
                    f"[engine-lag] {lag * 1e3:.1f} ms at monotonic {time.monotonic():.3f}",
                    file=sys.stderr,
                    flush=True,
                )

    def stop(self) -> None:
        self._stop.set()


def _bound_numeric_threads() -> None:
    """One BLAS/OpenMP thread.

    Unbounded, NumPy's pool spun 19 threads at ~40% CPU each in the receive
    engine during a 330 burst/s video run, and the decode owner starved.
    """

    from ofdm_link.runtime.cpu_budget import apply_cpu_budget

    apply_cpu_budget(1)


class _NoLag:
    max_lag_s = 0.0
    spikes = 0

    def stop(self) -> None:
        pass


def _apply_gain_command(worker, gain_db: float, sink: _EventSink) -> None:
    """Child side of a live gain change; a failure is shown, never swallowed."""

    try:
        worker.set_gain(gain_db)
    except Exception as error:
        sink.failed.emit(f"gain change failed: {type(error).__name__}: {error}")


def _serve(
    worker,
    control,
    replies,
    tick: Callable[[], None],
    handle: Callable[[tuple], None] = lambda command: None,
    *,
    switch_interval_s: float,
) -> None:
    """Child main loop shared by both directions."""

    # The parent owns Ctrl-C and closes the window, which stops this process.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.setswitchinterval(switch_interval_s)
    # Diagnostic only: a thread waking every 2 ms preempts the CPU-bound
    # receive owner often enough to cost it throughput.
    lag = _LagMonitor() if os.environ.get(LAG_MONITOR_ENV) == "1" else _NoLag()
    worker.lag_monitor = lag
    parent = os.getppid()
    next_tick = 0.0
    while os.getppid() == parent:
        try:
            command = control.get(timeout=0.05)
        except queue.Empty:
            command = None
        if command is not None:
            kind = command[0]
            if kind == "stop":
                break
            if kind == "attach":
                try:
                    endpoint = command[1]()
                    worker.attach(endpoint)
                except BaseException as error:  # reported, never silent
                    replies.put(("error", f"{type(error).__name__}: {error}"))
                else:
                    replies.put(("ok", getattr(endpoint, "description", "")))
            elif kind == "detach":
                worker.detach()
                replies.put(("ok", ""))
            else:
                handle(command)
        now = time.monotonic()
        if now >= next_tick:
            next_tick = now + _STATS_INTERVAL_S
            tick()
    lag.stop()


# --------------------------------------------------------------------------
# Receive
# --------------------------------------------------------------------------


def _rx_main(
    profile, egress_address, ui_fps, decode_workers, events, control, replies
) -> None:
    _bound_numeric_threads()
    from .link import MessageReceiver
    from .rx_app import EgressForwarder, ReceiveWorker

    sink = _EventSink(events, samples_per_second=ui_fps)
    receiver = MessageReceiver(profile, decode_workers=decode_workers)
    egress = EgressForwarder(*egress_address)
    worker = ReceiveWorker(receiver, sink, sink, egress=egress)
    worker.start()

    def tick() -> None:
        source = worker.source
        sink.put(
            "stats",
            {
                "receiver": receiver.stats,
                "decoder": receiver.decoder_snapshot(),
                "transport": None if source is None else source.snapshot(),
                "egress_datagrams": egress.datagrams,
                "dropped_display_events": sink.dropped,
                "max_lag_s": worker.lag_monitor.max_lag_s,
                "lag_spikes": worker.lag_monitor.spikes,
                "decode_pool": receiver.decode_pool_snapshot(),
            },
        )

    def handle(command: tuple) -> None:
        if command[0] == "gain":
            _apply_gain_command(worker, command[1], sink)

    try:
        # The receive owner is CPU-bound; a short switch interval makes the
        # sink block, queue feeder and pool threads preempt it constantly
        # (OTA: 37,518 vs 111 queue back-pressure events at 240 burst/s).
        _serve(worker, control, replies, tick, handle, switch_interval_s=0.005)
    finally:
        worker.stop()
        egress.close()


class ProcessReceiveWorker:
    """The receive worker's surface, with the work done in a child process."""

    def __init__(
        self,
        profile,
        bridge,
        plot_buffers,
        *,
        egress_address: tuple[str, int],
        ui_fps: int = 15,
        decode_workers: int = 1,
    ) -> None:
        from .link import ReceiveStats

        self._bridge = bridge
        self._plot_buffers = plot_buffers
        self._stats = ReceiveStats()
        self._decoder = None
        self._egress_datagrams = 0
        self._dropped = 0
        self._source: _TransportView | None = None
        self._engine = _EngineProcess(
            _rx_main,
            (profile, egress_address, ui_fps, decode_workers),
            self._dispatch,
        )

    def _dispatch(self, kind: str, value) -> None:
        if kind == "message":
            self._bridge.message.emit(value)
        elif kind == "level":
            self._bridge.level.emit(value)
        elif kind == "failed":
            self._bridge.failed.emit(value)
        elif kind == "samples":
            self._plot_buffers.set_samples(value)
        elif kind == "symbols":
            symbols, context = value
            self._plot_buffers.add_symbols(symbols, context=context)
        elif kind == "stats":
            self._stats = value["receiver"]
            self._decoder = value["decoder"]
            self._egress_datagrams = value["egress_datagrams"]
            self._dropped = value["dropped_display_events"]
            source = self._source
            if source is not None and value["transport"] is not None:
                source._snapshot = {
                    **value["transport"],
                    "engine_pid": self._engine.pid,
                    "engine_dropped_display_events": self._dropped,
                    "engine_max_lag_ms": round(value["max_lag_s"] * 1e3, 1),
                    "engine_lag_spikes_over_20ms": value["lag_spikes"],
                    "decode_pool": value["decode_pool"],
                }

    @property
    def stats(self):
        return self._stats

    @property
    def source(self) -> _TransportView | None:
        return self._source

    @property
    def egress_datagrams(self) -> int:
        return self._egress_datagrams

    def decoder_snapshot(self):
        return self._decoder

    def set_gain(self, gain_db: float) -> None:
        """Ask the child to change the receive gain; failures come back as events."""

        if self._source is None:
            raise TransportError("the radio is not running")
        if not self._engine.post(("gain", float(gain_db))):
            raise TransportError("radio engine control queue is full")

    def start(self) -> None:
        self._engine.start()

    def attach_from(self, factory: Callable[[], Any]) -> str:
        status, detail = self._engine.request(("attach", factory), _ATTACH_TIMEOUT_S)
        if status != "ok":
            raise TransportError(detail)
        self._source = _TransportView(detail)
        return detail

    def detach(self) -> None:
        if self._source is None:
            return
        self._source = None
        self._engine.request(("detach",), _DETACH_TIMEOUT_S)

    def stop(self) -> None:
        self._source = None
        self._engine.stop()


# --------------------------------------------------------------------------
# Transmit
# --------------------------------------------------------------------------


def _tx_main(profile, mcs_entry, ingress_port, ui_fps, events, control, replies) -> None:
    _bound_numeric_threads()
    from .link import MessageTransmitter
    from .tx_app import IngressListener, TransmitWorker

    sink = _EventSink(events, samples_per_second=ui_fps)
    transmitter = MessageTransmitter(profile, mcs_entry=mcs_entry)
    worker = TransmitWorker(transmitter, sink, sink)
    ingress = IngressListener("127.0.0.1", ingress_port, worker.submit)

    def handle(command: tuple) -> None:
        if command[0] == "submit":
            if not worker.submit(command[1], command[2]):
                sink.failed.emit("send queue is full; the transport is not keeping up")
        elif command[0] == "mcs":
            try:
                transmitter.select_mcs_entry(command[1])
            except Exception as error:
                sink.failed.emit(f"MCS change failed: {error}")
        elif command[0] == "gain":
            _apply_gain_command(worker, command[1], sink)

    def tick() -> None:
        radio = worker.sink
        sink.put(
            "stats",
            {
                "pending": worker.pending,
                "active_mcs_entry": transmitter.active_mcs_entry,
                "ingress_datagrams": ingress.datagrams,
                "transport": None if radio is None else radio.snapshot(),
                "dropped_display_events": sink.dropped,
                "max_lag_s": worker.lag_monitor.max_lag_s,
                "lag_spikes": worker.lag_monitor.spikes,
            },
        )

    worker.start()
    try:
        ingress.start()
    except OSError as error:
        sink.failed.emit(f"cannot open UDP ingress port {ingress_port}: {error}")
    try:
        # The transmit source block must get the GIL promptly while a burst
        # is on air, and nothing here is CPU-bound for long.
        _serve(worker, control, replies, tick, handle, switch_interval_s=0.0005)
    finally:
        ingress.stop()
        worker.stop()


class ProcessTransmitWorker:
    """The transmit worker's surface, with the work done in a child process."""

    def __init__(
        self,
        profile,
        mcs_entry,
        bridge,
        plot_buffers,
        *,
        ingress_port: int,
        ui_fps: int = 15,
    ) -> None:
        self._bridge = bridge
        self._plot_buffers = plot_buffers
        self._pending = 0
        self._submitted = 0
        self._active_mcs_entry = mcs_entry
        self._ingress_datagrams = 0
        self._sink: _TransportView | None = None
        self._engine = _EngineProcess(
            _tx_main,
            (profile, mcs_entry, ingress_port, ui_fps),
            self._dispatch,
        )

    def _dispatch(self, kind: str, value) -> None:
        if kind == "sent":
            self._bridge.sent.emit(value)
        elif kind == "failed":
            self._bridge.failed.emit(value)
        elif kind == "samples":
            self._plot_buffers.set_samples(value)
        elif kind == "stats":
            self._pending = value["pending"]
            self._active_mcs_entry = value["active_mcs_entry"]
            self._ingress_datagrams = value["ingress_datagrams"]
            sink = self._sink
            if sink is not None and value["transport"] is not None:
                sink._snapshot = {
                    **value["transport"],
                    "engine_pid": self._engine.pid,
                    "engine_dropped_display_events": value["dropped_display_events"],
                    "engine_max_lag_ms": round(value["max_lag_s"] * 1e3, 1),
                    "engine_lag_spikes_over_20ms": value["lag_spikes"],
                }

    @property
    def pending(self) -> int:
        return self._pending

    @property
    def sink(self) -> _TransportView | None:
        return self._sink

    @property
    def active_mcs_entry(self):
        return self._active_mcs_entry

    @property
    def ingress_datagrams(self) -> int:
        return self._ingress_datagrams

    def select_mcs_entry(self, entry) -> None:
        if not self._engine.post(("mcs", entry)):
            raise TransportError("radio engine control queue is full")
        self._active_mcs_entry = entry

    def set_gain(self, gain_db: float) -> None:
        """Ask the child to change the transmit gain; failures come back as events."""

        if self._sink is None:
            raise TransportError("the radio is not running")
        if not self._engine.post(("gain", float(gain_db))):
            raise TransportError("radio engine control queue is full")

    def submit(self, source: str, payload: bytes) -> bool:
        return self._engine.post(("submit", source, payload))

    def start(self) -> None:
        self._engine.start()

    def attach_from(self, factory: Callable[[], Any]) -> str:
        status, detail = self._engine.request(("attach", factory), _ATTACH_TIMEOUT_S)
        if status != "ok":
            raise TransportError(detail)
        self._sink = _TransportView(detail)
        return detail

    def detach(self) -> None:
        if self._sink is None:
            return
        self._sink = None
        self._engine.request(("detach",), _DETACH_TIMEOUT_S)

    def stop(self) -> None:
        self._sink = None
        self._engine.stop()
