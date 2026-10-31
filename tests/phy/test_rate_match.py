from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.phy.codec import map_symbols
from ofdm_link.phy.mcs_table import v5_entries, v5_payload_geometry
from ofdm_link.phy.psdu_codec import RateMatchedTurboCodec
from ofdm_link.phy.rate_match import (
    SUBBLOCK_PERMUTATION,
    RateMatchError,
    rate_dematch,
    rate_match,
    rate_match_plan,
    subblock_interleaver_pattern,
)
from ofdm_link.phy.soft import soft_demap_symbols
from ofdm_link.phy.turbo import (
    NativeTurboFecAdapter,
    _rsc_encode,
    qpp_permutation,
    turbo_segmentation_plan,
)

_ALL_V5 = v5_entries(include_hidden=True)


def _native_encode(information_bits: np.ndarray) -> np.ndarray:
    adapter = NativeTurboFecAdapter(max_information_bits=1 << 20)
    return adapter.encode(information_bits)


def _filler_positions(plan) -> set[int]:
    filler = plan.segmentation.filler_bit_count
    return {3 * k + stream for k in range(filler) for stream in (0, 1)}


def test_permutation_is_the_ts36212_inter_column_table() -> None:
    assert len(SUBBLOCK_PERMUTATION) == 32
    assert sorted(SUBBLOCK_PERMUTATION) == list(range(32))
    assert SUBBLOCK_PERMUTATION[:8] == (0, 16, 8, 24, 4, 20, 12, 28)
    assert SUBBLOCK_PERMUTATION[-4:] == (7, 23, 15, 31)


def test_native_coded_order_is_the_lte_d_streams_including_the_tail_table() -> None:
    """``d(i)_k = c[3k + i]``, checked against the clause 5.1.3.2.2 tail table."""

    rng = np.random.default_rng(5)
    block = rng.integers(0, 2, 40, dtype=np.uint8)
    native = _native_encode(block)
    z, x_tail, z_tail = _rsc_encode(block)
    z2, x2_tail, z2_tail = _rsc_encode(block[qpp_permutation(40)])
    d = native.reshape(44, 3).T

    np.testing.assert_array_equal(d[0, :40], block)
    np.testing.assert_array_equal(d[1, :40], z)
    np.testing.assert_array_equal(d[2, :40], z2)
    # Rows: d(0), d(1), d(2); columns k = K .. K+3.
    expected_tail = np.array(
        [
            [x_tail[0], z_tail[1], x2_tail[0], z2_tail[1]],
            [z_tail[0], x_tail[2], z2_tail[0], x2_tail[2]],
            [x_tail[1], z_tail[2], x2_tail[1], z2_tail[2]],
        ]
    )
    np.testing.assert_array_equal(d[:, 40:], expected_tail)


def test_subblock_interleaver_matches_a_hand_worked_d44_case() -> None:
    """D = 44: R_sb = 2, K_Pi = 64, N_D = 20 dummies; worked by hand from 5.1.4.1.1."""

    systematic = subblock_interleaver_pattern(44, 0)
    second_parity = subblock_interleaver_pattern(44, 2)

    assert systematic.size == second_parity.size == 64
    # v_k = y[row * 32 + P[col]] with col = k // 2, row = k % 2; y[i] = d[i - 20].
    assert systematic[:8].tolist() == [-1, 12, -1, 28, -1, 20, 4, 36]
    assert systematic[-2:].tolist() == [11, 43]  # P[31] = 31
    # pi(k) = (P[k // 2] + 32 (k % 2) + 1) mod 64.
    assert second_parity[:8].tolist() == [-1, 13, -1, 29, -1, 21, 5, 37]
    assert second_parity[-2:].tolist() == [12, -1]  # 31 + 1 = 32 -> d12; 64 mod 64 -> dummy
    for pattern in (systematic, subblock_interleaver_pattern(44, 1), second_parity):
        assert np.count_nonzero(pattern == -1) == 20
        assert sorted(pattern[pattern >= 0].tolist()) == list(range(44))


