from __future__ import annotations

from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from ofdm_link.phy.burst import (
    BurstConfig,
    BurstDecodeError,
    BurstValidationError,
    SampleClockTrackingConfig,
    decode_burst,
    encode_burst,
)
from ofdm_link.phy.burst_header import (
    BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT,
    CURRENT_BURST_HEADER_VERSION,
    BurstHeader,
    encode_burst_header,
)
from ofdm_link.phy.channel import generate_training_symbol
from ofdm_link.phy.codec import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    Frame,
    FrameKind,
    demap_symbols,
    encode_frame,
    make_soft_convolutional_frame_codec,
    map_symbols,
)
from ofdm_link.phy.ofdm import modulate_ofdm
from ofdm_link.phy.soft import soft_demap_symbols
from ofdm_link.phy.sync import generate_preamble
from ofdm_link.radio import SimulationChannelConfig, apply_simulation_channel


def _frame(mcs: MCS, payload_size: int = 73) -> Frame:
    return Frame(
        protocol_version=CURRENT_PROTOCOL_VERSION,
        kind=FrameKind.DATA,
        mcs=mcs,
        sequence=0x1357,
        payload=bytes((index * 17) % 256 for index in range(payload_size)),
    )


@pytest.mark.parametrize("mcs", list(MCS))
@pytest.mark.parametrize("payload_size", [0, 1, 47, 48, 49, 512])
def test_complete_burst_round_trip_supports_soft_llr_fec(
    mcs: MCS,
    payload_size: int,
) -> None:
    frame = _frame(mcs, payload_size)
    transmitted = encode_burst(frame)
    codec = make_soft_convolutional_frame_codec()

    decoded = decode_burst(
        transmitted.samples,
        frame_decoder=codec.decode,
        soft_symbol_demapper=soft_demap_symbols,
        input_noise_variance=0.1,
    )

    assert decoded.frame == frame
    assert decoded.diagnostics.decision_mode == "soft"
    assert decoded.diagnostics.estimated_noise_variance is not None
    assert decoded.diagnostics.llr_saturation_count >= 0
    assert decoded.diagnostics.decoder_latency_seconds >= 0.0


@pytest.mark.parametrize("mcs", list(MCS))
def test_burst_round_trip_finds_one_frame_inside_a_finite_buffer(mcs: MCS) -> None:
    frame = _frame(mcs)
    transmitted = encode_burst(frame)
    leading = np.zeros(37, dtype=np.complex64)
    trailing = np.zeros(29, dtype=np.complex64)
    received = np.concatenate((leading, transmitted.samples, trailing))

    decoded = decode_burst(received)

    assert decoded.frame == frame
    assert decoded.burst_start == leading.size
    assert decoded.burst_end == leading.size + transmitted.samples.size
    assert decoded.consumed_range == (decoded.burst_start, decoded.burst_end)
    assert decoded.diagnostics.acquisition.preamble_start == leading.size
    assert decoded.diagnostics.channel_estimate.coefficients.shape == (52,)
    assert len(decoded.diagnostics.header_common_phase_rad) == 3
    assert len(decoded.diagnostics.payload_common_phase_rad) > 0
    assert decoded.payload_symbols.size > 0
    assert not decoded.payload_symbols.flags.writeable
    np.testing.assert_array_equal(
        demap_symbols(decoded.payload_symbols, mcs)[: encode_frame(frame).size],
        encode_frame(frame),
    )


def test_burst_config_is_explicit_and_validated() -> None:
    config = BurstConfig()

    assert config.max_coded_payload_bits > 0
    assert config.max_input_samples >= config.max_burst_samples

    with pytest.raises(FrozenInstanceError):
        config.max_input_samples = 1  # type: ignore[misc]
    with pytest.raises(BurstValidationError, match="FFT/CP"):
        BurstConfig(sync=type(config.sync)(fft_size=128, cyclic_prefix_length=16))
    with pytest.raises(BurstValidationError, match="at least"):
        BurstConfig(max_burst_samples=1000, max_input_samples=999)
    with pytest.raises(BurstValidationError, match="max_abs_correction_ppm"):
        SampleClockTrackingConfig(max_abs_correction_ppm=0.0)
    with pytest.raises(BurstValidationError, match="minimum_payload_symbols"):
        SampleClockTrackingConfig(minimum_payload_symbols=1)


