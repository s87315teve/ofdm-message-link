from __future__ import annotations

import struct
import zlib
from dataclasses import FrozenInstanceError
from itertools import product

import numpy as np
import pytest

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MAX_PAYLOAD_LENGTH,
    MCS,
    Frame,
    FrameCodec,
    FrameDecodeError,
    FrameIntegrityError,
    FrameKind,
    FrameValidationError,
    PhyCodecError,
    decode_frame,
    decode_uncoded_frame,
    demap_symbols,
    encode_frame,
    make_convolutional_frame_codec,
    map_symbols,
)
from ofdm_link.phy.codec import deserialize_frame, serialize_frame


@pytest.mark.parametrize("mcs", list(MCS))
@pytest.mark.parametrize("payload", [b"", b"a", bytes(range(251))])
def test_frame_and_constellation_round_trip(mcs: MCS, payload: bytes) -> None:
    frame = Frame(
        protocol_version=CURRENT_PROTOCOL_VERSION,
        kind=FrameKind.DATA,
        mcs=mcs,
        sequence=0xA15E,
        payload=payload,
    )

    coded = encode_frame(frame)
    symbols = map_symbols(coded, mcs)
    recovered = decode_frame(demap_symbols(symbols, mcs))

    assert recovered == frame
    assert recovered.payload_length == len(payload)


def test_frame_is_immutable() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 7, b"ack")

    with pytest.raises(FrozenInstanceError):
        frame.sequence = 8  # type: ignore[misc]


def test_wire_encoding_is_deterministic() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.CONTROL, MCS.QAM16, 0x1234, b"OFDM")

    packed = np.packbits(encode_frame(frame), bitorder="big").tobytes().hex()

    assert packed == "00038f7db2b38cf1404a47000038cf21a171f20fcf2f718b17dc81c185c916c0"


def test_canonical_frame_bytes_are_stable_and_round_trip() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.CONTROL, MCS.QAM16, 0x1234, b"OFDM")

    serialized = serialize_frame(frame)

    assert serialized.hex() == "010301123400044f46444dea6fde8e"
    assert deserialize_frame(serialized) == frame


def test_canonical_frame_bytes_reject_corruption_and_trailing_data() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, 7, b"payload")
    serialized = serialize_frame(frame)
    corrupted = bytearray(serialized)
    corrupted[-1] ^= 1
    overlong_protected = serialized[:-4] + b"trailing"
    overlong = overlong_protected + struct.pack(
        ">I", zlib.crc32(overlong_protected) & 0xFFFFFFFF
    )

    with pytest.raises(FrameIntegrityError, match="CRC-32"):
        deserialize_frame(bytes(corrupted))
    with pytest.raises(FrameDecodeError, match="payload length"):
        deserialize_frame(overlong)
    with pytest.raises(FrameDecodeError, match="too short"):
        deserialize_frame(b"")


def test_formal_frame_codec_preserves_public_v1_wire_behavior() -> None:
    codec = make_convolutional_frame_codec()
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.CONTROL, MCS.QAM16, 0x1234, b"OFDM")

    assert isinstance(codec, FrameCodec)
    np.testing.assert_array_equal(codec.encode(frame), encode_frame(frame))
    assert codec.decode(codec.encode(frame)) == frame
    assert codec.fec_profile.name == "convolutional-k7-r1/2-133/171"
    assert codec.fec_profile.decision_mode == "hard"


def test_qpsk_constellation_and_normalization() -> None:
    bits = np.array(list(product((0, 1), repeat=2)), dtype=np.uint8).reshape(-1)

    symbols = map_symbols(bits, MCS.QPSK)

    expected = np.array([1 + 1j, 1 - 1j, -1 + 1j, -1 - 1j]) / np.sqrt(2)
    np.testing.assert_allclose(symbols, expected)
    assert np.mean(np.abs(symbols) ** 2) == pytest.approx(1.0)
    np.testing.assert_array_equal(demap_symbols(symbols, MCS.QPSK), bits)


