from __future__ import annotations

import struct
import zlib
from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from ofdm_link.phy.burst_header import (
    BURST_HEADER_CODED_BIT_COUNT,
    BURST_HEADER_MAGIC,
    BURST_HEADER_REPETITION_FACTOR,
    BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT,
    BURST_HEADER_SIGNALING_SYMBOL_COUNT,
    BURST_HEADER_UNCODED_BIT_COUNT,
    CURRENT_BURST_HEADER_VERSION,
    TURBO_BURST_HEADER_VERSION,
    BurstHeader,
    BurstHeaderDecodeError,
    BurstHeaderIntegrityError,
    BurstHeaderValidationError,
    TurboBurstHeader,
    decode_burst_header,
    decode_burst_header_soft,
    encode_burst_header,
)
from ofdm_link.phy.codec import MCS


@pytest.mark.parametrize("mcs", list(MCS))
def test_header_round_trip_uses_fixed_qpsk_signaling(mcs: MCS) -> None:
    header = BurstHeader(CURRENT_BURST_HEADER_VERSION, mcs, 1_234)

    coded = encode_burst_header(header)

    assert decode_burst_header(coded) == header
    assert coded.dtype == np.uint8
    assert coded.shape == (BURST_HEADER_CODED_BIT_COUNT,)
    assert BURST_HEADER_UNCODED_BIT_COUNT == 96
    assert BURST_HEADER_CODED_BIT_COUNT == 288
    assert BURST_HEADER_SIGNALING_SYMBOL_COUNT == 144
    assert BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT == 3


def test_header_wire_vector_is_stable_and_big_endian() -> None:
    header = BurstHeader(CURRENT_BURST_HEADER_VERSION, MCS.QAM16, 0x12345678)

    packed = np.packbits(encode_burst_header(header), bitorder="big").tobytes().hex()

    assert packed == (
        "1c0fff1c01f800000700000700703803f1c01c71f81ffe00038fff03f1c71f81c0fffe38"
    )


@pytest.mark.parametrize(
    ("mcs", "coded_bits", "payload_symbols", "bit_padding", "ofdm_symbols", "carriers"),
    [
        (MCS.QPSK, 193, 97, 1, 3, 47),
        (MCS.QAM16, 193, 49, 3, 2, 47),
        (MCS.QPSK, 192, 96, 0, 2, 0),
        (MCS.QAM16, 384, 96, 0, 2, 0),
    ],
)
def test_payload_size_properties_include_mapping_and_carrier_padding(
    mcs: MCS,
    coded_bits: int,
    payload_symbols: int,
    bit_padding: int,
    ofdm_symbols: int,
    carriers: int,
) -> None:
    header = BurstHeader(CURRENT_BURST_HEADER_VERSION, mcs, coded_bits)

    assert header.payload_symbol_count == payload_symbols
    assert header.payload_bit_padding_count == bit_padding
    assert header.payload_ofdm_symbol_count == ofdm_symbols
    assert header.payload_carrier_padding_symbol_count == carriers


def test_majority_vote_corrects_one_error_in_every_repetition_group() -> None:
    header = BurstHeader(CURRENT_BURST_HEADER_VERSION, MCS.QPSK, 4097)
    corrupted = encode_burst_header(header)
    corrupted[::BURST_HEADER_REPETITION_FACTOR] ^= 1

    assert decode_burst_header(corrupted) == header


def test_crc_rejects_two_errors_in_one_repetition_group() -> None:
    header = BurstHeader(CURRENT_BURST_HEADER_VERSION, MCS.QPSK, 4097)
    corrupted = encode_burst_header(header)
    corrupted[-3:-1] ^= 1

    with pytest.raises(BurstHeaderIntegrityError, match="CRC-32"):
        decode_burst_header(corrupted)


@pytest.mark.parametrize(
    ("prefix", "message"),
    [
        (struct.pack(">2sBBI", b"NO", 1, int(MCS.QPSK), 128), "magic"),
        (struct.pack(">2sBBI", BURST_HEADER_MAGIC, 5, int(MCS.QPSK), 128), "wire_version"),
        (struct.pack(">2sBBI", BURST_HEADER_MAGIC, 1, 0xFF, 128), "unknown MCS"),
        (struct.pack(">2sBBI", BURST_HEADER_MAGIC, 1, int(MCS.QPSK), 0), "bit_count"),
    ],
)
def test_decoder_rejects_valid_crc_with_invalid_metadata(prefix: bytes, message: str) -> None:
    raw = prefix + struct.pack(">I", zlib.crc32(prefix) & 0xFFFFFFFF)
    bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="big")
    coded = np.repeat(bits, BURST_HEADER_REPETITION_FACTOR)

    with pytest.raises(BurstHeaderDecodeError, match=message):
        decode_burst_header(coded)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("wire_version", 2, "wire_version"),
        ("wire_version", True, "integer"),
        ("mcs", 0, "MCS"),
        ("coded_payload_bit_count", 0, "bit_count"),
        ("coded_payload_bit_count", 1 << 32, "bit_count"),
        ("coded_payload_bit_count", True, "integer"),
    ],
)
def test_header_rejects_invalid_metadata(field: str, value: object, message: str) -> None:
    arguments: dict[str, object] = {
        "wire_version": CURRENT_BURST_HEADER_VERSION,
        "mcs": MCS.QPSK,
        "coded_payload_bit_count": 128,
    }
    arguments[field] = value

    with pytest.raises(BurstHeaderValidationError, match=message):
        BurstHeader(**arguments)  # type: ignore[arg-type]


