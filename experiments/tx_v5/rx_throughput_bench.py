"""Offline receive-throughput bench: the rx_app receive path, fed from memory.

Replays a synthetic sample stream through exactly the objects the receive
engine child runs -- :class:`~ofdm_message_link.rx_app.ReceiveWorker` on its
own thread, :class:`~ofdm_message_link.link.MessageReceiver` with a
``SegmentDecodePool`` of ``--workers`` processes, and the engine's display
event sink feeding a real multiprocessing queue drained by another process --
and reports how fast that path keeps up and where its owner thread spends
its time.

The stream: ``--rate`` bursts per second at ``--fs`` samples per second, one
datagram of ``--payload`` bytes per burst, encoded either as wire v5
(``--family v5``, the app's own ``MessageTransmitter``) or as a legacy burst
(``--family legacy``, ``encode_burst`` with the MCS-table entry's codecs, as
the improve-rx transmitter did), in complex white noise at ``--snr-db``
(burst mean power over noise variance, or ``--noise-power``) with a 10 kHz
CFO, optionally a DC offset (``--dc``), a CW tone (``--tone``) and Pluto-like
extra guard-band noise (``--guard-noise-db``); ``--rate 0`` is noise and
interference alone.  The samples reach
the worker in ``--chunk``-sample chunks (the Pluto source's 65536) through a
16-deep queue fed by a reader thread, as ``PlutoSampleSource`` does.

Two modes:

``fast``   the reader thread feeds the queue as fast as the worker drains it;
           the real-time factor is stream duration over wall time.
``paced``  the reader delivers chunks on the sample clock and drops a chunk
           (counted as an overflow) when the queue is full, as the device
           does; reports overflows and the owner thread's CPU share.

Per-stage times are inclusive ``perf_counter`` sums over calls on the owner
thread (nested stages overlap).  ``--profile`` also runs cProfile on the
owner thread and prints the top functions.  Runs from either tree, so the
same stream can be timed on an older receiver::

    PYTHONPATH=src python experiments/tx_v5/rx_throughput_bench.py \\
        --family v5 --mcs 16 --payload 900 --rate 500 --seconds 3 --workers 4

Set OMP_NUM_THREADS=OPENBLAS_NUM_THREADS=MKL_NUM_THREADS=1 (the engine child
runs with one numeric thread).
"""

from __future__ import annotations

import argparse
import cProfile
import functools
import io
import json
import multiprocessing
import os
import pstats
import queue
import sys
import threading
import time
from collections import defaultdict

for _name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_name, "1")

import numpy as np  # noqa: E402

QUEUE_DEPTH = 16  # PlutoSampleSource default
BURST_POWER = 0.01  # -20 dBFS mean burst power
CFO_HZ = 10e3


# --------------------------------------------------------------------------
# Stream
# --------------------------------------------------------------------------


def _profile(overlay: str | None):
    from ofdm_message_link.link import build_phy_profile, load_link_config
    from ofdm_message_link.options import DEFAULT_CONFIG, DEFAULT_OVERLAY

    config = load_link_config(DEFAULT_CONFIG, [overlay or DEFAULT_OVERLAY])
    return config, build_phy_profile(config)


def _v5_bursts(profile, mcs: int, payload: int, count: int, rng) -> list[np.ndarray]:
    from ofdm_link.phy.mcs_table import mcs_entry
    from ofdm_message_link.link import MessageTransmitter

    transmitter = MessageTransmitter(profile, mcs_entry=mcs_entry(mcs))
    bursts = []
    for _ in range(count):
        message = rng.integers(0, 256, payload, dtype=np.uint8).tobytes()
        (burst,) = transmitter.encode_message(message)
        bursts.append(np.asarray(burst.samples))
    return bursts


