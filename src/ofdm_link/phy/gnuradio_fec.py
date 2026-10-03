"""GNU Radio native decoder for the project's convolutional wire code.

GNU Radio API references used by this adapter:

* ``cc_decoder``: https://www.gnuradio.org/doc/doxygen/classgr_1_1fec_1_1code_1_1cc__decoder.html
* ``extended_decoder``: https://wiki.gnuradio.org/index.php/FEC_Extended_Decoder
* ``vector_source``: https://www.gnuradio.org/doc/doxygen/classgr_1_1blocks_1_1vector__source.html
* ``vector_sink``: https://www.gnuradio.org/doc/doxygen/classgr_1_1blocks_1_1vector__sink.html
* ``top_block``: https://www.gnuradio.org/doc/doxygen/classgr_1_1top__block.html

GNU Radio is imported only when :func:`decode_frame_native` is called or a
:class:`NativeFrameDecoder` is constructed, keeping the portable codec safe to
import on CPU-only/headless installations.
"""

from __future__ import annotations

import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .codec import MAX_PAYLOAD_LENGTH, Frame, FrameDecodeError, decode_uncoded_frame
from .fec import SOFT_LLR_CLIP, _generators_for

_HEADER_BYTES = 7
_CRC_BYTES = 4
_TAIL_BITS = 6
_CONSTRAINT_LENGTH = 7
_RATE = 2
_POLYNOMIALS = (0o133, 0o171)
_STOP_GRACE_SECONDS = 0.5
_NATIVE_FLOAT_LIMIT = 2.5

MIN_CODED_BITS = _RATE * ((_HEADER_BYTES + _CRC_BYTES) * 8 + _TAIL_BITS)
MAX_CODED_BITS = _RATE * (
    (_HEADER_BYTES + MAX_PAYLOAD_LENGTH + _CRC_BYTES) * 8 + _TAIL_BITS
)


class GnuRadioFecUnavailableError(RuntimeError):
    """Raised when the optional GNU Radio FEC runtime cannot be imported."""


class GnuRadioFecTimeoutError(TimeoutError):
    """Raised when the finite decoder flowgraph misses its watchdog deadline."""


@dataclass(frozen=True, slots=True)
class NativeDecodeTiming:
    """Wall-clock service times for one native FEC decode.

    ``native_service_seconds`` includes graph construction and scheduler time.
    ``total_seconds`` additionally includes input and decoded-frame validation.
    These measurements describe this adapter call, not end-to-end link latency.
    """

    construction_seconds: float
    scheduler_seconds: float
    native_service_seconds: float
    total_seconds: float
    coded_bits: int
    decoded_bits: int


@dataclass(frozen=True, slots=True)
class _GnuRadioModules:
    gr: Any
    blocks: Any
    fec: Any


class _WaitTask:
    """One blocking GNU Radio ``wait`` call owned by a reusable worker."""

    def __init__(self, wait: Any) -> None:
        self.wait = wait
        self.done = threading.Event()
        self.error: BaseException | None = None