def test_k0_is_two_rows_into_the_systematic_stream() -> None:
    """K = 6144: R_sb = 193, N_D = 28; w[386] = y[8] is a dummy, w[387] = y[40] = d12."""

    plan = rate_match_plan(6144, 9000, 2)
    (block,) = plan.blocks

    assert block.rows == 193
    assert block.k0 == 2 * block.rows
    assert int(block.read_index[0]) == 3 * 12


@pytest.mark.parametrize("block_count", range(1, 13))
@pytest.mark.parametrize("modulation_order", [2, 4, 6])
def test_per_block_e_follows_the_g_prime_split(block_count: int, modulation_order: int) -> None:
    information_bits = 1000 if block_count == 1 else (block_count - 1) * 6120 + 777
    assert turbo_segmentation_plan(information_bits).block_count == block_count
    symbols = 48 * (information_bits // 48 + 7)
    available = modulation_order * symbols

    plan = rate_match_plan(information_bits, available, modulation_order)
    sizes = [block.e for block in plan.blocks]

    assert sum(sizes) == available
    assert all(size % modulation_order == 0 for size in sizes)
    assert sizes == sorted(sizes)
    assert max(sizes) - min(sizes) <= modulation_order
    assert all(block.k0 == 2 * block.rows for block in plan.blocks)


@pytest.mark.parametrize(
    ("information_bits", "available"),
    [(152, 600), (7576, 22752), (7576, 10176), (20000, 30000), (60000, 80004)],
)
def test_nulls_and_filler_are_never_selected_and_puncturing_never_repeats(
    information_bits: int, available: int
) -> None:
    plan = rate_match_plan(information_bits, available, 2)
    filler = _filler_positions(plan)
    zero_llrs = rate_dematch(np.ones(available, dtype=np.float32), plan) == 0.0

    offset = 0
    for block_index, block in enumerate(plan.blocks):
        indices = block.read_index.astype(np.int64)
        assert indices.size == block.e
        assert indices.min() >= 0 and indices.max() < block.native_bit_count
        filler_count = 2 * plan.segmentation.filler_bit_count if block_index == 0 else 0
        if block_index == 0:
            assert not filler.intersection(indices.tolist())
        selectable = block.native_bit_count - filler_count
        punctured = int(np.count_nonzero(zero_llrs[offset : offset + block.native_bit_count]))
        if block.e <= selectable:
            # Puncturing: each bit at most once, the remaining selectable bits get no LLR.
            assert np.unique(indices).size == block.e
            assert punctured == selectable - block.e
        else:
            assert punctured == 0
        offset += block.native_bit_count


@pytest.mark.parametrize("information_bits", [32, 152, 7576, 20000])
def test_low_rates_send_every_non_null_bit_at_least_once(information_bits: int) -> None:
    segmentation = turbo_segmentation_plan(information_bits)
    available = 2 * (segmentation.coded_bit_count + 600)
    plan = rate_match_plan(information_bits, available, 2)
    filler = _filler_positions(plan)

    for block_index, block in enumerate(plan.blocks):
        expected = set(range(block.native_bit_count))
        if block_index == 0:
            expected -= filler
        assert set(block.read_index.tolist()) == expected


@pytest.mark.parametrize(
    ("information_bits", "available"), [(152, 300), (7576, 30000), (7576, 12000)]
)
def test_rate_dematch_inverts_rate_match_and_combines_repeats(
    information_bits: int, available: int
) -> None:
    rng = np.random.default_rng(information_bits)
    native = _native_encode(rng.integers(0, 2, information_bits, dtype=np.uint8))
    plan = rate_match_plan(information_bits, available, 2)
    sent = rate_match(native, plan)

    assert sent.size == available
    native_llrs = rate_dematch(2.0 * (1.0 - 2.0 * sent.astype(np.float64)), plan, clip=50.0)
    assert native_llrs.dtype == np.float32 and native_llrs.size == native.size

    offset = 0
    filler = _filler_positions(plan)
    for block_index, block in enumerate(plan.blocks):
        values = native_llrs[offset : offset + block.native_bit_count]
        bits = native[offset : offset + block.native_bit_count]
        counts = np.bincount(block.read_index, minlength=block.native_bit_count)
        selected = counts > 0
        np.testing.assert_array_equal((values[selected] < 0).astype(np.uint8), bits[selected])
        np.testing.assert_allclose(
            np.abs(values[selected]), np.minimum(2.0 * counts[selected], 50.0)
        )
        unselected = np.flatnonzero(~selected)
        known = [index for index in unselected if block_index == 0 and index in filler]
        assert np.all(values[known] == 50.0)
        assert np.all(values[[index for index in unselected if index not in known]] == 0.0)
        offset += block.native_bit_count


def test_rate_matching_rejects_inconsistent_geometry() -> None:
    with pytest.raises(RateMatchError, match="multiple of modulation_order"):
        rate_match_plan(1000, 1001, 2)
    with pytest.raises(RateMatchError, match="modulation_order"):
        rate_match_plan(1000, 1200, 3)
    with pytest.raises(RateMatchError, match="integer"):
        rate_match_plan(1000.0, 1200, 2)
    plan = rate_match_plan(1000, 1200, 2)
    with pytest.raises(RateMatchError, match="native coded input"):
        rate_match(np.zeros(10, dtype=np.uint8), plan)
    with pytest.raises(RateMatchError, match="expected 1200"):
        rate_dematch(np.zeros(1199, dtype=np.float32), plan)


@pytest.mark.parametrize("entry", _ALL_V5, ids=lambda entry: f"mcs{entry.index}")
@pytest.mark.parametrize("psdu_bytes", [19, 100, 767, 768, 769, 947, 2000, 7555, 8192])
def test_noiseless_round_trip_for_every_v5_entry(entry, psdu_bytes: int) -> None:
    codec = RateMatchedTurboCodec()
    geometry = v5_payload_geometry(entry, psdu_bytes)
    rng = np.random.default_rng(psdu_bytes + 1000 * entry.index)
    body = rng.integers(0, 256, psdu_bytes - 4, dtype=np.uint8).tobytes()

    bits = codec.encode(body, geometry)
    result = codec.decode(4.0 * (1.0 - 2.0 * bits.astype(np.float32)), geometry)

    assert bits.size == geometry.available_bits
    assert result.fcs_ok
    assert result.body == body
    assert all(result.block_crc_ok)


# Ideal-CSI AWGN Es/N0 about 0.5-1 dB above each entry's measured 1 % BLER
# point for a 900-byte PSDU (see the WP-B sweep); one seeded burst each.
_SMOKE_SNR_DB = {
    8: 0.0, 9: 2.5, 10: 4.5, 11: 5.0, 12: 8.0, 13: 10.5, 14: 11.5, 15: 12.5, 16: 15.5, 17: 16.5,
}


@pytest.mark.parametrize("entry", _ALL_V5, ids=lambda entry: f"mcs{entry.index}")
def test_awgn_decode_succeeds_above_the_waterfall(entry) -> None:
    codec = RateMatchedTurboCodec()
    geometry = v5_payload_geometry(entry, 900)
    rng = np.random.default_rng(entry.index)
    variance = 10.0 ** (-_SMOKE_SNR_DB[entry.index] / 10.0)
    body = rng.integers(0, 256, 896, dtype=np.uint8).tobytes()

    symbols = map_symbols(codec.encode(body, geometry), entry.modulation)
    noise = rng.normal(scale=np.sqrt(variance / 2.0), size=(symbols.size, 2)) @ np.array([1.0, 1j])
    llrs = soft_demap_symbols(symbols + noise, entry.modulation, variance).llrs
    result = codec.decode(llrs, geometry)

    assert result.fcs_ok
    assert result.body == body