@pytest.mark.parametrize("mcs", list(MCS))
def test_burst_layout_uses_one_training_and_three_fixed_qpsk_header_symbols(
    mcs: MCS,
) -> None:
    frame = _frame(mcs, payload_size=31)
    coded_payload = encode_frame(frame)
    transmitted = encode_burst(frame)
    block_length = 80
    expected_payload_ofdm_symbols = (
        (coded_payload.size + mcs.bits_per_symbol - 1) // mcs.bits_per_symbol + 47
    ) // 48

    assert transmitted.preamble_sample_count == block_length
    assert transmitted.training_sample_count == block_length
    assert transmitted.header_sample_count == 3 * block_length
    assert transmitted.payload_sample_count == expected_payload_ofdm_symbols * block_length
    assert transmitted.sample_count == (5 + expected_payload_ofdm_symbols) * block_length
    assert not transmitted.samples.flags.writeable

    expected_header = BurstHeader(
        CURRENT_BURST_HEADER_VERSION,
        mcs,
        int(coded_payload.size),
    )
    fixed_qpsk = map_symbols(encode_burst_header(expected_header), MCS.QPSK)
    expected_header_samples = modulate_ofdm(
        fixed_qpsk,
        first_symbol_index=0,
    ).samples
    np.testing.assert_array_equal(transmitted.samples[:80], generate_preamble())
    np.testing.assert_array_equal(
        transmitted.samples[80:160],
        generate_training_symbol().samples,
    )
    np.testing.assert_array_equal(
        transmitted.samples[160:400],
        expected_header_samples,
    )


def test_burst_accepts_preencoded_frame_bits_without_changing_the_waveform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _frame(MCS.QAM16, payload_size=127)
    coded = encode_frame(frame)
    expected = encode_burst(frame)

    monkeypatch.setattr(
        "ofdm_link.phy.burst.encode_frame_by_wire_version",
        lambda *_: pytest.fail("preencoded path must not encode the frame again"),
    )
    actual = encode_burst(frame, preencoded_frame_bits=coded)

    assert actual.header == expected.header
    np.testing.assert_array_equal(actual.samples, expected.samples)


@pytest.mark.parametrize(
    "coded",
    [
        np.zeros((2, 94), dtype=np.uint8),
        np.zeros(188, dtype=np.float32),
        np.full(188, 2, dtype=np.uint8),
        np.zeros(188, dtype=np.uint8),
    ],
)
def test_burst_rejects_invalid_preencoded_frame_bits(coded: np.ndarray) -> None:
    with pytest.raises(BurstValidationError, match="preencoded_frame_bits"):
        encode_burst(_frame(MCS.QPSK), preencoded_frame_bits=coded)


@pytest.mark.parametrize("mcs", list(MCS))
@pytest.mark.parametrize("normalized_cfo", [-0.2, 0.2])
@pytest.mark.parametrize("gain", [0.5, 2.0])
def test_burst_compensates_scalar_gain_and_edge_cfo(
    mcs: MCS,
    normalized_cfo: float,
    gain: float,
) -> None:
    frame = _frame(mcs)
    transmitted = encode_burst(frame)
    buffered = np.concatenate(
        (
            np.zeros(23, dtype=np.complex64),
            transmitted.samples,
            np.zeros(17, dtype=np.complex64),
        )
    )
    sample_index = np.arange(buffered.size, dtype=np.float64)
    received = gain * buffered * np.exp(2j * np.pi * normalized_cfo * sample_index / 64)

    decoded = decode_burst(received.astype(np.complex64))

    assert decoded.frame == frame
    assert decoded.diagnostics.acquisition.normalized_cfo == pytest.approx(
        normalized_cfo,
        abs=2e-6,
    )
    assert decoded.diagnostics.acquisition.amplitude_gain == pytest.approx(
        gain,
        rel=2e-6,
    )


@pytest.mark.parametrize("mcs", list(MCS))
def test_burst_equalizes_three_tap_multipath_within_cp(mcs: MCS) -> None:
    frame = _frame(mcs, payload_size=80)
    transmitted = encode_burst(frame)
    buffered = np.concatenate(
        (
            np.zeros(31, dtype=np.complex64),
            transmitted.samples,
            np.zeros(32, dtype=np.complex64),
        )
    )
    taps = np.array([0.82 + 0.13j, 0, 0.21 - 0.17j], dtype=np.complex64)
    received = np.convolve(buffered, taps).astype(np.complex64)

    decoded = decode_burst(received)

    assert decoded.frame == frame
    assert decoded.burst_start == 31
    assert np.ptp(np.abs(decoded.diagnostics.channel_estimate.coefficients)) > 0.1


