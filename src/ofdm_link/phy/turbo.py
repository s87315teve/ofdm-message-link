"""LTE-derived rate-1/3 Turbo-code contract and portable correctness oracle.

The normative primitives follow ETSI TS 136 212 V19.2.0 clauses 5.1.1,
5.1.2, and 5.1.3.2.  This project deliberately stops before LTE rate
matching: every systematic/parity output, including zero-valued filler
positions, is transmitted in time-major ``systematic, parity-1, parity-2``
order.  The scalar trellis loops in this module are a portable test oracle;
the production adapter is implemented by the private native extension.
"""

from __future__ import annotations

import math
import threading
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .fec import FecError, FecProfile, _generators_for

TURBO_MAX_CODE_BLOCK_BITS = 6_144
TURBO_CODE_BLOCK_CRC_BITS = 24
TURBO_TAIL_BITS_PER_BLOCK = 12
TURBO_MAX_ITERATIONS = 8
CRC24B_POLYNOMIAL = 0x1800063


class TurboError(ValueError):
    """Raised when the fixed LTE-derived Turbo profile rejects an input."""


class TurboNativeUnavailableError(RuntimeError):
    """Raised when the private compiled Turbo core is missing from this install.

    Unlike the optional GNU Radio runtime, this extension backs the default
    wire-v2 FEC profile, so an unbuilt extension breaks the documented default
    path rather than an optional one.
    """


# ETSI TS 136 212 V19.2.0, table 5.1.3-3.  Each tuple is (K, f1, f2).
QPP_PARAMETERS: tuple[tuple[int, int, int], ...] = (
    (40, 3, 10), (48, 7, 12), (56, 19, 42), (64, 7, 16),
    (72, 7, 18), (80, 11, 20), (88, 5, 22), (96, 11, 24),
    (104, 7, 26), (112, 41, 84), (120, 103, 90), (128, 15, 32),
    (136, 9, 34), (144, 17, 108), (152, 9, 38), (160, 21, 120),
    (168, 101, 84), (176, 21, 44), (184, 57, 46), (192, 23, 48),
    (200, 13, 50), (208, 27, 52), (216, 11, 36), (224, 27, 56),
    (232, 85, 58), (240, 29, 60), (248, 33, 62), (256, 15, 32),
    (264, 17, 198), (272, 33, 68), (280, 103, 210), (288, 19, 36),
    (296, 19, 74), (304, 37, 76), (312, 19, 78), (320, 21, 120),
    (328, 21, 82), (336, 115, 84), (344, 193, 86), (352, 21, 44),
    (360, 133, 90), (368, 81, 46), (376, 45, 94), (384, 23, 48),
    (392, 243, 98), (400, 151, 40), (408, 155, 102), (416, 25, 52),
    (424, 51, 106), (432, 47, 72), (440, 91, 110), (448, 29, 168),
    (456, 29, 114), (464, 247, 58), (472, 29, 118), (480, 89, 180),
    (488, 91, 122), (496, 157, 62), (504, 55, 84), (512, 31, 64),
    (528, 17, 66), (544, 35, 68), (560, 227, 420), (576, 65, 96),
    (592, 19, 74), (608, 37, 76), (624, 41, 234), (640, 39, 80),
    (656, 185, 82), (672, 43, 252), (688, 21, 86), (704, 155, 44),
    (720, 79, 120), (736, 139, 92), (752, 23, 94), (768, 217, 48),
    (784, 25, 98), (800, 17, 80), (816, 127, 102), (832, 25, 52),
    (848, 239, 106), (864, 17, 48), (880, 137, 110), (896, 215, 112),
    (912, 29, 114), (928, 15, 58), (944, 147, 118), (960, 29, 60),
    (976, 59, 122), (992, 65, 124), (1008, 55, 84), (1024, 31, 64),
    (1056, 17, 66), (1088, 171, 204), (1120, 67, 140), (1152, 35, 72),
    (1184, 19, 74), (1216, 39, 76), (1248, 19, 78), (1280, 199, 240),
    (1312, 21, 82), (1344, 211, 252), (1376, 21, 86), (1408, 43, 88),
    (1440, 149, 60), (1472, 45, 92), (1504, 49, 846), (1536, 71, 48),
    (1568, 13, 28), (1600, 17, 80), (1632, 25, 102), (1664, 183, 104),
    (1696, 55, 954), (1728, 127, 96), (1760, 27, 110), (1792, 29, 112),
    (1824, 29, 114), (1856, 57, 116), (1888, 45, 354), (1920, 31, 120),
    (1952, 59, 610), (1984, 185, 124), (2016, 113, 420), (2048, 31, 64),
    (2112, 17, 66), (2176, 171, 136), (2240, 209, 420), (2304, 253, 216),
    (2368, 367, 444), (2432, 265, 456), (2496, 181, 468), (2560, 39, 80),
    (2624, 27, 164), (2688, 127, 504), (2752, 143, 172), (2816, 43, 88),
    (2880, 29, 300), (2944, 45, 92), (3008, 157, 188), (3072, 47, 96),
    (3136, 13, 28), (3200, 111, 240), (3264, 443, 204), (3328, 51, 104),
    (3392, 51, 212), (3456, 451, 192), (3520, 257, 220), (3584, 57, 336),
    (3648, 313, 228), (3712, 271, 232), (3776, 179, 236), (3840, 331, 120),
    (3904, 363, 244), (3968, 375, 248), (4032, 127, 168), (4096, 31, 64),
    (4160, 33, 130), (4224, 43, 264), (4288, 33, 134), (4352, 477, 408),
    (4416, 35, 138), (4480, 233, 280), (4544, 357, 142), (4608, 337, 480),
    (4672, 37, 146), (4736, 71, 444), (4800, 71, 120), (4864, 37, 152),
    (4928, 39, 462), (4992, 127, 234), (5056, 39, 158), (5120, 39, 80),
    (5184, 31, 96), (5248, 113, 902), (5312, 41, 166), (5376, 251, 336),
    (5440, 43, 170), (5504, 21, 86), (5568, 43, 174), (5632, 45, 176),
    (5696, 45, 178), (5760, 161, 120), (5824, 89, 182), (5888, 323, 184),
    (5952, 47, 186), (6016, 23, 94), (6080, 47, 190), (6144, 263, 480),
)

