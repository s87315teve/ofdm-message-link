from __future__ import annotations

import struct
import zlib
from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from ofdm_link.phy.turbo import (
    QPP_PARAMETERS,
    TurboError,
    crc24b,
    crc24b_ok,
    desegment_information,
    qpp_permutation,
    segment_information,
    turbo_coded_bit_count,
    turbo_decode_logmap_oracle,
    turbo_encode_oracle,
    turbo_segmentation_plan,
)


def test_crc24b_known_answer_uses_msb_first_zero_initial_state() -> None:
    bits = np.unpackbits(np.frombuffer(b"123456789", dtype=np.uint8), bitorder="big")

    parity = crc24b(bits)

    assert np.packbits(parity, bitorder="big").tobytes().hex() == "23ef52"
    assert crc24b_ok(np.concatenate((bits, parity)))
    corrupted = np.concatenate((bits, parity)).copy()
    corrupted[17] ^= 1
    assert not crc24b_ok(corrupted)


def test_qpp_table_has_all_188_legal_unique_permutations_and_inverses() -> None:
    assert len(QPP_PARAMETERS) == 188
    assert QPP_PARAMETERS[0] == (40, 3, 10)
    assert QPP_PARAMETERS[-1] == (6144, 263, 480)

    for block_size, _, _ in QPP_PARAMETERS:
        permutation = qpp_permutation(block_size)
        inverse = np.argsort(permutation)
        np.testing.assert_array_equal(permutation[inverse], np.arange(block_size))
        assert not permutation.flags.writeable


@pytest.mark.parametrize(
    ("information_bits", "block_sizes", "filler_bits", "coded_bits"),
    [
        (0, (40,), 40, 132),
        (1, (40,), 39, 132),
        (39, (40,), 1, 132),
        (40, (40,), 0, 132),
        (41, (48,), 7, 156),
        (6143, (6144,), 1, 18_444),
        (6144, (6144,), 0, 18_444),
        (6145, (3072, 3136), 15, 18_648),
        (524_368, (6080,) * 30 + (6144,) * 56, 32, 1_580_424),
    ],
)
def test_segmentation_and_exact_coded_length_boundaries(
    information_bits: int,
    block_sizes: tuple[int, ...],
    filler_bits: int,
    coded_bits: int,
) -> None:
    plan = turbo_segmentation_plan(information_bits)

    assert plan.block_sizes == block_sizes
    assert plan.filler_bit_count == filler_bits
    assert plan.coded_bit_count == coded_bits
    assert turbo_coded_bit_count(information_bits) == coded_bits
    assert plan.code_block_crc_bits == (24 if len(block_sizes) > 1 else 0)


@pytest.mark.parametrize("information_bit_count", [0, 8, 88, 512, 6144, 6145, 8056, 10240, 32768])
def test_segmentation_round_trip_keeps_filler_and_crc_internal(
    information_bit_count: int,
) -> None:
    information = (
        (np.arange(information_bit_count, dtype=np.uint32) * 17 + 3) & 1
    ).astype(np.uint8)

    plan, blocks = segment_information(information)
    recovered = desegment_information(blocks, plan)

    np.testing.assert_array_equal(recovered, information)
    assert len(blocks) == plan.block_count
    assert sum(block.size for block in blocks) == sum(plan.block_sizes)
    if plan.filler_bit_count:
        assert not np.any(blocks[0][: plan.filler_bit_count])
    if plan.segmented:
        assert all(crc24b_ok(block) for block in blocks)


def test_desegmentation_rejects_filler_crc_and_block_count_mismatches() -> None:
    information = np.arange(6145, dtype=np.uint8) & 1
    plan, blocks = segment_information(information)

    bad_filler = [block.copy() for block in blocks]
    bad_filler[0][0] = 1
    with pytest.raises(TurboError, match="filler"):
        desegment_information(tuple(bad_filler), plan)

    bad_crc = [block.copy() for block in blocks]
    bad_crc[1][-1] ^= 1
    with pytest.raises(TurboError, match="CRC-24B"):
        desegment_information(tuple(bad_crc), plan)

    with pytest.raises(TurboError, match="block count"):
        desegment_information(blocks[:-1], plan)


