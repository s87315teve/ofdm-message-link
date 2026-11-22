"""A process killed partway through a pipe write must not hang the other side.

A multiprocessing pipe reports a dead peer only once no other process holds
that end open, and a reader that has a message's header waits for the rest of
it.  Each scenario runs in its own interpreter under a timeout, so a
regression fails its test instead of hanging the suite.
"""

from __future__ import annotations

import gc
import json
import multiprocessing.queues
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCENARIO_TIMEOUT_S = 25.0


def _write_part_of_a_message(connection) -> None:
    """A header promising 100 kB and ten bytes of it: a write cut off by SIGKILL."""

    os.write(connection.fileno(), struct.pack("!i", 100_000) + b"x" * 10)


def _die() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


# ------------------------------------------------------ engine children


def _child_cut_off_in_an_event(events, _control, _replies):
    events.put_batch([("message", 1)])
    while events._taken.value < 1:  # the parent has the whole first batch
        time.sleep(0.005)
    _write_part_of_a_message(events._queue._writer)
    _die()


def _child_cut_off_in_a_reply(_events, control, replies):
    control.get(timeout=30)
    _write_part_of_a_message(replies._writer)
    _die()


def _child_dies_at_once(_events, _control, _replies):
    _die()


def _child_serves(pid_path, _events, control, replies):
    from ofdm_message_link import engine

    class Worker:
        lag_monitor = None

    Path(pid_path).write_text(str(os.getpid()))
    try:
        engine._serve(Worker(), control, replies, lambda: None, switch_interval_s=0.005)
    finally:
        Path(pid_path + ".served").write_text("left the loop")


# ------------------------------------------------------------ scenarios


def _engine(target, arguments=(), dispatch=lambda kind, value: None):
    from ofdm_message_link import engine

    return engine._EngineProcess(target, arguments, dispatch)


def scenario_event_cut_off():
    got, failed = [], threading.Event()

    def dispatch(kind, value):
        got.append(kind)
        if kind == "failed":
            failed.set()

    process = _engine(_child_cut_off_in_an_event, dispatch=dispatch)
    process.start()
    assert failed.wait(10.0), "the pump never reported the dead child"
    # What the child finished writing came first.
    assert got == ["message", "failed"]
    process.stop()


def scenario_reply_cut_off():
    from ofdm_message_link.transport import TransportError

    process = _engine(_child_cut_off_in_a_reply)
    process.start()
    started = time.monotonic()
    try:
        process.request(("attach",), 5.0)
    except TransportError:
        pass
    else:
        raise AssertionError("a cut-off reply was returned")
    assert time.monotonic() - started < 10.0
    process.stop()


def scenario_stop_with_a_full_control_pipe():
    process = _engine(_child_dies_at_once)
    process.start()
    process._process.join(30.0)
    # More than the pipe holds, for a reader that no longer exists.
    for _ in range(16):
        process.post(("submit", "test", bytes(60_000)))
    time.sleep(0.3)
    started = time.monotonic()
    process.stop()
    assert time.monotonic() - started < 10.0


def scenario_parent_of_an_engine(pid_path, cut_off):
    process = _engine(_child_serves, (pid_path,))
    process.start()
    deadline = time.monotonic() + 30.0
    while not (os.path.exists(pid_path) and Path(pid_path).read_text()):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    time.sleep(0.3)  # in its control.get
    if cut_off == "cut_off":
        _write_part_of_a_message(process._control._writer)
        time.sleep(0.2)
    _die()


def _annotate_then_die_writing_the_result(_burst):
    """In a decode worker: what a SIGKILL during the result write leaves behind."""

    results = [item for item in gc.get_objects()
               if isinstance(item, multiprocessing.queues.SimpleQueue)]
    assert len(results) == 1
    results[0]._wlock.acquire()  # the writer holds it for the whole message
    _write_part_of_a_message(results[0]._writer)
    _die()


def _receiver_with(annotate):
    from stream_fixtures import MEASUREMENT, profile, v5_stream

    from ofdm_link.runtime.segment_decode_pool import SegmentDecodePool
    from ofdm_message_link.link import MessageReceiver

    link = profile(MEASUREMENT)
    stream, _ = v5_stream(link, seed=83, bursts=6, mcs_indices=(8, 12, 16))
    receiver = MessageReceiver(link, decode_workers=2)
    receiver._pool.close()
    receiver._pool = SegmentDecodePool(link.config, workers=2, annotate=annotate, max_batch=1)
    return receiver, stream


