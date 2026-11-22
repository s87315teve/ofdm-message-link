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

Segments travel in batches of up to ``max_batch`` (:meth:`submit_many`).  The
executor costs the submitting process a handful of thread wake-ups per task --
its management thread, the call-queue feeder, the wake-up pipe -- and each
one takes the GIL from the receive owner, whose acquisition is the
bottleneck; at 500 bursts/s those handoffs cost the owner about a quarter of
its time.  A batch pays them once.

A worker that raises fails only what it was decoding.  A segment whose decode
or annotation raises anything but :class:`BurstDecodeError` comes back as an
undiagnosed :class:`PayloadFailure` carrying the error text, as does every
segment of a task whose result cannot come back at all (for example one that
cannot be pickled), so each segment still gets exactly one result, in order,
and later segments keep flowing.  Only a dead worker (``BrokenExecutor``) or
an executor that refuses new work closes the pool with :class:`DecodePoolError`.
What had finished by then is kept, in order, with a failure in place of each
segment that had not, and the next :meth:`~SegmentDecodePool.collect` or
:meth:`~SegmentDecodePool.drain` returns it.
"""

from __future__ import annotations

import multiprocessing
import multiprocessing.connection
import os
import signal
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import BrokenExecutor, CancelledError, Future, ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any

from ofdm_link.config import LinkConfig
from ofdm_link.phy.burst import BurstDecodeError
from ofdm_link.phy.burst_v5 import AnyDecodedBurst
from ofdm_link.phy.streaming import BurstSegment, PayloadFailure, decode_burst_segment

from .overload import OFF as OVERLOAD_OFF
from .overload import OverloadProtection

# Never fork this process. By the time a pool is built the parent has loaded
# NumPy and its BLAS, and may have loaded GNU Radio or CUDA, so it is
# multi-threaded; forking it copies threads' locks in whatever state they were
# in and can deadlock the child. "forkserver" forks from a small server that is
# itself started by exec, so no multi-threaded fork happens, and it still pays
# the imports once instead of once per worker.
_START_METHODS = ("forkserver", "spawn")


class DecodePoolError(RuntimeError):
    """Raised when the decode pool cannot produce trustworthy results."""

    # The messages decoded before the failure (set by MessageReceiver).
    delivered: tuple = ()


class DecodePoolClosed(DecodePoolError):
    """The pool's owner closed it (a stop); no failure of the pool or a worker."""


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
# Segments per task: enough to amortize the executor's per-task thread
# handoffs, few enough that a 65536-sample chunk of short bursts still
# spreads over several workers.
DEFAULT_MAX_BATCH = 4
_READY_TIMEOUT_S = 120.0
# How long the executor gets to notice a worker's exit before it counts as stuck.
_STUCK_MANAGER_GRACE_S = 1.0
# How long close() waits for the workers before killing them, and again after.
# Longer than the grace above, which is what frees a manager stuck on a result.
_CLOSE_WAIT_S = 1.5

Annotate = Callable[[AnyDecodedBurst], Any]


@dataclass(frozen=True, slots=True)
class SegmentDecodeResult:
    """One segment's outcome: a burst and its annotation, or a failure.

    A v5 PSDU whose FCS failed carries both: the burst, because its CRC-valid
    subframes are still deliverable, and the failure the counters record.
    """

    burst: AnyDecodedBurst | None
    failure: PayloadFailure | None
    annotation: Any = None
    # Set when the worker raised instead of decoding: "Type: message".  The
    # failure is then an undiagnosed one, as for a payload the inline decoder
    # could not classify.
    error: str | None = None


def failed_result(error: BaseException) -> SegmentDecodeResult:
    """The result of a segment whose decode raised something unexpected."""

    return SegmentDecodeResult(
        burst=None,
        failure=PayloadFailure(diagnosed=False, crc_failed=False),
        error=f"{type(error).__name__}: {error}",
    )


@dataclass(frozen=True, slots=True)
class SegmentDecodePoolSnapshot:
    workers: int
    max_in_flight: int
    in_flight: int
    submitted: int
    completed: int
    backpressure_waits: int
    # Segments dropped because the pool was full (opt-in overload
    # protection); never decoded and never counted as payload attempts.
    dropped_full: int = 0
    # Segments whose worker raised (SegmentDecodeResult.error); each is
    # also an undiagnosed payload failure.
    worker_errors: int = 0


_WORKER: dict[str, Any] = {}


