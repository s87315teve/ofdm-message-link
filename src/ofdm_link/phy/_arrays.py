"""Shared array predicates for the receive hot path.

These helpers exist so the sample-validation the decoder repeats for every
burst costs one contiguous pass instead of several strided ones.  They hold
no configuration and no state.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

_COMPONENT_DTYPE = {
    np.dtype(np.complex64): np.float32,
    np.dtype(np.complex128): np.float64,
}


def all_finite(values: NDArray[np.generic]) -> bool:
    """Return whether every real or imaginary component is finite.

    ``values.real`` and ``values.imag`` are strided views of a complex array,
    so testing them separately walks the buffer twice without ever reading a
    contiguous cache line.  A contiguous complex buffer viewed as its
    component type gives the same answer in one pass.  Anything else falls
    back to ``isfinite`` on the array itself, which is already defined
    component-wise for complex input.
    """

    component = _COMPONENT_DTYPE.get(values.dtype)
    if component is not None and values.flags.c_contiguous:
        values = values.view(component)
    return bool(np.isfinite(values).all())
