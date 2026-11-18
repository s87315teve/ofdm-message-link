"""The continuous decoder's sample buffer and its finite check.

The buffer is one reused block of storage with a head offset: an append
writes after the tail, a discard moves the head, and the samples kept move
only when an append does not fit (compaction, growth or shrink).  None of
that may change what is decoded, where (absolute stream offsets) or with
which SNR.  Every sample is checked finite once, on its way in.
"""

from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    BurstConfig,
    BurstDecodeError,
    DecodedPsduBurst,
    Frame,
    FrameKind,
    encode_burst,
    encode_burst_v5,
    make_dual_version_frame_decoder,
    mcs_entry,
)
from ofdm_link.phy import burst as burst_module
from ofdm_link.phy import streaming as streaming_module
from ofdm_link.phy import sync as sync_module
from ofdm_link.phy.streaming import (
    PayloadFailure,
    StreamingBurstDecoder,
    decode_burst_segment,
    peak_amplitude,
)
from ofdm_link.phy.sync import SyncError, acquire_all
from ofdm_link.phy.sync_v5 import V5Acquisition, V5SyncConfig, measure_ltf_pair

ALL_LEGACY = make_dual_version_frame_decoder(accept_all_wire_versions=True)
V5_MCS = tuple(range(8, 18))


def _decoder(config: BurstConfig | None = None) -> StreamingBurstDecoder:
    return StreamingBurstDecoder(config, frame_decoder=ALL_LEGACY)


def _stream(seed: int = 0, *, legacy: bool = True, snr_db: float = 25.0):
    """v5 bursts of every MCS 8-17 (payloads up to 7000 bytes), with legacy ones between."""

    rng = np.random.default_rng(seed)
    parts: list[np.ndarray] = [np.zeros(300)]
    starts: list[int] = []
    cursor = 300
    for index, mcs in enumerate(V5_MCS):
        body = rng.integers(0, 256, int(rng.choice((15, 900, 2500, 7000))), dtype=np.uint8)
        samples = encode_burst_v5(body.tobytes(), mcs_entry(mcs), scrambler_seed=1 + index).samples
        parts.extend((np.zeros(16), samples, np.zeros(int(rng.integers(64, 900)))))
        starts.append(cursor + 16)
        cursor += 16 + samples.size + parts[-1].size
        if legacy and index % 3 == 1:
            payload = rng.integers(0, 256, 400, dtype=np.uint8).tobytes()
            frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QAM16, index, payload)
            samples = encode_burst(frame, wire_version=1 + index % 4).samples
            parts.extend((np.zeros(256), samples, np.zeros(256)))
            starts.append(cursor + 256)
            cursor += 512 + samples.size
    stream = np.concatenate(parts + [np.zeros(1000)])
    variance = 0.8 / 10 ** (snr_db / 10)
    stream = stream + np.sqrt(variance / 2) * (
        rng.standard_normal(stream.size) + 1j * rng.standard_normal(stream.size)
    )
    return stream.astype(np.complex64), starts


def _summary(burst) -> tuple:
    if isinstance(burst, DecodedPsduBurst):
        content = (burst.body, burst.mcs_index, burst.fcs_ok)
    else:
        content = (burst.frame.payload, burst.frame.mcs)
    return (burst.burst_start, burst.burst_end, content)


def _decode_inline(stream, chunk, config=None):
    decoder = _decoder(config)
    decoded: list = []
    for start in range(0, stream.size, chunk):
        decoded.extend(decoder.feed(stream[start : start + chunk]))
    return decoder, decoded


def _decode_segments(stream, chunk, config=None):
    owner = _decoder(config)
    decoded: list = []
    for start in range(0, stream.size, chunk):
        for segment in owner.feed_segments(stream[start : start + chunk]):
            try:
                burst = decode_burst_segment(segment, owner.config, frame_decoder=ALL_LEGACY)
            except BurstDecodeError as error:
                owner.record_payload_outcome(PayloadFailure.from_error(error))
                continue
            owner.record_payload_outcome(PayloadFailure.from_burst(burst), burst)
            decoded.append(burst)
    return owner, decoded


def _outcome(decoder, decoded) -> tuple:
    return (
        [_summary(burst) for burst in decoded],
        decoder.snapshot(),
        [decoder.snr_at(burst.burst_start) for burst in decoded],
    )