def _legacy_bursts(config, profile, mcs: int, payload: int, count: int, rng) -> list[np.ndarray]:
    from ofdm_link.phy import Frame, FrameKind, encode_burst
    from ofdm_link.phy.codec import CURRENT_PROTOCOL_VERSION
    from ofdm_link.phy.mcs_table import mcs_entry
    from ofdm_link.runtime.factory import build_burst_config, select_frame_decoder_for_entry
    from ofdm_message_link import datagram

    entry = mcs_entry(mcs)
    selection = select_frame_decoder_for_entry(config, entry)
    burst_config = build_burst_config(config)
    origin = datagram.local_origin()
    bursts = []
    for index in range(count):
        message = rng.integers(0, 256, payload, dtype=np.uint8).tobytes()
        (piece,) = datagram.fragment(
            message,
            message_id=index + 1,
            max_payload_bytes=profile.max_message_payload_bytes,
            origin=origin,
        )
        frame = Frame(
            protocol_version=CURRENT_PROTOCOL_VERSION,
            kind=FrameKind.DATA,
            mcs=entry.modulation,
            sequence=index % 65536,
            payload=piece.encode(),
        )
        encoded = encode_burst(
            frame,
            burst_config,
            symbol_mapper=selection.symbol_mapper,
            wire_version=selection.wire_version,
            frame_encoder=selection.frame_encoder,
        )
        bursts.append(np.asarray(encoded.samples))
    return bursts


def build_stream(args, config, profile) -> tuple[np.ndarray, int, int]:
    """Return (samples, burst count, longest burst in samples)."""

    rng = np.random.default_rng(args.seed)
    count = int(round(args.rate * args.seconds))
    period = args.fs / args.rate if args.rate > 0 else 0.0
    if count == 0:
        bursts = []
    elif args.family == "v5":
        bursts = _v5_bursts(profile, args.mcs, args.payload, count, rng)
    else:
        bursts = _legacy_bursts(config, profile, args.mcs, args.payload, count, rng)
    longest = max((burst.size for burst in bursts), default=0)
    if bursts and longest + 200 > period:
        raise SystemExit(
            f"a {longest}-sample burst does not fit a {period:.0f}-sample period "
            f"at {args.rate}/s"
        )
    total = (int(round(count * period)) if count else int(round(args.seconds * args.fs)))
    total += args.chunk
    stream = np.zeros(total, dtype=np.complex64)
    for index, burst in enumerate(bursts):
        scale = np.sqrt(BURST_POWER / np.mean(np.abs(burst) ** 2))
        start = int(round(index * period)) + int(rng.integers(0, 64))
        stream[start : start + burst.size] = burst * scale
    stream *= np.exp(2j * np.pi * CFO_HZ / args.fs * np.arange(total)).astype(np.complex64)
    variance = noise_power(args)
    noise = rng.standard_normal((total, 2), dtype=np.float32).view(np.complex64)[:, 0]
    if args.guard_noise_db:
        # Pluto-like: the guard band (outside the 52 used carriers) sits
        # this many dB above the in-band noise.  High-pass the extra part.
        index = np.arange(31) - 15
        lowpass = 2 * 0.44 * np.sinc(2 * 0.44 * index) * np.hamming(31)
        lowpass /= lowpass.sum()
        extra = rng.standard_normal((total, 2), dtype=np.float32).view(np.complex64)[:, 0]
        highpass = extra - np.convolve(extra, lowpass, mode="same").astype(np.complex64)
        gain = np.sqrt(10 ** (args.guard_noise_db / 10) - 1)
        noise = noise + np.float32(gain) * highpass
    stream += np.float32(np.sqrt(variance / 2)) * noise
    if args.dc:
        stream += np.complex64(args.dc * np.exp(0.7j))
    if args.tone:
        phase = 2 * np.pi * args.tone_hz / args.fs * np.arange(total)
        stream += (args.tone * np.exp(1j * phase)).astype(np.complex64)
    return stream, count, longest


