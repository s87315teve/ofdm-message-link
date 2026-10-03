from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from ofdm_link.phy import burst as burst_module
from ofdm_link.phy import streaming as streaming_module
from ofdm_link.phy.facade import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    BurstConfig,
    Frame,
    FrameKind,
    StreamingBurstDecoder,
    encode_burst,
)


def _frame(sequence: int, payload: bytes) -> Frame:
    return Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QPSK,
        sequence,
        payload,
    )


def _feed_in_chunks(
    decoder: StreamingBurstDecoder,
    samples: np.ndarray,
    chunk_sizes: tuple[int, ...],
) -> tuple[object, ...]:
    decoded: list[object] = []
    offset = 0
    for size in chunk_sizes:
        decoded.extend(decoder.feed(samples[offset : offset + size]))
        offset += size
    decoded.extend(decoder.feed(samples[offset:]))
    return tuple(decoded)


def test_arbitrary_chunks_decode_noise_prefixed_back_to_back_bursts() -> None:
    first_frame = _frame(10, b"first streaming payload")
    second_frame = _frame(11, b"second streaming payload")
    first = encode_burst(first_frame)
    second = encode_burst(second_frame)
    rng = np.random.default_rng(20260920)
    prefix = (0.01 * (rng.standard_normal(113) + 1j * rng.standard_normal(113))).astype(
        np.complex64
    )
    stream = np.concatenate((prefix, first.samples, second.samples))

    decoder = StreamingBurstDecoder()
    decoded = _feed_in_chunks(decoder, stream, (7, 81, 509, 997, 43, 1601))

    assert [burst.frame for burst in decoded] == [first_frame, second_frame]
    assert [burst.burst_start for burst in decoded] == [
        prefix.size,
        prefix.size + first.sample_count,
    ]
    assert decoder.buffered_sample_count < decoder.config.sync.preamble_length


def test_streaming_decode_reuses_the_preamble_it_already_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One whole burst must cost one acquisition scan, not two.

    The continuous decoder has to scan before it can bound a burst, and the
    finite decoder used to scan the very same samples again.  That second
    full-buffer scan was the largest non-FEC item in the receive budget, so
    failing here is a real-time budget regression, not a style change.
    """

    frame = _frame(12, b"one scan per burst")
    burst = encode_burst(frame)

    def rescanned(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the burst decoder re-acquired a known preamble")

    monkeypatch.setattr(burst_module, "acquire_preamble", rescanned)
    decoder = StreamingBurstDecoder()
    decoded = _feed_in_chunks(decoder, burst.samples, (211, 1009))

    assert [item.frame for item in decoded] == [frame]


@pytest.mark.parametrize("failure", ["corrupt", "truncated"])
def test_bad_or_truncated_burst_resynchronizes_at_the_next_preamble(
    failure: str,
) -> None:
    rejected = encode_burst(_frame(20, b"rejected burst payload"))
    accepted_frame = _frame(21, b"accepted after resynchronization")
    accepted = encode_burst(accepted_frame)

    if failure == "corrupt":
        bad_samples = rejected.samples.copy()
        header_start = rejected.preamble_sample_count + rejected.training_sample_count
        bad_samples[header_start : header_start + rejected.header_sample_count] = 0
    else:
        bad_samples = rejected.samples[:-400]
    gap = np.zeros(37, dtype=np.complex64)
    stream = np.concatenate((bad_samples, gap, accepted.samples))

    decoded = StreamingBurstDecoder().feed(stream)

    assert [burst.frame for burst in decoded] == [accepted_frame]
    assert decoded[0].burst_start == bad_samples.size + gap.size


def test_noise_buffer_is_bounded_even_for_one_oversized_chunk() -> None:
    config = BurstConfig(max_burst_samples=256, max_input_samples=256)
    decoder = StreamingBurstDecoder(config)
    noise = np.zeros(10_000, dtype=np.complex64)

    assert decoder.feed(noise) == ()
    assert decoder.buffered_sample_count <= config.max_input_samples
    assert decoder.buffered_sample_count < config.sync.preamble_length


def test_feed_rejects_malformed_samples_without_mutating_the_buffer() -> None:
    decoder = StreamingBurstDecoder()

    with pytest.raises(ValueError, match="one-dimensional"):
        decoder.feed(np.zeros((2, 2), dtype=np.complex64))

    assert decoder.buffered_sample_count == 0


def test_a_burst_completed_by_a_later_chunk_is_decoded() -> None:
    """A burst that is one sample short is incomplete, not invalid.

    The continuous decoder sees every burst split across chunks.  When the
    split lands inside the last OFDM symbol the finite decoder must say how
    many samples it still needs, so the stream can wait for them.  Treating
    that as a validation failure discards a burst the link did transmit.
    """

    frame = _frame(30, b"completed one sample later")
    burst = encode_burst(frame)
    decoder = StreamingBurstDecoder()

    assert decoder.feed(burst.samples[:-1]) == ()

    assert [item.frame for item in decoder.feed(burst.samples[-1:])] == [frame]


def test_a_preamble_candidate_inside_a_signalled_burst_does_not_discard_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correlation peak does not outrank a CRC-checked signaling header.

    Back-to-back bursts let the correlator report the next preamble a little
    early, which places a candidate inside the burst being decoded.  The
    signalled length came from a CRC-checked header, so it wins: the burst is
    decoded, not abandoned.
    """

    frame = _frame(31, b"kept despite a spurious candidate")
    burst = encode_burst(frame)
    spurious_start = burst.sample_count - 31
    real = streaming_module._acquisition_candidates

    def with_spurious_candidate(buffer: np.ndarray, config: object) -> tuple[object, ...]:
        found = real(buffer, config)
        if not found or buffer.size <= spurious_start:
            return found
        return (*found, replace(found[0], preamble_start=spurious_start))

    monkeypatch.setattr(streaming_module, "_acquisition_candidates", with_spurious_candidate)
    decoder = StreamingBurstDecoder()

    assert [item.frame for item in decoder.feed(burst.samples)] == [frame]


