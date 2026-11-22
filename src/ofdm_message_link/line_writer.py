"""Log files written from a thread of their own.

On an SD card (ext4, the Jetson) a small ``write`` + ``flush`` blocks for
80-190 ms about every 5 s, the rhythm of the journal commit.  A radio thread
that writes its log line itself stops for that long.  :class:`BackgroundLineWriter`
takes finished lines on a :class:`queue.SimpleQueue` instead (never blocks on
I/O, keeps the order of the calls) and a daemon thread writes them in that
order.  It flushes whenever the queue runs empty, and at least every
``flush_every_s`` while lines keep coming.

The caller's thread never sees a write error: the writer counts them
(``errors``), passes each to ``on_error`` and prints the first one on stderr,
from the writer thread (never from a caller's while the thread runs; after
it has finished, from the caller's thread that writes inline).  A line that cannot be
queued is dropped and counted the same way (``dropped`` as well): beyond
``max_queue`` waiting lines (a disk that stopped), or while :meth:`close` is
still writing the queue out.  After the thread has finished, a line is
written on the caller's thread.  A :meth:`close` that times out counts every
line not yet written as dropped and says so once on stderr, because the
process usually exits next and they are lost.  The thread keeps writing
meanwhile, and each line it really writes comes off the count.

It stands in for a text file that only gets ``write``/``flush``/``close``:
``write`` queues and ``flush`` returns at once (the thread flushes).  Under
``--realtime`` the writer thread stays ``SCHED_OTHER``
(:func:`ofdm_link.runtime.realtime.keep_normal`): it only waits for the disk.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Any, TextIO

from ofdm_link.runtime import realtime

FLUSH_EVERY_S = 0.1
"""Longest time written lines wait for a flush while the queue never runs empty."""
CLOSE_TIMEOUT_S = 2.0
MAX_QUEUE = 100_000
"""Lines waiting beyond this are dropped (and counted): memory stays bounded."""

_STOP = object()


class LineDropped(RuntimeError):
    """A line the writer could not take (queue full, or the writer is closing)."""


class _Drained:
    """A queue marker: set once every line queued before it is written and flushed."""

    __slots__ = ("event",)

    def __init__(self) -> None:
        self.event = threading.Event()


class BackgroundLineWriter:
    """Writes text to ``file`` from a daemon thread, in the order it was queued."""

    def __init__(
        self,
        file: Any,
        *,
        name: str = "line-writer",
        flush_every_s: float = FLUSH_EVERY_S,
        max_queue: int = MAX_QUEUE,
        on_error: Any = None,
        close_file: bool = False,
        stderr: TextIO | None = None,
    ) -> None:
        self._file = file
        self.name = str(name)
        self._flush_every_s = float(flush_every_s)
        self._max_queue = int(max_queue)
        self._on_error = on_error
        self._close_file = bool(close_file)
        self._stderr = stderr
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        # Never held during I/O: makes "closed?" + put one step, so no line
        # lands behind the stop marker.
        self._state_lock = threading.Lock()
        # Held by whoever writes to the file: the thread, or a caller once it is done.
        self._io_lock = threading.Lock()
        self._closed = False
        self._reported = False
        # Lines queued, and lines the thread is done with (written or failed).
        self._queued = 0
        self._finished = 0
        self._close_timed_out = False
        # Counted as dropped by a close that timed out, and not written yet.
        self._forfeited = 0
        self._forfeit_failed = False
        self._error_before_timeout: str | None = None
        self.errors = 0
        self.dropped = 0
        self.last_error: str | None = None
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ callers

    def put(self, text: str) -> None:
        """Queue one line.  Never blocks on the file and never raises."""

        with self._state_lock:
            if not self._closed:
                if self._queue.qsize() < self._max_queue:
                    self._queued += 1
                    self._queue.put(text)
                    return
                self._drop("queue full")
                return
        if self._thread.is_alive():
            # close() is still writing the queue: writing here would jump
            # ahead of those lines (and wait for the disk).
            self._drop("writer closing")
            return
        with self._io_lock:
            if self._write(text):
                self._flush()
            self._report_once()

    def write(self, text: str) -> int:
        self.put(text)
        return len(text)

    def flush(self) -> None:
        """Returns at once: the writer thread flushes (see :meth:`drain` to wait)."""

    def drain(self, timeout: float | None = CLOSE_TIMEOUT_S) -> bool:
        """Wait until every line queued so far is written and flushed; False on timeout."""

        marker = _Drained()
        with self._state_lock:
            if self._closed:
                return not self._thread.is_alive()
            self._queue.put(marker)
        return marker.event.wait(timeout)

    def close(self, timeout: float = CLOSE_TIMEOUT_S) -> bool:
        """Write what is queued, stop the thread (and close ``file`` if owned).

        True when the thread finished within ``timeout``.  Idempotent.  A
        file the writer owns is closed only once the thread has finished.
        On a timeout, the lines not written yet count as dropped.
        """

        with self._state_lock:
            if not self._closed:
                self._closed = True
                self._queue.put(_STOP)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout)
            if self._thread.is_alive():
                self._count_unwritten(timeout)
        done = not self._thread.is_alive()
        if done and self._close_file:
            with self._io_lock:
                try:
                    if not getattr(self._file, "closed", False):
                        self._file.close()
                except Exception as error:
                    self._failed(error)
                self._report_once()
        return done

    def _count_unwritten(self, timeout: float) -> None:
        with self._state_lock:
            if self._close_timed_out:
                return
            self._close_timed_out = True
            left = self._queued - self._finished
            if not left:
                return
            self._forfeited = left
            self.dropped += left
            self.errors += left
            self._error_before_timeout = self.last_error
            self.last_error = f"close timed out with {left} line(s) not written"
            # This is the report of these; the thread must not add its own.
            self._reported = True
        try:
            print(
                f"{self.name}: close timed out after {timeout:g} s; "
                f"{left} line(s) not written",
                file=self._stderr if self._stderr is not None else sys.stderr,
                flush=True,
            )
        except Exception:
            pass

    def _line_finished(self, written: bool) -> None:
        with self._state_lock:
            self._finished += 1
            if not self._forfeited:
                return
            self._forfeited -= 1
            # One error per line: a write that failed has just counted its own.
            self.errors -= 1
            if written:
                self.dropped -= 1
            else:
                self._forfeit_failed = True
            if not self._forfeited and not self._forfeit_failed:
                self.last_error = self._error_before_timeout

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------ the thread

    def _run(self) -> None:
        realtime.keep_normal()
        try:
            self._loop()
        finally:
            realtime.release_normal()

    def _loop(self) -> None:
        get = self._queue.get
        empty = self._queue.empty
        clock = time.monotonic
        last_flush = clock()
        dirty = False
        while True:
            item = get()
            if item is _STOP:
                break
            try:
                with self._io_lock:
                    if isinstance(item, _Drained):
                        if dirty:
                            self._flush()
                            dirty = False
                            last_flush = clock()
                        item.event.set()
                    else:
                        written = self._write(item)
                        dirty = written or dirty
                        self._line_finished(written)
                        if dirty and (empty() or clock() - last_flush >= self._flush_every_s):
                            self._flush()
                            dirty = False
                            last_flush = clock()
                    self._report_once()
            except Exception as error:  # never lets the thread die
                self._failed(error)
        with self._io_lock:
            if dirty:
                self._flush()
            self._report_once()

    def _write(self, text: str) -> bool:
        try:
            self._file.write(text)
            return True
        except Exception as error:
            self._failed(error)
            return False

    def _flush(self) -> None:
        try:
            self._file.flush()
        except Exception as error:
            self._failed(error)

    def _drop(self, why: str) -> None:
        self.dropped += 1
        self._failed(LineDropped(f"line dropped: {why}"))

    def _failed(self, error: BaseException) -> None:
        """Count one failure; no I/O here (a caller's thread may be the one failing)."""

        self.errors += 1
        self.last_error = f"{type(error).__name__}: {error}"
        if self._on_error is not None:
            try:
                self._on_error(error)
            except Exception:
                pass

    def _report_once(self) -> None:
        if self._reported or not self.errors:
            return
        self._reported = True
        try:
            print(
                f"{self.name}: write failed ({self.last_error}); further errors are only counted",
                file=self._stderr if self._stderr is not None else sys.stderr,
                flush=True,
            )
        except Exception:
            pass


__all__ = ["CLOSE_TIMEOUT_S", "FLUSH_EVERY_S", "MAX_QUEUE", "BackgroundLineWriter", "LineDropped"]