def test_qam16_is_gray_coded_and_normalized() -> None:
    bit_words = np.array(list(product((0, 1), repeat=4)), dtype=np.uint8)

    symbols = map_symbols(bit_words.reshape(-1), MCS.QAM16)

    assert np.mean(np.abs(symbols) ** 2) == pytest.approx(1.0)
    np.testing.assert_array_equal(demap_symbols(symbols, MCS.QAM16), bit_words.reshape(-1))

    ordered_axis_pairs = np.array([[0, 0], [0, 1], [1, 1], [1, 0]], dtype=np.uint8)
    hamming_distances = np.count_nonzero(ordered_axis_pairs[:-1] != ordered_axis_pairs[1:], axis=1)
    np.testing.assert_array_equal(hamming_distances, np.ones(3, dtype=np.int64))


def test_viterbi_corrects_separated_coded_bit_errors() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, 42, b"coded payload")
    coded = encode_frame(frame)
    corrupted = coded.copy()
    corrupted[[25, 91, 173]] ^= 1

    assert decode_frame(corrupted) == frame


def test_already_fec_decoded_bits_share_wire_and_crc_validation() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QAM16, 0x1234, b"native")
    protected = struct.pack(
        ">BBBHH",
        frame.protocol_version,
        int(frame.kind),
        int(frame.mcs),
        frame.sequence,
        len(frame.payload),
    ) + frame.payload
    raw = protected + struct.pack(">I", zlib.crc32(protected) & 0xFFFFFFFF)
    decoded_bits = np.pad(
        np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="big"),
        (0, 6),
    )

    assert decode_uncoded_frame(decoded_bits) == frame

    corrupted = decoded_bits.copy()
    corrupted[80] ^= 1
    with pytest.raises(FrameIntegrityError, match="CRC-32"):
        decode_uncoded_frame(corrupted)


def test_crc_rejects_uncorrectable_corruption() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, 19, b"integrity matters")
    corrupted = encode_frame(frame).copy()
    corrupted[180:196] ^= 1

    with pytest.raises(FrameIntegrityError, match="CRC-32"):
        decode_frame(corrupted)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("protocol_version", 2, "unsupported protocol_version"),
        ("kind", 0, "kind must be a FrameKind"),
        ("mcs", "qpsk", "mcs must be an MCS"),
        ("sequence", -1, "sequence"),
        ("sequence", 65536, "sequence"),
        ("payload", bytearray(b"mutable"), "immutable bytes"),
        ("payload", b"x" * (MAX_PAYLOAD_LENGTH + 1), "wire limit"),
    ],
)
def test_frame_rejects_invalid_header_fields(field: str, value: object, message: str) -> None:
    arguments: dict[str, object] = {
        "protocol_version": CURRENT_PROTOCOL_VERSION,
        "kind": FrameKind.DATA,
        "mcs": MCS.QPSK,
        "sequence": 1,
        "payload": b"payload",
    }
    arguments[field] = value

    with pytest.raises(FrameValidationError, match=message):
        Frame(**arguments)  # type: ignore[arg-type]


def test_codec_rejects_malformed_bit_and_symbol_inputs() -> None:
    with pytest.raises(PhyCodecError, match="only 0 and 1"):
        map_symbols(np.array([0, 2]), MCS.QPSK)
    with pytest.raises(PhyCodecError, match="multiple of 4"):
        map_symbols(np.array([0, 1]), MCS.QAM16)
    with pytest.raises(PhyCodecError, match="one-dimensional"):
        demap_symbols(np.zeros((2, 2), dtype=np.complex64), MCS.QPSK)
    with pytest.raises(PhyCodecError, match="finite"):
        demap_symbols(np.array([complex(np.nan, 0)]), MCS.QPSK)
    with pytest.raises(FrameDecodeError, match="too short"):
        decode_frame(np.zeros(10, dtype=np.uint8))
    with pytest.raises(FrameDecodeError, match="even number"):
        decode_frame(np.zeros(189, dtype=np.uint8))
