from __future__ import annotations

from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from ofdm_link.phy.fec import (
    ConvolutionalFecAdapter,
    FecError,
    FecProfile,
    SoftConvolutionalFecAdapter,
)


def test_profile_is_immutable_and_hides_convolutional_details() -> None:
    profile = FecProfile("stable", "hard", 128)

    assert profile.coded_bit_count(0) == 12
    assert profile.coded_bit_count(128) == 268
    assert profile.information_bit_count(268) == 128
    assert not hasattr(profile, "generators")
    assert not hasattr(profile, "tail_bits")
    with pytest.raises(FrozenInstanceError):
        profile.name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("size", [0, 1, 47, 48, 49, 128])
def test_convolutional_adapter_round_trip_owns_tail_semantics(size: int) -> None:
    codec = ConvolutionalFecAdapter(max_information_bits=128)
    information = (np.arange(size, dtype=np.uint8) % 2).astype(np.uint8)

    coded = codec.encode(information)
    decoded = codec.decode(coded)

    np.testing.assert_array_equal(decoded, information)
    assert coded.size == codec.profile.coded_bit_count(size)
    assert not decoded.flags.writeable


@pytest.mark.parametrize(
    ("observations", "message"),
    [
        ([0] * 11, "too short"),
        ([0] * 13, "even"),
        ([0] * 270, "too long"),
        ([0, 1, 2] * 4, "only 0 and 1"),
        ([0.0] * 12, "integer bits"),
    ],
)
def test_convolutional_adapter_rejects_invalid_observations(
    observations: object,
    message: str,
) -> None:
    codec = ConvolutionalFecAdapter(max_information_bits=128)

    with pytest.raises(FecError, match=message):
        codec.decode(observations)


def test_convolutional_adapter_rejects_bad_backend_output() -> None:
    codec = ConvolutionalFecAdapter(
        max_information_bits=8,
        hard_decoder=lambda _coded: np.ones(14, dtype=np.uint8),
    )

    with pytest.raises(FecError, match="zero-state tail"):
        codec.decode(np.zeros(codec.profile.coded_bit_count(8), dtype=np.uint8))


def test_soft_convolutional_oracle_round_trip_and_profile() -> None:
    codec = SoftConvolutionalFecAdapter(max_information_bits=64)
    information = np.unpackbits(np.array([0xA5, 0x71], dtype=np.uint8))
    coded = codec.encode(information)
    llrs = np.where(coded == 0, 8.0, -8.0).astype(np.float32)

    np.testing.assert_array_equal(codec.decode(llrs), information)
    assert codec.profile.decision_mode == "soft"


@pytest.mark.parametrize("bad", [[0.0] * 11, [np.nan] * 12, [np.inf] * 12])
def test_soft_convolutional_adapter_rejects_invalid_llrs(bad: object) -> None:
    codec = SoftConvolutionalFecAdapter(max_information_bits=64)

    with pytest.raises(FecError):
        codec.decode(bad)
