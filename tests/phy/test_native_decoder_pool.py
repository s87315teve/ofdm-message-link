from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields

import numpy as np
import pytest

from ofdm_link.phy.codec import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    Frame,
    FrameKind,
    encode_frame,
)
from ofdm_link.phy.native_decoder_pool import NativeFrameDecoderPool


def _coded_bits(bit_count: int) -> np.ndarray:
    return np.zeros(bit_count, dtype=np.uint8)


class _FakeDecoder:
    def __init__(self, coded_bit_count: int) -> None:
        self.coded_bit_count = coded_bit_count
        self.closed = False
        self.close_calls = 0
        self.decode_calls = 0
        self.poison_next_decode = False

    def decode(self, coded_bits: np.ndarray) -> Frame:
        if self.closed:
            raise RuntimeError("fake decoder is closed")
        if coded_bits.size != self.coded_bit_count:
            raise ValueError("wrong coded length")
        self.decode_calls += 1
        if self.poison_next_decode:
            self.closed = True
            raise RuntimeError("forced scheduler failure")
        return Frame(
            CURRENT_PROTOCOL_VERSION,
            FrameKind.CONTROL,
            MCS.QPSK,
            self.coded_bit_count,
            b"",
        )

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class _Factory:
    def __init__(self) -> None:
        self.created: list[_FakeDecoder] = []

    def __call__(self, coded_bit_count: int) -> _FakeDecoder:
        decoder = _FakeDecoder(coded_bit_count)
        self.created.append(decoder)
        return decoder


@pytest.mark.parametrize("max_lengths", [True, 1.0, 0, -1, 257])
def test_pool_rejects_invalid_cache_bound(max_lengths: object) -> None:
    with pytest.raises(ValueError, match=r"\[1, 256\]"):
        NativeFrameDecoderPool(max_lengths=max_lengths)  # type: ignore[arg-type]


def test_pool_rejects_non_vector_input_before_constructing_decoder() -> None:
    factory = _Factory()
    pool = NativeFrameDecoderPool(max_lengths=2, decoder_factory=factory)

    with pytest.raises(ValueError, match="one-dimensional"):
        pool(np.zeros((2, 94), dtype=np.uint8))

    assert factory.created == []


def test_pool_builds_lazily_and_reuses_decoder_by_coded_length() -> None:
    factory = _Factory()
    pool = NativeFrameDecoderPool(max_lengths=2, decoder_factory=factory)

    assert factory.created == []
    assert pool(_coded_bits(188)).sequence == 188
    assert pool(_coded_bits(188)).sequence == 188

    assert len(factory.created) == 1
    assert factory.created[0].decode_calls == 2
    snapshot = pool.snapshot()
    assert snapshot.hits == 1
    assert snapshot.misses == 1
    assert snapshot.evictions == 0
    assert snapshot.cached_lengths == (188,)


def test_pool_evicts_least_recently_used_decoder_and_closes_it() -> None:
    factory = _Factory()
    pool = NativeFrameDecoderPool(max_lengths=2, decoder_factory=factory)

    pool(_coded_bits(188))
    pool(_coded_bits(204))
    pool(_coded_bits(188))
    pool(_coded_bits(220))

    assert factory.created[0].close_calls == 0
    assert factory.created[1].close_calls == 1
    assert factory.created[2].close_calls == 0
    snapshot = pool.snapshot()
    assert snapshot.hits == 1
    assert snapshot.misses == 3
    assert snapshot.evictions == 1
    assert snapshot.cached_lengths == (188, 220)
    assert len(snapshot.cached_lengths) <= 2


def test_pool_snapshot_is_immutable_and_contains_only_bounded_metadata() -> None:
    factory = _Factory()
    pool = NativeFrameDecoderPool(max_lengths=1, decoder_factory=factory)
    pool(_coded_bits(188))

    snapshot = pool.snapshot()

    assert {field.name for field in fields(snapshot)} == {
        "hits",
        "misses",
        "evictions",
        "cached_lengths",
    }
    assert isinstance(snapshot.cached_lengths, tuple)
    with pytest.raises(FrozenInstanceError):
        snapshot.hits = 99  # type: ignore[misc]


def test_pool_context_manager_closes_every_decoder_once_and_rejects_more_work() -> None:
    factory = _Factory()

    with NativeFrameDecoderPool(max_lengths=2, decoder_factory=factory) as pool:
        pool(_coded_bits(188))
        pool(_coded_bits(204))

    pool.close()

    assert [decoder.close_calls for decoder in factory.created] == [1, 1]
    with pytest.raises(RuntimeError, match="closed"):
        pool(_coded_bits(188))


def test_pool_discards_a_poisoned_decoder_and_rebuilds_on_next_call() -> None:
    factory = _Factory()
    pool = NativeFrameDecoderPool(max_lengths=1, decoder_factory=factory)
    failing = factory(188)
    failing.poison_next_decode = True
    factory.created.clear()
    create_count = 0

    def fail_once_factory(coded_bit_count: int) -> _FakeDecoder:
        nonlocal create_count
        create_count += 1
        if create_count == 1:
            return failing
        return factory(coded_bit_count)

    pool = NativeFrameDecoderPool(max_lengths=1, decoder_factory=fail_once_factory)

    with pytest.raises(RuntimeError, match="forced scheduler failure"):
        pool(_coded_bits(188))

    assert pool.snapshot().cached_lengths == ()
    assert pool(_coded_bits(188)).sequence == 188
    assert create_count == 2
    assert pool.snapshot().misses == 2


def test_pool_constructs_only_one_decoder_for_concurrent_same_length_calls() -> None:
    factory = _Factory()
    pool = NativeFrameDecoderPool(max_lengths=2, decoder_factory=factory)
    start = threading.Barrier(8)

    def decode_once() -> Frame:
        start.wait()
        return pool(_coded_bits(188))

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: decode_once(), range(8)))

    assert all(frame.sequence == 188 for frame in results)
    assert len(factory.created) == 1
    assert factory.created[0].decode_calls == 8
    assert pool.snapshot().hits == 7
    assert pool.snapshot().misses == 1


@pytest.mark.gnuradio
def test_pool_native_decoder_is_bit_exact_for_data_ack_and_payload_lengths() -> None:
    frames = [
        Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, 17, b"short"),
        Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 17, b""),
        Frame(
            CURRENT_PROTOCOL_VERSION,
            FrameKind.DATA,
            MCS.QAM16,
            18,
            bytes((index * 29 + 7) & 0xFF for index in range(257)),
        ),
    ]

    with NativeFrameDecoderPool(max_lengths=3) as pool:
        decoded = [pool(encode_frame(frame)) for frame in frames]
        decoded.append(pool(encode_frame(frames[0])))
        snapshot = pool.snapshot()

    assert decoded == [*frames, frames[0]]
    assert snapshot.hits == 1
    assert snapshot.misses == 3
    assert snapshot.evictions == 0
    assert snapshot.cached_lengths == (
        encode_frame(frames[1]).size,
        encode_frame(frames[2]).size,
        encode_frame(frames[0]).size,
    )