# --------------------------------------------------------------------------
# The buffer against a model built from np.concatenate
# --------------------------------------------------------------------------


def test_appends_and_discards_match_a_concatenating_model(monkeypatch):
    monkeypatch.setattr(streaming_module, "INITIAL_BUFFER_SAMPLES", 64)
    limit = 4096
    decoder = _decoder(BurstConfig(max_burst_samples=1024, max_input_samples=limit))
    rng = np.random.default_rng(11)
    model = np.empty(0, np.complex64)
    model_start = 0
    fed = 0
    overshoot = 0
    seen = {"compacted": 0, "grown": 0, "shrunk": 0}
    for _ in range(4000):
        storage, head, live = decoder._storage, decoder._head, decoder._buffer.size
        if rng.random() < 0.5:
            room = limit - live
            capacity_left = storage.size - decoder._tail
            # Nothing, exactly filling the storage, more than all of it,
            # anything that fits the bound, or a short piece.
            choice = rng.integers(0, 5)
            count = int(
                (0, min(capacity_left, room), min(storage.size + 1, room),
                 rng.integers(0, room + 1), rng.integers(0, min(room, 20) + 1))[choice]
            )
            values = (rng.standard_normal(count) + 1j * rng.standard_normal(count))
            values = values.astype(np.complex64)
            decoder._append(values)
            model = np.concatenate((model, values))
            fed += count
            if decoder._storage is not storage:
                seen["grown" if decoder._storage.size > storage.size else "shrunk"] += 1
            elif head and decoder._head == 0 and live:
                seen["compacted"] += 1
        else:
            # Nothing, one, all, more than all, any, or all but a short tail.
            choice = rng.integers(0, 6)
            count = int((0, 1, live, live + int(rng.integers(1, 50)), rng.integers(0, live + 1),
                         max(0, live - int(rng.integers(1, 20))))[choice])
            decoder._discard(count)
            overshoot += max(0, count - model.size)
            model = model[count:]
            model_start += count
        buffer = decoder._buffer
        np.testing.assert_array_equal(buffer, model)
        assert decoder._buffer_start == model_start
        assert decoder._buffer_start + buffer.size == fed + overshoot
        assert buffer.dtype == np.complex64 and buffer.flags.c_contiguous
        assert not buffer.flags.writeable
        assert np.shares_memory(buffer, decoder._storage) or buffer.size == 0
        assert decoder._storage.size <= limit
        assert decoder.buffered_sample_count == model.size
    assert all(count > 0 for count in seen.values()), seen


def test_an_append_past_the_input_bound_is_refused():
    decoder = _decoder(BurstConfig(max_burst_samples=256, max_input_samples=256))
    decoder._append(np.zeros(200, np.complex64))
    with pytest.raises(BurstDecodeError, match="max_input_samples"):
        decoder._append(np.zeros(57, np.complex64))


# --------------------------------------------------------------------------
# What is decoded does not depend on the storage
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mixed_stream():
    return _stream(seed=5)


@pytest.fixture(scope="module")
def reference(mixed_stream):
    stream, starts = mixed_stream
    decoder, decoded = _decode_inline(stream, stream.size)
    assert [burst.burst_start for burst in decoded] == starts
    assert all(getattr(burst, "fcs_ok", True) for burst in decoded)
    return _outcome(decoder, decoded)


CHUNKS = (37, 559, 560, 561, 4096, 8191, 8192, 8193, 65536)


@pytest.mark.parametrize("chunk", [*CHUNKS, None])
def test_every_chunking_and_storage_decodes_the_same_bursts(
    monkeypatch, mixed_stream, reference, chunk
):
    stream, _ = mixed_stream
    chunk = chunk or stream.size
    inline = _outcome(*_decode_inline(stream, chunk))
    segments = _outcome(*_decode_segments(stream, chunk))
    # Compaction and growth then happen while a burst is still pending; the
    # outcome must not move by a bit, SNRs included.
    monkeypatch.setattr(streaming_module, "INITIAL_BUFFER_SAMPLES", 1024)
    assert _outcome(*_decode_inline(stream, chunk)) == inline
    assert _outcome(*_decode_segments(stream, chunk)) == segments

    assert segments == inline
    # Which bursts, where and what does not depend on the chunking either
    # (the SNR can: acquisition refines on the samples it has seen).
    assert inline[0] == reference[0]
    assert inline[1].valid_decoded_bursts == len(reference[0])
    assert None not in inline[2]


