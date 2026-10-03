from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.phy.fec import (
    CONVOLUTIONAL_RATE_INVERSES,
    ConvolutionalFecAdapter,
    FecError,
    SoftConvolutionalFecAdapter,
    SoftUncodedFecAdapter,
    UncodedFecAdapter,
)
from ofdm_link.phy.turbo import native_convolutional_soft_decoder

_MAX_BITS = 200_000


def _bits(count: int, seed: int = 20260918) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 2, count).astype(np.uint8)


@pytest.mark.parametrize("rate_inverse", CONVOLUTIONAL_RATE_INVERSES)
@pytest.mark.parametrize("information_bits", [0, 8, 88, 512, 8_056])
def test_convolutional_round_trip_at_every_supported_rate(
    rate_inverse: int,
    information_bits: int,
) -> None:
    adapter = ConvolutionalFecAdapter(
        max_information_bits=_MAX_BITS,
        rate_inverse=rate_inverse,
    )
    information = _bits(information_bits)

    coded = adapter.encode(information)

    assert coded.size == rate_inverse * (information_bits + 6)
    assert adapter.profile.rate_inverse == rate_inverse
    np.testing.assert_array_equal(adapter.decode(coded), information)


@pytest.mark.parametrize("information_bits", [0, 8, 88, 512, 8_056])
def test_uncoded_adds_no_redundancy_and_no_tail(information_bits: int) -> None:
    adapter = UncodedFecAdapter(max_information_bits=_MAX_BITS)
    information = _bits(information_bits)

    coded = adapter.encode(information)

    assert coded.size == information_bits
    np.testing.assert_array_equal(coded, information)
    np.testing.assert_array_equal(adapter.decode(coded), information)


def test_uncoded_soft_slicing_matches_the_hard_decision() -> None:
    information = _bits(512)
    hard = UncodedFecAdapter(max_information_bits=_MAX_BITS)
    soft = SoftUncodedFecAdapter(max_information_bits=_MAX_BITS)
    coded = hard.encode(information)
    llrs = np.where(coded == 0, 3.5, -3.5).astype(np.float32)

    np.testing.assert_array_equal(soft.decode(llrs), hard.decode(coded))


def test_rate_one_third_is_a_distinct_mother_code_not_a_punctured_rate_one_half() -> None:
    """Rate matching is out of scope, so the codes must differ structurally."""

    information = _bits(256)
    half = ConvolutionalFecAdapter(max_information_bits=_MAX_BITS, rate_inverse=2)
    third = ConvolutionalFecAdapter(max_information_bits=_MAX_BITS, rate_inverse=3)

    half_coded = half.encode(information).reshape(-1, 2)
    third_coded = third.encode(information).reshape(-1, 3)

    # The first two output streams are shared; the third is genuinely new.
    np.testing.assert_array_equal(third_coded[:, :2], half_coded)
    assert not np.array_equal(third_coded[:, 2], third_coded[:, 0])
    assert not np.array_equal(third_coded[:, 2], third_coded[:, 1])


def test_rate_one_third_corrects_more_errors_than_rate_one_half() -> None:
    """The added redundancy has to buy real correction capability."""

    rng = np.random.default_rng(7)
    recovered = {}
    for rate_inverse in (2, 3):
        adapter = ConvolutionalFecAdapter(
            max_information_bits=_MAX_BITS,
            rate_inverse=rate_inverse,
        )
        successes = 0
        for _ in range(40):
            information = rng.integers(0, 2, 128).astype(np.uint8)
            coded = adapter.encode(information).copy()
            flips = rng.choice(coded.size, size=14, replace=False)
            coded[flips] ^= 1
            try:
                successes += int(np.array_equal(adapter.decode(coded), information))
            except FecError:
                pass
        recovered[rate_inverse] = successes

    assert recovered[3] > recovered[2]


def test_unsupported_rate_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported convolutional rate"):
        ConvolutionalFecAdapter(max_information_bits=_MAX_BITS, rate_inverse=4)


@pytest.mark.parametrize("rate_inverse", CONVOLUTIONAL_RATE_INVERSES)
def test_native_viterbi_matches_the_portable_reference(rate_inverse: int) -> None:
    """The compiled decoder is an optimization, never a different code."""

    information = _bits(2_048, seed=5)
    portable = SoftConvolutionalFecAdapter(
        max_information_bits=_MAX_BITS,
        rate_inverse=rate_inverse,
    )
    native = SoftConvolutionalFecAdapter(
        max_information_bits=_MAX_BITS,
        rate_inverse=rate_inverse,
        soft_decoder=native_convolutional_soft_decoder(rate_inverse),
    )
    coded = portable.encode(information)
    rng = np.random.default_rng(3)
    llrs = np.where(coded == 0, 4.0, -4.0) + rng.normal(0.0, 1.0, coded.size)
    llrs = llrs.astype(np.float32)

    np.testing.assert_array_equal(native.decode(llrs), portable.decode(llrs))


def test_profile_names_distinguish_the_two_mother_codes() -> None:
    names = {
        ConvolutionalFecAdapter(
            max_information_bits=_MAX_BITS,
            rate_inverse=rate_inverse,
        ).profile.name
        for rate_inverse in CONVOLUTIONAL_RATE_INVERSES
    }

    assert names == {
        "convolutional-k7-r1/2-133/171",
        "convolutional-k7-r1/3-133/171/165",
    }