def scenario_decode_worker_cut_off(when):
    from ofdm_link.runtime.segment_decode_pool import DecodePoolError

    receiver, stream = _receiver_with(_annotate_then_die_writing_the_result)
    started = time.monotonic()
    if when == "drain":
        try:
            receiver.feed(stream)
            receiver.flush(30.0)
        except DecodePoolError:
            pass
        else:
            raise AssertionError("a dead worker went unreported")
        assert time.monotonic() - started < 20.0
    else:  # close() straight after the worker died, with nobody waiting on a result
        try:
            receiver.feed(stream)
        except DecodePoolError:
            pass
        time.sleep(0.05)
    receiver.close()
    assert time.monotonic() - started < 30.0
    # The script now exits normally: the interpreter joins the executor's
    # manager thread, so a thread still stuck in recv would hang it here.


def scenario_owner_of_a_pool(pid_path):
    from stream_fixtures import MEASUREMENT, profile

    from ofdm_link.runtime.segment_decode_pool import SegmentDecodePool

    pool = SegmentDecodePool(profile(MEASUREMENT).config, workers=2)
    Path(pid_path).write_text(json.dumps(list(pool._executor._processes)))
    _die()




def _hang_in_annotate(_burst):
    time.sleep(3600)


def scenario_stop_with_a_hung_worker():
    from test_rx_chunk_merge import _Bridge, _QueueSource

    from ofdm_message_link.rx_app import ReceiveWorker

    receiver, stream = _receiver_with(_hang_in_annotate)
    workers = list(receiver._pool._executor._processes)
    worker = ReceiveWorker(receiver, _Bridge())
    worker.start()
    worker.attach(_QueueSource([stream]))
    deadline = time.monotonic() + 15.0
    while receiver.stats.samples_consumed == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    started = time.monotonic()
    worker.stop()
    assert time.monotonic() - started < 9.0  # inside the engine's own stop timeout
    assert _all_gone(workers, 3.0)


def _die_writing_a_result_at_startup():
    """Runs as the worker unpickles its arguments, before its initializer."""

    results = [item for item in gc.get_objects()
               if isinstance(item, multiprocessing.queues.SimpleQueue)]
    _write_part_of_a_message(results[0]._writer)
    _die()


class _KillsTheWorkerAtStartup:
    def __call__(self, _burst):
        return None

    def __reduce__(self):
        return (_die_writing_a_result_at_startup, ())


def scenario_worker_cut_off_during_startup():
    from stream_fixtures import MEASUREMENT, profile

    from ofdm_link.runtime.segment_decode_pool import DecodePoolError, SegmentDecodePool

    started = time.monotonic()
    try:
        SegmentDecodePool(profile(MEASUREMENT).config, workers=2,
                          annotate=_KillsTheWorkerAtStartup())
    except DecodePoolError:
        pass
    else:
        raise AssertionError("a pool whose workers died was built")
    assert time.monotonic() - started < 15.0


def _die_writing_after(delay_s):
    time.sleep(delay_s)
    results = [item for item in gc.get_objects()
               if isinstance(item, multiprocessing.queues.SimpleQueue)]
    results[0]._wlock.acquire()
    _write_part_of_a_message(results[0]._writer)
    _die()


def _sleep_for(delay_s):
    time.sleep(delay_s)


def scenario_a_worker_the_executor_would_start_late():
    """Only one worker started with the pool; the other would come with a later submit."""

    from concurrent.futures.process import ProcessPoolExecutor

    from stream_fixtures import MEASUREMENT, profile

    from ofdm_link.runtime.segment_decode_pool import SegmentDecodePool

    adjust = ProcessPoolExecutor._adjust_process_count

    def only_the_first(executor):
        if not executor._processes:
            adjust(executor)

    ProcessPoolExecutor._adjust_process_count = only_the_first
    pool = SegmentDecodePool(profile(MEASUREMENT).config, workers=2)
    ProcessPoolExecutor._adjust_process_count = adjust
    dying = pool._executor.submit(_die_writing_after, 0.3)
    for _ in range(3):
        pool._executor.submit(_sleep_for, 2.0)
    assert dying.exception(timeout=15.0) is not None
    pool.close()
    # The script exits normally: a manager thread still stuck would hang it here.