def noise_power(args) -> float:
    """Complex white-noise variance: ``--noise-power``, else from ``--snr-db``."""

    if args.noise_power is not None:
        return args.noise_power
    return BURST_POWER / 10 ** (args.snr_db / 10)


# --------------------------------------------------------------------------
# Instrumentation
# --------------------------------------------------------------------------


class StageTimers:
    """Inclusive perf_counter sums for wrapped callables (owner thread only)."""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = defaultdict(float)
        self.calls: dict[str, int] = defaultdict(int)
        self._restore: list[tuple[object, str, object]] = []

    def wrap(self, owner, name: str, label: str) -> None:
        original = owner.__dict__.get(name) if isinstance(owner, type) else getattr(
            owner, name, None
        )
        if original is None:
            return
        seconds, calls = self.seconds, self.calls

        @functools.wraps(original)
        def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                seconds[label] += time.perf_counter() - started
                calls[label] += 1

        setattr(owner, name, timed)
        self._restore.append((owner, name, original))

    def restore(self) -> None:
        for owner, name, original in reversed(self._restore):
            setattr(owner, name, original)


def instrument(timers: StageTimers) -> None:
    import ofdm_link.phy.streaming as streaming
    import ofdm_link.phy.sync as sync
    from ofdm_message_link import link

    try:
        import ofdm_link.phy.sync_v5 as sync_v5
    except ImportError:  # the improve-rx tree has no v5
        sync_v5 = None
    try:
        from ofdm_link.runtime import segment_decode_pool as pool
    except ImportError:
        pool = None

    timers.wrap(link.MessageReceiver, "feed", "receiver.feed")
    timers.wrap(link.MessageReceiver, "_accept_results", "receiver.accept_results")
    timers.wrap(link.MessageReceiver, "_accept", "receiver.accept")
    decoder = streaming.StreamingBurstDecoder
    timers.wrap(decoder, "feed_segments", "decoder.feed_segments")
    timers.wrap(decoder, "feed", "decoder.feed")
    timers.wrap(decoder, "_acquire_next", "decoder.acquire_next")
    timers.wrap(decoder, "_note_acquisition", "decoder.note_acquisition (SNR)")
    timers.wrap(decoder, "_retain_possible_preamble_prefix", "decoder.retain_prefix")
    timers.wrap(decoder, "_discard", "decoder.discard")
    timers.wrap(streaming, "acquire_all", "acquire_all")
    timers.wrap(streaming, "_decode_burst_with_acquisition", "header probe / inline decode")
    timers.wrap(sync, "_shared_correlation", "  shared_correlation")
    timers.wrap(sync, "_acquisition_candidates", "  legacy candidates")
    if sync_v5 is not None:
        # acquire_all calls the unchecked core where the tree has one.
        name = "_v5_candidates" if hasattr(sync_v5, "_v5_candidates") else "v5_candidates"
        timers.wrap(sync_v5, name, "  v5_candidates")
    if pool is not None:
        timers.wrap(pool.SegmentDecodePool, "submit", "pool.submit")
        timers.wrap(pool.SegmentDecodePool, "collect", "pool.collect")


def thread_cpu_seconds(thread: threading.Thread) -> float:
    return time.clock_gettime(time.pthread_getcpuclockid(thread.ident))


# --------------------------------------------------------------------------
# Source and sink
# --------------------------------------------------------------------------