def test_resynchronization_never_discards_past_a_known_next_preamble() -> None:
    """A rejected burst must not consume the preamble that follows it."""

    rejected = encode_burst(_frame(32, b"rejected burst payload"))
    accepted_frame = _frame(33, b"accepted after resynchronization")
    accepted = encode_burst(accepted_frame)
    truncated = rejected.samples[:-400]
    stream = np.concatenate((truncated, accepted.samples))

    decoded = StreamingBurstDecoder().feed(stream)

    assert [item.frame for item in decoded] == [accepted_frame]
    assert decoded[0].burst_start == truncated.size


def _segment_decode_in_chunks(
    decoder: StreamingBurstDecoder,
    samples: np.ndarray,
    chunk_sizes: tuple[int, ...],
) -> tuple[object, ...]:
    decoded: list[object] = []
    offset = 0
    chunks = [*chunk_sizes, samples.size]
    for size in chunks:
        for segment in decoder.feed_segments(samples[offset : offset + size]):
            try:
                burst = streaming_module.decode_burst_segment(segment, decoder.config)
            except burst_module.BurstDecodeError as error:
                decoder.record_payload_outcome(streaming_module.PayloadFailure.from_error(error))
            else:
                decoder.record_payload_outcome(None)
                decoded.append(burst)
        offset += size
    return tuple(decoded)


@pytest.mark.parametrize("gap", [0, 37, 900])
def test_deferred_payload_segments_decode_exactly_like_the_inline_path(gap: int) -> None:
    rng = np.random.default_rng(20260923 + gap)
    frames = [_frame(20 + index, rng.bytes(40 + 97 * index)) for index in range(6)]
    blocks = []
    for index, frame in enumerate(frames):
        blocks.append(0.01 * (rng.standard_normal(gap) + 1j * rng.standard_normal(gap)))
        samples = encode_burst(frame).samples.copy()
        if index == 3:
            # Header intact, payload destroyed: a counted payload failure.
            samples[-400:] = 0.01 * rng.standard_normal(400)
        blocks.append(samples)
    blocks.append(np.zeros(2000))
    stream = (np.concatenate(blocks) + 0.003 * rng.standard_normal(sum(map(len, blocks)))).astype(
        np.complex64
    )
    chunks = tuple(int(size) for size in rng.integers(50, 3000, size=40))

    inline = StreamingBurstDecoder()
    expected = _feed_in_chunks(inline, stream, chunks)
    deferred = StreamingBurstDecoder()
    actual = _segment_decode_in_chunks(deferred, stream, chunks)

    assert [burst.frame for burst in actual] == [burst.frame for burst in expected]
    assert len(expected) == 5
    assert [(b.burst_start, b.burst_end) for b in actual] == [
        (b.burst_start, b.burst_end) for b in expected
    ]
    for got, want in zip(actual, expected, strict=True):
        np.testing.assert_array_equal(got.payload_symbols, want.payload_symbols)
    counters = deferred.snapshot()
    assert counters.valid_decoded_bursts == 5
    assert counters.payload_decode_failure == inline.snapshot().payload_decode_failure == 1
    assert counters.header_decode_success == inline.snapshot().header_decode_success


def test_one_decoder_refuses_to_mix_inline_and_deferred_feeding() -> None:
    decoder = StreamingBurstDecoder()
    decoder.feed_segments(np.zeros(64, dtype=np.complex64))
    with pytest.raises(burst_module.BurstValidationError):
        decoder.feed(np.zeros(64, dtype=np.complex64))