def _exit_with_owner() -> None:
    """End this worker when the process that owns the pool dies.

    A worker waits on the task pipe, which it and its siblings also hold open
    for writing, so a killed owner would never show up there as end of file.
    The thread sleeps in the kernel and costs the decode nothing.
    """

    owner = multiprocessing.parent_process()
    if owner is None:
        return

    def watch() -> None:
        owner.join()
        os._exit(1)

    threading.Thread(target=watch, name="decode-owner-watch", daemon=True).start()


def _break_pool_if_stuck(manager: threading.Thread) -> None:
    """Break the pool when a worker died partway through writing a result.

    The executor's manager thread then sits in ``recv`` for the rest of a
    message nobody will write: the siblings and this process hold the result
    pipe open, so no end of file comes, and every future, ``shutdown`` and the
    interpreter's exit wait on that thread.  Closing every write end turns the
    wait into the error the executor already handles as a broken pool.

    This thread sleeps on the workers' exit sentinels, waking once a second to
    pick up any worker started later.  A worker that exits normally (only at
    shutdown) is removed by the manager thread within the grace period.  It
    holds the manager thread, not the executor, so a pool nobody holds is
    still collected and shuts down.
    """

    while manager.is_alive() and manager.processes:
        workers = dict(manager.processes)
        exited = multiprocessing.connection.wait(
            [process.sentinel for process in workers.values()], 1.0
        )
        if not exited:
            continue
        gone = {pid for pid, process in workers.items() if process.sentinel in exited}
        deadline = time.monotonic() + _STUCK_MANAGER_GRACE_S
        while time.monotonic() < deadline:
            if not manager.is_alive() or gone.isdisjoint(manager.processes):
                break
            time.sleep(0.02)
        else:
            for pid, process in workers.items():
                if pid not in gone:  # a dead worker's PID may already be reused
                    process.kill()
            manager.result_queue._writer.close()
            return


def _initialize_worker(
    config: LinkConfig, annotate: Annotate | None, give_up_after: int = 0
) -> None:
    # First, before the imports below: Ctrl-C goes to the whole process
    # group, and the owner decides when the workers stop (close()).  A
    # KeyboardInterrupt here would come back as a task's exception, or kill
    # the worker and break the pool, mid-stream.  (A Ctrl-C between the
    # worker's start and this line still reaches it, as it always did.)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _exit_with_owner()

    from ofdm_link.phy.turbo import set_hopeless_block_give_up

    from .allocator import keep_freed_memory
    from .cpu_budget import apply_cpu_budget
    from .factory import build_burst_config, select_frame_decoder

    # One numeric thread per worker; the parallelism is the worker count.
    apply_cpu_budget(1)
    keep_freed_memory()
    set_hopeless_block_give_up(give_up_after)
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
        failure=PayloadFailure.from_burst(burst),
        annotation=None if annotate is None else annotate(burst),
    )


def _run_in_worker(segments: tuple[BurstSegment, ...]) -> tuple[SegmentDecodeResult, ...]:
    if not _worker_is_ready():
        raise DecodePoolError("segment decode worker was not initialized")
    results: list[SegmentDecodeResult] = []
    for segment in segments:
        # One segment that raises must not take the rest of its task along.
        try:
            results.append(
                decode_segment(
                    segment,
                    _WORKER["burst_config"],
                    frame_decoder=_WORKER["frame_decoder"],
                    symbol_demapper=_WORKER["symbol_demapper"],
                    annotate=_WORKER["annotate"],
                )
            )
        except Exception as error:
            results.append(failed_result(error))
    return tuple(results)


def _raised_by_task(future: Future, error: BaseException) -> bool:
    """Whether ``error``, raised by ``future.result()``, is the task's own outcome.

    Not when the wait for it timed out: the task may finish between the
    timeout and any later look at the future, and its results must not be
    thrown away.  A done future raises exactly its stored exception.
    """

    if not future.done():
        return False
    try:
        return future.exception(timeout=0) is error
    except BaseException:  # cancelled
        return False