class _WaitWorker:
    """Serialize blocking scheduler waits on one bounded daemon thread."""

    def __init__(self) -> None:
        self._tasks: queue.Queue[_WaitTask | None] = queue.Queue(maxsize=1)
        self._closed = False
        self._thread = threading.Thread(
            target=self._serve,
            name="ofdm-link-viterbi-wait",
            daemon=True,
        )
        self._thread.start()

    @property
    def thread_ident(self) -> int | None:
        return self._thread.ident

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def submit(self, wait: Any) -> _WaitTask:
        if self._closed:
            raise RuntimeError("GNU Radio wait worker is closed")
        task = _WaitTask(wait)
        self._tasks.put_nowait(task)
        return task

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._tasks.put_nowait(None)
        except queue.Full:
            return
        self._thread.join(_STOP_GRACE_SECONDS)

    def _serve(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            try:
                task.wait()
            except BaseException as error:
                task.error = error
            finally:
                task.done.set()


class NativeFrameDecoder:
    """Reuse one finite GNU Radio decoder graph for a fixed coded frame length.

    GNU Radio's finite vector source, head, and vector sink expose explicit
    rewind/reset operations. A completed top block may be started again after
    ``wait()``. This adapter uses those operations only while the graph is idle;
    calls to :meth:`decode` are serialized.

    The coded bit count cannot change because ``cc_decoder`` is constructed for
    one decoded frame size. Create a separate instance for every distinct frame
    length. The instance becomes unusable after :meth:`close` or a scheduler
    failure.
    """

    def __init__(
        self,
        coded_bit_count: int,
        *,
        timeout_s: float = 2.0,
        rate_inverse: int = _RATE,
    ) -> None:
        polynomials = _generators_for(rate_inverse)
        self._rate_inverse = rate_inverse
        self._coded_bit_count = _validated_coded_bit_count(
            coded_bit_count,
            rate_inverse,
        )
        self._decoded_count = self._coded_bit_count // rate_inverse
        self._timeout_s = _validated_timeout(timeout_s)
        self._lock = threading.Lock()
        self._closed = False

        runtime = _load_gnuradio()
        decoder = runtime.fec.cc_decoder.make(
            self._decoded_count,
            _CONSTRAINT_LENGTH,
            rate_inverse,
            list(polynomials),
            0,
            0,
            runtime.fec.CC_TRUNCATED,
            False,
        )
        self._flowgraph = runtime.gr.top_block("ofdm_link_reusable_native_viterbi")
        self._source = runtime.blocks.vector_source_f(
            [0.0] * self._coded_bit_count,
            False,
        )
        native_decoder = runtime.fec.extended_decoder(decoder, "none")
        self._output_limit = runtime.blocks.head(
            runtime.gr.sizeof_char,
            self._decoded_count,
        )
        self._sink = runtime.blocks.vector_sink_b()
        self._flowgraph.connect(
            self._source,
            native_decoder,
            self._output_limit,
            self._sink,
        )
        self._wait_worker = _WaitWorker()

    @property
    def coded_bit_count(self) -> int:
        """Return the only coded frame length accepted by this instance."""

        return self._coded_bit_count

    @property
    def closed(self) -> bool:
        """Return whether this instance can no longer decode frames."""

        with self._lock:
            return self._closed

    def decode(self, coded_bits: ArrayLike) -> Frame:
        """Decode one frame while reusing the already-constructed graph."""

        return decode_uncoded_frame(self.decode_fec_bits(coded_bits))

    def decode_fec_bits(self, coded_bits: ArrayLike) -> NDArray[np.uint8]:
        """Decode to information plus tail bits for the FEC adapter seam."""

        coded = _validated_coded_bits(coded_bits)
        if coded.size != self._coded_bit_count:
            raise FrameDecodeError(
                f"native decoder is configured for {self._coded_bit_count} coded bits; "
                f"got {coded.size}"
            )
        soft_values = np.where(coded == 0, -1.0, 1.0).astype(np.float32)

        return self._decode_native_values(soft_values)

    def _decode_native_values(
        self,
        soft_values: NDArray[np.float32],
    ) -> NDArray[np.uint8]:
        """Schedule one already-validated GNU Radio float metric vector."""

        with self._lock:
            if self._closed:
                raise RuntimeError("native frame decoder is closed")

            self._source.set_data(soft_values.tolist())
            self._source.rewind()
            self._output_limit.reset()
            self._sink.reset()
            try:
                _run_with_watchdog(
                    self._flowgraph,
                    self._timeout_s,
                    wait_worker=self._wait_worker,
                )
            except BaseException:
                self._closed = True
                self._wait_worker.close()
                raise

            decoded = np.asarray(self._sink.data(), dtype=np.uint8)
            if decoded.size != self._decoded_count:
                self._closed = True
                raise FrameDecodeError(
                    "GNU Radio decoder returned "
                    f"{decoded.size} bits; expected exactly {self._decoded_count}"
                )
            decoded.setflags(write=False)
            return decoded

    def close(self) -> None:
        """Release the graph; repeated calls are safe."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._flowgraph.stop()
            try:
                _wait_after_stop(
                    self._flowgraph,
                    _STOP_GRACE_SECONDS,
                    wait_worker=self._wait_worker,
                )
            finally:
                self._wait_worker.close()

    def __enter__(self) -> NativeFrameDecoder:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


class NativeFecDecoder(NativeFrameDecoder):
    """Native decoder graph whose public result is FEC bits, not a frame."""

    def decode(self, coded_bits: ArrayLike) -> NDArray[np.uint8]:
        """Return information plus terminating bits for ``FecCodec``."""

        return self.decode_fec_bits(coded_bits)


class NativeSoftFecDecoder(NativeFrameDecoder):
    """Native soft Viterbi adapter for positive-bit-zero project LLRs.

    GNU Radio 3.10.12 ``extended_decoder`` converts floats with scale 48 and
    bias 128 before ``cc_decoder``.  Its branch table maps low unsigned metrics
    to bit 0 and high metrics to bit 1, so project LLRs are sign-inverted and
    linearly normalized into the safe interior of that unsigned range.
    """

    def decode(self, llrs: ArrayLike) -> NDArray[np.uint8]:
        """Decode finite LLRs, preserving relative reliability and ordering."""

        values = _validated_soft_llrs(llrs)
        if values.size != self._coded_bit_count:
            raise FrameDecodeError(
                f"native decoder is configured for {self._coded_bit_count} coded values; "
                f"got {values.size}"
            )
        bounded = np.clip(values, -SOFT_LLR_CLIP, SOFT_LLR_CLIP)
        native_values = (
            -bounded * (_NATIVE_FLOAT_LIMIT / SOFT_LLR_CLIP)
        ).astype(np.float32, copy=False)
        return self._decode_native_values(native_values)


def decode_frame_native(
    coded_bits: ArrayLike,
    *,
    timeout_s: float = 2.0,
) -> Frame:
    """Decode one frame with GNU Radio using the portable decoder contract."""

    frame, _timing = decode_frame_native_timed(coded_bits, timeout_s=timeout_s)
    return frame


def decode_frame_native_timed(
    coded_bits: ArrayLike,
    *,
    timeout_s: float = 2.0,
) -> tuple[Frame, NativeDecodeTiming]:
    """Decode one complete terminated K=7 frame with GNU Radio's Viterbi block.

    Hard bit 0 maps to soft value -1 and bit 1 maps to +1. The configured
    ``extended_decoder`` converts those values to the unsigned soft metrics
    expected by ``cc_decoder``. Exactly one finite frame is scheduled.
    """

    call_started = time.perf_counter()
    coded = _validated_coded_bits(coded_bits)
    deadline = _validated_timeout(timeout_s)

    construction_started = time.perf_counter()
    runtime = _load_gnuradio()
    decoded_count = coded.size // _RATE
    decoder = runtime.fec.cc_decoder.make(
        decoded_count,
        _CONSTRAINT_LENGTH,
        _RATE,
        list(_POLYNOMIALS),
        0,
        0,
        runtime.fec.CC_TRUNCATED,
        False,
    )
    flowgraph = runtime.gr.top_block("ofdm_link_native_viterbi")
    soft_values = np.where(coded == 0, -1.0, 1.0).astype(np.float32)
    source = runtime.blocks.vector_source_f(soft_values.tolist(), False)
    native_decoder = runtime.fec.extended_decoder(decoder, "none")
    output_limit = runtime.blocks.head(runtime.gr.sizeof_char, decoded_count)
    sink = runtime.blocks.vector_sink_b()
    flowgraph.connect(source, native_decoder, output_limit, sink)
    construction_finished = time.perf_counter()

    scheduler_started = time.perf_counter()
    _run_with_watchdog(flowgraph, deadline)
    scheduler_finished = time.perf_counter()

    decoded = np.asarray(sink.data(), dtype=np.uint8)
    if decoded.size != decoded_count:
        raise FrameDecodeError(
            "GNU Radio decoder returned "
            f"{decoded.size} bits; expected exactly {decoded_count}"
        )
    frame = decode_uncoded_frame(decoded)
    call_finished = time.perf_counter()

    construction_seconds = construction_finished - construction_started
    scheduler_seconds = scheduler_finished - scheduler_started
    return frame, NativeDecodeTiming(
        construction_seconds=construction_seconds,
        scheduler_seconds=scheduler_seconds,
        native_service_seconds=construction_seconds + scheduler_seconds,
        total_seconds=call_finished - call_started,
        coded_bits=int(coded.size),
        decoded_bits=int(decoded.size),
    )


def decode_fec_bits_native(coded_bits: ArrayLike) -> NDArray[np.uint8]:
    """Decode hard observations to information-plus-tail bits with GNU Radio."""

    coded = _validated_coded_bits(coded_bits)
    with NativeFecDecoder(int(coded.size)) as decoder:
        return decoder.decode(coded)


def decode_soft_fec_bits_native(llrs: ArrayLike) -> NDArray[np.uint8]:
    """Decode project-convention LLRs with GNU Radio's native Viterbi."""

    values = _validated_soft_llrs(llrs)
    with NativeSoftFecDecoder(int(values.size)) as decoder:
        return decoder.decode(values)


def _validated_coded_bits(coded_bits: ArrayLike) -> NDArray[np.uint8]:
    values = np.asarray(coded_bits)
    if values.ndim != 1:
        raise FrameDecodeError("coded_bits must be a one-dimensional array")
    if not (
        np.issubdtype(values.dtype, np.integer)
        or np.issubdtype(values.dtype, np.bool_)
    ):
        raise FrameDecodeError("coded_bits must contain integer bits")
    if np.any((values != 0) & (values != 1)):
        raise FrameDecodeError("coded_bits must contain only 0 and 1")

    bit_count = values.size
    if bit_count < MIN_CODED_BITS:
        raise FrameDecodeError(
            f"coded frame is too short: got {bit_count} bits, need at least {MIN_CODED_BITS}"
        )
    if bit_count > MAX_CODED_BITS:
        raise FrameDecodeError(
            f"coded frame is too long: got {bit_count} bits, maximum is {MAX_CODED_BITS}"
        )
    if bit_count % _RATE:
        raise FrameDecodeError("rate-1/2 coded frame must contain an even number of bits")
    if (bit_count // _RATE - _TAIL_BITS) % 8:
        raise FrameDecodeError("coded frame length does not end on a decoded byte boundary")
    return values.astype(np.uint8, copy=False)


def _validated_soft_llrs(llrs: ArrayLike) -> NDArray[np.float32]:
    values = np.asarray(llrs)
    if values.ndim != 1 or not np.issubdtype(values.dtype, np.number):
        raise FrameDecodeError("llrs must be a numeric one-dimensional array")
    converted = values.astype(np.float32, copy=False)
    if not np.all(np.isfinite(converted)):
        raise FrameDecodeError("llrs must contain only finite values")
    _validated_coded_bit_count(int(converted.size))
    return converted


def _validated_coded_bit_count(
    coded_bit_count: int,
    rate_inverse: int = _RATE,
) -> int:
    if isinstance(coded_bit_count, bool) or not isinstance(coded_bit_count, int):
        raise ValueError("coded_bit_count must be an integer")
    minimum = rate_inverse * ((_HEADER_BYTES + _CRC_BYTES) * 8 + _TAIL_BITS)
    maximum = rate_inverse * (
        (_HEADER_BYTES + MAX_PAYLOAD_LENGTH + _CRC_BYTES) * 8 + _TAIL_BITS
    )
    if coded_bit_count < minimum:
        raise ValueError(
            "coded_bit_count is too short: "
            f"got {coded_bit_count}, need at least {minimum}"
        )
    if coded_bit_count > maximum:
        raise ValueError(
            "coded_bit_count is too long: "
            f"got {coded_bit_count}, maximum is {maximum}"
        )
    if coded_bit_count % rate_inverse:
        if rate_inverse == _RATE:
            raise ValueError("rate-1/2 coded_bit_count must be even")
        raise ValueError(
            f"rate-1/{rate_inverse} coded_bit_count must be a multiple of "
            f"{rate_inverse}"
        )
    if (coded_bit_count // rate_inverse - _TAIL_BITS) % 8:
        raise ValueError("coded_bit_count must end on a decoded byte boundary")
    return coded_bit_count


def _validated_timeout(timeout_s: float) -> float:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise ValueError("timeout_s must be a finite positive number")
    timeout = float(timeout_s)
    if not math.isfinite(timeout) or timeout <= 0.0:
        raise ValueError("timeout_s must be a finite positive number")
    return timeout


def _load_gnuradio() -> _GnuRadioModules:
    try:
        from gnuradio import blocks, fec, gr
    except (ImportError, ModuleNotFoundError) as error:
        raise GnuRadioFecUnavailableError(
            "GNU Radio FEC runtime is unavailable; use the portable decode_frame path"
        ) from error
    return _GnuRadioModules(gr=gr, blocks=blocks, fec=fec)


def _run_with_watchdog(
    flowgraph: Any,
    timeout_s: float,
    *,
    wait_worker: _WaitWorker | None = None,
) -> None:
    flowgraph.start()
    owned_worker = wait_worker is None
    worker = wait_worker or _WaitWorker()
    try:
        task = worker.submit(flowgraph.wait)
        if task.done.wait(timeout_s):
            if task.error is not None:
                raise task.error
            return

        flowgraph.stop()
        task.done.wait(_STOP_GRACE_SECONDS)
        raise GnuRadioFecTimeoutError(
            f"GNU Radio Viterbi decoder exceeded the {timeout_s:g}-second watchdog"
        )
    finally:
        if owned_worker:
            worker.close()


def _wait_after_stop(
    flowgraph: Any,
    timeout_s: float,
    *,
    wait_worker: _WaitWorker | None = None,
) -> None:
    owned_worker = wait_worker is None
    worker = wait_worker or _WaitWorker()
    try:
        task = worker.submit(flowgraph.wait)
        if not task.done.wait(timeout_s):
            raise GnuRadioFecTimeoutError(
                "GNU Radio Viterbi decoder did not stop within the close grace period"
            )
        if task.error is not None:
            raise task.error
    finally:
        if owned_worker:
            worker.close()