def test_a_legacy_only_stream_decodes_the_same_with_tiny_storage(monkeypatch):
    rng = np.random.default_rng(9)
    parts = []
    for index in range(6):
        payload = rng.integers(0, 256, 300, dtype=np.uint8).tobytes()
        frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, index, payload)
        parts.extend((np.zeros(256), encode_burst(frame, wire_version=1 + index % 4).samples,
                      np.zeros(256)))
    stream = np.concatenate(parts).astype(np.complex64)
    for chunk in (37, 561, 8192, stream.size):
        monkeypatch.setattr(streaming_module, "INITIAL_BUFFER_SAMPLES", 1 << 17)
        expected = _outcome(*_decode_inline(stream, chunk))
        assert len(expected[0]) == 6
        assert _outcome(*_decode_segments(stream, chunk)) == expected
        monkeypatch.setattr(streaming_module, "INITIAL_BUFFER_SAMPLES", 1024)
        assert _outcome(*_decode_inline(stream, chunk)) == expected
        assert _outcome(*_decode_segments(stream, chunk)) == expected


def test_kept_segments_and_bursts_survive_later_compaction(monkeypatch, mixed_stream):
    monkeypatch.setattr(streaming_module, "INITIAL_BUFFER_SAMPLES", 1024)
    stream, _ = mixed_stream
    owner = _decoder()
    kept = []
    for start in range(0, stream.size, 559):
        for segment in owner.feed_segments(stream[start : start + 559]):
            kept.append((segment, segment.samples.copy()))
    assert len(kept) > 10
    for segment, original in kept:
        np.testing.assert_array_equal(segment.samples, original)
        offset = segment.stream_offset
        np.testing.assert_array_equal(segment.samples, stream[offset : offset + original.size])

    decoder = _decoder()
    decoded = []
    for start in range(0, stream.size, 559):
        for burst in decoder.feed(stream[start : start + 559]):
            decoded.append((burst, burst.payload_symbols.copy()))
    for burst, symbols in decoded:
        np.testing.assert_array_equal(burst.payload_symbols, symbols)


def test_one_feed_of_three_input_bounds_still_finds_the_burst_at_its_end():
    config = BurstConfig(max_burst_samples=20_000, max_input_samples=40_000)
    burst = encode_burst_v5(b"at the very end" * 20, mcs_entry(12), scrambler_seed=3).samples
    rng = np.random.default_rng(4)
    noise = 0.01 * (rng.standard_normal(3 * 40_000) + 1j * rng.standard_normal(3 * 40_000))
    stream = np.concatenate((noise, np.zeros(16), burst, np.zeros(200))).astype(np.complex64)
    decoder = _decoder(config)

    decoded = decoder.feed(stream)

    assert [(item.burst_start, item.body) for item in decoded] == [
        (3 * 40_000 + 16, b"at the very end" * 20)
    ]
    assert decoder._storage.size <= config.max_input_samples


def test_an_empty_feed_while_a_burst_is_pending_changes_nothing(mixed_stream, reference):
    stream, starts = mixed_stream
    decoder = _decoder()
    decoded = list(decoder.feed(stream[: starts[0] + 700]))
    assert decoded == [] and decoder._pending_acquisition is not None
    state = (decoder._buffer.copy(), decoder._buffer_start, decoder._pending_acquisition)
    assert decoder.feed(np.zeros(0, np.complex64)) == ()
    assert decoder.feed_segments is not None
    np.testing.assert_array_equal(decoder._buffer, state[0])
    assert (decoder._buffer_start, decoder._pending_acquisition) == state[1:]
    decoded.extend(decoder.feed(stream[starts[0] + 700 :]))
    assert [_summary(burst) for burst in decoded] == reference[0]


# --------------------------------------------------------------------------
# The finite check: once per sample, on the way in
# --------------------------------------------------------------------------


def test_a_non_finite_chunk_is_refused_and_leaves_the_buffer_as_it_was(mixed_stream):
    stream, starts = mixed_stream
    decoder = _decoder()
    decoder.feed(stream[: starts[0] + 700])
    before = (decoder._buffer.copy(), decoder._buffer_start)
    for bad in (np.nan, np.inf):
        chunk = stream[starts[0] + 700 : starts[0] + 1700].copy()
        chunk[500] = bad
        with pytest.raises(BurstDecodeError, match="finite"):
            decoder.feed(chunk)
        np.testing.assert_array_equal(decoder._buffer, before[0])
        assert decoder._buffer_start == before[1]


