"""Bounded reuse of fixed-length GNU Radio frame decoders."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar, cast

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .codec import Frame, FrameDecodeError
from .gnuradio_fec import NativeFecDecoder, NativeFrameDecoder, NativeSoftFecDecoder

_MAX_CACHED_LENGTHS = 256


_Decoded = TypeVar("_Decoded")


class _Decoder(Protocol[_Decoded]):
    @property
    def closed(self) -> bool: ...

    def decode(self, coded_bits: NDArray[np.uint8]) -> _Decoded: ...

    def close(self) -> None: ...


DecoderFactory = Callable[[int], _Decoder[object]]


@dataclass(frozen=True, slots=True)
class NativeFrameDecoderPoolSnapshot:
    """Immutable cache counters and lengths ordered least to most recent."""

    hits: int
    misses: int
    evictions: int
    cached_lengths: tuple[int, ...]


class NativeFrameDecoderPool:
    """Act as a frame decoder while reusing a bounded set of native graphs.

    A ``NativeFrameDecoder`` is fixed to one coded frame length. This pool
    creates those decoders lazily and closes the least-recently-used decoder
    before admitting a new length. Calls are serialized so eviction and pool
    shutdown can never close a decoder that is still scheduling work.
    """

    def __init__(
        self,
        *,
        max_lengths: int,
        decoder_factory: DecoderFactory = NativeFrameDecoder,
    ) -> None:
        if (
            type(max_lengths) is not int
            or not 1 <= max_lengths <= _MAX_CACHED_LENGTHS
        ):
            raise ValueError(
                f"max_lengths must be an integer in [1, {_MAX_CACHED_LENGTHS}]"
            )
        if not callable(decoder_factory):
            raise TypeError("decoder_factory must be callable")
        self._max_lengths = max_lengths
        self._decoder_factory = decoder_factory
        self._decoders: OrderedDict[int, _Decoder[object]] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._closed = False
        self._lock = threading.RLock()

    def __call__(self, coded_bits: ArrayLike) -> Frame:
        """Decode one frame, selecting the native graph by coded bit length."""

        coded = np.asarray(coded_bits)
        if coded.ndim != 1:
            raise FrameDecodeError("coded_bits must be a one-dimensional array")
        coded_bit_count = int(coded.size)
        with self._lock:
            if self._closed:
                raise RuntimeError("native frame decoder pool is closed")

            decoder = self._decoders.get(coded_bit_count)
            if decoder is None:
                self._misses += 1
                decoder = self._decoder_factory(coded_bit_count)
                self._admit(coded_bit_count, decoder)
            else:
                self._hits += 1
                self._decoders.move_to_end(coded_bit_count)

            try:
                return cast(Frame, decoder.decode(coded))
            except BaseException:
                if self._is_poisoned(decoder):
                    self._decoders.pop(coded_bit_count, None)
                    try:
                        decoder.close()
                    except BaseException:
                        pass
                raise

    def snapshot(self) -> NativeFrameDecoderPoolSnapshot:
        """Return counters without retaining input or decoded frame data."""

        with self._lock:
            return NativeFrameDecoderPoolSnapshot(
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                cached_lengths=tuple(self._decoders),
            )

    def close(self) -> None:
        """Close all cached decoders; repeated calls are safe."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            decoders = tuple(self._decoders.values())
            self._decoders.clear()
            first_error: BaseException | None = None
            for decoder in decoders:
                try:
                    decoder.close()
                except BaseException as error:
                    if first_error is None:
                        first_error = error
            if first_error is not None:
                raise first_error

    def __enter__(self) -> NativeFrameDecoderPool:
        with self._lock:
            if self._closed:
                raise RuntimeError("native frame decoder pool is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def _admit(self, coded_bit_count: int, decoder: _Decoder[object]) -> None:
        if len(self._decoders) == self._max_lengths:
            _, evicted = self._decoders.popitem(last=False)
            self._evictions += 1
            try:
                evicted.close()
            except BaseException:
                try:
                    decoder.close()
                except BaseException:
                    pass
                raise
        self._decoders[coded_bit_count] = decoder

    @staticmethod
    def _is_poisoned(decoder: _Decoder[object]) -> bool:
        try:
            return decoder.closed
        except BaseException:
            return True


class NativeFecDecoderPool(NativeFrameDecoderPool):
    """Bounded native graph pool returning FEC bits through the formal seam."""

    def __init__(self, *, max_lengths: int) -> None:
        super().__init__(
            max_lengths=max_lengths,
            decoder_factory=cast(DecoderFactory, NativeFecDecoder),
        )

    def __call__(self, coded_bits: ArrayLike) -> NDArray[np.uint8]:
        return cast(NDArray[np.uint8], super().__call__(coded_bits))


class NativeSoftFecDecoderPool(NativeFrameDecoderPool):
    """Bounded native graph pool accepting project-convention float LLRs."""

    def __init__(self, *, max_lengths: int) -> None:
        super().__init__(
            max_lengths=max_lengths,
            decoder_factory=cast(DecoderFactory, NativeSoftFecDecoder),
        )

    def __call__(self, llrs: ArrayLike) -> NDArray[np.uint8]:
        return cast(NDArray[np.uint8], super().__call__(llrs))
