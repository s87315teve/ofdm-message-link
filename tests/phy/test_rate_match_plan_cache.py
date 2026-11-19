"""Rate-match plans are built once per distinct code block, and stay bit-exact.

A burst is one PSDU, so the first burst of a never-seen shape builds a whole
plan.  A plan holds at most a few distinct ``(block_size, filler, e, rv)``
tuples however many code blocks it has, and the sub-block interleaver pattern
depends only on ``(D, stream)``; building them once per tuple is what keeps
that first burst on time.  The reference below is the per-block construction
as it was before the caches, kept here verbatim so the cached plans are held to
it bit for bit.
"""

from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.phy import rate_match as rm
from ofdm_link.phy.rate_match import (
    BlockRateMatch,
    RateMatchError,
    rate_match,
    rate_match_plan,
    subblock_interleaver_pattern,
)
from ofdm_link.phy.turbo import turbo_segmentation_plan


def _reference_pattern(stream_length: int, stream: int) -> np.ndarray:
    rows = -(-stream_length // 32)
    k_pi = rows * 32
    dummy = k_pi - stream_length
    permutation = np.asarray(rm.SUBBLOCK_PERMUTATION, dtype=np.int64)
    output = np.arange(k_pi, dtype=np.int64)
    column = output // rows
    row = output % rows
    if stream < 2:
        y_index = row * 32 + permutation[column]
    else:
        y_index = (permutation[column] + 32 * row + 1) % k_pi
    return np.where(y_index < dummy, -1, y_index - dummy)


def _reference_block(block_size: int, filler: int, e: int, rv: int):
    stream_length = block_size + 4
    rows = -(-stream_length // 32)
    k_pi = rows * 32
    streams = []
    for stream in range(3):
        source = _reference_pattern(stream_length, stream)
        native = np.where(source == -1, -1, 3 * source + stream)
        if stream < 2 and filler:
            native = np.where((source >= 0) & (source < filler), -1, native)
        streams.append(native)
    buffer = np.empty(3 * k_pi, dtype=np.int64)
    buffer[:k_pi] = streams[0]
    buffer[k_pi::2] = streams[1]
    buffer[k_pi + 1 :: 2] = streams[2]
    k0 = rows * (2 * -(-buffer.size // (8 * rows)) * rv + 2)
    ordered = np.roll(buffer, -k0)
    selectable = ordered[ordered != -1]
    read_index = selectable[np.arange(e) % selectable.size].astype(np.int32)
    return block_size, rows, e, k0, read_index


def _reference_plan(b: int, g: int, qm: int, rv: int):
    segmentation = turbo_segmentation_plan(b)
    count = segmentation.block_count
    symbols = g // qm
    gamma = symbols % count
    blocks = []
    for index, size in enumerate(segmentation.block_sizes):
        if index <= count - gamma - 1:
            e = qm * (symbols // count)
        else:
            e = qm * -(-symbols // count)
        filler = segmentation.filler_bit_count if index == 0 else 0
        blocks.append(_reference_block(size, filler, e, rv))
    return segmentation, blocks


def _clear_caches() -> None:
    rm._rate_match_plan.cache_clear()
    for name in ("_block_rate_match", "_subblock_interleaver_pattern"):
        cached = getattr(rm, name, None)
        if hasattr(cached, "cache_clear"):
            cached.cache_clear()
    rm._GLOBAL_INDEX.clear()


@pytest.fixture(autouse=True)
def _fresh_caches():
    _clear_caches()
    yield
    _clear_caches()


# B with and without filler, one block, two blocks, two block sizes (K+ and
# K-), and the 26-block PSDU of a 13-subframe burst.
_INFORMATION_BITS = [32, 40, 152, 1000, 6144, 6145, 7576, 20000, 60000, 26 * 6120]


def _available_bits(b: int, qm: int, ratio: float, extra_symbols: int) -> int:
    coded = turbo_segmentation_plan(b).coded_bit_count
    return qm * (max(1, -(-int(coded * ratio) // qm)) + extra_symbols)


@pytest.mark.parametrize("information_bits", _INFORMATION_BITS)
@pytest.mark.parametrize("qm", [1, 2, 4, 6, 8])
def test_cached_plans_are_bit_identical_to_the_per_block_reference(
    information_bits: int, qm: int
) -> None:
    segmentation = turbo_segmentation_plan(information_bits)
    count = segmentation.block_count
    geometries = [
        _available_bits(information_bits, qm, 1.7, 1),  # repetition
        _available_bits(information_bits, qm, 0.6, 0),  # puncturing
        _available_bits(information_bits, qm, 0.6, count // 2 + 1),  # gamma split
        qm * (count // 2 + 1),  # so few bits that some blocks get E = 0
    ]
    for available in geometries:
        for rv in range(4):
            plan = rate_match_plan(information_bits, available, qm, rv)
            ref_segmentation, ref_blocks = _reference_plan(information_bits, available, qm, rv)
            assert plan.segmentation == ref_segmentation
            assert plan.available_bits == available
            assert plan.modulation_order == qm
            assert len(plan.blocks) == len(ref_blocks)
            for block, (size, rows, e, k0, read_index) in zip(plan.blocks, ref_blocks, strict=True):
                assert (block.block_size, block.rows, block.e, block.k0) == (size, rows, e, k0)
                assert block.read_index.dtype == np.int32
                np.testing.assert_array_equal(block.read_index, read_index)
                assert not block.read_index.flags.writeable


def test_the_gamma_split_and_filler_cases_are_actually_covered() -> None:
    """Guard the sweep above: it must see two E values, two K values and filler."""

    # 6145 bits: K- = 3072 and K+ = 3136 with 15 filler bits; an odd symbol
    # count over two blocks gives the second one E + qm.
    plan = rate_match_plan(6145, 2 * (2 * 3000 + 1), 2)
    assert len({block.e for block in plan.blocks}) == 2
    assert len({block.block_size for block in plan.blocks}) == 2
    assert plan.segmentation.filler_bit_count > 0


def test_a_26_block_plan_builds_each_distinct_block_once(monkeypatch) -> None:
    built: list[tuple[int, int, int, int]] = []

    def counting(block_size, rows, e, k0, read_index):
        built.append((block_size, rows, e, k0))
        return BlockRateMatch(block_size, rows, e, k0, read_index)

    monkeypatch.setattr(rm, "BlockRateMatch", counting)
    information_bits = 26 * 6120
    # 26 n + 3 symbols: 23 blocks get n symbols, 3 get n + 1.
    available = 6 * (26 * 400 + 3)
    plan = rate_match_plan(information_bits, available, 6)

    assert len(plan.blocks) == 26
    assert len(built) == len({(b.block_size, b.e) for b in plan.blocks}) == 2
    assert len({id(block) for block in plan.blocks}) == 2


def test_plans_of_different_shapes_share_their_common_blocks() -> None:
    information_bits = 26 * 6120
    one = rate_match_plan(information_bits, 6 * (26 * 400 + 1), 6)
    two = rate_match_plan(information_bits, 6 * (26 * 400 + 2), 6)

    assert one is not two
    assert one.blocks[0] is two.blocks[0]
    assert one.blocks[-1] is two.blocks[-1]
    assert one.blocks[-2] is not two.blocks[-2]


def test_the_interleaver_pattern_is_cached_and_read_only() -> None:
    first = subblock_interleaver_pattern(6148, 2)

    assert subblock_interleaver_pattern(6148, 2) is first
    assert first.dtype == np.int64
    assert not first.flags.writeable
    with pytest.raises(ValueError):
        first[0] = 0
    np.testing.assert_array_equal(first, _reference_pattern(6148, 2))
    second = subblock_interleaver_pattern(6148, 1)
    np.testing.assert_array_equal(second, _reference_pattern(6148, 1))


def test_validation_is_not_bypassed_by_a_cached_equal_key() -> None:
    subblock_interleaver_pattern(44, 0)
    rate_match_plan(1000, 1200, 2)

    with pytest.raises(RateMatchError, match="positive integer"):
        subblock_interleaver_pattern(44.0, 0)
    with pytest.raises(RateMatchError, match="positive integer"):
        subblock_interleaver_pattern(True, 0)
    with pytest.raises(RateMatchError, match="stream"):
        subblock_interleaver_pattern(44, 3)
    with pytest.raises(RateMatchError, match="integer"):
        rate_match_plan(1000, 1200.0, 2)


def test_the_global_read_index_is_the_offset_concatenation_and_read_only() -> None:
    information_bits = 7576
    plan = rate_match_plan(information_bits, _available_bits(information_bits, 2, 0.6, 1), 2)
    index = rm._global_read_index(plan)

    offsets = np.cumsum([0] + [block.native_bit_count for block in plan.blocks[:-1]])
    expected = np.concatenate(
        [
            block.read_index.astype(np.int64) + offset
            for block, offset in zip(plan.blocks, offsets, strict=True)
        ]
    )
    assert index.dtype == np.int32
    np.testing.assert_array_equal(index, expected)
    assert not index.flags.writeable
    assert rm._global_read_index(plan) is index

    coded = np.random.default_rng(1).integers(0, 2, plan.native_bit_count, dtype=np.uint8)
    np.testing.assert_array_equal(rate_match(coded, plan), coded[expected])