@pytest.fixture
def finite_calls(monkeypatch):
    """Sizes of the finite checks of samples; the decoded burst's own
    ``_frozen_samples`` (its equalized symbols, not the stream) is not one."""

    import sys

    calls: list[int] = []
    real = streaming_module.all_finite

    def counted(values):
        if sys._getframe(1).f_code.co_name != "_frozen_samples":
            calls.append(int(np.asarray(values).size))
        return real(values)

    for module in (streaming_module, sync_module, burst_module):
        monkeypatch.setattr(module, "all_finite", counted)
    return calls


def test_the_owner_checks_each_feed_once(mixed_stream, finite_calls):
    stream, _ = mixed_stream
    for segments in (False, True):
        finite_calls.clear()
        decoder = _decoder()
        feed = decoder.feed_segments if segments else decoder.feed
        pieces = range(0, stream.size, 8192)
        for start in pieces:
            feed(stream[start : start + 8192])
        assert finite_calls == [stream[s : s + 8192].size for s in pieces]


def test_a_trusted_complex64_feed_is_not_checked_at_all(mixed_stream, finite_calls):
    stream, starts = mixed_stream
    decoder = _decoder()
    decoded = []
    for start in range(0, stream.size, 8192):
        decoded.extend(decoder.feed(stream[start : start + 8192], assume_finite=True))
    assert [burst.burst_start for burst in decoded] == starts
    assert finite_calls == []
    owner = _decoder()
    for start in range(0, stream.size, 8192):
        owner.feed_segments(stream[start : start + 8192], assume_finite=True)
    assert finite_calls == []


def test_a_trusted_complex128_feed_is_still_checked(finite_calls):
    decoder = _decoder()
    chunk = np.zeros(1000, np.complex128)
    decoder.feed(chunk, assume_finite=True)
    assert finite_calls == [1000]
    # A finite complex128 can still overflow complex64.
    chunk[3] = 1e300
    with np.errstate(over="ignore"), pytest.raises(BurstDecodeError, match="finite"):
        decoder.feed(chunk, assume_finite=True)
    chunk[3] = np.nan
    with pytest.raises(BurstDecodeError, match="finite"):
        decoder.feed(chunk, assume_finite=True)


def test_the_public_functions_still_refuse_non_finite_samples(mixed_stream):
    stream, starts = mixed_stream
    window = stream[starts[0] - 16 : starts[0] + 2000].copy()
    found = acquire_all(window, None, V5SyncConfig())
    assert found and isinstance(found[0], V5Acquisition)
    assert measure_ltf_pair(window, found[0]) is not None
    window[100] = np.nan
    with pytest.raises(SyncError, match="finite"):
        acquire_all(window, None, V5SyncConfig())
    with pytest.raises(SyncError, match="finite"):
        measure_ltf_pair(window, found[0])


# --------------------------------------------------------------------------
# peak_amplitude
# --------------------------------------------------------------------------


def _old_peak(samples):
    if samples.size == 0:
        return None
    return float(max(np.max(np.abs(samples.real)), np.max(np.abs(samples.imag))))


def test_peak_amplitude_equals_the_old_formula():
    rng = np.random.default_rng(2)
    for _ in range(200):
        size = int(rng.integers(1, 3000))
        values = rng.standard_normal(size) * rng.uniform(0.01, 3) + 1j * rng.standard_normal(size)
        for array in (values.astype(np.complex64), values, values[::3], values[::-1],
                      values.astype(np.complex64)[1::2]):
            assert peak_amplitude(array) == _old_peak(array)
    assert peak_amplitude(np.zeros(0, np.complex64)) is None
    negative_zero = np.full(5, -0.0 - 0.0j, np.complex64)
    assert peak_amplitude(negative_zero) == _old_peak(negative_zero) == 0.0
    assert peak_amplitude(np.array([-0.9 + 0.1j], np.complex64)) == np.float32(0.9)
    assert peak_amplitude(np.array([0.2 - 0.95j], np.complex64)) == np.float32(0.95)
