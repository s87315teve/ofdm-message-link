"""Producer-side cost of engine->parent events: one queue put per event vs batched.

Not a test.  A child process plays the engine's receive thread and emits
receive-like events (``message``, ``symbols``, ``link_snr``), a few per
receive iteration, in one of three modes:

* ``none``: the same loop and events, never put (the baseline);
* ``old``:  ``put_nowait`` of every event on the multiprocessing queue, as
  ``engine._EventSink.put`` did up to v2.1.2;
* ``new``:  today's ``engine._EventSink``: buffered, one put per flush, one
  flush per iteration (as ``ReceiveWorker`` does).

Two scenarios:

* ``paced``: the thread sleeps between iterations to hold ``--rate`` events/s;
  the result is the child's CPU per event (``time.process_time``: every thread,
  including the queue's feeder thread) above the baseline.
* ``owner``: the thread is CPU-bound like the receive owner (pure-Python work
  of ``--work-ms`` per iteration, switch interval 5 ms as in the receive
  engine), and each message is followed by a UDP ``sendto`` like the egress,
  which drops the GIL; the result is the owner's work units per second (what
  the GIL left it) against the baseline.

The parent drains the queue like the engine pump.

    PYTHONPATH=src python tools/bench_event_batching.py [--scenario both] [--seconds 10]
"""

from __future__ import annotations

import argparse
import queue
import socket
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from ofdm_link.runtime.segment_decode_pool import _safe_context
from ofdm_message_link import engine


@dataclass(frozen=True)
class _Message:
    """About the size of a delivered message event (one 752-byte datagram)."""

    payload: bytes
    sequence: int
    snr_db: float
    evm_db: float
    extra: dict = field(default_factory=dict)


def _events(index: int, count: int):
    """One receive iteration's events: link SNR, then a symbols and a message per burst."""

    events = [("link_snr", tuple(20.0 + 0.001 * index for _ in range(max(1, count // 2))))]
    burst = 0
    while len(events) < count:
        events.append(("symbols", (np.zeros(512, np.complex64), {"burst": index, "n": burst})))
        if len(events) < count:
            events.append(("message", _Message(bytes(752), index, 20.0, -25.0, {"n": burst})))
        burst += 1
    return events


def _busy(seconds: float) -> int:
    """Pure-Python work for ``seconds`` of wall time; returns the work units done."""

    end = time.perf_counter() + seconds
    total = units = 0
    while time.perf_counter() < end:
        for value in range(200):
            total += value * value
        units += 1
    return units


def _producer(mode, scenario, rate, seconds, per_iteration, work_s, channel, result) -> None:
    if scenario == "owner":
        sys.setswitchinterval(0.005)
    sink = engine._EventSink(channel, samples_per_second=1.0)
    egress = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload = bytes(752)
    interval = per_iteration / rate
    started_cpu = time.process_time()
    started = time.monotonic()
    sent = iterations = work = 0
    while time.monotonic() - started < seconds:
        if scenario == "paced":
            delay = started + iterations * interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        else:
            work += _busy(work_s)
        for kind, value in _events(iterations, per_iteration):
            if mode == "old":
                try:
                    channel._queue.put_nowait((kind, value))
                except queue.Full:
                    pass
            elif mode == "new":
                sink.put(kind, value)
            if scenario == "owner" and kind == "message":
                egress.sendto(payload, ("127.0.0.1", 9))  # discard port; drops the GIL
            sent += 1
        if mode == "new":
            sink.flush_events()
        iterations += 1
    elapsed = time.monotonic() - started
    sink.flush_events()
    # Let the feeder thread write what is left before measuring is over.
    channel._queue.close()
    channel._queue.join_thread()
    cpu = time.process_time() - started_cpu
    egress.close()
    result.put((sent, work / elapsed, cpu, sink.dropped))


def run(mode, scenario, rate, seconds, per_iteration, work_s) -> dict:
    context = _safe_context()
    channel = engine._EventQueue(context, engine._EVENT_DEPTH)
    result = context.Queue()
    process = context.Process(
        target=_producer,
        args=(mode, scenario, rate, seconds, per_iteration, work_s, channel, result),
    )
    process.start()
    received = 0
    stop = threading.Event()

    def drain():
        nonlocal received
        while not stop.is_set():
            try:
                if mode == "old":
                    channel._queue.get(timeout=0.1)  # one (kind, value)
                    received += 1
                else:
                    received += len(channel.get(0.1))
            except queue.Empty:
                continue

    pump = threading.Thread(target=drain, daemon=True)
    pump.start()
    sent, work_per_s, cpu, dropped = result.get(timeout=seconds + 60)
    process.join(30)
    time.sleep(0.2)
    stop.set()
    pump.join(1)
    return {"events": sent, "received": received, "dropped": dropped,
            "work_per_s": work_per_s, "cpu_us_per_event": 1e6 * cpu / sent}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=("paced", "owner", "both"), default="both")
    parser.add_argument("--rate", type=float, default=280.0, help="paced: events per second")
    parser.add_argument("--work-ms", type=float, default=10.0,
                        help="owner: pure-Python work per iteration")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--per-iteration", type=int, default=4,
                        help="events per receive iteration (one flush each)")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    scenarios = ("paced", "owner") if args.scenario == "both" else (args.scenario,)
    for scenario in scenarios:
        print(f"[{scenario}] {args.per_iteration} events per iteration, {args.seconds:g} s, "
              f"{args.repeats} repeats, "
              + (f"{args.rate:g} events/s" if scenario == "paced"
                 else f"{args.work_ms:g} ms work per iteration"))
        results: dict[str, list[dict]] = {}
        for _ in range(args.repeats):
            # Interleaved, so drift in the machine's load hits every mode alike.
            for mode in ("none", "old", "new"):
                results.setdefault(mode, []).append(run(
                    mode, scenario, args.rate, args.seconds, args.per_iteration,
                    args.work_ms / 1e3))
        base_cpu = statistics.median(row["cpu_us_per_event"] for row in results["none"])
        print(f"  none: {base_cpu:.1f} us CPU/event")
        base_rate = statistics.median(row["work_per_s"] for row in results["none"]) or 1.0
        for mode in ("old", "new"):
            rows = results[mode]
            cpu = [row["cpu_us_per_event"] - base_cpu for row in rows]
            work = ""
            if scenario == "owner":
                rate = statistics.median(row["work_per_s"] for row in rows)
                work = f"owner work {100 * (rate / base_rate - 1):+.2f} % vs none; "
            print(f"  {mode}: {statistics.median(cpu):+.1f} us CPU/event "
                  f"(runs {', '.join(f'{value:+.1f}' for value in cpu)}); {work}"
                  f"received {rows[-1]['received']}/{rows[-1]['events']}, "
                  f"dropped {rows[-1]['dropped']}")


if __name__ == "__main__":
    main()
