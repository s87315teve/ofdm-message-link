"""The continuous receiver with legacy (v1-v4) and v5 bursts in one stream."""

from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    BurstConfig,
    BurstDecodeError,
    DecodedBurst,
    DecodedPsduBurst,
    Frame,
    FrameKind,
    V5RxConfig,
    encode_burst,
    encode_burst_v5,
    make_dual_version_frame_decoder,
    mcs_entry,
)
from ofdm_link.phy.streaming import (
    PayloadFailure,
    StreamingBurstDecoder,
    decode_burst_segment,
)
from ofdm_link.phy.sync_v5 import V5_RETAIN_SAMPLES

V5_GUARD_PRE, V5_GUARD_POST = 16, 64
LEGACY_GUARD = 256
# Every legacy wire version, as the app's receiver accepts them.
ALL_LEGACY = make_dual_version_frame_decoder(accept_all_wire_versions=True)


def _decoder(config: BurstConfig | None = None) -> StreamingBurstDecoder:
    return StreamingBurstDecoder(config, frame_decoder=ALL_LEGACY)


def _frame(sequence: int, mcs: MCS, size: int) -> Frame:
    payload = np.random.default_rng(sequence).integers(0, 256, size, dtype=np.uint8).tobytes()
    return Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, mcs, sequence, payload)


def _body(size: int, seed: int) -> bytes:
    return np.random.default_rng(1000 + seed).integers(0, 256, size, dtype=np.uint8).tobytes()


def _mixed_stream(snr_db: float | None = 25.0, seed: int = 0):
    """v1, v5, v2, v5, v3, v5, v4, v5 with each family's own guards."""

    parts: list[np.ndarray] = []
    expected: list[tuple[str, object, int]] = []
    cursor = 0
    plan = [
        ("legacy", 1, MCS.QPSK, 300),
        ("v5", 8, None, 900),
        ("legacy", 2, MCS.QAM16, 996),
        ("v5", 13, None, 2500),
        ("legacy", 3, MCS.QPSK, 50),
        ("v5", 10, None, 15),
        ("legacy", 4, MCS.QAM16, 400),
        ("v5", 12, None, 7000),
    ]
    for index, (family, version_or_mcs, mcs, size) in enumerate(plan):
        if family == "legacy":
            frame = _frame(index, mcs, size)
            samples = encode_burst(frame, wire_version=version_or_mcs).samples
            pre = post = LEGACY_GUARD
            expected.append(("legacy", frame, cursor + pre))
        else:
            body = _body(size, index)
            samples = encode_burst_v5(
                body, mcs_entry(version_or_mcs), scrambler_seed=1 + index
            ).samples
            pre, post = V5_GUARD_PRE, V5_GUARD_POST
            expected.append(("v5", body, cursor + pre))
        parts.extend((np.zeros(pre), samples, np.zeros(post)))
        cursor += pre + samples.size + post
    stream = np.concatenate(parts + [np.zeros(1000)])
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        variance = 0.8 / 10 ** (snr_db / 10)
        stream = stream + np.sqrt(variance / 2) * (
            rng.standard_normal(stream.size) + 1j * rng.standard_normal(stream.size)
        )
    return stream.astype(np.complex64), expected


def _check(decoded, expected) -> None:
    assert len(decoded) == len(expected)
    for burst, (family, content, start) in zip(decoded, expected, strict=True):
        assert burst.burst_start == start
        if family == "legacy":
            assert isinstance(burst, DecodedBurst) and burst.frame == content
        else:
            assert isinstance(burst, DecodedPsduBurst)
            assert burst.fcs_ok and burst.body == content


def _feed(decoder: StreamingBurstDecoder, stream: np.ndarray, chunk: int) -> list:
    decoded: list = []
    for start in range(0, stream.size, chunk):
        decoded.extend(decoder.feed(stream[start : start + chunk]))
    return decoded


@pytest.mark.parametrize("chunk", [997, 8192, 65536])
def test_mixed_legacy_and_v5_stream_is_fully_decoded_in_order(chunk: int) -> None:
    stream, expected = _mixed_stream()
    decoder = _decoder()

    decoded = _feed(decoder, stream, chunk)

    _check(decoded, expected)
    snapshot = decoder.snapshot()
    assert snapshot.detected_legacy_candidates == 4
    assert snapshot.detected_v5_candidates == 4
    assert snapshot.bursts_legacy == 4 and snapshot.bursts_v5 == 4
    assert snapshot.valid_decoded_bursts == 8
    assert snapshot.header_decode_failure == 0 and snapshot.psdu_fcs_failures == 0
    assert len(decoder.drain_link_snr_db()) == 8


def test_mixed_stream_through_deferred_segments_matches_inline_decoding() -> None:
    stream, expected = _mixed_stream(seed=1)
    owner = _decoder()
    decoded: list = []
    for start in range(0, stream.size, 4096):
        for segment in owner.feed_segments(stream[start : start + 4096]):
            try:
                burst = decode_burst_segment(segment, owner.config, frame_decoder=ALL_LEGACY)
            except BurstDecodeError as error:
                owner.record_payload_outcome(PayloadFailure.from_error(error))
                continue
            owner.record_payload_outcome(PayloadFailure.from_burst(burst), burst)
            decoded.append(burst)

    _check(decoded, expected)
    snapshot = owner.snapshot()
    assert snapshot.bursts_legacy == 4 and snapshot.bursts_v5 == 4
    assert snapshot.valid_decoded_bursts == 8


