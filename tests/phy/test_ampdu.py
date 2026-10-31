from __future__ import annotations

import random
from dataclasses import FrozenInstanceError

import pytest

from ofdm_link.phy.ampdu import (
    DELIMITER_BYTES,
    DELIMITER_SIGNATURE,
    MAX_MPDU_BYTES,
    MIN_MPDU_BYTES,
    AmpduParseResult,
    ampdu_body_length,
    delimiter_crc8,
    pack_ampdu,
    unpack_ampdu,
)


def _mpdu(length: int, seed: int) -> bytes:
    rng = random.Random(seed)
    return bytes(rng.randrange(1, 256) for _ in range(length))


def _mpdus(lengths: list[int]) -> list[bytes]:
    return [_mpdu(n, i) for i, n in enumerate(lengths)]


def test_crc8_check_value_poly_07_init_ff() -> None:
    assert delimiter_crc8(b"123456789") == 0xFB
    assert delimiter_crc8(b"") == 0xFF


@pytest.mark.parametrize("count", range(1, 17))
def test_round_trip_varied_lengths(count: int) -> None:
    lengths = [MIN_MPDU_BYTES + (i * 97 + count * 13) % 1090 for i in range(count)]
    mpdus = _mpdus(lengths)
    body = pack_ampdu(mpdus)
    assert len(body) == ampdu_body_length(lengths)
    result = unpack_ampdu(body)
    assert list(result.mpdus) == mpdus
    assert result.bad_delimiters == 0
    assert result.resyncs == 0


def test_round_trip_extreme_lengths() -> None:
    mpdus = [_mpdu(MIN_MPDU_BYTES, 1), _mpdu(MAX_MPDU_BYTES, 2), _mpdu(MIN_MPDU_BYTES, 3)]
    assert list(unpack_ampdu(pack_ampdu(mpdus)).mpdus) == mpdus


@pytest.mark.parametrize("length", [0, 1, MIN_MPDU_BYTES - 1, MAX_MPDU_BYTES + 1])
def test_out_of_range_lengths_are_rejected(length: int) -> None:
    with pytest.raises(ValueError):
        pack_ampdu([bytes(length)])
    with pytest.raises(ValueError):
        ampdu_body_length([length])


def test_empty_aggregate_is_rejected() -> None:
    with pytest.raises(ValueError):
        pack_ampdu([])


def test_delimiters_are_four_byte_aligned_and_padding_is_zero() -> None:
    lengths = [11, 12, 13, 14, 15]
    mpdus = _mpdus(lengths)
    body = pack_ampdu(mpdus)
    result = unpack_ampdu(body)
    for offset, mpdu in zip(result.offsets, result.mpdus, strict=True):
        delimiter = offset - DELIMITER_BYTES
        assert delimiter % 4 == 0
        assert body[delimiter + 3] == DELIMITER_SIGNATURE
        assert int.from_bytes(body[delimiter : delimiter + 2], "big") == len(mpdu)
        assert body[delimiter + 2] == delimiter_crc8(body[delimiter : delimiter + 2])
        end = offset + len(mpdu)
        pad_end = min(-end % 4 + end, len(body))
        assert body[end:pad_end] == bytes(pad_end - end)
    # No padding after the last subframe.
    assert len(body) == result.offsets[-1] + lengths[-1]
    assert ampdu_body_length(lengths) == len(body)


def test_corrupt_mpdu_body_keeps_neighbours() -> None:
    mpdus = _mpdus([40, 50, 60, 70])
    body = bytearray(pack_ampdu(mpdus))
    offsets = unpack_ampdu(bytes(body)).offsets
    body[offsets[1] + 5] ^= 0xFF
    result = unpack_ampdu(bytes(body))
    assert len(result.mpdus) == 4
    assert result.mpdus[0] == mpdus[0]
    assert result.mpdus[1] != mpdus[1]
    assert result.mpdus[2:] == tuple(mpdus[2:])
    assert result.bad_delimiters == 0


@pytest.mark.parametrize("victim", [0, 1, 2, 3])
@pytest.mark.parametrize("byte", [0, 1, 2, 3])
def test_corrupt_delimiter_resyncs(victim: int, byte: int) -> None:
    mpdus = _mpdus([40, 50, 60, 70])
    body = bytearray(pack_ampdu(mpdus))
    offsets = unpack_ampdu(bytes(body)).offsets
    body[offsets[victim] - DELIMITER_BYTES + byte] ^= 0x55
    result = unpack_ampdu(bytes(body))
    assert list(result.mpdus[len(result.mpdus) - (3 - victim) :]) == mpdus[victim + 1 :]
    assert len(result.mpdus) == 3
    assert result.bad_delimiters == 1
    assert result.resyncs == (1 if victim < 3 else 0)
    assert mpdus[victim] not in result.mpdus
    if victim:
        assert result.mpdus[0] == mpdus[0]


def test_truncated_body_returns_complete_subframes_only() -> None:
    mpdus = _mpdus([40, 50, 60])
    body = pack_ampdu(mpdus)
    offsets = unpack_ampdu(body).offsets
    cut = offsets[2] + 10
    result = unpack_ampdu(body[:cut])
    assert list(result.mpdus) == mpdus[:2]
    assert result.bad_delimiters == 1
    assert result.resyncs == 0
    assert unpack_ampdu(b"") == AmpduParseResult((), (), 0, 0)
    assert unpack_ampdu(body[:3]).mpdus == ()


def test_trailing_garbage_is_counted_not_returned() -> None:
    mpdus = _mpdus([40, 50])
    result = unpack_ampdu(pack_ampdu(mpdus) + bytes(range(1, 30)))
    assert list(result.mpdus) == mpdus
    assert result.bad_delimiters == 1


def test_random_garbage_never_raises_or_overruns() -> None:
    rng = random.Random(7)
    for trial in range(300):
        size = rng.randrange(0, 600)
        data = bytearray(rng.randrange(256) for _ in range(size))
        if trial % 3 == 0 and size > 16:
            # Plant plausible delimiters with bogus lengths.
            for pos in range(0, size - 4, 8):
                field = rng.randrange(0, 0x4000).to_bytes(2, "big")
                data[pos : pos + 4] = field + bytes((delimiter_crc8(field), DELIMITER_SIGNATURE))
        result = unpack_ampdu(bytes(data))
        for offset, mpdu in zip(result.offsets, result.mpdus, strict=True):
            assert offset + len(mpdu) <= size
            assert bytes(data[offset : offset + len(mpdu)]) == mpdu
            assert MIN_MPDU_BYTES <= len(mpdu) <= MAX_MPDU_BYTES


def test_result_is_frozen() -> None:
    result = unpack_ampdu(pack_ampdu(_mpdus([20])))
    with pytest.raises(FrozenInstanceError):
        result.resyncs = 1  # type: ignore[misc]