class MemorySource:
    """PlutoSampleSource stand-in: a reader thread and a 16-deep queue."""

    description = "memory"

    def __init__(self, stream: np.ndarray, chunk: int, *, paced: bool, fs: float) -> None:
        self._chunks = [stream[start : start + chunk] for start in range(0, stream.size, chunk)]
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_DEPTH)
        self._paced = paced
        self._period = chunk / fs
        self.overflows = 0
        self.delivered = 0
        self.taken = 0
        self.finished = threading.Event()
        self.started_at: float | None = None
        self._thread = threading.Thread(target=self._run, name="memory-source", daemon=True)

    @property
    def total(self) -> int:
        return len(self._chunks)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        self.started_at = time.monotonic()
        for index, chunk in enumerate(self._chunks):
            if self._paced:
                due = self.started_at + (index + 1) * self._period
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                try:
                    self._queue.put_nowait(chunk)
                    self.delivered += 1
                except queue.Full:
                    self.overflows += 1
            else:
                self._queue.put(chunk)
                self.delivered += 1
        self.finished.set()

    def recv(self, timeout: float):
        try:
            chunk = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        self.taken += 1
        return chunk

    def idle(self) -> bool:
        return self.finished.is_set() and self._queue.empty()

    def stop(self) -> None:
        pass

    def snapshot(self) -> dict:
        return {"overflows": self.overflows}


def _drain_events(events, counts) -> None:
    """Parent-process stand-in: take display events until the sentinel."""

    while True:
        item = events.get()
        if item is None:
            return
        counts[item[0]] = counts.get(item[0], 0) + 1


class NullSink:
    """Bridge and plot-buffer stand-in when the engine sink is not wanted."""

    class _Signal:
        def emit(self, value) -> None:
            pass

    def __init__(self) -> None:
        self.dropped = 0
        self.message = self.level = self.failed = self.link_snr = NullSink._Signal()

    def set_samples(self, samples) -> None:
        pass

    def add_symbols(self, symbols, *, context) -> None:
        pass


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


