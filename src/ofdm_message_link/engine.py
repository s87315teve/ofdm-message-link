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

Events cross in batches.  Every queue put costs the child a wake-up of the
queue's feeder thread, which pickles and writes under the GIL the receive
owner needs: on a Jetson at about 280 small events/s that was 18.5 % of the
engine's GIL time.  So :class:`_EventSink` keeps one buffer, in put order,
for every thread of the child, and sends it as one item when a producer ends
its unit of work (a receive iteration, a transmit burst, a control command or
stats tick), when it reaches ``_MAX_BATCH`` events, before every reply to the
parent, and at least every ``_serve`` loop pass (50 ms).  The queue is bounded
in events, not batches (``_EVENT_DEPTH``), and a batch that does not fit is
dropped and counted as its events.  The parent's pump hands the events on one
by one, in order, so a parent-side timestamp is at most one flush later.
"""

from __future__ import annotations

import atexit
import multiprocessing.queues
import os
import queue
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from multiprocessing.reduction import ForkingPickler as _ForkingPickler
from typing import Any

import numpy as np

from ofdm_link.runtime import realtime
from ofdm_link.runtime.segment_decode_pool import _safe_context

from .transport import ReceiveFeed, TransportError

# Events (not batches) the engine->parent queue may hold.
_EVENT_DEPTH = 4096
# Events per queue item at most; a fuller buffer is sent at once.
_MAX_BATCH = 64
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


class _BatchQueue(multiprocessing.queues.Queue):
    """A multiprocessing queue whose feeder salvages a batch it cannot pickle.

    The stock feeder prints a traceback and loses the whole item.  Here the
    events of the batch that do pickle are still sent, in place, so one bad
    value costs only itself, and ``on_lost(count, kinds, error)`` hears of the
    rest so the event bound gets their room back.
    """

    def __init__(self, maxsize: int, *, ctx) -> None:
        super().__init__(maxsize, ctx=ctx)
        # A parent that is gone ends the feeder quietly (see _run_child).
        self._ignore_epipe = True
        self.on_lost: Callable[[int, list, BaseException], None] | None = None

    def __setstate__(self, state) -> None:
        super().__setstate__(state)
        self.on_lost = None

    def _on_queue_feeder_error(self, error, obj) -> None:
        # Runs on the feeder thread before it takes the next item, so a
        # salvaged batch keeps its place.  The feeder has already given the
        # item's slot back to the queue's semaphore.
        if not isinstance(obj, list):
            # The send itself failed (the parent is gone): nothing to save.
            lost, kinds = 0, []
        else:
            good, bad = [], []
            for event in obj:
                try:
                    _ForkingPickler.dumps(event)
                except Exception:
                    bad.append(event)
                else:
                    good.append(event)
            lost, kinds = len(bad), [event[0] for event in bad]
            if good:
                sent = False
                if self._sem.acquire(False):
                    try:
                        data = _ForkingPickler.dumps(good)
                        if self._wlock is None:
                            self._send_bytes(data)
                        else:
                            with self._wlock:
                                self._send_bytes(data)
                        sent = True
                    except Exception:
                        self._sem.release()
                if not sent:
                    lost, kinds = len(obj), [event[0] for event in obj]
        if self.on_lost is not None:
            try:
                self.on_lost(lost, kinds, error)
            except Exception:
                # Runs on the feeder thread: reporting a loss (e.g. a broken
                # stderr) must never kill it and silence the channel.
                pass


class _EventQueue:
    """The engine->parent channel: batches of ``(kind, value)``, bounded in events.

    Built by the parent and handed to the child at start.  No lock is shared
    between the two processes, so a parent pump that is descheduled or
    starved of its GIL, or a process that dies, can never make the other
    wait.  The child counts the events it has put (``_sent``, a plain int
    behind a lock of its own); the parent alone writes how many it has taken
    (``_taken``, a lock-free shared integer the child only reads); the child
    drops a batch that would put more than ``depth`` events in flight.  The
    queue's own item bound is the same number, which a batch of at least one
    event never reaches first.
    """

    def __init__(self, context, depth: int = _EVENT_DEPTH) -> None:
        self._queue = _BatchQueue(depth, ctx=context.get_context())
        self._taken = context.RawValue("q", 0)
        self._depth = depth
        self._reset()

    def _reset(self) -> None:
        self._sent = 0
        # Child-local: put_batch and the feeder thread's give-back.
        self._lock = threading.Lock()
        # Events lost because they could not be pickled (counted as dropped).
        self.lost = 0
        self._queue.on_lost = self._lost

    def __getstate__(self):
        return (self._queue, self._taken, self._depth)

    def __setstate__(self, state) -> None:
        self._queue, self._taken, self._depth = state
        self._reset()

    def put_batch(self, batch: list) -> bool:
        """Child side: queue the whole batch, or nothing (False) if it does not fit."""

        count = len(batch)
        with self._lock:
            if self._sent - self._taken.value + count > self._depth:
                return False
            self._sent += count
        try:
            self._queue.put_nowait(batch)
        except queue.Full:
            with self._lock:
                self._sent -= count
            return False
        return True

    def _lost(self, count: int, kinds: list, error: BaseException) -> None:
        with self._lock:
            # Never sent, so never taken: their room comes back.
            self._sent -= count
            self.lost += count
        names = ", ".join(sorted({str(kind) for kind in kinds}))
        try:
            print(f"[engine-events] dropped {count} event(s) that cannot be pickled "
                  f"({names}): {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def get(self, timeout: float) -> list:
        """Parent side: the next batch, oldest first; ``queue.Empty`` on timeout."""

        batch = self._queue.get(timeout=timeout)
        # Only this side writes it; the child only reads it.
        self._taken.value += len(batch)
        return batch

    def close(self) -> None:
        self._queue.close()

    def join_thread(self) -> None:
        self._queue.join_thread()


class _EventSink:
    """Child-side stand-in for the Qt bridge and the plot buffers.

    Every thread of the child puts here; one lock keeps a single buffer in
    put order, and :meth:`flush_events` sends it as one queue item.  Producers
    call it at the end of each unit of work, and ``_serve`` at least every
    loop pass, so nothing waits longer than that (see the module docstring).
    """

    def __init__(self, events: Any, *, samples_per_second: float,
                 max_batch: int = _MAX_BATCH) -> None:
        self._events = events
        self._samples_interval = 1.0 / samples_per_second
        self._last_samples = 0.0
        self._lock = threading.Lock()
        self._buffer: list[tuple[str, object]] = []
        self._max_batch = max_batch
        # Events (not batches) dropped because the parent fell behind.
        self._dropped = 0
        self.message = _Signal(self, "message")
        self.sent = _Signal(self, "sent")
        self.level = _Signal(self, "level")
        self.failed = _Signal(self, "failed")
        self.link_snr = _Signal(self, "link_snr")

    @property
    def dropped(self) -> int:
        """Events never delivered: no room in the queue, or not picklable."""

        return self._dropped + getattr(self._events, "lost", 0)

    def put(self, kind: str, value: object) -> None:
        with self._lock:
            self._buffer.append((kind, value))
            if len(self._buffer) >= self._max_batch:
                self._send_locked()

    def flush_events(self) -> None:
        """Send what is buffered now, as one batch (nothing if empty)."""

        # Unlocked fast path: a put racing past it is sent by its own
        # producer's flush, which always follows its put.
        if not self._buffer:
            return
        with self._lock:
            if self._buffer:
                self._send_locked()

    def _send_locked(self) -> None:
        # Under the lock, so batches leave in the order their events came.
        batch, self._buffer = self._buffer, []
        try:
            accepted = self._events.put_batch(batch)
        except Exception:
            self._dropped += len(batch)
            raise
        if not accepted:
            self._dropped += len(batch)

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


def _run_child(target: Callable[..., None], *arguments) -> None:
    """Keep only the child's end of each pipe, then run the engine.

    A pipe tells a reader its writer died (end of file), or a writer its
    reader died (EPIPE), only once no other process holds that end.  With
    both ends open on both sides, a process killed partway through a write
    left the other blocked for the rest of a message that would never come.
    :meth:`_EngineProcess.start` closes the parent's unused ends.
    """

    events, control, replies = arguments[-3:]
    events._queue._reader.close()
    control._writer.close()
    replies._reader.close()
    target(*arguments)


class _EngineProcess:
    """Parent-side lifecycle of one engine child and its event pump."""

    def __init__(self, target: Callable[..., None], arguments: tuple, dispatch) -> None:
        context = _safe_context()
        self._events = _EventQueue(context, _EVENT_DEPTH)
        self._control = context.Queue(maxsize=_CONTROL_DEPTH)
        self._replies = context.Queue()
        # A peer that is gone ends a feeder quietly, without a traceback.
        self._control._ignore_epipe = True
        self._replies._ignore_epipe = True
        self._process = context.Process(
            target=_run_child,
            args=(target, *arguments, self._events, self._control, self._replies),
            name="ofdm-message-engine",
            # Not daemonic: the receive engine owns its own decode worker
            # processes, which a daemonic process may not start.  atexit and
            # end of file on the control pipe stand in for daemon cleanup.
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
        # The child has its own copies now (see _run_child).
        self._events._queue._writer.close()
        self._control._reader.close()
        self._replies._writer.close()
        # Registered after multiprocessing's own exit handler, so it runs first
        # and the join that handler does on non-daemonic children cannot hang.
        atexit.register(self.stop)
        self._pump = threading.Thread(target=self._run_pump, name="engine-pump", daemon=True)
        self._pump.start()

    def _run_pump(self) -> None:
        while not self._stopping.is_set():
            try:
                batch = self._events.get(timeout=0.1)
            except queue.Empty:
                if not self._process.is_alive():
                    self._dispatch("failed", "radio engine process exited")
                    return
                continue
            except (EOFError, OSError):
                # No writer is left, possibly partway through a batch.
                if not self._stopping.is_set():
                    self._dispatch("failed", "radio engine process exited")
                return
            # One queue item is a batch; its events go on in their order.
            for kind, value in batch:
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
                except (EOFError, OSError):
                    raise TransportError("radio engine process exited") from None
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


def _bound_numeric_threads(cpu_cores: int) -> None:
    """Bound BLAS/OpenMP threads to ``cpu_cores``, and keep freed buffers for reuse.

    Unbounded, NumPy's pool spun 19 threads at ~40% CPU each in the receive
    engine during a 330 burst/s video run, and the decode owner starved.
    ``cpu_cores`` is the budget the app's ``main`` resolved from ``--cpu-cores``
    and ``acceleration.cpu_cores`` (1 by default), handed to the engine as an
    argument; a process with no app ``main`` passes 1.
    The allocator setting stops the owner faulting in fresh pages for every
    large temporary (see :mod:`ofdm_link.runtime.allocator`).
    """

    from ofdm_link.runtime.allocator import keep_freed_memory
    from ofdm_link.runtime.cpu_budget import apply_cpu_budget

    apply_cpu_budget(cpu_cores)
    keep_freed_memory()


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
    flush: Callable[[], None] = lambda: None,
) -> None:
    """Child main loop shared by both directions.

    ``flush`` sends the batched events (``_EventSink.flush_events``): before
    every reply, so the parent has what came before it, and after every pass,
    which is at most the 50 ms control poll plus a tick apart.
    """

    # The parent owns Ctrl-C and closes the window, which stops this process.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    sys.setswitchinterval(switch_interval_s)
    # Opt-in --realtime: every engine thread that exists now (the receive
    # owner, the transmit worker, ingress); radio threads after each attach.
    realtime.apply_requested()
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
        except (EOFError, OSError):
            # The parent is gone, possibly partway through a command.  The
            # parent-PID check cannot see this: under forkserver the PID is
            # the server's, which outlives the parent.
            break
        if command is not None:
            kind = command[0]
            if kind == "stop":
                break
            if kind == "attach":
                try:
                    endpoint = command[1]()
                    worker.attach(endpoint)
                except BaseException as error:  # reported, never silent
                    flush()
                    replies.put(("error", f"{type(error).__name__}: {error}"))
                else:
                    realtime.refresh()
                    flush()
                    replies.put(("ok", getattr(endpoint, "description", "")))
            elif kind == "detach":
                worker.detach()
                flush()
                replies.put(("ok", ""))
            else:
                handle(command)
        now = time.monotonic()
        if now >= next_tick:
            next_tick = now + _STATS_INTERVAL_S
            tick()
        flush()
    flush()
    lag.stop()


# --------------------------------------------------------------------------
# Receive
# --------------------------------------------------------------------------


def _rx_main(
    profile, egress_address, ui_fps, decode_workers, receive_feed, cpu_cores,
    events, control, replies,
) -> None:
    _bound_numeric_threads(cpu_cores)
    from .link import MessageReceiver
    from .rx_app import EgressForwarder, ReceiveWorker

    sink = _EventSink(events, samples_per_second=ui_fps)
    receiver = MessageReceiver(profile, decode_workers=decode_workers)
    egress = EgressForwarder(*egress_address)
    worker = ReceiveWorker(
        receiver,
        sink,
        sink,
        egress=egress,
        min_feed_samples=receive_feed.min_samples,
        max_merge_wait_s=receive_feed.max_wait_s,
    )
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
        _serve(worker, control, replies, tick, handle, switch_interval_s=0.005,
               flush=sink.flush_events)
    finally:
        try:
            worker.stop()
            egress.close()
        finally:
            # Even when stopping raised: the events before it still go out.
            sink.flush_events()


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
        cpu_cores: int = 1,
        receive_feed: ReceiveFeed | None = None,
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
            (
                profile,
                egress_address,
                ui_fps,
                decode_workers,
                ReceiveFeed() if receive_feed is None else receive_feed,
                cpu_cores,
            ),
            self._dispatch,
        )

    def _dispatch(self, kind: str, value) -> None:
        if kind == "message":
            self._bridge.message.emit(value)
        elif kind == "level":
            self._bridge.level.emit(value)
        elif kind == "failed":
            self._bridge.failed.emit(value)
        elif kind == "link_snr":
            self._bridge.link_snr.emit(value)
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


def _tx_main(
    profile, mcs_entry, ingress_port, ui_fps, aggregation, cpu_cores, events, control, replies
) -> None:
    _bound_numeric_threads(cpu_cores)
    from .link import MessageTransmitter
    from .tx_app import IngressListener, TransmitWorker

    sink = _EventSink(events, samples_per_second=ui_fps)
    transmitter = MessageTransmitter(profile, mcs_entry=mcs_entry)
    worker = TransmitWorker(transmitter, sink, sink, aggregation=aggregation)
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
                "ingress_dropped_queue_full": ingress.dropped_queue_full,
                "send_failures": worker.send_failures,
                "dropped_send_failed": worker.dropped_send_failed,
                "dropped_encode_failed": worker.dropped_encode_datagrams,
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
        _serve(worker, control, replies, tick, handle, switch_interval_s=0.0005,
               flush=sink.flush_events)
    finally:
        try:
            ingress.stop()
            worker.stop()
        finally:
            sink.flush_events()


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
        aggregation=None,
        cpu_cores: int = 1,
    ) -> None:
        self._bridge = bridge
        self._plot_buffers = plot_buffers
        self._pending = 0
        self._submitted = 0
        self._active_mcs_entry = mcs_entry
        self._ingress_datagrams = 0
        self._ingress_dropped = 0
        self.send_failures = 0
        self.dropped_send_failed = 0
        self.dropped_encode_datagrams = 0
        self._sink: _TransportView | None = None
        self._engine = _EngineProcess(
            _tx_main,
            (profile, mcs_entry, ingress_port, ui_fps, aggregation, cpu_cores),
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
            self._ingress_dropped = value.get("ingress_dropped_queue_full", 0)
            self.send_failures = value.get("send_failures", 0)
            self.dropped_send_failed = value.get("dropped_send_failed", 0)
            self.dropped_encode_datagrams = value.get("dropped_encode_failed", 0)
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

    @property
    def ingress_dropped_queue_full(self) -> int:
        return self._ingress_dropped

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