def test_turbo_encoder_known_answer_fixes_rsc_qpp_and_tail_order() -> None:
    information = np.zeros(40, dtype=np.uint8)
    information[0] = 1

    encoded = turbo_encode_oracle(information)

    # Independently generated with Sionna 2.1.0's 3GPP QPP Turbo encoder.
    assert encoded.size == 132
    assert np.packbits(encoded, bitorder="big").tobytes().hex() == (
        "edb0186d80c36c061b6030db0186d81c70"
    )


def test_portable_logmap_ideal_decode_uses_outer_crc_early_stop() -> None:
    protected = b"M3 oracle"
    raw = protected + struct.pack(">I", zlib.crc32(protected) & 0xFFFFFFFF)
    information = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="big")
    coded = turbo_encode_oracle(information)
    llrs = np.where(coded == 0, 12.0, -12.0)

    decoded = turbo_decode_logmap_oracle(
        llrs,
        int(information.size),
        max_iterations=8,
    )

    np.testing.assert_array_equal(decoded.information_bits, information)
    assert decoded.report.actual_iterations_per_block == (1,)
    assert decoded.report.early_stop_reasons == ("outer_crc32",)
    assert decoded.report.outer_crc_ok is True


@pytest.mark.parametrize("iterations", [2, 4, 6, 8])
def test_portable_logmap_accepts_committed_iteration_counts(iterations: int) -> None:
    protected = b"iter"
    raw = protected + struct.pack(">I", zlib.crc32(protected) & 0xFFFFFFFF)
    information = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="big")
    coded = turbo_encode_oracle(information)
    llrs = np.where(coded == 0, 8.0, -8.0)

    decoded = turbo_decode_logmap_oracle(
        llrs,
        int(information.size),
        max_iterations=iterations,
    )

    np.testing.assert_array_equal(decoded.information_bits, information)
    assert decoded.report.configured_max_iterations == iterations


@pytest.mark.parametrize(
    "bad",
    [np.array([np.nan] * 132), np.array([np.inf] * 132), np.zeros((1, 132))],
)
def test_portable_logmap_rejects_nonfinite_or_wrong_shape(bad: np.ndarray) -> None:
    with pytest.raises(TurboError):
        turbo_decode_logmap_oracle(bad, 40, max_iterations=2)


@pytest.mark.parametrize("iterations", [0, 1, 3, 5, 7, 9, True])
def test_portable_logmap_rejects_invalid_iteration_count(iterations: object) -> None:
    with pytest.raises(TurboError, match="2, 4, 6, or 8"):
        turbo_decode_logmap_oracle(
            np.zeros(132),
            40,
            max_iterations=iterations,  # type: ignore[arg-type]
        )


def test_segmentation_plan_rejects_a_float_that_equals_a_cached_integer() -> None:
    """Validation must stay outside the cache, which keys on hash and equality."""

    cached = turbo_segmentation_plan(4096)

    with pytest.raises(TurboError):
        turbo_segmentation_plan(4096.0)  # type: ignore[arg-type]
    with pytest.raises(TurboError):
        turbo_segmentation_plan(True)  # type: ignore[arg-type]
    with pytest.raises(TurboError):
        turbo_segmentation_plan(-1)

    assert turbo_segmentation_plan(4096) == cached


def test_repeated_segmentation_plans_are_equal_and_immutable() -> None:
    first = turbo_segmentation_plan(8232)
    second = turbo_segmentation_plan(8232)

    assert first == second
    assert first.block_sizes == second.block_sizes
    with pytest.raises(FrozenInstanceError):
        first.filler_bit_count = 0  # type: ignore[misc]