_QPP_BY_SIZE = {size: (f1, f2) for size, f1, f2 in QPP_PARAMETERS}
_QPP_SIZES = tuple(size for size, _, _ in QPP_PARAMETERS)


@dataclass(frozen=True, slots=True)
class TurboSegmentationPlan:
    """Constant-size description of one information frame's code blocks."""

    information_bit_count: int
    block_sizes: tuple[int, ...]
    filler_bit_count: int
    code_block_crc_bits: int

    @property
    def block_count(self) -> int:
        return len(self.block_sizes)

    @property
    def segmented(self) -> bool:
        return self.block_count > 1

    @property
    def coded_bit_count(self) -> int:
        return sum(3 * (size + 4) for size in self.block_sizes)


@dataclass(frozen=True, slots=True)
class TurboDecodeReport:
    """Bounded per-frame decoder observations; no LLR or frame history."""

    configured_max_iterations: int
    actual_iterations_per_block: tuple[int, ...]
    early_stop_reasons: tuple[str, ...]
    code_block_crc_ok: tuple[bool | None, ...]
    outer_crc_ok: bool | None
    block_count: int
    filler_bit_count: int
    filler_ok: bool = True


@dataclass(frozen=True, slots=True)
class TurboDecodeResult:
    information_bits: NDArray[np.uint8]
    report: TurboDecodeReport


