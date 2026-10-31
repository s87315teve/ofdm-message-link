from __future__ import annotations

import zlib

import numpy as np
import pytest

from ofdm_link.phy.mcs_table import mcs_entry, v5_payload_geometry
from ofdm_link.phy.psdu_codec import (
    PsduCodecError,
    RateMatchedTurboCodec,
    psdu_block_byte_ranges,
)
from ofdm_link.phy.rate_match import rate_match
from ofdm_link.phy.turbo import NativeTurboFecAdapter


def _body(size: int, seed: int = 3) -> bytes:
    return np.random.default_rng(seed).integers(0, 256, size, dtype=np.uint8).tobytes()


def _llrs(bits: np.ndarray, magnitude: float = 6.0) -> np.ndarray:
    return magnitude * (1.0 - 2.0 * bits.astype(np.float32))


def test_encode_appends_a_big_endian_crc32_fcs_before_coding() -> None:
    body = _body(200)
    geometry = v5_payload_geometry(mcs_entry(9), len(body) + 4)
    psdu = body + zlib.crc32(body).to_bytes(4, "big")
    bits = np.unpackbits(np.frombuffer(psdu, dtype=np.uint8), bitorder="big")
    native = NativeTurboFecAdapter(max_information_bits=1 << 20).encode(bits)

    encoded = RateMatchedTurboCodec().encode(body, geometry)

    np.testing.assert_array_equal(encoded, rate_match(native, geometry.rate_match))


def test_corrupted_fcs_is_reported_not_raised() -> None:
    body = _body(300)
    geometry = v5_payload_geometry(mcs_entry(12), len(body) + 4)
    codec = RateMatchedTurboCodec(max_iterations=2)
    bits = codec.encode(body, geometry)
    # Strong wrong LLRs on most bits: the decoder cannot land on a valid FCS.
    llrs = _llrs(bits)
    llrs[::3] *= -1.0

    result = codec.decode(llrs, geometry)

    assert result.fcs_ok is False
    assert result.block_crc_ok == (False,)
    assert len(result.body) == len(body)
    assert result.report.outer_crc_ok is False


def test_one_failed_code_block_only_marks_its_own_byte_range() -> None:
    body = _body(2000)
    geometry = v5_payload_geometry(mcs_entry(9), len(body) + 4)
    codec = RateMatchedTurboCodec(max_iterations=2)
    plan = geometry.rate_match
    assert plan.segmentation.block_count == 3
    llrs = _llrs(codec.encode(body, geometry))
    # Replace block 1's LLRs with confident garbage.  (All-zero LLRs would not
    # do: they decode to an all-zero block, which passes CRC-24B by design.)
    start = plan.blocks[0].e
    garbage = np.random.default_rng(9).choice([-6.0, 6.0], plan.blocks[1].e)
    llrs[start : start + plan.blocks[1].e] = garbage

    result = codec.decode(llrs, geometry)

    assert result.fcs_ok is False
    assert result.block_crc_ok == (True, False, True)
    (first_start, first_end), _, (last_start, last_end) = result.block_byte_ranges
    assert result.body[first_start:first_end] == body[first_start:first_end]
    assert result.body[last_start:last_end] == body[last_start:last_end]


@pytest.mark.parametrize("psdu_bytes", [4, 19, 767, 768, 769, 947, 2000, 7555, 16384])
def test_block_byte_ranges_tile_the_body_in_order(psdu_bytes: int) -> None:
    ranges = psdu_block_byte_ranges(psdu_bytes)

    assert ranges[0][0] == 0
    assert ranges[-1][1] == psdu_bytes - 4
    for (_, end), (start, _) in zip(ranges, ranges[1:], strict=False):
        assert start == end


def test_decode_reports_tiled_ranges_and_per_block_crc() -> None:
    body = _body(7551)
    geometry = v5_payload_geometry(mcs_entry(13), len(body) + 4)
    codec = RateMatchedTurboCodec()

    result = codec.decode(_llrs(codec.encode(body, geometry)), geometry)

    assert result.fcs_ok
    assert result.body == body
    assert result.block_crc_ok == (True,) * geometry.rate_match.segmentation.block_count
    assert result.block_byte_ranges == psdu_block_byte_ranges(len(body) + 4)
    assert result.report.block_count == len(result.block_byte_ranges)


def test_codec_rejects_mismatched_inputs() -> None:
    geometry = v5_payload_geometry(mcs_entry(8), 100)
    codec = RateMatchedTurboCodec()

    with pytest.raises(PsduCodecError, match="does not match"):
        codec.encode(bytes(95), geometry)
    with pytest.raises(PsduCodecError, match="must be bytes"):
        codec.encode(bytearray(96), geometry)
    with pytest.raises(PsduCodecError, match="LLRs"):
        codec.decode(np.zeros(geometry.available_bits - 1, dtype=np.float32), geometry)
