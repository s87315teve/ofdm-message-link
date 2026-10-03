"""Decode header-bounded burst segments on worker processes, in stream order.

A continuous receiver has two kinds of work.  Acquisition, the fixed-header
probe and the burst boundary carry state from one burst to the next, so they
stay on one owner (:meth:`StreamingBurstDecoder.feed_segments`).  Everything
after the header -- equalization, demapping, FEC, CRC and any per-burst
measurement -- depends only on the segment, so it can run anywhere.

Processes, not threads: the native Turbo core already releases the GIL, and
the NumPy glue around it does not, so threads stop scaling below 2x.  Workers
are started with ``forkserver`` so this multi-threaded process is never forked.

Order is the stream's.  Results are handed back strictly in submission order,
however the workers finish, so the caller sees exactly the sequence a single
inline decoder would have produced.  In-flight work is bounded: when it is
full, :meth:`SegmentDecodePool.submit` waits for the oldest result and counts
the wait, which pushes back on the sample source instead of growing a queue.
"""

from __future__ import annotations

import multiprocessing
import os
from collections import deque
from collections.abc import Callable
from concurrent.futures import BrokenExecutor, Future, ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any

from ofdm_link.config import LinkConfig
from ofdm_link.phy.burst import BurstDecodeError, DecodedBurst
from ofdm_link.phy.streaming import BurstSegment, PayloadFailure, decode_burst_segment

# Never fork this process. By the time a pool is built the parent has loaded
# NumPy and its BLAS, and may have loaded GNU Radio or CUDA, so it is
# multi-threaded; forking it copies threads' locks in whatever state they were
# in and can deadlock the child. "forkserver" forks from a small server that is
# itself started by exec, so no multi-threaded fork happens, and it still pays
# the imports once instead of once per worker.
_START_METHODS = ("forkserver", "spawn")


class DecodePoolError(RuntimeError):
    """Raised when the decode pool cannot produce trustworthy results."""


def _safe_context() -> multiprocessing.context.BaseContext:
    """Return a start method that never forks this multi-threaded process."""

    available = multiprocessing.get_all_start_methods()
    for method in _START_METHODS:
        if method in available:
            return multiprocessing.get_context(method)
    raise DecodePoolError(
        "no multiprocessing start method available that avoids forking"
    )


_MAX_WORKERS = 64
_READY_TIMEOUT_S = 120.0

Annotate = Callable[[DecodedBurst], Any]


@dataclass(frozen=True, slots=True)
class SegmentDecodeResult:
    """One segment's outcome: a burst and its annotation, or a failure."""

    burst: DecodedBurst | None
    failure: PayloadFailure | None
    annotation: Any = None


@dataclass(frozen=True, slots=True)
class SegmentDecodePoolSnapshot:
    workers: int
    max_in_flight: int
    in_flight: int
    submitted: int
    completed: int
    backpressure_waits: int


_WORKER: dict[str, Any] = {}


def _initialize_worker(config: LinkConfig, annotate: Annotate | None) -> None:
    from .cpu_budget import apply_cpu_budget
    from .factory import build_burst_config, select_frame_decoder

    # One numeric thread per worker; the parallelism is the worker count.
    apply_cpu_budget(1)
    selection = select_frame_decoder(config)
    _WORKER["burst_config"] = build_burst_config(config)
    _WORKER["frame_decoder"] = selection.frame_decoder
    _WORKER["symbol_demapper"] = selection.symbol_demapper
    _WORKER["annotate"] = annotate


def _worker_is_ready() -> bool:
    return "burst_config" in _WORKER


def decode_segment(
    segment: BurstSegment,
    burst_config,
    *,
    frame_decoder,
    symbol_demapper,
    annotate: Annotate | None = None,
) -> SegmentDecodeResult:
    """Decode one segment and annotate it; the same code runs inline and in workers."""

    try:
        burst = decode_burst_segment(
            segment,
            burst_config,
            frame_decoder=frame_decoder,
            symbol_demapper=symbol_demapper,
        )
    except BurstDecodeError as error:
        return SegmentDecodeResult(burst=None, failure=PayloadFailure.from_error(error))
    return SegmentDecodeResult(
        burst=burst,
        failure=None,
        annotation=None if annotate is None else annotate(burst),
    )


def _run_in_worker(segment: BurstSegment) -> SegmentDecodeResult:
    if not _worker_is_ready():
        raise DecodePoolError("segment decode worker was not initialized")
    return decode_segment(
        segment,
        _WORKER["burst_config"],
        frame_decoder=_WORKER["frame_decoder"],
        symbol_demapper=_WORKER["symbol_demapper"],
        annotate=_WORKER["annotate"],
    )