class NativeTurboFecAdapter:
    """Whole-frame adapter around the private compiled Max-Log-MAP core."""

    def __init__(self, *, max_information_bits: int, max_iterations: int = 8) -> None:
        if type(max_iterations) is not int or max_iterations not in {2, 4, 6, 8}:
            raise ValueError("max_iterations must be one of 2, 4, 6, or 8")
        self._profile = FecProfile(
            name="lte-derived-turbo-r1/3",
            decision_mode="turbo",
            max_information_bits=max_information_bits,
            _coded_bit_counter=turbo_coded_bit_count,
        )
        self._max_iterations = max_iterations
        self._last_decode_report: TurboDecodeReport | None = None
        self._report_lock = threading.Lock()

    @property
    def profile(self) -> FecProfile:
        return self._profile

    @property
    def max_iterations(self) -> int:
        return self._max_iterations

    @property
    def last_decode_report(self) -> TurboDecodeReport | None:
        """Return the last completed report for compatibility-only observers.

        Callers that need a report associated with one decode must use
        :meth:`decode_result`; this property cannot provide that association
        across concurrent calls.
        """

        with self._report_lock:
            return self._last_decode_report

    def encode(self, information_bits: ArrayLike) -> NDArray[np.uint8]:
        try:
            information = _as_bits(information_bits, "information_bits")
        except TurboError as error:
            raise FecError(str(error)) from error
        self.profile.validate_information_bit_count(int(information.size))
        plan = turbo_segmentation_plan(int(information.size))
        try:
            native = _native_module()
            encoded = native.encode(
                information.tobytes(),
                plan.block_sizes,
                plan.filler_bit_count,
                _qpp_for_plan(plan),
            )
        except (ImportError, ValueError, RuntimeError) as error:
            raise FecError(f"native Turbo encode failed: {error}") from error
        result = np.frombuffer(encoded, dtype=np.uint8)
        if result.size != plan.coded_bit_count:
            raise FecError("native Turbo encoder returned an inconsistent length")
        result.setflags(write=False)
        return result

    def decode(
        self,
        observations: ArrayLike,
        *,
        information_bit_count: int | None = None,
    ) -> NDArray[np.uint8]:
        """Compatibility wrapper returning only decoded information bits."""

        return self.decode_result(
            observations,
            information_bit_count=information_bit_count,
        ).information_bits

    def decode_result(
        self,
        observations: ArrayLike,
        *,
        information_bit_count: int | None = None,
    ) -> TurboDecodeResult:
        """Decode one frame and return its immutable per-call report."""

        with self._report_lock:
            self._last_decode_report = None
        if information_bit_count is None:
            raise FecError("Turbo decode requires information_bit_count")
        count = self.profile.validate_information_bit_count(information_bit_count)
        try:
            llrs = _as_llrs(observations).astype(np.float32, copy=False)
        except TurboError as error:
            raise FecError(str(error)) from error
        plan = turbo_segmentation_plan(count)
        if llrs.size != plan.coded_bit_count:
            raise FecError(
                f"observations contain {llrs.size} LLRs; expected {plan.coded_bit_count}"
            )
        try:
            native = _native_module()
            decoded, raw_report = native.decode(
                llrs.tobytes(),
                count,
                plan.block_sizes,
                plan.filler_bit_count,
                _qpp_for_plan(plan),
                self.max_iterations,
            )
        except (ImportError, ValueError, RuntimeError) as error:
            raise FecError(f"native Turbo decode failed: {error}") from error
        report = TurboDecodeReport(
            configured_max_iterations=int(raw_report["configured_max_iterations"]),
            actual_iterations_per_block=tuple(raw_report["actual_iterations_per_block"]),
            early_stop_reasons=tuple(raw_report["early_stop_reasons"]),
            code_block_crc_ok=tuple(raw_report["code_block_crc_ok"]),
            outer_crc_ok=raw_report["outer_crc_ok"],
            block_count=int(raw_report["block_count"]),
            filler_bit_count=int(raw_report["filler_bit_count"]),
            filler_ok=bool(raw_report["filler_ok"]),
        )
        with self._report_lock:
            self._last_decode_report = report
        if not report.filler_ok:
            raise FecError(
                "decoded Turbo filler bits are not zero",
                decode_report=report,
            )
        if plan.segmented and not all(outcome is True for outcome in report.code_block_crc_ok):
            raise FecError(
                "one or more Turbo code-block CRC-24B checks failed",
                decode_report=report,
            )
        result = np.frombuffer(decoded, dtype=np.uint8)
        if result.size != count or np.any(result > 1):
            raise FecError(
                "native Turbo decoder returned invalid information bits",
                decode_report=report,
            )
        result.setflags(write=False)
        return TurboDecodeResult(result, report)


