"""Deterministic PHY frame, FEC, and constellation codec.

The wire representation in this module is deliberately explicit: multibyte
integers and the CRC are big-endian, bytes are expanded most-significant bit
first, and convolutional encoder output bits are emitted in 133/171 order.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Protocol, runtime_checkable

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .fec import (
    ConvolutionalFecAdapter,
    FecCodec,
    FecError,
    FecProfile,
    HardDecoder,
    SoftConvolutionalFecAdapter,
    SoftDecoder,
    SoftUncodedFecAdapter,
    UncodedFecAdapter,
)
from .fec import _convolutional_encode as _convolutional_encode
from .turbo import (
    NativeTurboFecAdapter,
    TurboDecodeReport,
    turbo_coded_bit_count,
)

CURRENT_PROTOCOL_VERSION = 1
MAX_PAYLOAD_LENGTH = (1 << 16) - 1

_HEADER = struct.Struct(">BBBHH")
_CRC = struct.Struct(">I")
_TAIL_BITS = 6
_MAX_INFORMATION_BITS = (_HEADER.size + MAX_PAYLOAD_LENGTH + _CRC.size) * 8
MIN_INFORMATION_FRAME_BITS = (_HEADER.size + _CRC.size) * 8
MAX_INFORMATION_FRAME_BITS = _MAX_INFORMATION_BITS


class PhyCodecError(ValueError):
    """Base class for rejected PHY codec inputs."""


@dataclass(frozen=True, slots=True)
class FrameDecodeFailure:
    """Immutable context produced by one rejected frame decode attempt."""

    wire_version: int
    fec_profile: FecProfile
    fec_report: TurboDecodeReport | None


class FrameValidationError(PhyCodecError):
    """Raised when frame metadata or payload is outside the wire contract."""


class FrameDecodeError(PhyCodecError):
    """Raised when coded input cannot represent one complete frame."""

    def __init__(
        self,
        message: str,
        *,
        failure: FrameDecodeFailure | None = None,
    ) -> None:
        super().__init__(message)
        self.failure = failure


class FrameIntegrityError(FrameDecodeError):
    """Raised when a decoded frame fails its CRC-32 integrity check."""


class FrameKind(IntEnum):
    """Frame purpose encoded in the fixed PHY header."""

    DATA = 0
    ACK = 1
    BEACON = 2
    CONTROL = 3


class MCS(IntEnum):
    """Payload constellation encoded in the fixed PHY header."""

    QPSK = 0
    QAM16 = 1

    @property
    def bits_per_symbol(self) -> int:
        return 2 if self is MCS.QPSK else 4


@dataclass(frozen=True, slots=True)
class Frame:
    """Immutable metadata and payload for one PHY frame."""

    protocol_version: int
    kind: FrameKind
    mcs: MCS
    sequence: int
    payload: bytes

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int:
            raise FrameValidationError("protocol_version must be an integer")
        if self.protocol_version != CURRENT_PROTOCOL_VERSION:
            raise FrameValidationError(
                f"unsupported protocol_version {self.protocol_version}; "
                f"expected {CURRENT_PROTOCOL_VERSION}"
            )
        if not isinstance(self.kind, FrameKind):
            raise FrameValidationError("kind must be a FrameKind")
        if not isinstance(self.mcs, MCS):
            raise FrameValidationError("mcs must be an MCS")
        if type(self.sequence) is not int or not 0 <= self.sequence <= 0xFFFF:
            raise FrameValidationError("sequence must be an integer in [0, 65535]")
        if type(self.payload) is not bytes:
            raise FrameValidationError("payload must be immutable bytes")
        if len(self.payload) > MAX_PAYLOAD_LENGTH:
            raise FrameValidationError(
                f"payload exceeds the {MAX_PAYLOAD_LENGTH}-byte wire limit"
            )

    @property
    def payload_length(self) -> int:
        """Payload length carried by the serialized header."""

        return len(self.payload)


@dataclass(frozen=True, slots=True)
class FrameDecodeResult:
    """One validated frame and diagnostics owned by the same decode call."""

    frame: Frame
    wire_version: int
    fec_profile: FecProfile
    fec_report: TurboDecodeReport | None

    def __post_init__(self) -> None:
        if not isinstance(self.frame, Frame):
            raise TypeError("frame must be a Frame")
        if self.wire_version not in _FRAME_CODEC_BY_WIRE_VERSION:
            raise ValueError(
                "wire_version must identify a defined burst wire version"
            )
        if not isinstance(self.fec_profile, FecProfile):
            raise TypeError("fec_profile must be a FecProfile")
        if self.fec_report is not None and not isinstance(
            self.fec_report, TurboDecodeReport
        ):
            raise TypeError("fec_report must be a TurboDecodeReport or None")
        if self.wire_version == 1 and self.fec_report is not None:
            raise ValueError("wire v1 cannot carry a Turbo decode report")
        if self.wire_version == 2 and self.fec_report is None:
            raise ValueError("wire v2 requires its current Turbo decode report")


@runtime_checkable
class FrameCodecInterface(Protocol):
    """Frame-level contract used outside the PHY FEC implementation."""

    @property
    def fec_profile(self) -> FecProfile: ...

    @property
    def wire_version(self) -> int: ...

    def encode(self, frame: Frame) -> NDArray[np.uint8]: ...

    def decode_result(
        self,
        observations: ArrayLike,
        *,
        information_bit_count: int | None = None,
    ) -> FrameDecodeResult: ...

    def decode(
        self,
        observations: ArrayLike,
        *,
        information_bit_count: int | None = None,
    ) -> Frame: ...


# Burst wire versions, mirrored in `burst_header` (which imports this module,
# so the constants cannot be shared in the other direction).  Each version
# names one complete FEC profile; `mcs_table` maps them to LTE-style indices.
_CONVOLUTIONAL_R12_WIRE_VERSION = 1
_TURBO_WIRE_VERSION = 2
_CONVOLUTIONAL_R13_WIRE_VERSION = 3
_UNCODED_WIRE_VERSION = 4


def _wire_version_for_profile(profile: FecProfile) -> int:
    """Return the wire version that uniquely identifies one FEC profile."""

    if profile.decision_mode == "turbo":
        return _TURBO_WIRE_VERSION
    if profile.name.startswith("uncoded"):
        return _UNCODED_WIRE_VERSION
    if profile.rate_inverse == 3:
        return _CONVOLUTIONAL_R13_WIRE_VERSION
    return _CONVOLUTIONAL_R12_WIRE_VERSION


@dataclass(frozen=True, slots=True)
class FrameCodec:
    """Serialize/validate frames around one complete PHY-internal FEC codec."""

    fec: FecCodec
    wire_version: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.fec, FecCodec):
            raise TypeError("fec must implement the complete FecCodec interface")
        object.__setattr__(
            self,
            "wire_version",
            _wire_version_for_profile(self.fec.profile),
        )

    @property
    def fec_profile(self) -> FecProfile:
        """Expose only the bounded descriptor, never codec internals."""

        return self.fec.profile

    @property
    def last_decode_report(self) -> TurboDecodeReport | None:
        """Return compatibility-only last report, not per-call diagnostics."""

        return getattr(self.fec, "last_decode_report", None)

    def encode(self, frame: Frame) -> NDArray[np.uint8]:
        """Serialize, CRC-protect, and FEC-encode one frame."""

        try:
            serialized = np.frombuffer(serialize_frame(frame), dtype=np.uint8)
            information_bits = np.unpackbits(serialized, bitorder="big")
            return self.fec.encode(information_bits)
        except FecError as error:
            raise PhyCodecError(str(error)) from error

    def decode(
        self,
        observations: ArrayLike,
        *,
        information_bit_count: int | None = None,
    ) -> Frame:
        """Compatibility wrapper returning only the decoded frame."""

        return self.decode_result(
            observations,
            information_bit_count=information_bit_count,
        ).frame

    def decode_result(
        self,
        observations: ArrayLike,
        *,
        information_bit_count: int | None = None,
    ) -> FrameDecodeResult:
        """FEC-decode one frame with immutable per-call diagnostics."""

        fec_report: TurboDecodeReport | None = None
        try:
            if isinstance(self.fec, NativeTurboFecAdapter):
                fec_result = self.fec.decode_result(
                    observations,
                    information_bit_count=information_bit_count,
                )
                information = fec_result.information_bits
                fec_report = fec_result.report
            else:
                information = self.fec.decode(
                    observations,
                    information_bit_count=information_bit_count,
                )
        except FecError as error:
            report = getattr(error, "decode_report", None)
            if isinstance(report, TurboDecodeReport):
                fec_report = report
            failure = FrameDecodeFailure(
                self.wire_version,
                self.fec_profile,
                fec_report,
            )
            raise FrameDecodeError(str(error), failure=failure) from error
        try:
            frame = _decode_information_frame(information)
        except FrameDecodeError as error:
            failure = FrameDecodeFailure(
                self.wire_version,
                self.fec_profile,
                fec_report,
            )
            raise type(error)(str(error), failure=failure) from error
        return FrameDecodeResult(
            frame,
            self.wire_version,
            self.fec_profile,
            fec_report,
        )


@dataclass(frozen=True, slots=True)
class DualVersionFrameDecoder:
    """Dispatch one complete coded frame using the burst wire version."""

    v1: FrameCodec
    v2: FrameCodec
    v3: FrameCodec | None = None
    v4: FrameCodec | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.v1, FrameCodec) or not isinstance(self.v2, FrameCodec):
            raise TypeError("v1 and v2 must be FrameCodec instances")
        for optional in (self.v3, self.v4):
            if optional is not None and not isinstance(optional, FrameCodec):
                raise TypeError("v3 and v4 must be FrameCodec instances or None")

    @property
    def last_decode_report(self) -> TurboDecodeReport | None:
        return self.v2.last_decode_report

    def __call__(
        self,
        observations: ArrayLike,
        *,
        wire_version: int = 1,
        information_bit_count: int | None = None,
    ) -> Frame:
        """Compatibility wrapper returning only the decoded frame."""

        return self.decode_result(
            observations,
            wire_version=wire_version,
            information_bit_count=information_bit_count,
        ).frame

    def decode_result(
        self,
        observations: ArrayLike,
        *,
        wire_version: int = 1,
        information_bit_count: int | None = None,
    ) -> FrameDecodeResult:
        """Dispatch one decode and return its frame/report atomically."""

        if wire_version == 1:
            return self.v1.decode_result(
                observations,
                information_bit_count=information_bit_count,
            )
        if wire_version == 2:
            return self.v2.decode_result(
                observations,
                information_bit_count=information_bit_count,
            )
        selected = {3: self.v3, 4: self.v4}.get(wire_version)
        if selected is not None:
            return selected.decode_result(
                observations,
                information_bit_count=information_bit_count,
            )
        raise FrameDecodeError(f"unsupported burst wire version {wire_version}")


def make_convolutional_frame_codec(
    *,
    hard_decoder: HardDecoder | None = None,
    rate_inverse: int = 2,
) -> FrameCodec:
    """Build a convolutional frame codec, optionally with a native decoder.

    Rate 1/2 is the stable wire-v1 contract; rate 1/3 is the separate wire-v3
    mother code.
    """

    return FrameCodec(
        ConvolutionalFecAdapter(
            max_information_bits=_MAX_INFORMATION_BITS,
            hard_decoder=hard_decoder,
            rate_inverse=rate_inverse,
        )
    )


def make_soft_convolutional_frame_codec(
    *,
    soft_decoder: SoftDecoder | None = None,
    rate_inverse: int = 2,
) -> FrameCodec:
    """Build the optional soft-input codec with unchanged encoded bits."""

    return FrameCodec(
        SoftConvolutionalFecAdapter(
            max_information_bits=_MAX_INFORMATION_BITS,
            soft_decoder=soft_decoder,
            rate_inverse=rate_inverse,
        )
    )


def make_uncoded_frame_codec(*, soft: bool = False) -> FrameCodec:
    """Build the wire-v4 rate-1 codec, which detects but never corrects errors."""

    adapter = (
        SoftUncodedFecAdapter(max_information_bits=_MAX_INFORMATION_BITS)
        if soft
        else UncodedFecAdapter(max_information_bits=_MAX_INFORMATION_BITS)
    )
    return FrameCodec(adapter)


def make_turbo_frame_codec(*, max_iterations: int = 8) -> FrameCodec:
    """Build the native LTE-derived rate-1/3 Turbo frame codec."""

    return FrameCodec(
        NativeTurboFecAdapter(
            max_information_bits=_MAX_INFORMATION_BITS,
            max_iterations=max_iterations,
        )
    )


def make_dual_version_frame_decoder(
    *,
    v1_codec: FrameCodec | None = None,
    v2_codec: FrameCodec | None = None,
    v3_codec: FrameCodec | None = None,
    v4_codec: FrameCodec | None = None,
    turbo_max_iterations: int = 8,
    accept_all_wire_versions: bool = False,
) -> DualVersionFrameDecoder:
    """Build a receiver accepting every configured burst wire version.

    The v1/v2 pair is always present.  The rate-1/3 and uncoded versions cost a
    portable decoder each, so they are attached only when a caller selects them
    or asks for the complete table.
    """

    if accept_all_wire_versions:
        v3_codec = v3_codec or make_convolutional_frame_codec(rate_inverse=3)
        v4_codec = v4_codec or make_uncoded_frame_codec()
    return DualVersionFrameDecoder(
        v1=v1_codec or make_convolutional_frame_codec(),
        v2=v2_codec or make_turbo_frame_codec(max_iterations=turbo_max_iterations),
        v3=v3_codec,
        v4=v4_codec,
    )


_DEFAULT_FRAME_CODEC = make_convolutional_frame_codec()
_DEFAULT_TURBO_FRAME_CODEC = make_turbo_frame_codec()
_DEFAULT_R13_FRAME_CODEC = make_convolutional_frame_codec(rate_inverse=3)
_DEFAULT_UNCODED_FRAME_CODEC = make_uncoded_frame_codec()
DEFAULT_DUAL_VERSION_FRAME_DECODER = make_dual_version_frame_decoder(
    v1_codec=_DEFAULT_FRAME_CODEC,
    v2_codec=_DEFAULT_TURBO_FRAME_CODEC,
)
MAX_CODED_FRAME_BITS = _DEFAULT_FRAME_CODEC.fec_profile.coded_bit_count(
    _MAX_INFORMATION_BITS
)
MAX_TURBO_CODED_FRAME_BITS = _DEFAULT_TURBO_FRAME_CODEC.fec_profile.coded_bit_count(
    _MAX_INFORMATION_BITS
)


def coded_frame_bit_count(frame: Frame) -> int:
    """Return the selected v1 coded length without exposing FEC internals."""

    if not isinstance(frame, Frame):
        raise FrameValidationError("frame must be a Frame")
    return _DEFAULT_FRAME_CODEC.fec_profile.coded_bit_count(
        information_frame_bit_count(frame)
    )


def information_frame_bit_count(frame: Frame) -> int:
    """Return the exact serialized frame length before FEC."""

    if not isinstance(frame, Frame):
        raise FrameValidationError("frame must be a Frame")
    return (_HEADER.size + frame.payload_length + _CRC.size) * 8


def turbo_coded_frame_bit_count(frame: Frame) -> int:
    """Return the fixed v2 Turbo coded length for one serialized frame."""

    return turbo_coded_bit_count(information_frame_bit_count(frame))


def encode_frame(frame: Frame) -> NDArray[np.uint8]:
    """Serialize, CRC-protect, and rate-1/2 encode one frame.

    The returned one-dimensional array contains one ``uint8`` value per coded
    bit. Six zero input bits terminate the encoder in the all-zero state.
    """

    return _DEFAULT_FRAME_CODEC.encode(frame)


def decode_frame(coded_bits: ArrayLike) -> Frame:
    """Hard-decision Viterbi decode and validate one complete coded frame."""

    return _DEFAULT_FRAME_CODEC.decode(coded_bits)


def encode_turbo_frame(frame: Frame) -> NDArray[np.uint8]:
    """Serialize and encode one frame with the fixed wire-v2 Turbo profile."""

    return _DEFAULT_TURBO_FRAME_CODEC.encode(frame)


_FRAME_CODEC_BY_WIRE_VERSION: dict[int, FrameCodec] = {
    _CONVOLUTIONAL_R12_WIRE_VERSION: _DEFAULT_FRAME_CODEC,
    _TURBO_WIRE_VERSION: _DEFAULT_TURBO_FRAME_CODEC,
    _CONVOLUTIONAL_R13_WIRE_VERSION: _DEFAULT_R13_FRAME_CODEC,
    _UNCODED_WIRE_VERSION: _DEFAULT_UNCODED_FRAME_CODEC,
}
SUPPORTED_FRAME_WIRE_VERSIONS = tuple(sorted(_FRAME_CODEC_BY_WIRE_VERSION))


def frame_codec_for_wire_version(wire_version: int) -> FrameCodec:
    """Return the fixed reference codec for one burst wire version."""

    try:
        return _FRAME_CODEC_BY_WIRE_VERSION[wire_version]
    except KeyError:
        raise FrameValidationError(
            f"unsupported burst wire version {wire_version}; "
            f"expected one of {list(SUPPORTED_FRAME_WIRE_VERSIONS)}"
        ) from None


def encode_frame_by_wire_version(frame: Frame, wire_version: int) -> NDArray[np.uint8]:
    """Serialize and encode one frame with the profile that version names."""

    return frame_codec_for_wire_version(wire_version).encode(frame)


def coded_frame_bit_count_by_wire_version(frame: Frame, wire_version: int) -> int:
    """Return the exact coded length one wire version produces."""

    if not isinstance(frame, Frame):
        raise FrameValidationError("frame must be a Frame")
    codec = frame_codec_for_wire_version(wire_version)
    return codec.fec_profile.coded_bit_count(information_frame_bit_count(frame))


def decode_frame_by_wire_version(
    observations: ArrayLike,
    *,
    wire_version: int = 1,
    information_bit_count: int | None = None,
) -> Frame:
    """Receive either stable convolutional v1 or fixed Turbo v2."""

    return DEFAULT_DUAL_VERSION_FRAME_DECODER(
        observations,
        wire_version=wire_version,
        information_bit_count=information_bit_count,
    )


def decode_uncoded_frame(decoded_bits: ArrayLike) -> Frame:
    """Validate frame bits already decoded by a compatible FEC backend.

    The input includes the six terminating zero-state bits emitted by the K=7
    decoder. This seam lets the production GNU Radio Viterbi implementation
    share the exact same byte-order, length, CRC, and metadata validation as
    the portable NumPy reference decoder.
    """

    decoded = _as_bits(decoded_bits, "decoded_bits")
    minimum = (_HEADER.size + _CRC.size) * 8 + _TAIL_BITS
    if decoded.size < minimum:
        raise FrameDecodeError(
            f"decoded frame is too short: got {decoded.size} bits, need at least {minimum}"
        )
    if np.any(decoded[-_TAIL_BITS:]):
        raise FrameDecodeError("decoded frame does not end in the required zero-state tail")
    return _decode_information_frame(decoded[:-_TAIL_BITS])


def _decode_information_frame(information_bits: ArrayLike) -> Frame:
    data_bits = _as_bits(information_bits, "information_bits")
    minimum = (_HEADER.size + _CRC.size) * 8
    if data_bits.size < minimum:
        raise FrameDecodeError(
            f"decoded frame is too short: got {data_bits.size} bits, need at least {minimum}"
        )
    if data_bits.size % 8:
        raise FrameDecodeError("decoded frame does not end on a byte boundary")
    return deserialize_frame(np.packbits(data_bits, bitorder="big").tobytes())


def deserialize_frame(data: bytes) -> Frame:
    """Validate canonical header/payload/CRC bytes and return one frame.

    This is the byte-oriented PDU boundary.  FEC codewords, burst headers, and
    sample boundaries are intentionally outside this representation.
    """

    if type(data) is not bytes:
        raise FrameDecodeError("serialized frame must be immutable bytes")
    minimum = _HEADER.size + _CRC.size
    if len(data) < minimum:
        raise FrameDecodeError(
            f"decoded frame is too short: got {len(data)} bytes, need at least {minimum}"
        )

    protected, received_crc = data[:-_CRC.size], data[-_CRC.size :]
    expected_crc = _CRC.pack(zlib.crc32(protected) & 0xFFFFFFFF)
    if received_crc != expected_crc:
        raise FrameIntegrityError("frame CRC-32 mismatch")

    version, kind_value, mcs_value, sequence, payload_length = _HEADER.unpack_from(data)
    expected_size = _HEADER.size + payload_length + _CRC.size
    if len(data) != expected_size:
        raise FrameDecodeError(
            f"payload length field requires {expected_size} bytes, decoded {len(data)}"
        )

    try:
        kind = FrameKind(kind_value)
    except ValueError as error:
        raise FrameDecodeError(f"unknown frame kind {kind_value}") from error
    try:
        mcs = MCS(mcs_value)
    except ValueError as error:
        raise FrameDecodeError(f"unknown MCS {mcs_value}") from error

    try:
        return Frame(
            protocol_version=version,
            kind=kind,
            mcs=mcs,
            sequence=sequence,
            payload=data[_HEADER.size : -_CRC.size],
        )
    except FrameValidationError as error:
        raise FrameDecodeError(str(error)) from error


def serialize_frame(frame: Frame) -> bytes:
    """Return canonical big-endian header/payload/CRC bytes for one frame."""

    if not isinstance(frame, Frame):
        raise FrameValidationError("frame must be a Frame")
    header = _HEADER.pack(
        frame.protocol_version,
        int(frame.kind),
        int(frame.mcs),
        frame.sequence,
        frame.payload_length,
    )
    protected = header + frame.payload
    return protected + _CRC.pack(zlib.crc32(protected) & 0xFFFFFFFF)


def map_symbols(bits: ArrayLike, mcs: MCS) -> NDArray[np.complex64]:
    """Map bits to unit-average-power QPSK or Gray-coded 16QAM symbols."""

    mode = _require_mcs(mcs)
    values = _as_bits(bits, "bits")
    if values.size % mode.bits_per_symbol:
        raise PhyCodecError(
            f"{mode.name} mapping requires a multiple of {mode.bits_per_symbol} bits"
        )
    if not values.size:
        return np.empty(0, dtype=np.complex64)

    grouped = values.reshape(-1, mode.bits_per_symbol)
    if mode is MCS.QPSK:
        real = 1.0 - 2.0 * grouped[:, 0]
        imag = 1.0 - 2.0 * grouped[:, 1]
        symbols = (real + 1j * imag) / np.sqrt(2.0)
    else:
        levels = np.array([-3.0, -1.0, 3.0, 1.0], dtype=np.float32)
        real_index = 2 * grouped[:, 0] + grouped[:, 1]
        imag_index = 2 * grouped[:, 2] + grouped[:, 3]
        symbols = (levels[real_index] + 1j * levels[imag_index]) / np.sqrt(10.0)
    return symbols.astype(np.complex64, copy=False)


def demap_symbols(symbols: ArrayLike, mcs: MCS) -> NDArray[np.uint8]:
    """Hard-decision demap QPSK or Gray-coded 16QAM symbols to bits."""

    mode = _require_mcs(mcs)
    values = np.asarray(symbols)
    if values.ndim != 1:
        raise PhyCodecError("symbols must be a one-dimensional array")
    if not np.issubdtype(values.dtype, np.number):
        raise PhyCodecError("symbols must be numeric")
    values = values.astype(np.complex64, copy=False)
    if not np.all(np.isfinite(values.real)) or not np.all(np.isfinite(values.imag)):
        raise PhyCodecError("symbols must contain only finite values")
    if not values.size:
        return np.empty(0, dtype=np.uint8)

    if mode is MCS.QPSK:
        decisions = np.column_stack((values.real < 0.0, values.imag < 0.0))
        return decisions.astype(np.uint8, copy=False).reshape(-1)

    scaled = values * np.sqrt(10.0)
    bins = np.array([-2.0, 0.0, 2.0], dtype=np.float32)
    pairs = np.array([[0, 0], [0, 1], [1, 1], [1, 0]], dtype=np.uint8)
    real_bits = pairs[np.digitize(scaled.real, bins)]
    imag_bits = pairs[np.digitize(scaled.imag, bins)]
    decisions = np.column_stack(
        (real_bits[:, 0], real_bits[:, 1], imag_bits[:, 0], imag_bits[:, 1])
    )
    return decisions.reshape(-1)


def _as_bits(values: ArrayLike, name: str) -> NDArray[np.uint8]:
    bits = np.asarray(values)
    if bits.ndim != 1:
        raise PhyCodecError(f"{name} must be a one-dimensional array")
    if not (np.issubdtype(bits.dtype, np.integer) or np.issubdtype(bits.dtype, np.bool_)):
        raise PhyCodecError(f"{name} must contain integer bits")
    if np.any((bits != 0) & (bits != 1)):
        raise PhyCodecError(f"{name} must contain only 0 and 1")
    return bits.astype(np.uint8, copy=False)


def _require_mcs(mcs: MCS) -> MCS:
    if not isinstance(mcs, MCS):
        raise PhyCodecError("mcs must be an MCS")
    return mcs