def run(args) -> dict:
    from ofdm_message_link.engine import _bound_numeric_threads
    from ofdm_message_link.link import MessageReceiver
    from ofdm_message_link.rx_app import ReceiveWorker

    # What the receive engine child does before it starts the worker.
    _bound_numeric_threads()
    sys.setswitchinterval(args.switch_interval)
    config, profile = _profile(args.overlay)
    built = time.perf_counter()
    stream, bursts, longest = build_stream(args, config, profile)
    build_s = time.perf_counter() - built

    if args.decoder_chunk:
        from ofdm_message_link import link

        link._DECODER_CHUNK_SAMPLES = args.decoder_chunk
    receiver = MessageReceiver(profile, decode_workers=args.workers)
    drain = None
    if args.sink == "engine":
        from ofdm_link.runtime.segment_decode_pool import _safe_context
        from ofdm_message_link.engine import _EVENT_DEPTH, _EventSink

        context = _safe_context()
        events = context.Queue(maxsize=_EVENT_DEPTH)
        manager_counts = context.Manager().dict()
        drain = context.Process(target=_drain_events, args=(events, manager_counts))
        drain.start()
        sink = _EventSink(events, samples_per_second=args.ui_fps)
    else:
        sink = NullSink()

    timers = StageTimers()
    instrument(timers)
    worker = ReceiveWorker(receiver, sink, sink)
    profiler = cProfile.Profile() if args.profile else None
    if profiler is not None:
        original_run = worker._run

        def profiled_run() -> None:
            profiler.enable()
            try:
                original_run()
            finally:
                profiler.disable()

        worker._run = profiled_run

    # The engine child's stats tick, every 0.25 s on another thread.
    ticking = threading.Event()

    def tick() -> None:
        while not ticking.wait(0.25):
            sink_put = getattr(sink, "put", None)
            payload = {
                "receiver": receiver.stats,
                "decoder": receiver.decoder_snapshot(),
                "decode_pool": receiver.decode_pool_snapshot(),
            }
            if sink_put is not None:
                sink_put("stats", payload)

    source = MemorySource(stream, args.chunk, paced=args.mode == "paced", fs=args.fs)
    duration_s = stream.size / args.fs
    worker.start()
    ticker = threading.Thread(target=tick, daemon=True)
    ticker.start()
    process_cpu_0 = time.process_time()
    pool_cpu_0 = receiver.decode_worker_cpu_seconds() or 0.0
    wall_0 = time.perf_counter()
    worker.attach(source)
    owner = worker._thread
    owner_cpu_0 = thread_cpu_seconds(owner)
    while not (source.idle() and source.taken == source.delivered):
        time.sleep(0.005)
    # The last chunk has been taken; wait for the worker to finish feeding it.
    feeds_seen = -1
    while True:
        time.sleep(0.02)
        done = timers.calls.get("receiver.feed", 0)
        if done == feeds_seen:
            break
        feeds_seen = done
    wall_s = time.perf_counter() - wall_0 - 0.02
    owner_cpu_s = thread_cpu_seconds(owner) - owner_cpu_0
    worker._stop.set()
    owner.join(2.0)
    flush_0 = time.perf_counter()
    receiver.flush(30.0)
    flush_s = time.perf_counter() - flush_0
    ticking.set()
    pool_cpu_s = (receiver.decode_worker_cpu_seconds() or 0.0) - pool_cpu_0
    process_cpu_s = time.process_time() - process_cpu_0
    stats = receiver.stats
    snapshot = receiver.decoder_snapshot()
    pool_snapshot = receiver.decode_pool_snapshot()
    receiver.close()
    timers.restore()
    event_counts = {}
    if drain is not None:
        events.put(None)
        drain.join(10.0)
        event_counts = dict(manager_counts)

    result = {
        "family": args.family,
        "mcs": args.mcs,
        "payload_bytes": args.payload,
        "rate_bursts_per_s": args.rate,
        "mode": args.mode,
        "workers": args.workers,
        "chunk": args.chunk,
        "snr_db": args.snr_db,
        "noise_power": noise_power(args),
        "dc": args.dc,
        "tone": args.tone,
        "tone_hz": args.tone_hz,
        "guard_noise_db": args.guard_noise_db,
        "bursts_sent": bursts,
        "burst_samples_max": longest,
        "stream_s": round(duration_s, 3),
        "build_s": round(build_s, 2),
        "wall_s": round(wall_s, 3),
        "realtime_factor": round(duration_s / max(wall_s, 1e-6), 3),
        "owner_thread_cpu_s": round(owner_cpu_s, 3),
        "owner_cpu_share_of_stream": round(owner_cpu_s / duration_s, 3),
        "owner_cpu_share_of_wall": round(owner_cpu_s / wall_s, 3),
        "process_cpu_s": round(process_cpu_s, 3),
        "pool_workers_cpu_s": round(pool_cpu_s, 3),
        "flush_s": round(flush_s, 3),
        "overflows": source.overflows,
        "chunks": source.total,
        "frames_decoded": stats.bursts_decoded,
        "messages_delivered": stats.messages_delivered,
        "missing": stats.missing_bursts,
        "decoder": {
            name: getattr(snapshot, name)
            for name in getattr(type(snapshot), "__slots__", ())
        },
        "decode_pool": None
        if pool_snapshot is None
        else {name: getattr(pool_snapshot, name) for name in type(pool_snapshot).__slots__},
        "events": event_counts,
        "stages_s": {
            label: {
                "s": round(seconds, 4),
                "per_stream_s": round(seconds / duration_s, 4),
                "calls": timers.calls[label],
            }
            for label, seconds in sorted(timers.seconds.items(), key=lambda kv: -kv[1])
        },
        "loadavg": open("/proc/loadavg").read().split()[:3],
        "tree": os.getcwd(),
    }
    if profiler is not None:
        text = io.StringIO()
        stats_view = pstats.Stats(profiler, stream=text).strip_dirs().sort_stats("tottime")
        stats_view.print_stats(args.profile_lines)
        result["profile_tottime"] = text.getvalue()
        text = io.StringIO()
        pstats.Stats(profiler, stream=text).strip_dirs().sort_stats("cumulative").print_stats(
            args.profile_lines
        )
        result["profile_cumulative"] = text.getvalue()
    return result