def _native_module():
    """Import the private extension only when the Turbo adapter is used."""

    try:
        from . import _turbo_native
    except ImportError as error:
        raise TurboNativeUnavailableError(
            "the native Turbo core is not built for this installation; rebuild it "
            "with `python -m pip install -e .` from the repository root, or select "
            "wire v1 with the convolutional_hard profile"
        ) from error

    return _turbo_native


def native_convolutional_soft_decoder(rate_inverse: int) -> Callable[..., NDArray[np.uint8]]:
    """Return a soft convolutional decoder backed by the native Viterbi.

    GNU Radio's ``cc_decoder`` only builds k=7 rate-1/2 graphs, so the rate-1/3
    mother code is decoded by the same private extension that hosts the Turbo
    core.  The returned callable keeps the project LLR convention where a
    positive value means bit 0, and includes the terminating tail bits so the
    FEC adapter can validate them.
    """

    generators = list(_generators_for(rate_inverse))

    def decode(llrs: ArrayLike) -> NDArray[np.uint8]:
        values = np.ascontiguousarray(llrs, dtype=np.float32)
        decoded = _native_module().viterbi(values.tobytes(), generators)
        return np.frombuffer(decoded, dtype=np.uint8)

    return decode


def _qpp_for_plan(plan: TurboSegmentationPlan) -> tuple[tuple[int, int], ...]:
    return tuple(_QPP_BY_SIZE[size] for size in plan.block_sizes)


def turbo_segmentation_plan(information_bit_count: int) -> TurboSegmentationPlan:
    """Apply TS 36.212 clause 5.1.2 to one bounded information bit count.

    Clause 5.1.2 is specified for ``B > 0``.  The project extends the same
    filler rule to ``B == 0`` so the FEC seam retains its existing empty-input
    test contract; real serialized frames are always non-empty.
    """

    if type(information_bit_count) is not int or information_bit_count < 0:
        raise TurboError("information_bit_count must be a non-negative integer")
    return _turbo_segmentation_plan(information_bit_count)


@lru_cache(maxsize=256)
def _turbo_segmentation_plan(b: int) -> TurboSegmentationPlan:
    """Derive the plan for one already-validated bit count.

    A link repeats a handful of frame sizes, so this was recomputed tens of
    thousands of times per run for the same few inputs.  The result is a frozen
    dataclass of integers and a tuple, so sharing one instance is safe.  The
    validating wrapper above stays outside the cache: lru_cache keys on hash
    and equality, so a float would otherwise reach a cached integer entry
    instead of being rejected.
    """

    if b <= TURBO_MAX_CODE_BLOCK_BITS:
        block_crc_bits = 0
        block_count = 1
        b_prime = b
    else:
        block_crc_bits = TURBO_CODE_BLOCK_CRC_BITS
        block_count = math.ceil(b / (TURBO_MAX_CODE_BLOCK_BITS - block_crc_bits))
        b_prime = b + block_count * block_crc_bits

    try:
        k_plus_index = next(
            index
            for index, size in enumerate(_QPP_SIZES)
            if block_count * size >= b_prime
        )
    except StopIteration as error:
        raise TurboError("information frame requires unsupported Turbo block sizes") from error
    k_plus = _QPP_SIZES[k_plus_index]

    if block_count == 1:
        block_sizes = (k_plus,)
    else:
        if k_plus_index == 0:
            raise TurboError("segmentation cannot select a smaller legal block size")
        k_minus = _QPP_SIZES[k_plus_index - 1]
        delta = k_plus - k_minus
        c_minus = (block_count * k_plus - b_prime) // delta
        c_plus = block_count - c_minus
        block_sizes = (k_minus,) * c_minus + (k_plus,) * c_plus

    filler = sum(block_sizes) - b_prime
    if not 0 <= filler <= block_sizes[0]:
        raise TurboError("invalid filler count derived from segmentation")
    return TurboSegmentationPlan(b, block_sizes, filler, block_crc_bits)


def turbo_coded_bit_count(information_bit_count: int) -> int:
    """Return the exact no-rate-matching coded length."""

    return turbo_segmentation_plan(information_bit_count).coded_bit_count


