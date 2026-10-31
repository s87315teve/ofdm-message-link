from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.phy.scrambler import (
    ScramblerError,
    descramble_llrs,
    scramble_bits,
    scrambler_sequence,
)

# IEEE 802.11 Annex (scrambler output for the all-ones initial state).
_IEEE_SEED_7F = "00001110" "11110010" "11001001" "00000010" "00100110" "00101110"


def test_all_ones_seed_reproduces_the_802_11_vector() -> None:
    expected = np.array([int(bit) for bit in _IEEE_SEED_7F], dtype=np.uint8)

    assert np.array_equal(scrambler_sequence(0x7F, expected.size), expected)


@pytest.mark.parametrize("seed", [1, 0x5D, 0x7F])
def test_every_nonzero_seed_has_period_127_and_64_ones(seed: int) -> None:
    sequence = scrambler_sequence(seed, 3 * 127)

    assert np.array_equal(sequence[:127], sequence[127:254])
    assert int(sequence[:127].sum()) == 64
    # The shortest period is the full 127: no shift below it repeats.
    period = sequence[:127]
    assert all(not np.array_equal(period, np.roll(period, k)) for k in range(1, 127))


def test_nonzero_seeds_are_cyclic_shifts_of_one_m_sequence() -> None:
    reference = scrambler_sequence(1, 127)
    rotations = {np.roll(reference, -k).tobytes(): k for k in range(127)}
    shifts = {rotations[scrambler_sequence(seed, 127).tobytes()] for seed in range(1, 128)}

    assert len(shifts) == 127


def test_seed_zero_is_the_identity() -> None:
    bits = np.random.default_rng(1).integers(0, 2, 500, dtype=np.uint8)

    assert not scrambler_sequence(0, 10).any()
    assert np.array_equal(scramble_bits(bits, 0), bits)
    assert np.array_equal(descramble_llrs(bits.astype(np.float32), 0), bits.astype(np.float32))


def test_scrambling_twice_restores_the_bits_and_llrs_flip_where_bits_do() -> None:
    rng = np.random.default_rng(2)
    bits = rng.integers(0, 2, 1000, dtype=np.uint8)
    scrambled = scramble_bits(bits, 93)
    llrs = (1.0 - 2.0 * scrambled).astype(np.float32) * 3.5

    assert np.array_equal(scramble_bits(scrambled, 93), bits)
    descrambled = descramble_llrs(llrs, 93)
    assert np.array_equal((descrambled < 0).astype(np.uint8), bits)
    assert np.allclose(np.abs(descrambled), 3.5)


@pytest.mark.parametrize("seed", [-1, 128, 1.0, True])
def test_seed_outside_seven_bits_is_rejected(seed: object) -> None:
    with pytest.raises(ScramblerError):
        scrambler_sequence(seed, 4)  # type: ignore[arg-type]


def test_non_binary_bits_are_rejected() -> None:
    with pytest.raises(ScramblerError):
        scramble_bits(np.array([0, 2, 1]), 5)