class SegmentDecodePool:
    """A bounded, order-preserving process pool for burst payload decoding."""

    def __init__(
        self,
        config: LinkConfig,
        *,
        workers: int,
        max_in_flight: int | None = None,
        annotate: Annotate | None = None,
    ) -> None:
        if not isinstance(config, LinkConfig):
            raise TypeError("config must be a LinkConfig")
        if type(workers) is not int or not 2 <= workers <= _MAX_WORKERS:
            raise ValueError(f"workers must be an integer in [2, {_MAX_WORKERS}]")
        limit = 4 * workers if max_in_flight is None else max_in_flight
        if type(limit) is not int or limit < workers:
            raise ValueError("max_in_flight must be an integer of at least workers")
        if annotate is not None and not callable(annotate):
            raise TypeError("annotate must be a module-level callable or None")
        self._workers = workers
        self._max_in_flight = limit
        self._pending: deque[Future[SegmentDecodeResult]] = deque()
        self._submitted = 0
        self._completed = 0
        self._backpressure_waits = 0
        self._closed = False
        self._executor = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=_safe_context(),
            initializer=_initialize_worker,
            initargs=(config, annotate),
        )
        self._start_workers()

    def _start_workers(self) -> None:
        try:
            futures = [self._executor.submit(_worker_is_ready) for _ in range(self._workers)]
            ready = [future.result(timeout=_READY_TIMEOUT_S) for future in futures]
        except Exception as error:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._closed = True
            raise DecodePoolError(
                "segment decode workers could not start; the caller's __main__ "
                'must be import-safe (if __name__ == "__main__"), or use 1 worker'
            ) from error
        if not all(ready):
            self.close()
            raise DecodePoolError("a segment decode worker did not finish initializing")

    @property
    def workers(self) -> int:
        return self._workers

    def submit(self, segment: BurstSegment) -> tuple[SegmentDecodeResult, ...]:
        """Queue one segment; return any results that are ready, in order.

        When the in-flight bound is reached this waits for the oldest result
        first, so a caller that submits faster than the workers decode is
        slowed down rather than allowed to queue without limit.
        """

        if self._closed:
            raise DecodePoolError("segment decode pool is closed")
        if not isinstance(segment, BurstSegment):
            raise TypeError("segment must be a BurstSegment")
        ready: list[SegmentDecodeResult] = []
        while len(self._pending) >= self._max_in_flight:
            self._backpressure_waits += 1
            ready.append(self._take_oldest(timeout=None))
        try:
            self._pending.append(self._executor.submit(_run_in_worker, segment))
        except BrokenExecutor as error:
            self.close()
            raise DecodePoolError("a segment decode worker died") from error
        self._submitted += 1
        ready.extend(self.collect())
        return tuple(ready)

    def collect(self) -> tuple[SegmentDecodeResult, ...]:
        """Return every result whose predecessors are all done, in order."""

        ready: list[SegmentDecodeResult] = []
        while self._pending and self._pending[0].done():
            ready.append(self._take_oldest(timeout=0.0))
        return tuple(ready)

    def drain(self, timeout: float | None = None) -> tuple[SegmentDecodeResult, ...]:
        """Wait for every in-flight segment and return all results in order."""

        ready: list[SegmentDecodeResult] = []
        while self._pending:
            ready.append(self._take_oldest(timeout=timeout))
        return tuple(ready)

    def _take_oldest(self, timeout: float | None) -> SegmentDecodeResult:
        future = self._pending[0]
        try:
            result = future.result(timeout=timeout)
        except BrokenExecutor as error:
            self.close()
            raise DecodePoolError("a segment decode worker died") from error
        self._pending.popleft()
        self._completed += 1
        return result

    def worker_cpu_seconds(self) -> float | None:
        """User plus system CPU time of the live workers, or None off Linux.

        Workers are forked by the forkserver, not by this process, so
        ``RUSAGE_CHILDREN`` never sees them; read their own accounting.
        """

        processes = getattr(self._executor, "_processes", None) or {}
        total = 0.0
        try:
            ticks = os.sysconf("SC_CLK_TCK")
            for pid in list(processes):
                with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
                    fields = handle.read().rsplit(")", 1)[1].split()
                total += (int(fields[11]) + int(fields[12])) / ticks
        except (OSError, ValueError, IndexError):
            return None
        return total

    def snapshot(self) -> SegmentDecodePoolSnapshot:
        return SegmentDecodePoolSnapshot(
            workers=self._workers,
            max_in_flight=self._max_in_flight,
            in_flight=len(self._pending),
            submitted=self._submitted,
            completed=self._completed,
            backpressure_waits=self._backpressure_waits,
        )

    def close(self) -> None:
        """Stop the workers and wait for them to exit; safe to repeat."""

        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> SegmentDecodePool:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