def qpp_permutation(block_size: int) -> NDArray[np.int64]:
    """Return ``Pi(i) = (f1*i + f2*i^2) mod K`` for one legal LTE K."""

    if type(block_size) is not int or block_size not in _QPP_BY_SIZE:
        raise TurboError("block_size must be one of the 188 LTE QPP sizes")
    f1, f2 = _QPP_BY_SIZE[block_size]
    indices = np.arange(block_size, dtype=np.int64)
    permutation = (f1 * indices + f2 * indices * indices) % block_size
    if np.unique(permutation).size != block_size:
        raise TurboError("QPP parameters did not produce a permutation")
    permutation.setflags(write=False)
    return permutation


def crc24b(bits: ArrayLike) -> NDArray[np.uint8]:
    """Return the 24 MSB-first parity bits with initial value and xor-out zero."""

    values = _as_bits(bits, "bits")
    register = 0
    polynomial_without_top = CRC24B_POLYNOMIAL & 0xFFFFFF
    for bit in values:
        feedback = ((register >> 23) & 1) ^ int(bit)
        register = (register << 1) & 0xFFFFFF
        if feedback:
            register ^= polynomial_without_top
    parity = np.array(
        [(register >> shift) & 1 for shift in range(23, -1, -1)],
        dtype=np.uint8,
    )
    parity.setflags(write=False)
    return parity


def crc24b_ok(bits_with_crc: ArrayLike) -> bool:
    values = _as_bits(bits_with_crc, "bits_with_crc")
    if values.size < TURBO_CODE_BLOCK_CRC_BITS:
        return False
    return bool(
        np.array_equal(
            crc24b(values[:-TURBO_CODE_BLOCK_CRC_BITS]),
            values[-TURBO_CODE_BLOCK_CRC_BITS:],
        )
    )


def segment_information(
    information_bits: ArrayLike,
) -> tuple[TurboSegmentationPlan, tuple[NDArray[np.uint8], ...]]:
    """Insert filler and segmented CRC-24B without exposing them to callers."""

    information = _as_bits(information_bits, "information_bits")
    plan = turbo_segmentation_plan(int(information.size))
    blocks: list[NDArray[np.uint8]] = []
    source_index = 0
    for block_index, block_size in enumerate(plan.block_sizes):
        block = np.zeros(block_size, dtype=np.uint8)
        data_start = plan.filler_bit_count if block_index == 0 else 0
        data_end = block_size - plan.code_block_crc_bits
        copy_count = data_end - data_start
        block[data_start:data_end] = information[source_index : source_index + copy_count]
        source_index += copy_count
        if plan.segmented:
            block[data_end:] = crc24b(block[:data_end])
        block.setflags(write=False)
        blocks.append(block)
    if source_index != information.size:
        raise TurboError("segmentation did not consume the exact information frame")
    return plan, tuple(blocks)


def desegment_information(
    blocks: tuple[ArrayLike, ...],
    plan: TurboSegmentationPlan,
) -> NDArray[np.uint8]:
    """Validate filler/CRC, remove FEC-internal fields, and join information."""

    if not isinstance(plan, TurboSegmentationPlan):
        raise TypeError("plan must be a TurboSegmentationPlan")
    if len(blocks) != plan.block_count:
        raise TurboError("decoded block count does not match the segmentation plan")
    pieces: list[NDArray[np.uint8]] = []
    for block_index, (values, expected_size) in enumerate(
        zip(blocks, plan.block_sizes, strict=True)
    ):
        block = _as_bits(values, f"block {block_index}")
        if block.size != expected_size:
            raise TurboError(f"decoded block {block_index} has the wrong length")
        data_start = plan.filler_bit_count if block_index == 0 else 0
        if data_start and np.any(block[:data_start]):
            raise TurboError("decoded filler bits are not zero")
        data_end = expected_size - plan.code_block_crc_bits
        if plan.segmented and not crc24b_ok(block):
            raise TurboError(f"code-block CRC-24B mismatch for block {block_index}")
        pieces.append(block[data_start:data_end])
    result = np.concatenate(pieces).astype(np.uint8, copy=False)
    if result.size != plan.information_bit_count:
        raise TurboError("desegmented information length is inconsistent")
    result.setflags(write=False)
    return result


