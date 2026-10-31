"""LTE Turbo rate matching (TS 36.212 clause 5.1.4.1) around the native coder.

Wire v5 fills an integer number of OFDM symbols exactly, whatever the PSDU
length and the nominal code rate, by puncturing or repeating the rate-1/3
Turbo output with LTE's circular buffer.  The Turbo coder itself is unchanged:
its native output order already *is* the clause 5.1.3.2 ``d(0) d(1) d(2)``
interleave.  Its body is time-major ``x_k z_k z'_k`` and its twelve tail bits
are ``x_K z_K x_K+1 z_K+1 x_K+2 z_K+2 x'_K z'_K x'_K+1 z'_K+1 x'_K+2 z'_K+2``,
which is exactly the clause 5.1.3.2.2 assignment read three at a time, so
``d(i)_k = c[3k + i]`` for every ``k < K + 4``.

Everything here is a pure index computation: per code block a precomputed
``read_index`` maps each transmitted bit to its position in the native coded
block.  Transmit gathers through it; receive scatters LLRs back with
``np.bincount``, which sums repeated positions and leaves punctured ones at
zero, so the native decoder needs no change either.

Simplifications against the full clause, all deliberate: only the RV that
starts at ``k0 = 2 R_sb`` matters on the wire (no HARQ), ``N_cb = K_w`` (no
soft-buffer limit, as for the uplink), and ``E`` is split over code blocks with
the clause's ``G' / C`` rule using the payload modulation order and one layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .turbo import TurboSegmentationPlan, turbo_segmentation_plan

SUBBLOCK_COLUMNS = 32

# TS 36.212 table 5.1.4-1, inter-column permutation of the sub-block interleaver.
SUBBLOCK_PERMUTATION: tuple[int, ...] = (
    0, 16, 8, 24, 4, 20, 12, 28, 2, 18, 10, 26, 6, 22, 14, 30,
    1, 17, 9, 25, 5, 21, 13, 29, 3, 19, 11, 27, 7, 23, 15, 31,
)

_NULL = -1


class RateMatchError(ValueError):
    """Raised when a rate-matching geometry or input is not consistent."""


@dataclass(frozen=True, slots=True)
class BlockRateMatch:
    """Selection of one code block's ``E`` transmitted bits.

    ``read_index[j]`` is the position, in this block's native coded output of
    length ``3 (K + 4)``, of the ``j``-th rate-matched bit.
    """

    block_size: int
    rows: int
    e: int
    k0: int
    read_index: NDArray[np.int32]

    @property
    def native_bit_count(self) -> int:
        return 3 * (self.block_size + 4)

    @property
    def circular_buffer_bits(self) -> int:
        """``K_w = 3 K_Pi``, the circular buffer length including NULLs."""

        return 3 * SUBBLOCK_COLUMNS * self.rows


@dataclass(frozen=True, slots=True)
class RateMatchPlan:
    """Rate matching of one whole Turbo frame onto ``available_bits`` (``G``)."""

    segmentation: TurboSegmentationPlan
    available_bits: int
    modulation_order: int
    blocks: tuple[BlockRateMatch, ...]

    @property
    def native_bit_count(self) -> int:
        return self.segmentation.coded_bit_count


def subblock_interleaver_pattern(stream_length: int, stream: int) -> NDArray[np.int64]:
    """Return, per interleaver output position, the source ``d`` index or ``-1``.

    ``stream_length`` is ``D = K + 4``.  ``-1`` marks the ``N_D`` dummy bits
    prepended to fill the ``R_sb x 32`` matrix.  Streams 0 and 1 are written
    row by row, column-permuted and read column by column (clause
    5.1.4.1.1); stream 2 uses ``pi(k) = (P[k // R_sb] + 32 (k mod R_sb) + 1)
    mod K_Pi``, which offsets the second parity by one position so the two
    parity streams do not puncture the same trellis steps.
    """

    if type(stream_length) is not int or stream_length <= 0:
        raise RateMatchError("stream_length must be a positive integer")
    if stream not in (0, 1, 2):
        raise RateMatchError("stream must be 0, 1, or 2")
    rows = -(-stream_length // SUBBLOCK_COLUMNS)
    k_pi = rows * SUBBLOCK_COLUMNS
    dummy = k_pi - stream_length
    permutation = np.asarray(SUBBLOCK_PERMUTATION, dtype=np.int64)
    output = np.arange(k_pi, dtype=np.int64)
    column = output // rows
    row = output % rows
    if stream < 2:
        y_index = row * SUBBLOCK_COLUMNS + permutation[column]
    else:
        y_index = (permutation[column] + SUBBLOCK_COLUMNS * row + 1) % k_pi
    return np.where(y_index < dummy, _NULL, y_index - dummy)


def rate_match_plan(
    information_bit_count: int,
    available_bits: int,
    modulation_order: int,
    rv: int = 0,
) -> RateMatchPlan:
    """Return the per-block selection for ``B`` information bits onto ``G`` bits.

    ``available_bits`` must be a multiple of ``modulation_order`` so that every
    code block's ``E_r`` is whole constellation symbols, as in LTE.  Results are
    cached: a link repeats few PSDU sizes per MCS.  The cache is kept small
    because a plan holds one int32 per transmitted bit (up to ~1 MB for the
    largest aggregated burst).
    """

    for name, value in (
        ("information_bit_count", information_bit_count),
        ("available_bits", available_bits),
        ("modulation_order", modulation_order),
        ("rv", rv),
    ):
        if type(value) is not int:
            raise RateMatchError(f"{name} must be an integer")
    if modulation_order not in (1, 2, 4, 6, 8):
        raise RateMatchError("modulation_order must be 1, 2, 4, 6, or 8")
    if available_bits <= 0 or available_bits % modulation_order:
        raise RateMatchError(
            "available_bits must be a positive multiple of modulation_order"
        )
    if rv not in (0, 1, 2, 3):
        raise RateMatchError("rv must be 0, 1, 2, or 3")
    return _rate_match_plan(information_bit_count, available_bits, modulation_order, rv)


@lru_cache(maxsize=64)
def _rate_match_plan(b: int, g: int, qm: int, rv: int) -> RateMatchPlan:
    segmentation = turbo_segmentation_plan(b)
    block_count = segmentation.block_count
    symbols = g // qm
    gamma = symbols % block_count
    blocks: list[BlockRateMatch] = []
    for block_index, block_size in enumerate(segmentation.block_sizes):
        if block_index <= block_count - gamma - 1:
            e = qm * (symbols // block_count)
        else:
            e = qm * -(-symbols // block_count)
        filler = segmentation.filler_bit_count if block_index == 0 else 0
        blocks.append(_block_rate_match(block_size, filler, e, rv))
    if sum(block.e for block in blocks) != g:
        raise RateMatchError("per-block E does not sum to the available bits")
    return RateMatchPlan(segmentation, g, qm, tuple(blocks))


def _block_rate_match(block_size: int, filler: int, e: int, rv: int) -> BlockRateMatch:
    stream_length = block_size + 4
    rows = -(-stream_length // SUBBLOCK_COLUMNS)
    k_pi = rows * SUBBLOCK_COLUMNS
    # Native position of every circular-buffer entry, NULL where the clause has
    # a dummy or a filler bit.  Filler occupies d(0) and d(1) for k < F only:
    # d(2) is the second encoder's parity over the QPP-interleaved block and
    # carries information.
    streams = []
    for stream in range(3):
        source = subblock_interleaver_pattern(stream_length, stream)
        native = np.where(source == _NULL, _NULL, 3 * source + stream)
        if stream < 2 and filler:
            native = np.where((source >= 0) & (source < filler), _NULL, native)
        streams.append(native)
    buffer = np.empty(3 * k_pi, dtype=np.int64)
    buffer[:k_pi] = streams[0]
    buffer[k_pi::2] = streams[1]
    buffer[k_pi + 1 :: 2] = streams[2]

    circular_length = buffer.size
    k0 = rows * (2 * -(-circular_length // (8 * rows)) * rv + 2)
    ordered = np.roll(buffer, -k0)
    selectable = ordered[ordered != _NULL]
    if e and not selectable.size:
        raise RateMatchError("code block has no selectable bits")
    read_index = selectable[np.arange(e) % selectable.size].astype(np.int32)
    read_index.setflags(write=False)
    return BlockRateMatch(block_size, rows, e, k0, read_index)


def rate_match(native_coded: ArrayLike, plan: RateMatchPlan) -> NDArray[np.uint8]:
    """Select the ``G`` transmitted bits from the native Turbo output, in order."""

    if not isinstance(plan, RateMatchPlan):
        raise TypeError("plan must be a RateMatchPlan")
    coded = np.asarray(native_coded)
    if coded.ndim != 1 or coded.size != plan.native_bit_count:
        raise RateMatchError(
            f"native coded input must hold {plan.native_bit_count} bits"
        )
    pieces = []
    offset = 0
    for block in plan.blocks:
        pieces.append(coded[offset : offset + block.native_bit_count][block.read_index])
        offset += block.native_bit_count
    result = np.concatenate(pieces).astype(np.uint8, copy=False)
    result.setflags(write=False)
    return result


def rate_dematch(
    llrs: ArrayLike,
    plan: RateMatchPlan,
    *,
    clip: float = 64.0,
) -> NDArray[np.float32]:
    """Return native-order LLRs for the unchanged Turbo decoder.

    Repeated positions are summed (maximum-ratio combining of independent
    observations), punctured positions stay 0 (no information), and filler
    positions get ``+clip`` because they are known zeros: the systematic bit
    is filler and the first parity bit of a zero-state encoder fed zeros is
    zero too.  The result is clipped to ``+-clip`` like every other LLR the
    decoder sees.
    """

    if not isinstance(plan, RateMatchPlan):
        raise TypeError("plan must be a RateMatchPlan")
    values = np.asarray(llrs)
    if values.ndim != 1 or not np.issubdtype(values.dtype, np.number):
        raise RateMatchError("llrs must be a numeric one-dimensional array")
    if values.size != plan.available_bits:
        raise RateMatchError(
            f"llrs contain {values.size} values; expected {plan.available_bits}"
        )
    if not (np.isfinite(clip) and clip > 0.0):
        raise RateMatchError("clip must be positive and finite")
    weights = values.astype(np.float64, copy=False)
    pieces = []
    offset = 0
    for block_index, block in enumerate(plan.blocks):
        combined = np.bincount(
            block.read_index,
            weights=weights[offset : offset + block.e],
            minlength=block.native_bit_count,
        )
        offset += block.e
        filler = plan.segmentation.filler_bit_count if block_index == 0 else 0
        if filler:
            combined[0 : 3 * filler : 3] = clip
            combined[1 : 3 * filler : 3] = clip
        pieces.append(combined)
    result = np.clip(np.concatenate(pieces), -clip, clip).astype(np.float32)
    return result