@pytest.mark.parametrize(
    ("mcs", "snr_db"),
    [(MCS.QPSK, 15.0), (MCS.QAM16, 24.0)],
)
def test_burst_decodes_deterministic_nominal_awgn(mcs: MCS, snr_db: float) -> None:
    frame = _frame(mcs, payload_size=120)
    transmitted = encode_burst(frame)
    leading = 47
    buffered = np.concatenate(
        (
            np.zeros(leading, dtype=np.complex64),
            transmitted.samples,
            np.zeros(53, dtype=np.complex64),
        )
    )
    taps = np.array([0.82 + 0.13j, 0, 0.21 - 0.17j], dtype=np.complex64)
    received = np.convolve(buffered, taps)[: buffered.size]
    sample_index = np.arange(received.size, dtype=np.float64)
    received = 0.7 * received * np.exp(2j * np.pi * 0.137 * sample_index / 64)
    signal_power = np.mean(np.abs(received[leading : leading + transmitted.sample_count]) ** 2)
    noise_power = signal_power / 10 ** (snr_db / 10)
    rng = np.random.default_rng(20260917)
    noise = np.sqrt(noise_power / 2) * (
        rng.standard_normal(received.size) + 1j * rng.standard_normal(received.size)
    )

    decoded = decode_burst((received + noise).astype(np.complex64))

    assert decoded.frame == frame
    assert decoded.burst_start == leading


@pytest.mark.parametrize("mcs", list(MCS))
@pytest.mark.parametrize("sfo_ppm", [-20.0, 20.0])
def test_burst_tracks_representative_sample_clock_offset(
    mcs: MCS,
    sfo_ppm: float,
) -> None:
    frame = _frame(mcs, payload_size=220)
    transmitted = encode_burst(frame)
    buffered = np.concatenate(
        (
            np.zeros(41, dtype=np.complex64),
            transmitted.samples,
            np.zeros(100, dtype=np.complex64),
        )
    )
    received, channel_diagnostics = apply_simulation_channel(
        buffered,
        SimulationChannelConfig(sample_rate_offset_ppm=sfo_ppm),
    )

    decoded = decode_burst(received)

    assert decoded.frame == frame
    assert channel_diagnostics.sample_rate_ratio == pytest.approx(1.0 + sfo_ppm * 1e-6)
    assert any(
        abs(slope) > 1e-4 for slope in decoded.diagnostics.payload_phase_slope_rad_per_carrier
    )


@pytest.mark.parametrize(
    ("mcs", "payload_size", "sfo_ppm", "expected_payload_symbols"),
    [
        (MCS.QPSK, 1273, -20.0, 215),
        (MCS.QPSK, 1273, 20.0, 215),
        (MCS.QAM16, 1273, -20.0, 108),
        (MCS.QAM16, 1273, 0.0, 108),
        (MCS.QAM16, 1272, 20.0, 107),
        (MCS.QAM16, 1273, 20.0, 108),
    ],
)
def test_burst_tracks_long_nominal_sample_clock_offset(
    mcs: MCS,
    payload_size: int,
    sfo_ppm: float,
    expected_payload_symbols: int,
) -> None:
    payload = bytes((index * 17 + 3) % 256 for index in range(payload_size))
    frame = Frame(
        protocol_version=CURRENT_PROTOCOL_VERSION,
        kind=FrameKind.DATA,
        mcs=mcs,
        sequence=0x1357,
        payload=payload,
    )
    transmitted = encode_burst(frame)
    received, channel_diagnostics = apply_simulation_channel(
        transmitted.samples,
        SimulationChannelConfig(
            taps=(1.0 + 0.0j, 0.18 + 0.08j, 0.06 - 0.04j),
            sample_rate_offset_ppm=sfo_ppm,
            cfo_subcarriers=0.2,
            snr_db=15.0 if mcs is MCS.QPSK else 24.0,
            seed=0,
        ),
    )

    decoded = decode_burst(received)

    assert decoded.frame == frame
    assert transmitted.header.payload_ofdm_symbol_count == expected_payload_symbols
    assert len(decoded.diagnostics.payload_common_phase_rad) == expected_payload_symbols
    assert len(decoded.diagnostics.payload_phase_slope_rad_per_carrier) == (
        expected_payload_symbols
    )
    assert decoded.diagnostics.decode_outcome == "decoded"
    assert decoded.diagnostics.sample_clock_tracking.symbols_observed == (
        expected_payload_symbols
    )
    assert decoded.diagnostics.sample_clock_tracking.max_abs_timing_correction_samples <= 4.0
    assert abs(decoded.diagnostics.sample_clock_tracking.applied_correction_ppm) <= 40.0
    assert decoded.diagnostics.payload_pilot_magnitude.enabled == (mcs is MCS.QAM16)
    assert 0.5 <= decoded.diagnostics.payload_pilot_magnitude.correction_gain_min
    assert decoded.diagnostics.payload_pilot_magnitude.correction_gain_max <= 2.0
    assert channel_diagnostics.sample_rate_ratio == pytest.approx(1.0 + sfo_ppm * 1e-6)
    assert channel_diagnostics.normalized_cfo_cycles_per_sample == pytest.approx(0.2 / 64)
    assert channel_diagnostics.seed == 0
    if sfo_ppm == 0.0:
        assert abs(decoded.diagnostics.sample_clock_tracking.estimated_sfo_ppm) < 5.0
        assert not decoded.diagnostics.sample_clock_tracking.correction_applied
    else:
        assert np.sign(decoded.diagnostics.sample_clock_tracking.estimated_sfo_ppm) == np.sign(
            sfo_ppm
        )
        assert decoded.diagnostics.sample_clock_tracking.correction_applied


