"""Optional CPU pinning for an application and every process it starts.

Off by default: nothing here runs unless ``--cpu-affinity`` is given.  The
setting is applied once, in the application's ``main``, before any engine or
decode worker exists; Linux children inherit their parent's CPU set through
``fork`` and ``exec``, so the engine child, the forkserver and every decode
worker run on the same CPUs.

No core numbers are built in.  A spec is either an explicit CPU list in the
``taskset -c`` / sysfs format (``"0-7"``, ``"2,4-5"``) or ``p-cores``, which
reads the performance-core list a hybrid Intel CPU's kernel exposes in
``/sys/devices/cpu_core/cpus``.  Where that file does not exist (a kernel or
CPU without the hybrid PMU, any ARM board) ``p-cores`` pins nothing and says
so in one log line.  Off Linux, any spec is a no-op with one log line.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

PERFORMANCE_CORES = "p-cores"
PERFORMANCE_CORE_LIST = Path("/sys/devices/cpu_core/cpus")
_LOG_PREFIX = "[cpu-affinity]"


class CpuAffinityError(ValueError):
    """Raised for a CPU list that cannot be parsed or names no usable CPU."""


def parse_cpu_list(text: str) -> frozenset[int]:
    """CPUs named by a list such as ``"0-3,8,10-11"`` (the sysfs format)."""

    cpus: set[int] = set()
    for part in str(text).strip().split(","):
        part = part.strip()
        if not part:
            raise CpuAffinityError(f"empty entry in CPU list {text!r}")
        low, dash, high = part.partition("-")
        try:
            first = int(low)
            last = int(high) if dash else first
        except ValueError as error:
            raise CpuAffinityError(f"cannot parse CPU list {text!r}") from error
        if first < 0 or last < first:
            raise CpuAffinityError(f"invalid CPU range {part!r} in {text!r}")
        cpus.update(range(first, last + 1))
    if not cpus:
        raise CpuAffinityError(f"CPU list {text!r} names no CPU")
    return frozenset(cpus)


def check_spec(spec: str) -> str:
    """Validate a spec at the command line; ``p-cores`` is checked when applied."""

    if spec != PERFORMANCE_CORES:
        parse_cpu_list(spec)
    return spec


def resolve(spec: str, *, core_list: Path = PERFORMANCE_CORE_LIST) -> frozenset[int] | None:
    """The CPU set a spec stands for, or None when ``p-cores`` has nothing to read."""

    if spec == PERFORMANCE_CORES:
        if not core_list.is_file():
            return None
        return parse_cpu_list(core_list.read_text(encoding="ascii"))
    return parse_cpu_list(spec)


def apply(
    spec: str | None,
    *,
    core_list: Path = PERFORMANCE_CORE_LIST,
    log: Callable[[str], None] | None = None,
) -> frozenset[int] | None:
    """Pin this process (and so its future children) to ``spec``; None leaves it alone.

    Returns the CPU set now in force, or None when nothing was pinned.  A CPU
    outside the set this process may use (a cgroup, an outer ``taskset``) is
    an error, not silently dropped.
    """

    if spec is None:
        return None
    emit = log or _stderr
    setter = getattr(os, "sched_setaffinity", None)
    if setter is None:
        emit(f"{_LOG_PREFIX} {sys.platform} has no sched_setaffinity; not pinning ({spec})")
        return None
    cpus = resolve(spec, core_list=core_list)
    if cpus is None:
        emit(f"{_LOG_PREFIX} {core_list} does not exist on this machine; not pinning")
        return None
    allowed = os.sched_getaffinity(0)
    if not cpus <= allowed:
        missing = ",".join(str(cpu) for cpu in sorted(cpus - allowed))
        raise CpuAffinityError(f"CPUs {missing} are not available to this process")
    setter(0, cpus)
    emit(f"{_LOG_PREFIX} pinned to CPUs {format_cpu_list(cpus)}")
    return frozenset(os.sched_getaffinity(0))


def format_cpu_list(cpus) -> str:
    """``{0,1,2,5}`` as ``"0-2,5"``."""

    ordered = sorted(cpus)
    runs: list[str] = []
    start = previous = None
    for cpu in ordered:
        if start is None:
            start = previous = cpu
        elif cpu == previous + 1:
            previous = cpu
        else:
            runs.append(f"{start}-{previous}" if previous != start else f"{start}")
            start = previous = cpu
    if start is not None:
        runs.append(f"{start}-{previous}" if previous != start else f"{start}")
    return ",".join(runs)


def _stderr(line: str) -> None:
    print(line, file=sys.stderr, flush=True)
