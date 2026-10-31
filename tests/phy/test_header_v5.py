from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.phy.fec import _convolutional_encode
from ofdm_link.phy.header_v5 import (
    V5_HEADER_CODED_BITS,
    V5_HEADER_INTERLEAVER,
    V5Header,
    V5HeaderDecodeError,
    V5HeaderError,
    V5HeaderIntegrityError,
    _body_value,
    _int_to_bits,
    crc16_ccitt_false,
    decode_v5_header,
    encode_v5_header,
    v5_header_symbols,
)
from ofdm_link.phy.mcs_table import v5_entries

VALID_MCS = frozenset(entry.index for entry in v5_entries(include_hidden=True))


def _llrs(bits: np.ndarray, scale: float = 8.0) -> np.ndarray:
    return ((1.0 - 2.0 * bits.astype(np.float32)) * scale).astype(np.float32)


def _decode(llrs: np.ndarray, **kwargs: object) -> V5Header:
    return decode_v5_header(llrs, valid_mcs=VALID_MCS, max_psdu_bytes=16384, **kwargs)


def test_crc16_matches_the_ccitt_false_check_value() -> None:
    assert crc16_ccitt_false(b"123456789") == 0x29B1


@pytest.mark.parametrize(
    "header",
    [
        V5Header(mcs_index=8, psdu_length=19, scrambler_seed=1),
        V5Header(mcs_index=17, psdu_length=16384, scrambler_seed=127),
        V5Header(mcs_index=12, psdu_length=947, scrambler_seed=0),
        V5Header(mcs_index=min(VALID_MCS), psdu_length=8192, scrambler_seed=64),
    ],
)
def test_fields_round_trip_including_their_extremes(header: V5Header) -> None:
    coded = encode_v5_header(header)

    assert coded.shape == (V5_HEADER_CODED_BITS,)
    assert _decode(_llrs(coded)) == header


def test_body_layout_puts_each_field_at_its_documented_bits() -> None:
    header = V5Header(mcs_index=0b101010, psdu_length=0xBEEF, scrambler_seed=0b1010101)
    body = _body_value(header)

    assert (body >> 44) & 0xF == 0
    assert (body >> 38) & 0x3F == 0b101010
    assert (body >> 22) & 0xFFFF == 0xBEEF
    assert (body >> 15) & 0x7F == 0b1010101
    assert body & 0x7FFF == 0


def test_interleaver_is_a_bijection_spreading_neighbours_nine_slots_apart() -> None:
    positions = V5_HEADER_INTERLEAVER

    assert sorted(positions.tolist()) == list(range(V5_HEADER_CODED_BITS))
    steps = np.diff(positions)
    # Inside each run of 16 coded bits neighbours are 9 data slots apart.
    assert set(steps[np.arange(steps.size) % 16 != 15].tolist()) == {9}
    # The code spans all three SIG symbols.
    assert {int(p) // 48 for p in positions[:16]} == {0, 1, 2}


def test_symbols_are_bpsk_with_bit_zero_on_plus_one() -> None:
    header = V5Header(mcs_index=9, psdu_length=500, scrambler_seed=3)
    symbols = v5_header_symbols(header)

    assert symbols.shape == (144,)
    assert np.array_equal(symbols.real, 1.0 - 2.0 * encode_v5_header(header))
    assert not symbols.imag.any()


def _coded(body: int, *, crc: int | None = None, pad: tuple[int, int] = (0, 0)) -> np.ndarray:
    """Encode an arbitrary 48-bit body, bypassing V5Header's own checks."""

    if crc is None:
        crc = crc16_ccitt_false(body.to_bytes(6, "big"))
    information = np.concatenate(
        (_int_to_bits(body, 48), _int_to_bits(crc, 16), np.array(pad, dtype=np.uint8))
    )
    coded = _convolutional_encode(information)
    grid = np.empty_like(coded)
    grid[V5_HEADER_INTERLEAVER] = coded
    return grid


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ((1 << 44) | (8 << 38) | (100 << 22) | (5 << 15), "format_version"),
        ((8 << 38) | (100 << 22) | (5 << 15) | (1 << 13), "payload_format"),
        ((8 << 38) | (100 << 22) | (5 << 15) | 1, "reserved"),
        ((2 << 38) | (100 << 22) | (5 << 15), "mcs_index"),  # a legacy index
        ((40 << 38) | (100 << 22) | (5 << 15), "mcs_index"),  # unknown
        ((8 << 38) | (18 << 22) | (5 << 15), "psdu_length"),  # below 19
        ((8 << 38) | (20000 << 22) | (5 << 15), "psdu_length"),  # above max
    ],
)
def test_crc_valid_but_unacceptable_fields_are_rejected(body: int, message: str) -> None:
    with pytest.raises(V5HeaderDecodeError, match=message):
        _decode(_llrs(_coded(body)))


