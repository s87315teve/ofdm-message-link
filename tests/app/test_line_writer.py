"""Log files written from a thread of their own (a slow disk must not stop a caller)."""

from __future__ import annotations

import argparse
import io
import os
import threading
import time

from ofdm_link.runtime import realtime
from ofdm_message_link.line_writer import BackgroundLineWriter

# A blocked write takes 200 ms; a caller that returns well under this did not wait for it
# (generous for a loaded Jetson; the calls themselves take microseconds).
NOT_BLOCKED_S = 0.05


class _File:
    """Records writes and flushes; each write takes ``delay_s`` (or waits for ``gate``)."""

    def __init__(self, delay_s=0.0, gate=None):
        self.delay_s = delay_s
        self.gate = gate
        self.text = []
        self.ops = []
        self.flush_times = []
        self.closed = False
        self.in_write = threading.Event()

    def write(self, text):
        self.in_write.set()
        if self.gate is not None:
            self.gate.wait(5.0)
        if self.delay_s:
            time.sleep(self.delay_s)
        self.text.append(text)
        self.ops.append("write")

    def flush(self):
        self.ops.append("flush")
        self.flush_times.append(time.monotonic())

    def close(self):
        self.closed = True


def test_lines_are_written_in_order_and_flushed_when_the_queue_runs_empty():
    file = _File(delay_s=0.001)
    writer = BackgroundLineWriter(file)
    for number in range(50):
        writer.write(f"{number}\n")
    assert writer.drain()
    assert "".join(file.text) == "".join(f"{n}\n" for n in range(50))
    assert file.ops[-1] == "flush"
    assert 1 <= file.ops.count("flush") < 50  # not one flush per line
    assert writer.close() and not file.closed  # not owned: left open


def test_a_busy_queue_is_still_flushed_every_flush_interval():
    file = _File(delay_s=0.04)
    writer = BackgroundLineWriter(file, flush_every_s=0.1)
    for number in range(8):  # 320 ms of writing, the queue never empty until the end
        writer.write(f"{number}\n")
    assert writer.close(5.0)
    assert len(file.text) == 8
    assert 3 <= file.ops.count("flush") <= 6
    gaps = [b - a for a, b in zip(file.flush_times, file.flush_times[1:], strict=False)]
    assert max(gaps) < 0.1 + 2 * 0.04 + 0.05


def test_put_returns_at_once_while_the_file_blocks():
    file = _File(delay_s=0.2)
    writer = BackgroundLineWriter(file)
    writer.write("first\n")
    assert file.in_write.wait(1.0)  # the thread is inside the 200 ms write
    durations = []
    for number in range(5):
        start = time.perf_counter()
        writer.write(f"{number}\n")
        durations.append(time.perf_counter() - start)
    assert max(durations) < NOT_BLOCKED_S, durations
    assert writer.close(5.0)
    assert file.text == ["first\n", "0\n", "1\n", "2\n", "3\n", "4\n"]


def test_errors_are_counted_passed_on_and_reported_once():
    class Broken:
        def write(self, text):
            raise OSError("disk full")

        def flush(self):
            raise OSError("flush failed")

    seen = []
    err = io.StringIO()
    writer = BackgroundLineWriter(Broken(), name="w", on_error=seen.append, stderr=err)
    for _ in range(3):
        writer.write("x\n")  # never raises
    assert writer.drain()
    assert writer.errors == 3 and len(seen) == 3
    assert writer.last_error == "OSError: disk full"
    assert err.getvalue().count("w: write failed") == 1 and "disk full" in err.getvalue()
    assert writer.close()


def test_an_unexpected_failure_does_not_kill_the_writer_thread():
    file = _File()
    writer = BackgroundLineWriter(file, stderr=io.StringIO())
    original = writer._write
    calls = []

    def once_broken(text):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("bug")
        return original(text)

    writer._write = once_broken
    writer.write("lost\n")
    writer.write("kept\n")
    assert writer.close()
    assert file.text == ["kept\n"] and writer.errors == 1 and "bug" in writer.last_error


