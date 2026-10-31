"""Wire-v5 PSDU coding: FCS, LTE Turbo, and rate matching onto whole symbols.

A v5 PSDU is ``body || FCS32`` with ``FCS32 = zlib.crc32(body)`` big-endian.
Putting the FCS in the last 32 information bits lets the unchanged native
decoder stop early on it when the PSDU is one code block; longer PSDUs stop
per block on their CRC-24B instead.  Scrambling and mapping happen outside
this module, on the ``G`` rate-matched bits it returns.

Decoding never raises for a failed CRC: an aggregated PSDU's subframes carry
their own CRC-32, so a failed block only spoils the bytes it covers, and the
result says which bytes those are.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .mcs_table import V5_MAX_PSDU_BYTES, V5_PSDU_FCS_BYTES, PayloadGeometry
from .rate_match import rate_dematch, rate_match
from .turbo import (
    NativeTurboFecAdapter,
    TurboDecodeReport,
    code_block_information_ranges,
)


class PsduCodecError(ValueError):
    """Raised when a body, LLR vector, or geometry does not fit together."""


@dataclass(frozen=True, slots=True)
class PsduDecodeResult:
    """Decoded PSDU body (FCS removed) and what is known about each part.

    ``block_crc_ok[r]`` is whether code block ``r`` is known good: its CRC-24B
    when the PSDU is segmented, the FCS when it is one block.
    ``block_byte_ranges[r]`` is the ``[start, end)`` span of ``body`` that
    block ``r`` carries; the spans tile the body in order (the last one is
    clipped where the FCS begins).  Every block boundary is byte aligned because
    LTE block sizes, the CRC-24B and the filler are all multiples of 8.
    """

    body: bytes
    fcs_ok: bool
    block_crc_ok: tuple[bool, ...]
    block_byte_ranges: tuple[tuple[int, int], ...]
    report: TurboDecodeReport


class RateMatchedTurboCodec:
    """Encode and decode one v5 PSDU against its :class:`PayloadGeometry`."""

    def __init__(self, *, max_iterations: int = 8) -> None:
        self._turbo = NativeTurboFecAdapter(
            max_information_bits=8 * V5_MAX_PSDU_BYTES,
            max_iterations=max_iterations,
        )

    @property
    def max_iterations(self) -> int:
        return self._turbo.max_iterations

    def encode(self, body: bytes, geometry: PayloadGeometry) -> NDArray[np.uint8]:
        """Append the FCS, Turbo encode, and return the ``G`` unscrambled bits."""

        if type(body) is not bytes:
            raise PsduCodecError("body must be bytes")
        if not isinstance(geometry, PayloadGeometry):
            raise TypeError("geometry must be a PayloadGeometry")
        if len(body) + V5_PSDU_FCS_BYTES != geometry.psdu_bytes:
            raise PsduCodecError(
                f"body of {len(body)} bytes does not match a geometry for "
                f"{geometry.psdu_bytes}-byte PSDUs"
            )
        psdu = body + (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")
        bits = np.unpackbits(np.frombuffer(psdu, dtype=np.uint8), bitorder="big")
        return rate_match(self._turbo.encode(bits), geometry.rate_match)

    def decode(self, llrs: ArrayLike, geometry: PayloadGeometry) -> PsduDecodeResult:
        """Decode ``G`` descrambled LLRs (positive = bit 0) into a result.

        A failed FCS or CRC-24B is reported in the result, never raised.
        """

        if not isinstance(geometry, PayloadGeometry):
            raise TypeError("geometry must be a PayloadGeometry")
        values = np.asarray(llrs)
        if values.ndim != 1 or values.size != geometry.available_bits:
            raise PsduCodecError(
                f"expected {geometry.available_bits} LLRs, got shape {values.shape}"
            )
        native = rate_dematch(values, geometry.rate_match)
        decoded = self._turbo.decode_result(
            native,
            information_bit_count=geometry.information_bits,
            allow_block_failures=True,
        )
        psdu = np.packbits(decoded.information_bits, bitorder="big").tobytes()
        body, fcs = psdu[:-V5_PSDU_FCS_BYTES], psdu[-V5_PSDU_FCS_BYTES:]
        fcs_ok = (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big") == fcs
        report = decoded.report
        if report.block_count > 1:
            block_ok = tuple(outcome is True for outcome in report.code_block_crc_ok)
        else:
            block_ok = (fcs_ok,)
        return PsduDecodeResult(
            body=body,
            fcs_ok=fcs_ok,
            block_crc_ok=block_ok,
            block_byte_ranges=psdu_block_byte_ranges(geometry.psdu_bytes),
            report=report,
        )


def psdu_block_byte_ranges(psdu_bytes: int) -> tuple[tuple[int, int], ...]:
    """Return each code block's ``[start, end)`` byte span of the PSDU body.

    A span covers every byte that has at least one bit in the block, so a
    caller that drops subframes overlapping a failed block never keeps a
    damaged byte.
    """

    body_bytes = psdu_bytes - V5_PSDU_FCS_BYTES
    ranges = []
    for start_bit, end_bit in code_block_information_ranges(8 * psdu_bytes):
        start = min(start_bit // 8, body_bytes)
        end = min(-(-end_bit // 8), body_bytes)
        ranges.append((start, end))
    return tuple(ranges)