def test_burst_decode_error_exposes_bounded_payload_diagnostics() -> None:
    frame = _frame(MCS.QAM16, payload_size=1273)
    transmitted = encode_burst(frame)

    def reject_frame(_coded: np.ndarray) -> Frame:
        raise ValueError("forced decoder rejection")

    with pytest.raises(BurstDecodeError, match="forced decoder rejection") as error:
        decode_burst(transmitted.samples, frame_decoder=reject_frame)

    assert error.value.diagnostics is not None
    assert error.value.diagnostics.decode_outcome == "payload_decode_failed"
    assert error.value.diagnostics.sample_clock_tracking.limit_event_count >= 0


def test_burst_reports_sample_clock_tracking_limit_events() -> None:
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QAM16,
        0x1357,
        bytes((index * 17 + 3) % 256 for index in range(1273)),
    )
    transmitted = encode_burst(frame)
    received, _ = apply_simulation_channel(
        transmitted.samples,
        SimulationChannelConfig(
            taps=(1.0 + 0.0j, 0.18 + 0.08j, 0.06 - 0.04j),
            sample_rate_offset_ppm=20.0,
            cfo_subcarriers=0.2,
            snr_db=24.0,
            seed=0,
        ),
    )
    config = BurstConfig(
        sample_clock_tracking=SampleClockTrackingConfig(
            loop_gain=10.0,
            max_abs_correction_ppm=1.0,
            minimum_normalized_timing_shift=0.0,
        )
    )

    decoded = decode_burst(received, config)

    assert decoded.frame == frame
    assert decoded.diagnostics.sample_clock_tracking.applied_correction_ppm == 1.0
    assert decoded.diagnostics.sample_clock_tracking.limit_hit
    assert decoded.diagnostics.sample_clock_tracking.limit_event_count > 0


def test_long_negative_sfo_uses_bounded_trailing_guard_for_interpolation() -> None:
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QAM16,
        0x1357,
        bytes((index * 17 + 3) % 256 for index in range(1273)),
    )
    transmitted = encode_burst(frame)
    guarded = np.concatenate((transmitted.samples, np.zeros(6, dtype=np.complex64)))
    received, _ = apply_simulation_channel(
        guarded,
        SimulationChannelConfig(sample_rate_offset_ppm=-20.0),
    )

    decoded = decode_burst(received)

    assert decoded.frame == frame
    assert decoded.diagnostics.sample_clock_tracking.correction_applied
    assert decoded.burst_end < received.size


def test_burst_rejects_corruption_truncation_and_ambiguous_trailing_burst() -> None:
    frame = _frame(MCS.QPSK)
    transmitted = encode_burst(frame)
    corrupted_header = transmitted.samples.copy()
    corrupted_header[160:400] = 0
    corrupted_payload = transmitted.samples.copy()
    corrupted_payload[400:] = 0

    with pytest.raises(BurstDecodeError, match="header decode"):
        decode_burst(corrupted_header)
    with pytest.raises(BurstDecodeError, match="payload frame decode"):
        decode_burst(corrupted_payload)
    with pytest.raises(BurstDecodeError, match="truncated payload"):
        decode_burst(transmitted.samples[:-1])
    with pytest.raises(BurstDecodeError, match="unambiguous"):
        decode_burst(
            np.concatenate(
                (
                    transmitted.samples,
                    np.zeros(91, dtype=np.complex64),
                    transmitted.samples,
                )
            )
        )


