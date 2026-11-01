"""Keep freed sample buffers in the process instead of handing them back.

glibc serves an allocation above its mmap threshold (128 KiB to start) with a
fresh ``mmap`` and returns it with ``munmap`` when freed; below it, freed heap
above the trim threshold is given back with ``sbrk``.  Either way the next
allocation of that size faults its pages in again, zero-filled by the kernel.
The receive path builds and frees many NumPy temporaries of exactly that size
for every sample chunk -- a 120-candidate correlation is 150 KiB of complex128
-- so the owner thread of the receiver spent a large part of its time in page
faults: one such multiply took 68 us against 8 us once the memory was kept.

Raising both thresholds keeps freed memory in the heap for reuse.  The cost is
that a process holds on to its peak heap; the bounded receive and decode
processes this is meant for have a small, steady one.

Like the CPU budget, this is process-wide, so only a process's entry point
calls it -- never an import.
"""

from __future__ import annotations

import ctypes
import ctypes.util

# glibc <malloc.h>
_M_TRIM_THRESHOLD = -1
_M_MMAP_THRESHOLD = -3
# The mmap threshold may not exceed HEAP_MAX_SIZE / 2 (32 MiB on 64-bit);
# a single allocation larger than this still gets its own mapping.
MMAP_THRESHOLD_BYTES = 16 * 1024 * 1024
TRIM_THRESHOLD_BYTES = 64 * 1024 * 1024


def keep_freed_memory() -> bool:
    """Stop glibc returning freed buffers to the kernel; whether it applied.

    A no-op returning ``False`` where the C library has no ``mallopt`` (not
    glibc) or refuses the setting: this is an efficiency measure only.
    """

    name = ctypes.util.find_library("c")
    if name is None:
        return False
    try:
        mallopt = ctypes.CDLL(name).mallopt
    except (OSError, AttributeError):
        return False
    mallopt.argtypes = (ctypes.c_int, ctypes.c_int)
    mallopt.restype = ctypes.c_int
    # mallopt returns 1 on success and 0 on error.
    return bool(mallopt(_M_MMAP_THRESHOLD, MMAP_THRESHOLD_BYTES)) and bool(
        mallopt(_M_TRIM_THRESHOLD, TRIM_THRESHOLD_BYTES)
    )
