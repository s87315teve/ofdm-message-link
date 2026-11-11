"""Opt-in real-time scheduling for the radio engine process.  Off by default.

``--realtime[=PRIO]`` puts every thread of the engine process (the receive
owner, the transmit worker, UDP ingress, GNU Radio and UHD threads) in
``SCHED_RR`` at ``PRIO`` (default 15), with ``SCHED_RESET_ON_FORK``: any
process or thread started afterwards begins in ``SCHED_OTHER``, so the
decode workers and the forkserver never run real-time.  Threads the engine
starts later (GNU Radio's, when a radio attaches) are brought up by
:func:`refresh`, which the engine calls after every attach.

Why only this far, from the offline phase-2 tests on two E-cores (n=2):
the whole process tree under ``chrt -f 50`` lost more bursts (0.52-0.69% at
airtime 0.3, against 0%) and stalled another program for up to 0.95 s,
while real-time for the main process alone, workers reset, did no harm to
loss.  So:

* ``SCHED_RR``, not ``SCHED_FIFO``: equal-priority threads still take turns.
* Priority 1-49, default 15: below the kernel's threaded IRQ handlers (50),
  including the USB ones the radio depends on.
* RT throttling (``/proc/sys/kernel/sched_rt_runtime_us``) is never touched:
  real-time threads keep at most 95% of each CPU by default.  If a system
  has it disabled, this says so and still applies only the policy.
* Fewer than 4 usable CPUs (after any ``--cpu-affinity``) is refused unless
  ``--force``: there the engine and the workers compete for every core.
* Linux only.  Elsewhere, and where the policy is not permitted
  (``RLIMIT_RTPRIO`` / ``CAP_SYS_NICE``), it is a no-op with one log line.

The app's ``main`` checks the request and records it in
``OFDM_LINK_REALTIME``; the engine child applies it (``engine._serve``,
which ``node_engine._node_main`` also runs).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

ENVIRONMENT = "OFDM_LINK_REALTIME"
DEFAULT_PRIORITY = 15
PRIORITY_RANGE = (1, 49)
MINIMUM_CPUS = 4
THROTTLING = Path("/proc/sys/kernel/sched_rt_runtime_us")
_TASKS = Path("/proc/self/task")
_PREFIX = "[realtime]"

_state: dict[str, object] = {"priority": None}


class RealtimeError(ValueError):
    """Raised for a request that must not be applied as given."""


def usable_cpus() -> int:
    getter = getattr(os, "sched_getaffinity", None)
    if getter is not None:
        return len(getter(0))
    return os.cpu_count() or 1


def check_request(priority: int, *, force: bool = False, cpus: int | None = None) -> int:
    """Validate a request in ``main``; returns the priority or raises RealtimeError."""

    low, high = PRIORITY_RANGE
    if type(priority) is not int or not low <= priority <= high:
        raise RealtimeError(f"priority must be an integer in [{low}, {high}]")
    available = usable_cpus() if cpus is None else cpus
    if available < MINIMUM_CPUS and not force:
        raise RealtimeError(
            f"only {available} usable CPUs (< {MINIMUM_CPUS}): real-time scheduling would let "
            "the engine starve the decode workers and the rest of the system; add --force to "
            "use it anyway"
        )
    return priority


def request(priority: int) -> None:
    """Record the request for the engine child, which inherits the environment."""

    os.environ[ENVIRONMENT] = str(int(priority))


def requested() -> int | None:
    value = os.environ.get(ENVIRONMENT, "").strip()
    if not value:
        return None
    try:
        priority = int(value)
    except ValueError:
        return None
    low, high = PRIORITY_RANGE
    return priority if low <= priority <= high else None


def apply_requested(log: Callable[[str], None] | None = None) -> int | None:
    """Apply a recorded request to every thread of this process; the priority, or None.

    Off (nothing recorded) is the default and does nothing at all.
    """

    priority = requested()
    if priority is None:
        return None
    emit = log or _stderr
    setter = getattr(os, "sched_setscheduler", None)
    if setter is None or not sys.platform.startswith("linux") or not _TASKS.is_dir():
        emit(f"{_PREFIX} real-time scheduling is Linux only; {sys.platform}: not applied")
        return None
    try:
        count = _apply_to_threads(priority)
    except PermissionError:
        emit(
            f"{_PREFIX} SCHED_RR priority {priority} not permitted (RLIMIT_RTPRIO or "
            "CAP_SYS_NICE); running without real-time scheduling"
        )
        return None
    _state["priority"] = priority
    throttling = _throttling()
    note = "" if throttling != "-1" else "; note: RT throttling is disabled on this system"
    emit(
        f"{_PREFIX} SCHED_RR priority {priority} on {count} engine threads (pid {os.getpid()}); "
        f"children reset to SCHED_OTHER{note}"
    )
    return priority


def refresh() -> int:
    """Bring threads started since :func:`apply_requested` up to its priority; how many."""

    priority = _state.get("priority")
    if priority is None:
        return 0
    try:
        return _apply_to_threads(int(priority), only_new=True)
    except PermissionError:
        return 0


def _apply_to_threads(priority: int, *, only_new: bool = False) -> int:
    reset = getattr(os, "SCHED_RESET_ON_FORK", 0)
    policy = os.SCHED_RR | reset
    parameter = os.sched_param(priority)
    count = 0
    for entry in _TASKS.iterdir():
        try:
            tid = int(entry.name)
            if only_new and os.sched_getscheduler(tid) & ~reset == os.SCHED_RR:
                continue
            os.sched_setscheduler(tid, policy, parameter)
            count += 1
        except (ProcessLookupError, FileNotFoundError, ValueError):
            continue  # a thread that ended meanwhile
    return count


def _throttling() -> str | None:
    try:
        return THROTTLING.read_text(encoding="ascii").strip()
    except OSError:
        return None


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)
