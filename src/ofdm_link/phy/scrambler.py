"""IEEE 802.11 data scrambler, ``S(x) = x^7 + x^4 + 1``, for wire v5.

The scrambler exists only so that constellation statistics, PAPR and -- after
the transmitter's per-burst peak normalization -- mean TX power do not depend
on what the payload says.  Any whitening sequence does that; the 802.11 one is
chosen because its period, 127, is prime and so never lines up with a whole
number of OFDM symbols (96/192/288 coded bits), and because every non-zero
seed is a cyclic shift of one m-sequence.  That makes the whole scrambler a
128-row lookup table and one ``np.resize``: no per-bit loop in Python, unlike
the LTE Gold sequence with its 1600-step warm-up, which buys nothing more for
whitening.

The state is a 7-bit integer whose bit 6 is the ``x^7`` cell and bit 3 the
``x^4`` cell.  Per output bit ``out = ((state >> 6) ^ (state >> 3)) & 1`` and
``state = ((state << 1) | out) & 0x7F``; the first output follows the seed.
Seed 0 is the all-zero state, whose sequence is all zeros: scrambling is then
the identity, which is how the experiment-only "scrambler off" setting is
signalled without a separate header flag.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from numpy.typing import ArrayLike, NDArray

SCRAMBLER_PERIOD = 127
SCRAMBLER_SEED_COUNT = 128


class ScramblerError(ValueError):
    """Raised for a seed outside the 7-bit range or malformed bits."""


@lru_cache(maxsize=1)
def _sequence_table() -> NDArray[np.uint8]:
    """One period of the scrambler output for every 7-bit seed.

    Built once by running the LFSR itself, so the table is the definition
    and not a hand-copied constant.  Row ``seed`` holds the first 127 output
    bits for that initial state.
    """

    table = np.zeros((SCRAMBLER_SEED_COUNT, SCRAMBLER_PERIOD), dtype=np.uint8)
    for seed in range(1, SCRAMBLER_SEED_COUNT):
        state = seed
        for index in range(SCRAMBLER_PERIOD):
            out = ((state >> 6) ^ (state >> 3)) & 1
            state = ((state << 1) | out) & 0x7F
            table[seed, index] = out
    table.setflags(write=False)
    return table


def scrambler_sequence(seed: int, length: int) -> NDArray[np.uint8]:
    """Return ``length`` scrambler bits for ``seed``; seed 0 gives zeros."""

    _require_seed(seed)
    if type(length) is not int or length < 0:
        raise ScramblerError("length must be a non-negative integer")
    if seed == 0:
        return np.zeros(length, dtype=np.uint8)
    return np.resize(_sequence_table()[seed], length)


def scramble_bits(bits: ArrayLike, seed: int) -> NDArray[np.uint8]:
    """XOR binary ``bits`` with the scrambler sequence; also descrambles bits."""

    values = np.asarray(bits)
    if values.ndim != 1:
        raise ScramblerError("bits must be one-dimensional")
    if not (np.issubdtype(values.dtype, np.integer) or values.dtype == np.bool_):
        raise ScramblerError("bits must be integer or boolean")
    if np.any((values != 0) & (values != 1)):
        raise ScramblerError("bits must contain only 0 and 1")
    return np.bitwise_xor(
        values.astype(np.uint8, copy=False),
        scrambler_sequence(seed, int(values.size)),
    )


def descramble_llrs(llrs: ArrayLike, seed: int) -> NDArray[np.float32]:
    """Undo the scrambler on soft values: a scrambler 1 flips the LLR sign.

    The project's LLRs are positive for bit 0, and XOR with 1 swaps the
    bit's two hypotheses, so descrambling is ``llr * (1 - 2 s)``.
    """

    values = np.asarray(llrs, dtype=np.float32)
    if values.ndim != 1:
        raise ScramblerError("llrs must be one-dimensional")
    if seed == 0:
        _require_seed(seed)
        return values.copy()
    signs = 1.0 - 2.0 * scrambler_sequence(seed, int(values.size)).astype(np.float32)
    return values * signs


def _require_seed(seed: object) -> None:
    if type(seed) is not int or not 0 <= seed < SCRAMBLER_SEED_COUNT:
        raise ScramblerError("scrambler seed must be an integer in [0, 127]")
