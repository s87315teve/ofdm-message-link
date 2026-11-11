"""--realtime: off by default, refused on small machines, SCHED_RR with reset-on-fork."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from ofdm_link.runtime import realtime
from ofdm_message_link import node_options, options, rx_app, tx_app


@pytest.fixture(autouse=True)
def _clean_environment():
    saved = os.environ.pop(realtime.ENVIRONMENT, None)
    yield
    os.environ.pop(realtime.ENVIRONMENT, None)
    if saved is not None:
        os.environ[realtime.ENVIRONMENT] = saved


def _parsers():
    return (
        (rx_app.build_parser(), []),
        (tx_app.build_parser(), []),
        (node_options.build_parser(), ["--node-id", "1"]),
    )


def test_off_by_default_everywhere():
    for parser, base in _parsers():
        args = parser.parse_args(base)
        assert args.realtime is None and args.force is False
        assert args.native_backend is None
        assert parser.parse_args([*base, "--realtime"]).realtime == 15
        assert parser.parse_args([*base, "--realtime", "20"]).realtime == 20
        with pytest.raises(SystemExit):
            parser.parse_args([*base, "--realtime", "50"])
    lines = []
    assert realtime.apply_requested(log=lines.append) is None and lines == []


def test_small_machines_are_refused_unless_forced():
    with pytest.raises(realtime.RealtimeError, match="--force"):
        realtime.check_request(15, cpus=2)
    assert realtime.check_request(15, cpus=2, force=True) == 15
    assert realtime.check_request(15, cpus=4) == 15
    with pytest.raises(realtime.RealtimeError):
        realtime.check_request(0, cpus=8)


def test_main_records_the_request_for_the_engine(monkeypatch):
    monkeypatch.setattr(realtime, "usable_cpus", lambda: 2)
    args = rx_app.build_parser().parse_args(["--realtime"])
    with pytest.raises(SystemExit, match="--realtime"):
        options.apply_performance_options(args)
    assert realtime.ENVIRONMENT not in os.environ
    args = rx_app.build_parser().parse_args(["--realtime", "12", "--force"])
    options.apply_performance_options(args)
    assert realtime.requested() == 12


def test_elsewhere_than_linux_it_is_one_log_line(monkeypatch):
    realtime.request(15)
    monkeypatch.delattr(os, "sched_setscheduler")
    lines = []
    assert realtime.apply_requested(log=lines.append) is None
    assert len(lines) == 1 and "Linux only" in lines[0]


_CHILD = r"""
import os, subprocess, sys, threading
from ofdm_link.runtime import realtime
ready = threading.Event(); stop = threading.Event()
def idle():
    ready.set(); stop.wait()
early = threading.Thread(target=idle); early.start(); ready.wait()
lines = []
result = realtime.apply_requested(log=lines.append)
print("RESULT", result)
print("LOG", lines[0] if lines else "")
if result is not None:
    reset = getattr(os, "SCHED_RESET_ON_FORK", 0)
    policies = [os.sched_getscheduler(int(t)) & ~reset for t in os.listdir("/proc/self/task")]
    print("ALL_RR", all(p == os.SCHED_RR for p in policies))
    late = threading.Thread(target=stop.wait); late.start()
    print("LATE", os.sched_getscheduler(late.native_id) & ~reset == os.SCHED_RR)
    print("REFRESHED", realtime.refresh())
    print("LATE_AFTER", os.sched_getscheduler(late.native_id) & ~reset == os.SCHED_RR)
    child = subprocess.run([sys.executable, "-c",
                            "import os; print(os.sched_getscheduler(0))"],
                           capture_output=True, text=True)
    print("CHILD", child.stdout.strip(), os.SCHED_OTHER)
stop.set()
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux scheduling only")
def test_engine_threads_go_rr_and_children_stay_other():
    env = {**os.environ, realtime.ENVIRONMENT: "1"}
    out = subprocess.run([sys.executable, "-c", _CHILD], capture_output=True, text=True,
                         env=env, check=True, timeout=60).stdout
    fields = dict(line.split(" ", 1) for line in out.strip().splitlines())
    if fields["RESULT"] == "None":
        # This account may not raise priorities: the request is a logged no-op.
        assert "not permitted" in fields["LOG"]
        return
    assert fields["RESULT"] == "1" and "SCHED_RR priority 1" in fields["LOG"]
    assert fields["ALL_RR"] == "True"
    assert fields["LATE"] == "False"  # reset-on-fork: a new thread starts SCHED_OTHER
    assert fields["REFRESHED"] == "1" and fields["LATE_AFTER"] == "True"
    child, other = fields["CHILD"].split()
    assert child == other  # decode workers and other children stay SCHED_OTHER
