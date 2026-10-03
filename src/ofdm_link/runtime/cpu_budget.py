"""Bound the CPU cores one process may occupy.

NumPy's bundled BLAS sizes its own thread pool from the machine's core count
and keeps those threads spinning between calls.  Nothing in the receive path
is large enough linear algebra to gain from them, so on a 20-core host they
burned most of the machine doing no useful work.  Bounding the pool returns
those cores to the decoder.

The budget is a *cap*, not a target: it limits wasted threads, it does not
make one Python thread faster.

Setting a thread count is process-wide, so only an application's ``main`` does
it -- never an import.  Importing this module changes nothing.
"""

from __future__ import annotations

import ctypes
import os
import re
from dataclasses import dataclass

MAX_CPU_CORES = 256

# Each entry is a library name pattern and the symbols that set and read back
# its thread count.  Only libraries this process has already loaded are touched.
_NUMERIC_LIBRARIES: tuple[tuple[str, str, str], ...] = (
    (r"libopenblas", "openblas_set_num_threads", "openblas_get_num_threads"),
    (r"libmkl_rt", "MKL_Set_Num_Threads", "MKL_Get_Max_Threads"),
    (r"libgomp", "omp_set_num_threads", "omp_get_max_threads"),
    (r"libiomp5|libomp\b", "omp_set_num_threads", "omp_get_max_threads"),
)
_MAPPED_LIBRARY = re.compile(r"\s(/\S+\.so[.\d]*)$")


@dataclass(frozen=True, slots=True)
class CpuBudget:
    """What an endpoint asked for and what it was actually able to bound."""

    cores: int
    numeric_threads: int
    bounded_libraries: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.cores) is not int or not 1 <= self.cores <= MAX_CPU_CORES:
            raise ValueError("cores must be a resolved positive core count")
        if type(self.numeric_threads) is not int or not 1 <= self.numeric_threads:
            raise ValueError("numeric_threads must be a positive integer")
        if not isinstance(self.bounded_libraries, tuple) or not all(
            type(name) is str for name in self.bounded_libraries
        ):
            raise TypeError("bounded_libraries must be a tuple of library names")


def resolve_cpu_cores(cpu_cores: int) -> int:
    """Map the configured value to the number of cores this endpoint may use.

    ``N > 0`` is taken literally and ``0`` means auto.  Auto asks which CPUs
    this process may actually run on rather than how many the machine has, so
    a cgroup or a ``taskset`` is respected instead of being reported through.
    """

    if type(cpu_cores) is not int or not 0 <= cpu_cores <= MAX_CPU_CORES:
        raise ValueError(f"cpu_cores must be an integer in [0, {MAX_CPU_CORES}]")
    if cpu_cores != 0:
        return cpu_cores
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is not None:
        return max(1, len(getaffinity(0)))
    return max(1, os.cpu_count() or 1)


def apply_cpu_budget(cpu_cores: int) -> CpuBudget:
    """Cap every loaded numeric library's thread pool at the core budget.

    Returns what was applied.  A library this process never loaded is not
    loaded here to bound it, and a library that exposes no setter is reported
    as unbounded rather than raised on: the budget is an efficiency measure,
    and failing to apply it must never take a link down.
    """

    cores = resolve_cpu_cores(cpu_cores)
    bounded: list[str] = []
    for path, _, setter in _mapped_numeric_libraries():
        if _set_thread_count(path, setter, cores):
            bounded.append(os.path.basename(path))
    return CpuBudget(
        cores=cores,
        numeric_threads=cores,
        bounded_libraries=tuple(sorted(set(bounded))),
    )


def numeric_thread_count() -> int | None:
    """Read back what the loaded numeric libraries are currently set to.

    Returns the largest count any of them reports, or ``None`` when no library
    this process loaded exposes a getter.  A caller that has to leave the
    process exactly as it found it -- a test, above all -- saves this value and
    passes it back to ``apply_cpu_budget``.
    """

    counts = [
        count
        for path, getter, _ in _mapped_numeric_libraries()
        if (count := _get_thread_count(path, getter)) is not None
    ]
    return max(counts) if counts else None


def _mapped_numeric_libraries() -> tuple[tuple[str, str, str], ...]:
    """Return ``(path, getter, setter)`` for each numeric library in this process."""

    found: list[tuple[str, str, str]] = []
    for path in _loaded_libraries():
        name = os.path.basename(path)
        for pattern, setter, getter in _NUMERIC_LIBRARIES:
            if re.search(pattern, name) is not None:
                found.append((path, getter, setter))
                break
    return tuple(found)


def _loaded_libraries() -> tuple[str, ...]:
    """Return the shared objects already mapped into this process."""

    try:
        with open("/proc/self/maps", encoding="utf-8") as maps:
            found = {
                match.group(1)
                for match in map(_MAPPED_LIBRARY.search, (line.rstrip() for line in maps))
                if match is not None
            }
    except OSError:
        return ()
    return tuple(sorted(found))


def _get_thread_count(path: str, symbol: str) -> int | None:
    try:
        getter = getattr(ctypes.CDLL(path), symbol)
    except (OSError, AttributeError):
        return None
    getter.argtypes = ()
    getter.restype = ctypes.c_int
    try:
        count = int(getter())
    except (OSError, ValueError):
        return None
    return count if count >= 1 else None


def _set_thread_count(path: str, symbol: str, threads: int) -> bool:
    try:
        library = ctypes.CDLL(path)
        setter = getattr(library, symbol)
    except (OSError, AttributeError):
        return False
    setter.argtypes = (ctypes.c_int,)
    setter.restype = None
    try:
        setter(threads)
    except (OSError, ValueError):
        return False
    return True
