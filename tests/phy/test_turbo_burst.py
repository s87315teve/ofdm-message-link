from __future__ import annotations

import numpy as np
import pytest

from ofdm_link.phy import (
    BURST_HEADER_CODED_BIT_COUNT,
    BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT,
    CURRENT_BURST_HEADER_VERSION,
    CURRENT_PROTOCOL_VERSION,
    MCS,
    TURBO_BURST_HEADER_VERSION,
    BurstDecodeError,
    BurstHeader,
    Frame,
    FrameKind,
    TurboBurstHeader,
    decode_burst,
    encode_burst,
    encode_burst_header,
    encode_frame,
    information_frame_bit_count,
    make_dual_version_frame_decoder,
    map_symbols,
    turbo_coded_frame_bit_count,
)
from ofdm_link.phy.channel import generate_training_symbol
from ofdm_link.phy.ofdm import modulate_ofdm
from ofdm_link.phy.sync import generate_preamble
from ofdm_link.radio import SimulationChannelConfig, apply_simulation_channel


def _frame(mcs: MCS, payload_size: int = 96) -> Frame:
    return Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        mcs,
        0x2468,
        bytes((index * 29 + 7) & 0xFF for index in range(payload_size)),
    )


@pytest.mark.parametrize("mcs", list(MCS))
def test_v2_turbo_complete_burst_round_trip_and_bounded_telemetry(mcs: MCS) -> None:
    frame = _frame(mcs)

    transmitted = encode_burst(frame, wire_version=TURBO_BURST_HEADER_VERSION)
    decoded = decode_burst(transmitted.samples, input_noise_variance=1e-6)

    assert decoded.frame == frame
    assert isinstance(transmitted.header, TurboBurstHeader)
    assert transmitted.header.information_frame_bit_count == information_frame_bit_count(frame)
    assert transmitted.header.coded_payload_bit_count == turbo_coded_frame_bit_count(frame)
    assert decoded.diagnostics.wire_version == TURBO_BURST_HEADER_VERSION
    assert decoded.diagnostics.active_fec_profile == "lte-derived-turbo-r1/3"
    assert decoded.diagnostics.decision_mode == "turbo"
    assert decoded.diagnostics.code_block_count >= 1
    assert decoded.diagnostics.configured_max_iterations == 8
    assert decoded.diagnostics.actual_iterations_per_block == (1,)
    assert decoded.diagnostics.early_stop_reasons == ("outer_crc32",)
    assert decoded.diagnostics.outer_crc_ok is True
    assert 0.0 <= decoded.diagnostics.llr_saturation_rate <= 1.0


@pytest.mark.parametrize(
    ("mcs", "snr_db"),
    [(MCS.QPSK, 15.0), (MCS.QAM16, 24.0)],
)
def test_v2_turbo_nominal_three_tap_channel_round_trip(mcs: MCS, snr_db: float) -> None:
    frame = _frame(mcs, payload_size=220)
    transmitted = encode_burst(frame, wire_version=TURBO_BURST_HEADER_VERSION)
    received, channel = apply_simulation_channel(
        transmitted.samples,
        SimulationChannelConfig(
            amplitude_gain=0.7,
            taps=(0.82 + 0.13j, 0j, 0.21 - 0.17j),
            cfo_subcarriers=0.137,
            sample_rate_offset_ppm=20.0,
            snr_db=snr_db,
            seed=20260917,
        ),
    )

    decoded = decode_burst(
        received,
        input_noise_variance=channel.noise_power,
    )

    assert decoded.frame == frame
    assert decoded.diagnostics.decision_mode == "turbo"
    assert decoded.diagnostics.estimated_noise_variance is not None


def test_one_receiver_accepts_v1_and_v2_without_negotiation() -> None:
    frame = _frame(MCS.QPSK, payload_size=32)

    v1 = decode_burst(encode_burst(frame).samples)
    v2 = decode_burst(
        encode_burst(frame, wire_version=TURBO_BURST_HEADER_VERSION).samples,
        input_noise_variance=1e-6,
    )

    assert v1.frame == v2.frame == frame
    assert v1.diagnostics.wire_version == CURRENT_BURST_HEADER_VERSION
    assert v2.diagnostics.wire_version == TURBO_BURST_HEADER_VERSION