def test_a_flipped_body_bit_fails_the_crc() -> None:
    body = _body_value(V5Header(mcs_index=10, psdu_length=900, scrambler_seed=77))
    crc = crc16_ccitt_false(body.to_bytes(6, "big"))

    with pytest.raises(V5HeaderIntegrityError, match="CRC"):
        _decode(_llrs(_coded(body ^ (1 << 30), crc=crc)))


def test_nonzero_pad_is_rejected() -> None:
    body = _body_value(V5Header(mcs_index=10, psdu_length=900, scrambler_seed=77))

    with pytest.raises(V5HeaderIntegrityError, match="pad"):
        _decode(_llrs(_coded(body, pad=(0, 1))))


def test_header_decodes_from_noisy_llrs_at_3_db_bpsk() -> None:
    """3 dB per BPSK symbol is about 2 dB above the code's 1 % point."""

    rng = np.random.default_rng(20261101)
    variance = 10 ** (-3 / 10)
    failures = 0
    for trial in range(200):
        header = V5Header(
            mcs_index=8 + trial % 10,
            psdu_length=int(rng.integers(19, 16384)),
            scrambler_seed=int(rng.integers(1, 128)),
        )
        symbols = v5_header_symbols(header).real.astype(np.float64)
        received = symbols + np.sqrt(variance / 2) * rng.standard_normal(symbols.size)
        llrs = (4.0 * received / variance).astype(np.float32)
        try:
            failures += _decode(llrs) != header
        except V5HeaderDecodeError:
            failures += 1
    assert failures <= 2


def test_noise_is_rejected() -> None:
    rng = np.random.default_rng(5)
    accepted = 0
    for _ in range(300):
        try:
            _decode(rng.standard_normal(144).astype(np.float32) * 4)
        except V5HeaderDecodeError:
            continue
        accepted += 1
    assert accepted == 0


def test_interleaver_can_be_bypassed_for_simulation() -> None:
    header = V5Header(mcs_index=13, psdu_length=2000, scrambler_seed=9)
    plain = encode_v5_header(header, interleave=False)

    assert _decode(_llrs(plain), interleave=False) == header
    assert np.array_equal(encode_v5_header(header)[V5_HEADER_INTERLEAVER], plain)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mcs_index": 64, "psdu_length": 100, "scrambler_seed": 1},
        {"mcs_index": 8, "psdu_length": 0, "scrambler_seed": 1},
        {"mcs_index": 8, "psdu_length": 1 << 16, "scrambler_seed": 1},
        {"mcs_index": 8, "psdu_length": 100, "scrambler_seed": 128},
        {"mcs_index": 8, "psdu_length": 100, "scrambler_seed": 1, "payload_format": 4},
    ],
)
def test_unrepresentable_fields_are_refused(kwargs: dict[str, int]) -> None:
    with pytest.raises(V5HeaderError):
        V5Header(**kwargs)