def test_header_is_immutable() -> None:
    header = BurstHeader(CURRENT_BURST_HEADER_VERSION, MCS.QPSK, 128)

    with pytest.raises(FrozenInstanceError):
        header.mcs = MCS.QAM16  # type: ignore[misc]


@pytest.mark.parametrize("mcs", list(MCS))
def test_v2_header_keeps_wire_size_but_carries_information_length(mcs: MCS) -> None:
    header = TurboBurstHeader(TURBO_BURST_HEADER_VERSION, mcs, 8_056)

    coded = encode_burst_header(header)
    decoded = decode_burst_header(coded)

    assert decoded == header
    assert isinstance(decoded, TurboBurstHeader)
    assert decoded.information_frame_bit_count == 8_056
    assert decoded.coded_payload_bit_count == 24_408
    assert coded.size == BURST_HEADER_CODED_BIT_COUNT


@pytest.mark.parametrize("information_bits", [0, 87, 89, 524_369, True])
def test_v2_header_rejects_invalid_information_frame_length(
    information_bits: object,
) -> None:
    with pytest.raises(BurstHeaderValidationError, match="information_frame_bit_count"):
        TurboBurstHeader(
            TURBO_BURST_HEADER_VERSION,
            MCS.QPSK,
            information_bits,  # type: ignore[arg-type]
        )


def test_decoder_rejects_malformed_inputs() -> None:
    valid = encode_burst_header(
        BurstHeader(CURRENT_BURST_HEADER_VERSION, MCS.QPSK, 128)
    )

    with pytest.raises(BurstHeaderDecodeError, match="expected"):
        decode_burst_header(valid[:-1])
    with pytest.raises(BurstHeaderDecodeError, match="one-dimensional"):
        decode_burst_header(valid.reshape(2, -1))
    with pytest.raises(BurstHeaderDecodeError, match="only 0 and 1"):
        decode_burst_header(np.full(BURST_HEADER_CODED_BIT_COUNT, 2))
    with pytest.raises(BurstHeaderDecodeError, match="only 0 and 1"):
        decode_burst_header(["0"] * BURST_HEADER_CODED_BIT_COUNT)


def _header_llrs(header: BurstHeader, magnitude: float = 4.0) -> np.ndarray:
    """Project convention: positive LLR selects bit 0."""

    coded = encode_burst_header(header)
    return np.where(coded == 0, magnitude, -magnitude).astype(np.float64)


def test_soft_decode_recovers_bit_whose_two_weak_copies_are_wrong() -> None:
    # One QPSK symbol carries two copies of the same header bit, so a single
    # noisy symbol can flip a hard majority; the strong third copy must win.
    header = TurboBurstHeader(TURBO_BURST_HEADER_VERSION, MCS.QPSK, 7512)
    llrs = _header_llrs(header)
    llrs[0:2] *= -0.25
    hard = (llrs < 0).astype(np.uint8)
    with pytest.raises(BurstHeaderIntegrityError):
        decode_burst_header(hard)

    assert decode_burst_header_soft(llrs) == header


def test_soft_decode_flips_least_reliable_bits_under_crc() -> None:
    header = BurstHeader(CURRENT_BURST_HEADER_VERSION, MCS.QPSK, 4097)
    llrs = _header_llrs(header)
    # Two information bits whose combined soft value is wrong but weak.
    for bit in (5, 60):
        llrs[3 * bit : 3 * bit + 3] *= -0.05

    assert decode_burst_header_soft(llrs) == header


def test_soft_decode_rejects_more_unreliable_errors_than_its_bound() -> None:
    header = BurstHeader(CURRENT_BURST_HEADER_VERSION, MCS.QPSK, 4097)
    llrs = _header_llrs(header)
    for bit in (5, 30, 60):
        llrs[3 * bit : 3 * bit + 3] *= -0.05

    with pytest.raises(BurstHeaderIntegrityError, match="CRC-32"):
        decode_burst_header_soft(llrs)


def test_soft_decode_does_not_accept_noise() -> None:
    generator = np.random.default_rng(20260923)
    for _ in range(2_000):
        with pytest.raises(BurstHeaderDecodeError):
            decode_burst_header_soft(generator.normal(size=BURST_HEADER_CODED_BIT_COUNT))


def test_soft_decode_validates_llr_shape_and_values() -> None:
    with pytest.raises(BurstHeaderDecodeError, match="expected 288"):
        decode_burst_header_soft(np.ones(BURST_HEADER_CODED_BIT_COUNT - 1))
    with pytest.raises(BurstHeaderDecodeError, match="finite"):
        decode_burst_header_soft(np.full(BURST_HEADER_CODED_BIT_COUNT, np.nan))