def test_snr_readings_track_the_channel_for_both_families() -> None:
    stream, _ = _mixed_stream(snr_db=12.0, seed=2)
    decoder = _decoder()
    _feed(decoder, stream, 8192)

    readings = decoder.drain_link_snr_db()

    assert len(readings) == 8
    # One reading per burst, each from a single preamble: the legacy one
    # rests on 26 noise samples (about 1 dB rms), the LTF pair on 52.
    assert all(abs(reading - 12.0) < 2.5 for reading in readings)


def test_legacy_acquisition_switch_ignores_legacy_bursts() -> None:
    stream, expected = _mixed_stream(seed=3)
    decoder = _decoder(BurstConfig(v5=V5RxConfig(legacy_acquisition=False)))

    decoded = _feed(decoder, stream, 8192)

    _check(decoded, [item for item in expected if item[0] == "v5"])
    assert decoder.snapshot().detected_legacy_candidates == 0


def test_legacy_results_are_identical_with_and_without_the_v5_detector() -> None:
    stream, expected = _mixed_stream(snr_db=10.0, seed=4)
    dual = _feed(_decoder(), stream, 8192)
    legacy_only = _feed(
        _decoder(BurstConfig(v5=V5RxConfig(v5_acquisition=False))), stream, 8192
    )

    dual_legacy = [burst for burst in dual if isinstance(burst, DecodedBurst)]
    assert all(isinstance(burst, DecodedBurst) for burst in legacy_only)
    assert len(dual_legacy) == len(legacy_only) >= 3
    for left, right in zip(dual_legacy, legacy_only, strict=True):
        assert left.frame == right.frame
        assert left.consumed_range == right.consumed_range
        assert np.array_equal(left.payload_symbols, right.payload_symbols)
        assert left.diagnostics.acquisition == right.diagnostics.acquisition


def test_a_failed_psdu_is_still_emitted_and_counted_as_a_crc_failure() -> None:
    body = _body(3000, 50)
    burst = encode_burst_v5(body, mcs_entry(8), scrambler_seed=4)
    damaged = np.array(burst.samples)
    damaged[-damaged.size // 3 :] = 0.0
    follower_body = _body(400, 51)
    follower = encode_burst_v5(follower_body, mcs_entry(8), scrambler_seed=5).samples
    stream = np.concatenate(
        (np.zeros(16), damaged, np.zeros(64 + 16), follower, np.zeros(800))
    )
    rng = np.random.default_rng(5)
    noise = rng.standard_normal(stream.size) + 1j * rng.standard_normal(stream.size)
    stream = (stream + 0.01 * noise).astype(np.complex64)
    decoder = _decoder()

    decoded = _feed(decoder, stream, 5000)

    assert [type(item) for item in decoded] == [DecodedPsduBurst, DecodedPsduBurst]
    assert not decoded[0].fcs_ok and decoded[1].fcs_ok and decoded[1].body == follower_body
    snapshot = decoder.snapshot()
    assert snapshot.psdu_fcs_failures == 1 and snapshot.crc_failures == 1
    assert snapshot.bursts_v5 == 1 and snapshot.valid_decoded_bursts == 1


def test_idle_and_noise_buffers_stay_bounded() -> None:
    idle = _decoder()
    idle.feed(np.zeros(50_000, dtype=np.complex64))
    assert idle.buffered_sample_count < idle.config.sync.preamble_length

    rng = np.random.default_rng(6)
    noisy = _decoder()
    noise = (rng.standard_normal(200_000) + 1j * rng.standard_normal(200_000)).astype(np.complex64)
    assert _feed(noisy, noise, 8192) == []
    assert noisy.buffered_sample_count <= V5_RETAIN_SAMPLES
    assert noisy.snapshot().detected_v5_candidates == 0


@pytest.mark.parametrize("interferer", ["dc", "tone"])
def test_a_stream_under_a_dc_offset_or_tone_still_decodes_v5(interferer: str) -> None:
    # Before the steady-part removal the v5 detector found nothing at all
    # here: the DC or tone kept every offset eligible and the run deferred.
    rng = np.random.default_rng(17)
    bodies = [_body(200, 70 + index) for index in range(4)]
    parts = [np.zeros(1000)]
    for index, body in enumerate(bodies):
        samples = encode_burst_v5(body, mcs_entry(8), scrambler_seed=20 + index).samples
        parts += [np.zeros(V5_GUARD_PRE), samples, np.zeros(V5_GUARD_POST + 700 * (index + 1))]
    stream = 0.5 * np.concatenate(parts)
    stream = stream * np.exp(2j * np.pi * 0.3 * np.arange(stream.size) / 64)
    variance = 0.2 / 10.0
    stream = stream + np.sqrt(variance / 2) * (
        rng.standard_normal(stream.size) + 1j * rng.standard_normal(stream.size)
    )
    if interferer == "dc":
        stream = stream + 0.45
    else:
        stream = stream + 0.6 * np.exp(2j * np.pi * 0.48 * np.arange(stream.size))

    decoded = _feed(_decoder(), stream.astype(np.complex64), 4000)

    assert [burst.body for burst in decoded if burst.fcs_ok] == bodies


def test_back_to_back_v5_bursts_with_the_short_guards() -> None:
    bodies = [_body(size, 60 + size) for size in (100, 1500, 19 - 4, 800)]
    parts = []
    for index, body in enumerate(bodies):
        samples = encode_burst_v5(body, mcs_entry(9 + index), scrambler_seed=10 + index).samples
        parts.extend((np.zeros(V5_GUARD_PRE), samples, np.zeros(V5_GUARD_POST)))
    stream = np.concatenate(parts + [np.zeros(700)]).astype(np.complex64)

    decoded = _feed(_decoder(), stream, 3000)

    assert [burst.body for burst in decoded] == bodies