def turbo_encode_oracle(information_bits: ArrayLike) -> NDArray[np.uint8]:
    """Portable TS 36.212 encoder oracle with no puncturing or rate matching."""

    plan, blocks = segment_information(information_bits)
    encoded = [_encode_block(block) for block in blocks]
    result = np.concatenate(encoded).astype(np.uint8, copy=False)
    if result.size != plan.coded_bit_count:
        raise TurboError("Turbo encoder produced an inconsistent coded length")
    result.setflags(write=False)
    return result


def turbo_decode_logmap_oracle(
    observations: ArrayLike,
    information_bit_count: int,
    *,
    max_iterations: int = TURBO_MAX_ITERATIONS,
) -> TurboDecodeResult:
    """Portable Log-MAP oracle for project-convention positive-for-zero LLRs."""

    llrs = _as_llrs(observations)
    if type(max_iterations) is not int or max_iterations not in {2, 4, 6, 8}:
        raise TurboError("max_iterations must be one of 2, 4, 6, or 8")
    plan = turbo_segmentation_plan(information_bit_count)
    if llrs.size != plan.coded_bit_count:
        raise TurboError(
            f"observations contain {llrs.size} LLRs; expected {plan.coded_bit_count}"
        )

    decoded_blocks: list[NDArray[np.uint8]] = []
    iterations: list[int] = []
    reasons: list[str] = []
    block_crc: list[bool | None] = []
    offset = 0
    outer_crc_ok: bool | None = None
    for block_index, block_size in enumerate(plan.block_sizes):
        coded_count = 3 * (block_size + 4)
        block_llrs = llrs[offset : offset + coded_count]
        offset += coded_count

        def stop(candidate: NDArray[np.uint8]) -> tuple[bool, str, bool | None]:
            if plan.segmented:
                ok = crc24b_ok(candidate)
                return ok, "code_block_crc24b" if ok else "max_iterations", ok
            information = candidate[plan.filler_bit_count :]
            ok = _outer_crc32_ok(information)
            return ok, "outer_crc32" if ok else "max_iterations", None

        candidate, used, reason, crc_ok = _decode_block_logmap(
            block_llrs,
            block_size,
            filler_bit_count=plan.filler_bit_count if block_index == 0 else 0,
            max_iterations=max_iterations,
            stop=stop,
        )
        decoded_blocks.append(candidate)
        iterations.append(used)
        reasons.append(reason)
        block_crc.append(crc_ok)
        if not plan.segmented:
            outer_crc_ok = _outer_crc32_ok(candidate[plan.filler_bit_count :])

    information = desegment_information(tuple(decoded_blocks), plan)
    report = TurboDecodeReport(
        configured_max_iterations=max_iterations,
        actual_iterations_per_block=tuple(iterations),
        early_stop_reasons=tuple(reasons),
        code_block_crc_ok=tuple(block_crc),
        outer_crc_ok=outer_crc_ok,
        block_count=plan.block_count,
        filler_bit_count=plan.filler_bit_count,
    )
    return TurboDecodeResult(information, report)


def _encode_block(block: NDArray[np.uint8]) -> NDArray[np.uint8]:
    block_size = int(block.size)
    permutation = qpp_permutation(block_size)
    parity_one, tail_systematic_one, tail_parity_one = _rsc_encode(block)
    interleaved = block[permutation]
    parity_two, tail_systematic_two, tail_parity_two = _rsc_encode(interleaved)
    body = np.column_stack((block, parity_one, parity_two)).reshape(-1)
    tail = np.concatenate(
        (
            np.column_stack((tail_systematic_one, tail_parity_one)).reshape(-1),
            np.column_stack((tail_systematic_two, tail_parity_two)).reshape(-1),
        )
    )
    return np.concatenate((body, tail)).astype(np.uint8, copy=False)