SCENARIOS = {
    "a_worker_the_executor_would_start_late": scenario_a_worker_the_executor_would_start_late,
    "stop_with_a_hung_worker": scenario_stop_with_a_hung_worker,
    "worker_cut_off_during_startup": scenario_worker_cut_off_during_startup,
    "event_cut_off": scenario_event_cut_off,
    "reply_cut_off": scenario_reply_cut_off,
    "stop_with_a_full_control_pipe": scenario_stop_with_a_full_control_pipe,
    "parent_of_an_engine": scenario_parent_of_an_engine,
    "decode_worker_cut_off": scenario_decode_worker_cut_off,
    "owner_of_a_pool": scenario_owner_of_a_pool,
}


# ---------------------------------------------------------------- tests


@pytest.fixture
def run():
    """Run a scenario in its own session; whatever it leaves behind dies at teardown."""

    sessions = []

    def _run(name, *arguments, expect_killed=False):
        environment = {**os.environ, "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), name, *arguments],
            cwd=_ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        sessions.append(process.pid)
        _, errors = process.communicate(timeout=_SCENARIO_TIMEOUT_S)
        expected = -signal.SIGKILL if expect_killed else 0
        assert process.returncode == expected, errors[-2000:]

    yield _run
    for session in sessions:
        try:
            os.killpg(session, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _running(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _all_gone(pids, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_running(pid) for pid in pids):
            return True
        time.sleep(0.05)
    for pid in pids:  # do not leave them behind when the test fails
        if _running(pid):
            os.kill(pid, signal.SIGKILL)
    return False


def test_a_child_killed_in_an_event_write_is_reported_by_the_pump(run):
    run("event_cut_off")


def test_a_child_killed_in_a_reply_write_fails_the_request(run):
    run("reply_cut_off")


def test_stop_returns_when_the_dead_child_left_the_control_pipe_full(run):
    run("stop_with_a_full_control_pipe")


@pytest.mark.parametrize("cut_off", ["idle", "cut_off"])
def test_an_engine_leaves_its_loop_when_its_parent_is_killed(run, tmp_path, cut_off):
    pid_path = str(tmp_path / "engine.pid")
    run("parent_of_an_engine", pid_path, cut_off, expect_killed=True)
    pid = int(Path(pid_path).read_text())
    assert _all_gone([pid]), "the engine child outlived its parent"
    assert os.path.exists(pid_path + ".served")


@pytest.mark.parametrize("when", ["drain", "close"])
def test_a_decode_worker_killed_in_a_result_write_breaks_the_pool(run, when):
    run("decode_worker_cut_off", when)


def test_stop_is_bounded_with_a_hung_decode_worker(run):
    run("stop_with_a_hung_worker")


def test_a_worker_killed_in_a_write_during_startup_fails_the_pool(run):
    run("worker_cut_off_during_startup")


def test_a_worker_started_late_cannot_hold_the_result_pipe_open(run):
    run("a_worker_the_executor_would_start_late")


def test_decode_workers_exit_when_the_pool_owner_is_killed(run, tmp_path):
    pid_path = tmp_path / "workers.json"
    run("owner_of_a_pool", str(pid_path), expect_killed=True)
    workers = json.loads(pid_path.read_text())
    assert len(workers) == 2
    assert _all_gone(workers), "decode workers outlived the process that owned them"


def test_the_watch_thread_does_not_keep_a_dropped_pool_alive():
    """The executor shuts its workers down when collected; the watch must allow it."""

    from stream_fixtures import MEASUREMENT, profile

    from ofdm_link.runtime.segment_decode_pool import SegmentDecodePool

    pool = SegmentDecodePool(profile(MEASUREMENT).config, workers=2)
    workers = list(pool._executor._processes)
    del pool
    gc.collect()
    assert _all_gone(workers), "a pool nobody holds kept its workers"


if __name__ == "__main__":
    SCENARIOS[sys.argv[1]](*sys.argv[2:])


def test_a_command_posted_after_stop_is_refused_not_raised():
    from ofdm_message_link.engine import _EngineProcess

    engine = _EngineProcess.__new__(_EngineProcess)
    engine._control = multiprocessing.get_context("spawn").Queue(1)
    engine._control.close()
    assert engine.post(("mcs", None)) is False