def _summary(result: dict) -> str:
    lines = [
        f"{result['family']} MCS {result['mcs']} {result['payload_bytes']} B at "
        f"{result['rate_bursts_per_s']}/s, {result['mode']}, {result['workers']} workers, "
        f"noise {result['noise_power']:.1e} dc {result['dc']} tone {result['tone']} "
        f"guard +{result['guard_noise_db']} dB, load {' '.join(result['loadavg'])}",
        f"  stream {result['stream_s']} s  wall {result['wall_s']} s  "
        f"RTF {result['realtime_factor']}  overflows {result['overflows']}/{result['chunks']}",
        f"  owner thread CPU {result['owner_thread_cpu_s']} s "
        f"({100 * result['owner_cpu_share_of_stream']:.0f}% of a core at real time, "
        f"{100 * result['owner_cpu_share_of_wall']:.0f}% of wall)  "
        f"process CPU {result['process_cpu_s']} s  pool CPU {result['pool_workers_cpu_s']} s",
        f"  frames {result['frames_decoded']}/{result['bursts_sent']}  "
        f"missing {result['missing']}  delivered {result['messages_delivered']}",
        "  stages (inclusive, s per stream second):",
    ]
    for label, value in result["stages_s"].items():
        lines.append(
            f"    {label:34s} {value['s']:8.3f} s  {100 * value['per_stream_s']:6.1f}%"
            f"  {value['calls']:7d} calls"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--family", choices=("v5", "legacy"), default="v5")
    parser.add_argument("--mcs", type=int, default=16, help="MCS-table index")
    parser.add_argument("--payload", type=int, default=900, help="datagram payload bytes")
    parser.add_argument("--rate", type=float, default=500.0, help="bursts per second")
    parser.add_argument("--seconds", type=float, default=3.0, help="stream length")
    parser.add_argument("--snr-db", type=float, default=20.0, help="burst power over noise")
    parser.add_argument(
        "--noise-power",
        type=float,
        default=None,
        help="complex noise variance (full scale 1); overrides --snr-db",
    )
    parser.add_argument("--dc", type=float, default=0.0, help="DC offset amplitude (LO leakage)")
    parser.add_argument("--tone", type=float, default=0.0, help="CW tone amplitude")
    parser.add_argument("--tone-hz", type=float, default=123e3, help="CW tone frequency")
    parser.add_argument(
        "--guard-noise-db",
        type=float,
        default=0.0,
        help="extra noise outside the used band, dB above in-band (Pluto: ~10)",
    )
    parser.add_argument("--fs", type=float, default=5e6, help="sample rate")
    parser.add_argument("--chunk", type=int, default=65536, help="samples per source chunk")
    parser.add_argument("--workers", type=int, default=4, help="decode workers")
    parser.add_argument(
        "--decoder-chunk",
        type=int,
        default=0,
        help="override MessageReceiver's decoder piece size (experiment; 0 = the tree's)",
    )
    parser.add_argument("--mode", choices=("fast", "paced"), default="fast")
    parser.add_argument("--sink", choices=("engine", "null"), default="engine")
    parser.add_argument("--ui-fps", type=int, default=15)
    parser.add_argument(
        "--switch-interval", type=float, default=0.005, help="GIL switch interval (engine: 5 ms)"
    )
    parser.add_argument("--overlay", default=None, help="config overlay (default: rx_app's)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--profile", action="store_true", help="cProfile the owner thread")
    parser.add_argument("--profile-lines", type=int, default=35)
    parser.add_argument("--json", default=None, help="append the result as one JSON line")
    args = parser.parse_args(argv)

    result = run(args)
    print(_summary(result))
    if args.profile:
        print(result["profile_tottime"])
        print(result["profile_cumulative"])
    if args.json:
        with open(args.json, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    multiprocessing.set_start_method("forkserver", force=True)
    sys.exit(main())