def test_every_truncated_burst_reports_the_sample_count_it_still_needs() -> None:
    """Shortage inside the timing margin is still a shortage.

    A continuous receiver only knows to wait for the rest of a burst when the
    error says how many samples the signaling header asked for.  A burst one
    sample short is the common chunk boundary, so losing that number there
    costs the whole burst.
    """

    frame = _frame(MCS.QPSK)
    transmitted = encode_burst(frame)

    for missing in (1, 3, 20, 400):
        with pytest.raises(BurstDecodeError, match="truncated payload") as raised:
            decode_burst(transmitted.samples[:-missing])
        assert raised.value.required_sample_count == transmitted.sample_count


def test_burst_rejects_signaled_sizes_before_payload_processing() -> None:
    frame = _frame(MCS.QPSK, payload_size=10)
    transmitted = encode_burst(frame)
    restrictive = BurstConfig(max_coded_payload_bits=300)

    with pytest.raises(BurstValidationError, match="coded frame"):
        encode_burst(frame, restrictive)
    with pytest.raises(BurstDecodeError, match="max_coded_payload_bits"):
        decode_burst(transmitted.samples, restrictive)
    with pytest.raises(BurstDecodeError, match="max_burst_samples"):
        decode_burst(
            transmitted.samples,
            BurstConfig(max_burst_samples=500),
        )
    with pytest.raises(BurstDecodeError, match="max_input_samples"):
        decode_burst(
            np.ones(101, dtype=np.complex64),
            BurstConfig(max_burst_samples=100, max_input_samples=100),
        )


@pytest.mark.parametrize(
    "samples",
    [
        np.ones((2, 80), dtype=np.complex64),
        np.array(["not-a-sample"]),
        np.array([complex(np.nan, 0)], dtype=np.complex64),
    ],
)
def test_burst_rejects_malformed_or_nonfinite_samples(samples: np.ndarray) -> None:
    with pytest.raises(BurstDecodeError):
        decode_burst(samples)


def test_burst_rejects_header_and_frame_mcs_mismatch() -> None:
    frame = _frame(MCS.QPSK, payload_size=24)
    coded_payload = encode_frame(frame)
    mismatched_header = BurstHeader(
        CURRENT_BURST_HEADER_VERSION,
        MCS.QAM16,
        int(coded_payload.size),
    )
    header_waveform = modulate_ofdm(
        map_symbols(encode_burst_header(mismatched_header), MCS.QPSK),
        first_symbol_index=0,
    )
    payload_padding = (-coded_payload.size) % MCS.QAM16.bits_per_symbol
    payload_waveform = modulate_ofdm(
        map_symbols(np.pad(coded_payload, (0, payload_padding)), MCS.QAM16),
        first_symbol_index=BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT,
    )
    samples = np.concatenate(
        (
            generate_preamble(),
            generate_training_symbol().samples,
            header_waveform.samples,
            payload_waveform.samples,
        )
    )

    with pytest.raises(BurstDecodeError, match="MCS do not match"):
        decode_burst(samples)


def test_burst_accepts_an_explicit_frame_decoder_backend() -> None:
    frame = _frame(MCS.QAM16, payload_size=41)
    transmitted = encode_burst(frame)
    observed: list[np.ndarray] = []

    def decoder(coded_bits: np.ndarray) -> Frame:
        observed.append(coded_bits.copy())
        return frame

    decoded = decode_burst(transmitted.samples, frame_decoder=decoder)

    assert decoded.frame == frame
    assert len(observed) == 1
    np.testing.assert_array_equal(observed[0], encode_frame(frame))


def test_burst_rejects_invalid_frame_decoder_contract() -> None:
    frame = _frame(MCS.QPSK, payload_size=8)
    transmitted = encode_burst(frame)

    with pytest.raises(BurstValidationError, match="frame_decoder"):
        decode_burst(transmitted.samples, frame_decoder=None)  # type: ignore[arg-type]
    with pytest.raises(BurstDecodeError, match="return a Frame"):
        decode_burst(
            transmitted.samples,
            frame_decoder=lambda _coded: object(),  # type: ignore[arg-type,return-value]
        )