class SegmentDecodePool:
    """A bounded, order-preserving process pool for burst payload decoding."""

    # Set by close() and interrupt(): what ends a wait from then on is our doing.
    _closing = False

    def __init__(
        self,
        config: LinkConfig,
        *,
        workers: int,
        max_in_flight: int | None = None,
        max_batch: int = DEFAULT_MAX_BATCH,
        annotate: Annotate | None = None,
        overload: OverloadProtection = OVERLOAD_OFF,
    ) -> None:
        if not isinstance(config, LinkConfig):
            raise TypeError("config must be a LinkConfig")
        if type(workers) is not int or not 2 <= workers <= _MAX_WORKERS:
            raise ValueError(f"workers must be an integer in [2, {_MAX_WORKERS}]")
        limit = 4 * workers if max_in_flight is None else max_in_flight
        if type(limit) is not int or limit < workers:
            raise ValueError("max_in_flight must be an integer of at least workers")
        if type(max_batch) is not int or max_batch < 1:
            raise ValueError("max_batch must be a positive integer")
        if annotate is not None and not callable(annotate):
            raise TypeError("annotate must be a module-level callable or None")
        if not isinstance(overload, OverloadProtection):
            raise TypeError("overload must be an OverloadProtection")
        self._overload = overload
        self._dropped_full = 0
        self._workers = workers
        self._max_in_flight = limit
        self._max_batch = max_batch
        # One entry per task, with the number of segments it carries.
        self._pending: deque[tuple[Future[tuple[SegmentDecodeResult, ...]], int]] = deque()
        # Taken from their futures, not yet returned.  They live here, not in
        # a local, so a wait that raises (a timeout, a dead worker) leaves
        # them for the next call, ahead of anything newer.
        self._taken: list[SegmentDecodeResult] = []
        self._in_flight = 0
        self._submitted = 0
        self._completed = 0
        self._backpressure_waits = 0
        self._worker_errors = 0
        self._closed = False
        self._executor = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=_safe_context(),
            initializer=_initialize_worker,
            initargs=(config, annotate, overload.give_up_after),
        )
        self._start_workers()

    def _start_workers(self) -> None:
        try:
            futures = [self._executor.submit(_worker_is_ready) for _ in range(self._workers)]
            # Before waiting: a worker can die in a write during startup too.
            threading.Thread(
                target=_break_pool_if_stuck,
                args=(self._executor._executor_manager_thread,),
                name="decode-pool-watch",
                daemon=True,
            ).start()
            ready = [future.result(timeout=_READY_TIMEOUT_S) for future in futures]
        except Exception as error:
            self._closed = True
            self._shut_down()
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

    @property
    def max_batch(self) -> int:
        return self._max_batch

    def submit(self, segment: BurstSegment) -> tuple[SegmentDecodeResult, ...]:
        """Queue one segment; return any results that are ready, in order."""

        return self.submit_many((segment,))

    def submit_many(self, segments) -> tuple[SegmentDecodeResult, ...]:
        """Queue segments in stream order; return any results that are ready.

        They go to the workers in tasks of up to ``max_batch``.  When the
        in-flight bound (counted in segments) is reached this waits for the
        oldest task first, so a caller that submits faster than the workers
        decode is slowed down rather than allowed to queue without limit.
        """

        if self._closed:
            raise self._failure("segment decode pool is closed")
        batch = tuple(segments)
        if not all(isinstance(segment, BurstSegment) for segment in batch):
            raise TypeError("segments must be BurstSegments")
        cursor = 0
        while cursor < len(batch):
            if self._overload.drop_when_full:
                # Opt-in: take what is already done, then drop what still
                # does not fit instead of making the owner wait.
                self._take_done()
                if self._in_flight >= self._max_in_flight:
                    self._dropped_full += len(batch) - cursor
                    break
            while self._in_flight >= self._max_in_flight:
                self._backpressure_waits += 1
                self._taken.extend(self._take_oldest(timeout=None))
            take = min(
                self._max_batch, len(batch) - cursor, self._max_in_flight - self._in_flight
            )
            try:
                future = self._executor.submit(_run_in_worker, batch[cursor : cursor + take])
            except BrokenExecutor as error:
                self._close_keeping_results()
                raise self._failure("a segment decode worker died") from error
            except Exception as error:
                # The executor refuses work (it is shutting down): these
                # segments would silently get no result, so stop here.
                self._close_keeping_results()
                raise self._failure("the segment decode pool refused work") from error
            self._pending.append((future, take))
            self._in_flight += take
            self._submitted += take
            cursor += take
        self._take_done()
        return self._hand_over()

    def collect(self) -> tuple[SegmentDecodeResult, ...]:
        """Return every result whose predecessors are all done, in order."""

        self._take_done()
        return self._hand_over()

    def drain(self, timeout: float | None = None) -> tuple[SegmentDecodeResult, ...]:
        """Wait for every in-flight segment and return all results in order.

        ``timeout`` bounds the whole wait.  When it expires this raises
        ``TimeoutError``; nothing is lost, and the next call returns the
        results taken so far, then the rest, in order.
        """

        deadline = None if timeout is None else time.monotonic() + timeout
        while self._pending:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            self._taken.extend(self._take_oldest(timeout=left))
        return self._hand_over()

    def _take_done(self) -> None:
        while self._pending and self._pending[0][0].done():
            self._taken.extend(self._take_oldest(timeout=0.0))

    def _hand_over(self) -> tuple[SegmentDecodeResult, ...]:
        ready, self._taken = tuple(self._taken), []
        return ready

    def _failure(self, message: str) -> DecodePoolError:
        """The error for a pool that stopped: not a failure if the owner stopped it."""

        if self._closing:
            return DecodePoolClosed("segment decode pool is closed")
        return DecodePoolError(message)

    def _take_oldest(self, timeout: float | None) -> tuple[SegmentDecodeResult, ...]:
        try:
            future, count = self._pending[0]
        except IndexError:  # close(), on another thread, emptied the queue
            raise self._failure("segment decode pool is closed") from None
        try:
            results = future.result(timeout=timeout)
        except (BrokenExecutor, CancelledError) as error:
            self._close_keeping_results()
            raise self._failure("a segment decode worker died") from error
        except BaseException as error:
            if not _raised_by_task(future, error):
                # The wait timed out (the task is still decoding, or finished
                # just after), or this thread was interrupted: the task stays
                # at the head, and its results come with a later call.
                raise
            # The worker returned, but the task's result did not come back
            # (it could not be pickled, say), or the task itself raised.
            # Each of its segments fails alone, so the ones behind it, and
            # the results already taken in this call, still reach the caller
            # in order.
            results = (failed_result(error),) * count
        try:
            self._pending.popleft()
        except IndexError:  # close() emptied the queue during the wait
            self._taken.extend(results)
            raise self._failure("segment decode pool is closed") from None
        self._in_flight -= count
        self._completed += count
        self._worker_errors += sum(1 for result in results if result.error is not None)
        return results

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
            in_flight=self._in_flight,
            submitted=self._submitted,
            completed=self._completed,
            backpressure_waits=self._backpressure_waits,
            dropped_full=self._dropped_full,
            worker_errors=self._worker_errors,
        )

    def close(self) -> None:
        """Stop the workers, killing any that do not stop in time; safe to repeat."""

        if self._closed:
            return
        self._closing = True
        self._closed = True
        self._pending.clear()
        self._in_flight = 0
        self._shut_down()

    def interrupt(self) -> None:
        """From another thread: end the owner's wait on the workers.

        The workers stop (bounded, as in :meth:`close`), and the owner's call
        raises :class:`DecodePoolClosed` with the queue still its own to empty.
        """

        if not self._closed:
            self._closing = True
            self._shut_down()

    def _close_keeping_results(self) -> None:
        """Close after a fatal error, with one result for each in-flight segment.

        A task that finished keeps its results, even behind one that died; any
        other gets a failure, so the stream's accounting stays one to one.
        """

        if self._closed:
            return
        self._closed = True
        self._shut_down()
        self.give_up()

    def give_up(self) -> int:
        """Stop waiting: keep what has finished, with a failure for what has not.

        Every in-flight segment gets its one result, in stream order, for the
        next :meth:`collect`.  Returns how many had not finished.
        """

        unfinished = 0
        while self._pending:
            future, count = self._pending.popleft()
            if future.done():
                try:
                    results = future.result(timeout=0.0)
                except Exception as error:  # broken, cancelled, or its own failure
                    results = (failed_result(error),) * count
            else:
                unfinished += count
                error = TimeoutError("still decoding when the pool stopped")
                results = (failed_result(error),) * count
            self._taken.extend(results)
            self._in_flight -= count
            self._completed += count
            self._worker_errors += sum(1 for result in results if result.error is not None)
        return unfinished

    def _shut_down(self) -> None:
        """Bounded even with a hung worker, which a plain shutdown would wait for."""

        manager = getattr(self._executor, "_executor_manager_thread", None)
        self._executor.shutdown(wait=False, cancel_futures=True)
        if manager is None:
            return
        manager.join(_CLOSE_WAIT_S)
        if manager.is_alive():
            for process in list(manager.processes.values()):
                # Not one already gone: its PID may have been reused.
                if not multiprocessing.connection.wait([process.sentinel], 0):
                    process.kill()
            manager.join(_CLOSE_WAIT_S)

    def __enter__(self) -> SegmentDecodePool:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