def _rsc_encode(
    bits: NDArray[np.uint8],
) -> tuple[NDArray[np.uint8], NDArray[np.uint8], NDArray[np.uint8]]:
    """Encode ``g1/g0`` where g0=1+D^2+D^3 and g1=1+D+D^3."""

    state = [0, 0, 0]
    parity = np.empty(bits.size, dtype=np.uint8)
    for index, input_bit in enumerate(bits):
        feedback = int(input_bit) ^ state[1] ^ state[2]
        parity[index] = feedback ^ state[0] ^ state[2]
        state = [feedback, state[0], state[1]]

    tail_systematic = np.empty(3, dtype=np.uint8)
    tail_parity = np.empty(3, dtype=np.uint8)
    for index in range(3):
        input_bit = state[1] ^ state[2]
        feedback = input_bit ^ state[1] ^ state[2]
        tail_systematic[index] = input_bit
        tail_parity[index] = feedback ^ state[0] ^ state[2]
        state = [feedback, state[0], state[1]]
    if any(state):
        raise TurboError("RSC termination did not reach the all-zero state")
    return parity, tail_systematic, tail_parity


def _decode_block_logmap(
    llrs: NDArray[np.float64],
    block_size: int,
    *,
    filler_bit_count: int,
    max_iterations: int,
    stop,
) -> tuple[NDArray[np.uint8], int, str, bool | None]:
    body = llrs[: 3 * block_size].reshape(block_size, 3)
    tail = llrs[3 * block_size :]
    if tail.size != TURBO_TAIL_BITS_PER_BLOCK:
        raise TurboError("Turbo block has an invalid tail length")
    tail_one = tail[:6].reshape(3, 2)
    tail_two = tail[6:].reshape(3, 2)
    permutation = qpp_permutation(block_size)

    systematic_one = np.concatenate((body[:, 0], tail_one[:, 0]))
    parity_one = np.concatenate((body[:, 1], tail_one[:, 1]))
    systematic_two = np.concatenate((body[permutation, 0], tail_two[:, 0]))
    parity_two = np.concatenate((body[:, 2], tail_two[:, 1]))
    a_priori_one = np.zeros(block_size, dtype=np.float64)
    if filler_bit_count:
        a_priori_one[:filler_bit_count] = 64.0

    candidate = np.zeros(block_size, dtype=np.uint8)
    reason = "max_iterations"
    crc_ok: bool | None = None
    for iteration in range(1, max_iterations + 1):
        _, extrinsic_one = _constituent_logmap(
            systematic_one,
            parity_one,
            a_priori_one,
        )
        a_priori_two = extrinsic_one[permutation]
        if filler_bit_count:
            a_priori_two = a_priori_two.copy()
            a_priori_two[permutation < filler_bit_count] += 64.0
        posterior_two, extrinsic_two = _constituent_logmap(
            systematic_two,
            parity_two,
            a_priori_two,
        )
        posterior = np.empty(block_size, dtype=np.float64)
        posterior[permutation] = posterior_two
        candidate = (posterior < 0.0).astype(np.uint8)
        if filler_bit_count:
            candidate[:filler_bit_count] = 0
        should_stop, stop_reason, crc_ok = stop(candidate)
        if should_stop:
            reason = stop_reason
            return candidate, iteration, reason, crc_ok
        a_priori_one = np.empty(block_size, dtype=np.float64)
        a_priori_one[permutation] = extrinsic_two
        if filler_bit_count:
            a_priori_one[:filler_bit_count] += 64.0
    return candidate, max_iterations, reason, crc_ok