@pytest.mark.parametrize("mcs", list(MCS))
def test_burst_round_trip_uses_selected_symbol_mapper_for_header_and_payload(
    mcs: MCS,
) -> None:
    frame = _frame(mcs, payload_size=41)
    observed: list[tuple[np.ndarray, MCS]] = []

    def mapper(bits: np.ndarray, selected_mcs: MCS) -> np.ndarray:
        observed.append((bits.copy(), selected_mcs))
        return map_symbols(bits, selected_mcs)

    transmitted = encode_burst(frame, symbol_mapper=mapper)
    decoded = decode_burst(transmitted.samples)

    assert decoded.frame == frame
    assert [selected_mcs for _, selected_mcs in observed] == [MCS.QPSK, mcs]
    np.testing.assert_array_equal(observed[0][0], encode_burst_header(transmitted.header))
    assert observed[1][0].size >= transmitted.header.coded_payload_bit_count


@pytest.mark.parametrize("mcs", list(MCS))
def test_burst_round_trip_uses_selected_symbol_demapper_for_header_and_payload(
    mcs: MCS,
) -> None:
    frame = _frame(mcs, payload_size=41)
    transmitted = encode_burst(frame)
    observed: list[tuple[np.ndarray, MCS]] = []

    def demapper(symbols: np.ndarray, selected_mcs: MCS) -> np.ndarray:
        observed.append((symbols.copy(), selected_mcs))
        return demap_symbols(symbols, selected_mcs)

    decoded = decode_burst(transmitted.samples, symbol_demapper=demapper)

    assert decoded.frame == frame
    assert [selected_mcs for _, selected_mcs in observed] == [MCS.QPSK, mcs]
    assert observed[0][0].size == 3 * 48
    assert observed[1][0].size == transmitted.header.payload_symbol_count


def test_burst_rejects_non_callable_symbol_backends() -> None:
    frame = _frame(MCS.QPSK, payload_size=8)
    transmitted = encode_burst(frame)

    with pytest.raises(BurstValidationError, match="symbol_mapper"):
        encode_burst(frame, symbol_mapper=None)  # type: ignore[arg-type]
    with pytest.raises(BurstValidationError, match="symbol_demapper"):
        decode_burst(transmitted.samples, symbol_demapper=None)  # type: ignore[arg-type]


def test_burst_rejects_invalid_symbol_backend_output_shapes() -> None:
    frame = _frame(MCS.QPSK, payload_size=8)
    transmitted = encode_burst(frame)

    with pytest.raises(BurstValidationError, match="symbol_mapper.*one-dimensional"):
        encode_burst(
            frame,
            symbol_mapper=lambda _bits, _mcs: np.ones((1, 2), dtype=np.complex64),
        )
    with pytest.raises(BurstDecodeError, match="symbol_demapper.*one-dimensional"):
        decode_burst(
            transmitted.samples,
            symbol_demapper=lambda _symbols, _mcs: np.ones((1, 2), dtype=np.uint8),
        )


def test_burst_wraps_symbol_backend_errors() -> None:
    frame = _frame(MCS.QPSK, payload_size=8)
    transmitted = encode_burst(frame)

    def failing_mapper(_bits: np.ndarray, _mcs: MCS) -> np.ndarray:
        raise RuntimeError("mapper unavailable")

    def failing_demapper(_symbols: np.ndarray, _mcs: MCS) -> np.ndarray:
        raise RuntimeError("demapper unavailable")

    with pytest.raises(BurstValidationError, match="symbol_mapper failed: mapper unavailable"):
        encode_burst(frame, symbol_mapper=failing_mapper)
    with pytest.raises(BurstDecodeError, match="symbol_demapper failed: demapper unavailable"):
        decode_burst(transmitted.samples, symbol_demapper=failing_demapper)


def test_burst_header_soft_fallback_recovers_one_bad_qpsk_header_symbol() -> None:
    # A single noisy QPSK symbol carries two of the three copies of one header
    # bit, which defeats the hard majority vote but not soft combining.
    frame = _frame(MCS.QPSK, payload_size=41)
    transmitted = encode_burst(frame)
    header_calls = 0

    def corrupting_demapper(symbols: np.ndarray, selected_mcs: MCS) -> np.ndarray:
        nonlocal header_calls
        bits = demap_symbols(symbols, selected_mcs)
        if symbols.size == 3 * 48 and header_calls == 0:
            header_calls += 1
            bits[0:2] ^= 1
        return bits

    decoded = decode_burst(transmitted.samples, symbol_demapper=corrupting_demapper)

    assert header_calls == 1
    assert decoded.frame == frame