@pytest.mark.parametrize("mcs", list(MCS))
def test_interleaved_v2_v1_v2_bursts_keep_decode_reports_atomic(mcs: MCS) -> None:
    decoder = make_dual_version_frame_decoder()
    frame = _frame(mcs, payload_size=32)

    first_v2 = decode_burst(
        encode_burst(frame, wire_version=TURBO_BURST_HEADER_VERSION).samples,
        frame_decoder=decoder,
        input_noise_variance=1e-6,
    )
    v1 = decode_burst(
        encode_burst(frame, wire_version=CURRENT_BURST_HEADER_VERSION).samples,
        frame_decoder=decoder,
    )
    second_v2 = decode_burst(
        encode_burst(frame, wire_version=TURBO_BURST_HEADER_VERSION).samples,
        frame_decoder=decoder,
        input_noise_variance=1e-6,
    )

    assert first_v2.diagnostics.actual_iterations_per_block == (1,)
    assert first_v2.diagnostics.early_stop_reasons == ("outer_crc32",)
    assert v1.diagnostics.actual_iterations_per_block == ()
    assert v1.diagnostics.early_stop_reasons == ()
    assert second_v2.diagnostics.actual_iterations_per_block == (1,)
    assert second_v2.diagnostics.early_stop_reasons == ("outer_crc32",)


def test_v1_header_field_remains_coded_length_and_v2_field_is_information_length() -> None:
    frame = _frame(MCS.QAM16, payload_size=41)

    v1 = encode_burst(frame)
    v2 = encode_burst(frame, wire_version=TURBO_BURST_HEADER_VERSION)

    assert isinstance(v1.header, BurstHeader)
    assert v1.header.coded_payload_bit_count == encode_frame(frame).size
    assert isinstance(v2.header, TurboBurstHeader)
    assert v2.header.information_frame_bit_count == information_frame_bit_count(frame)
    assert v1.header.wire_version == 1
    assert v2.header.wire_version == 2
    assert BURST_HEADER_CODED_BIT_COUNT == 288
    assert BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT == 3


def test_v2_rejects_a_v1_preencoded_payload() -> None:
    frame = _frame(MCS.QPSK, payload_size=8)

    with pytest.raises(ValueError, match="preencoded_frame_bits"):
        encode_burst(
            frame,
            wire_version=TURBO_BURST_HEADER_VERSION,
            preencoded_frame_bits=encode_frame(frame),
        )


def test_v2_rejects_decoder_without_versioned_contract() -> None:
    frame = _frame(MCS.QPSK, payload_size=8)
    transmitted = encode_burst(frame, wire_version=TURBO_BURST_HEADER_VERSION)

    with pytest.raises(BurstDecodeError, match="payload frame decode"):
        decode_burst(
            transmitted.samples,
            frame_decoder=lambda _observations: frame,
            input_noise_variance=1e-6,
        )


def test_v2_header_information_length_controls_payload_extraction() -> None:
    frame = _frame(MCS.QPSK, payload_size=20)
    turbo = encode_burst(frame, wire_version=TURBO_BURST_HEADER_VERSION)
    wrong_header = TurboBurstHeader(
        TURBO_BURST_HEADER_VERSION,
        frame.mcs,
        information_frame_bit_count(frame) + 8,
    )
    header_waveform = modulate_ofdm(
        map_symbols(encode_burst_header(wrong_header), MCS.QPSK),
        first_symbol_index=0,
    )
    samples = np.concatenate(
        (
            generate_preamble(),
            generate_training_symbol().samples,
            header_waveform.samples,
            turbo.samples[400:],
        )
    )

    with pytest.raises(BurstDecodeError, match="truncated payload"):
        decode_burst(samples, input_noise_variance=1e-6)