def _constituent_logmap(
    systematic: NDArray[np.float64],
    parity: NDArray[np.float64],
    a_priori_information: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    block_size = int(a_priori_information.size)
    trellis_steps = block_size + 3
    a_priori = np.pad(a_priori_information, (0, 3))
    next_state, parity_bit = _trellis_tables()
    alpha = np.full((trellis_steps + 1, 8), -np.inf, dtype=np.float64)
    beta = np.full((trellis_steps + 1, 8), -np.inf, dtype=np.float64)
    alpha[0, 0] = 0.0
    beta[-1, 0] = 0.0

    for time_index in range(trellis_steps):
        for state in range(8):
            if not np.isfinite(alpha[time_index, state]):
                continue
            for input_bit in (0, 1):
                destination = next_state[state, input_bit]
                metric = _branch_metric(
                    input_bit,
                    parity_bit[state, input_bit],
                    systematic[time_index],
                    parity[time_index],
                    a_priori[time_index],
                )
                alpha[time_index + 1, destination] = np.logaddexp(
                    alpha[time_index + 1, destination],
                    alpha[time_index, state] + metric,
                )
        alpha[time_index + 1] -= np.max(alpha[time_index + 1])

    for time_index in range(trellis_steps - 1, -1, -1):
        for state in range(8):
            values = []
            for input_bit in (0, 1):
                destination = next_state[state, input_bit]
                metric = _branch_metric(
                    input_bit,
                    parity_bit[state, input_bit],
                    systematic[time_index],
                    parity[time_index],
                    a_priori[time_index],
                )
                values.append(metric + beta[time_index + 1, destination])
            beta[time_index, state] = np.logaddexp(values[0], values[1])
        beta[time_index] -= np.max(beta[time_index])

    posterior = np.empty(block_size, dtype=np.float64)
    for time_index in range(block_size):
        hypotheses = [-np.inf, -np.inf]
        for state in range(8):
            for input_bit in (0, 1):
                destination = next_state[state, input_bit]
                metric = _branch_metric(
                    input_bit,
                    parity_bit[state, input_bit],
                    systematic[time_index],
                    parity[time_index],
                    a_priori[time_index],
                )
                value = (
                    alpha[time_index, state]
                    + metric
                    + beta[time_index + 1, destination]
                )
                hypotheses[input_bit] = np.logaddexp(hypotheses[input_bit], value)
        posterior[time_index] = hypotheses[0] - hypotheses[1]
    extrinsic = posterior - systematic[:block_size] - a_priori_information
    return posterior, np.clip(extrinsic, -64.0, 64.0)


def _trellis_tables() -> tuple[NDArray[np.int64], NDArray[np.uint8]]:
    next_state = np.empty((8, 2), dtype=np.int64)
    parity = np.empty((8, 2), dtype=np.uint8)
    for state in range(8):
        registers = [state & 1, (state >> 1) & 1, (state >> 2) & 1]
        for input_bit in (0, 1):
            feedback = input_bit ^ registers[1] ^ registers[2]
            parity[state, input_bit] = feedback ^ registers[0] ^ registers[2]
            next_state[state, input_bit] = (
                feedback | (registers[0] << 1) | (registers[1] << 2)
            )
    return next_state, parity


def _branch_metric(
    input_bit: int,
    parity_bit: int,
    systematic_llr: float,
    parity_llr: float,
    a_priori_llr: float,
) -> float:
    return 0.5 * (
        (1.0 - 2.0 * input_bit) * (systematic_llr + a_priori_llr)
        + (1.0 - 2.0 * parity_bit) * parity_llr
    )


def _outer_crc32_ok(information_bits: NDArray[np.uint8]) -> bool:
    if information_bits.size < 32 or information_bits.size % 8:
        return False
    raw = np.packbits(information_bits, bitorder="big").tobytes()
    expected = int.from_bytes(raw[-4:], "big")
    return (zlib.crc32(raw[:-4]) & 0xFFFFFFFF) == expected


def _as_bits(values: ArrayLike, name: str) -> NDArray[np.uint8]:
    bits = np.asarray(values)
    if bits.ndim != 1:
        raise TurboError(f"{name} must be one-dimensional")
    if not (
        np.issubdtype(bits.dtype, np.integer)
        or np.issubdtype(bits.dtype, np.bool_)
    ):
        raise TurboError(f"{name} must contain integer bits")
    if np.any((bits != 0) & (bits != 1)):
        raise TurboError(f"{name} must contain only 0 and 1")
    return bits.astype(np.uint8, copy=False)


def _as_llrs(values: ArrayLike) -> NDArray[np.float64]:
    llrs = np.asarray(values)
    if llrs.ndim != 1 or not np.issubdtype(llrs.dtype, np.number):
        raise TurboError("observations must be a numeric one-dimensional array")
    converted = llrs.astype(np.float64, copy=False)
    if not np.all(np.isfinite(converted)):
        raise TurboError("observations must contain only finite LLRs")
    return converted