def test_beyond_max_queue_lines_are_dropped_and_counted():
    gate = threading.Event()
    file = _File(gate=gate)
    seen = []
    writer = BackgroundLineWriter(file, max_queue=3, on_error=seen.append, stderr=io.StringIO())
    writer.write("0\n")
    assert file.in_write.wait(1.0)  # "0" is out of the queue, stuck in the write
    for number in range(1, 6):
        writer.write(f"{number}\n")  # 1-3 queued, 4 and 5 dropped
    assert writer.dropped == 2 and writer.errors == 2 and len(seen) == 2
    assert "queue full" in writer.last_error
    gate.set()
    assert writer.close(5.0)
    assert file.text == ["0\n", "1\n", "2\n", "3\n"]


def test_a_line_put_while_close_writes_the_queue_is_dropped_not_written_ahead():
    file = _File(delay_s=0.01)
    writer = BackgroundLineWriter(file, stderr=io.StringIO())
    for number in range(10):
        writer.write(f"q{number}\n")
    closer = threading.Thread(target=writer.close, args=(5.0,))
    closer.start()
    time.sleep(0.005)
    start = time.perf_counter()
    writer.write("late\n")
    took = time.perf_counter() - start
    closer.join()
    assert took < NOT_BLOCKED_S
    assert file.text == [f"q{number}\n" for number in range(10)]
    assert writer.dropped == 1 and "writer closing" in writer.last_error


def test_close_waits_for_a_stuck_write_and_closes_an_owned_file_only_after_it():
    gate = threading.Event()
    file = _File(gate=gate)
    writer = BackgroundLineWriter(file, close_file=True)
    writer.write("a\n")
    writer.write("b\n")
    assert writer.close(timeout=0.05) is False  # the write is stuck
    assert not file.closed
    gate.set()
    assert writer.close(5.0) is True
    assert file.text == ["a\n", "b\n"] and file.closed
    assert writer.closed and writer.drain()
    writer.write("late\n")  # the thread is done: written on the caller's thread
    assert file.text[-1] == "late\n"


def test_the_writer_thread_stays_out_of_realtime_scheduling(monkeypatch):
    file = _File()
    writer = BackgroundLineWriter(file, name="rt-probe")
    writer.write("x\n")
    assert writer.drain()
    tid = writer._thread.native_id
    assert tid in realtime._normal_threads
    applied = []
    monkeypatch.setattr(os, "sched_setscheduler", lambda t, *_: applied.append(t),
                        raising=False)
    monkeypatch.setattr(os, "sched_getscheduler", lambda t: 0, raising=False)
    monkeypatch.setattr(os, "SCHED_RR", 2, raising=False)
    monkeypatch.setattr(os, "sched_param", lambda priority: priority, raising=False)
    if realtime._TASKS.is_dir():
        realtime._apply_to_threads(1)
        assert applied and tid not in applied
        assert threading.get_native_id() in applied
    assert writer.close()
    assert tid not in realtime._normal_threads


def test_node_app_stats_log_is_written_by_a_thread(tmp_path):
    from ofdm_message_link.node_app import open_stats_log

    assert open_stats_log(argparse.Namespace(stats_log=None)) is None
    path = tmp_path / "stats.jsonl"
    log = open_stats_log(argparse.Namespace(stats_log=str(path)))
    assert isinstance(log, BackgroundLineWriter)
    log.write('{"n": 1}\n')
    log.flush()  # returns at once; the thread flushes
    log.close()
    assert path.read_text() == '{"n": 1}\n'
    assert log._file.closed  # the RotatingLog is owned and closed


def test_events_writer_counts_a_failure_on_its_writer_thread(tmp_path):
    from ofdm_message_link.burst_events import EventLogWriter, read_events

    path = tmp_path / "events.jsonl"
    writer = EventLogWriter(str(path), node_id=1, sample_rate=5e6)
    writer.start({"transport": "fake"})
    writer.on_event("mark", {"text": "one", "t": 1.0})
    assert writer._file.drain()
    real = writer._file._file

    class Failing:
        def write(self, text):
            raise OSError("disk full")

        def flush(self):
            pass

    writer._file._file = Failing()
    writer.on_event("mark", {"text": "two", "t": 2.0})  # never raises
    assert writer._file.drain() and writer.write_errors == 1
    writer._file._file = real
    writer.stop()
    rows = read_events(str(path))
    assert [row["k"] for row in rows] == ["start", "mark", "stop"]
    assert rows[-1]["write_errors"] == 1
