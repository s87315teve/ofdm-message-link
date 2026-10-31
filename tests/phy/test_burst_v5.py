from __future__ import annotations

import zlib
from dataclasses import replace

import numpy as np
import pytest

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    BurstConfig,
    BurstDecodeError,
    BurstValidationError,
    DecodedBurst,
    DecodedPsduBurst,
    Frame,
    FrameKind,
    V5RxConfig,
    decode_burst,
    encode_burst,
    encode_burst_v5,
    mcs_entry,
    v5_burst_sample_count,
    v5_entries,
    v5_payload_geometry,
)
from ofdm_link.phy.burst import _decode_burst_with_acquisition, _header_candidates
from ofdm_link.phy.burst_header import (
    _CRC,
    _PREFIX,
    BurstHeader,
    BurstHeaderDecodeError,
    BurstHeaderValidationError,
    TurboBurstHeader,
    _parse_header_bits,
)
from ofdm_link.phy.codec import DEFAULT_DUAL_VERSION_FRAME_DECODER, demap_symbols
from ofdm_link.phy.sync import acquire_all
from ofdm_link.phy.sync_v5 import V5SyncConfig
from ofdm_link.radio.simulation import SimulationChannelConfig, apply_simulation_channel

ALL_V5 = v5_entries(include_hidden=True)
# The fixed 5-tap channel of experiments/rx_improvement/sim_sweep.py.
TAPS = np.array([0.82 + 0.13j, 0, 0.21 - 0.17j, 0, 0.08j])
TAPS = tuple(complex(tap) for tap in TAPS / np.linalg.norm(TAPS))
# Comfortable operating SNR per constellation, a few dB above each family's
# highest-rate 1 % point, so a loopback failure means a bug, not bad luck.
COMFORTABLE_SNR_DB = {MCS.QPSK: 14.0, MCS.QAM16: 22.0, MCS.QAM64: 30.0}


def _body(size: int, seed: int) -> bytes:
    return np.random.default_rng(seed).integers(0, 256, size, dtype=np.uint8).tobytes()


def _awgn(samples: np.ndarray, snr_db: float, rng: np.random.Generator, power: float) -> np.ndarray:
    variance = power / 10 ** (snr_db / 10)
    noise = rng.standard_normal(samples.size) + 1j * rng.standard_normal(samples.size)
    return (samples + np.sqrt(variance / 2) * noise).astype(np.complex64)


def _framed(samples: np.ndarray, before: int = 16, after: int = 64) -> np.ndarray:
    return np.concatenate((np.zeros(before), samples, np.zeros(after))).astype(np.complex64)


@pytest.mark.parametrize("entry", ALL_V5, ids=lambda entry: f"mcs{entry.index}")
@pytest.mark.parametrize("size", [15, 943, 3000, 7551])
def test_every_v5_mcs_round_trips_noiseless_and_at_25_db(entry, size: int) -> None:
    body = _body(size, entry.index * 10_000 + size)
    burst = encode_burst_v5(body, entry, scrambler_seed=1 + (size % 127))
    geometry = v5_payload_geometry(entry, size + 4)

    assert burst.sample_count == 560 + 80 * geometry.ofdm_symbol_count
    assert burst.sample_count == v5_burst_sample_count(entry, size)
    assert burst.header.psdu_length == size + 4

    clean = decode_burst(_framed(burst.samples))
    assert isinstance(clean, DecodedPsduBurst)
    assert clean.fcs_ok and clean.body == body
    assert clean.mcs_index == entry.index and clean.modulation is entry.modulation
    assert clean.burst_start == 16
    assert clean.burst_end == 16 + burst.sample_count

    noisy = decode_burst(_awgn(_framed(burst.samples), 25.0, np.random.default_rng(size), 0.8))
    if entry.modulation is not MCS.QAM64 or entry.rate.numerator == 1:
        # 25 dB is short of 64QAM 2/3 and 3/4's operating point.
        assert noisy.fcs_ok and noisy.body == body


@pytest.mark.parametrize("entry", ALL_V5, ids=lambda entry: f"mcs{entry.index}")
def test_awgn_cfo_and_timing_offset_at_an_operating_snr(entry) -> None:
    rng = np.random.default_rng(100 + entry.index)
    snr = COMFORTABLE_SNR_DB[entry.modulation]
    for trial in range(3):
        body = _body(int(rng.integers(19, 2500)), 1000 * entry.index + trial)
        burst = encode_burst_v5(body, entry, scrambler_seed=int(rng.integers(1, 128)))
        lead = int(rng.integers(0, 300))
        stream = _framed(burst.samples, before=lead, after=200)
        cfo = float(rng.uniform(-1.5, 1.5))
        stream = stream * np.exp(2j * np.pi * cfo * np.arange(stream.size) / 64)
        decoded = decode_burst(_awgn(stream, snr, rng, 0.8))
        assert isinstance(decoded, DecodedPsduBurst)
        assert decoded.burst_start == lead
        assert abs(decoded.diagnostics.acquisition.normalized_cfo - cfo) < 0.01
        assert decoded.fcs_ok and decoded.body == body


@pytest.mark.parametrize("mcs_index", [8, 10, 12, 14, 15])
def test_multipath_with_sample_clock_offset_decodes(mcs_index: int) -> None:
    entry = mcs_entry(mcs_index)
    body = _body(2000, mcs_index)
    burst = encode_burst_v5(body, entry, scrambler_seed=42)
    snr = COMFORTABLE_SNR_DB[entry.modulation] + 2.0
    received, _ = apply_simulation_channel(
        _framed(burst.samples, before=200, after=300),
        SimulationChannelConfig(
            amplitude_gain=0.7,
            phase_offset_radians=1.1,
            taps=TAPS,
            sample_rate_offset_ppm=20.0,
            cfo_subcarriers=0.3,
            snr_db=snr,
            seed=mcs_index,
        ),
    )

    decoded = decode_burst(received.astype(np.complex64))

    assert isinstance(decoded, DecodedPsduBurst)
    assert decoded.fcs_ok and decoded.body == body
    assert decoded.diagnostics.wire_version == 5
    assert decoded.diagnostics.active_fec_profile == f"lte-turbo-rm r{entry.rate}"


@pytest.mark.parametrize("late", [1, 2, 3])
def test_fft_backoff_absorbs_late_timing_without_isi(late: int) -> None:
    # A window that starts at the CP end takes samples of the next symbol as
    # soon as the timing is late; three samples of backoff keep it inside
    # the symbol, and the LTF estimate absorbs the linear phase.
    entry = mcs_entry(14)
    body = _body(2000, 77)
    burst = encode_burst_v5(body, entry, scrambler_seed=9)
    framed = _framed(burst.samples, before=200, after=300)
    stream = _awgn(framed, 35.0, np.random.default_rng(7), 0.8)
    acquisition = acquire_all(stream, None, V5SyncConfig())[0]
    assert acquisition.stf_start == 200
    late_acquisition = replace(
        acquisition, stf_start=200 + late, ltf_start=acquisition.ltf_start + late
    )
    reference = decode_burst(stream).payload_symbols

    def evm_db(backoff: int) -> tuple[float, bool]:
        decoded = _decode_burst_with_acquisition(
            stream,
            BurstConfig(v5=V5RxConfig(fft_backoff_samples=backoff)),
            late_acquisition,
            frame_decoder=DEFAULT_DUAL_VERSION_FRAME_DECODER,
            symbol_demapper=demap_symbols,
            soft_symbol_demapper=None,
            input_noise_variance=None,
        )
        error = np.mean(np.abs(decoded.payload_symbols - reference) ** 2)
        return float(10 * np.log10(error)), decoded.fcs_ok

    default_evm, default_ok = evm_db(V5RxConfig().fft_backoff_samples)
    no_backoff_evm, _ = evm_db(0)
    assert default_ok and default_evm < -28.0
    assert no_backoff_evm > -20.0


def test_fft_backoff_is_validated() -> None:
    with pytest.raises(ValueError):
        V5RxConfig(fft_backoff_samples=9)
    with pytest.raises(ValueError):
        V5RxConfig(fft_backoff_samples=-1)
    assert V5RxConfig().fft_backoff_samples == 3


def test_truncated_burst_asks_for_the_signalled_length() -> None:
    entry = mcs_entry(9)
    burst = encode_burst_v5(_body(900, 1), entry, scrambler_seed=5)
    stream = _framed(burst.samples)

    with pytest.raises(BurstDecodeError) as raised:
        decode_burst(stream[: 16 + 560 + 800])

    assert raised.value.required_sample_count == 16 + burst.sample_count


def test_a_bad_fcs_is_returned_as_a_result_not_raised() -> None:
    entry = mcs_entry(8)
    body = _body(3000, 2)
    burst = encode_burst_v5(body, entry, scrambler_seed=6)
    damaged = np.array(burst.samples)
    # Wipe the last third of the payload: the SIG and preamble stay intact.
    damaged[-damaged.size // 3 :] = 0.0
    rng = np.random.default_rng(3)
    stream = _awgn(_framed(damaged), 30.0, rng, 0.8)

    decoded = decode_burst(stream)

    assert isinstance(decoded, DecodedPsduBurst)
    assert not decoded.fcs_ok
    assert decoded.diagnostics.decode_outcome == "payload_decode_failed"
    assert decoded.diagnostics.outer_crc_ok is False
    assert len(decoded.body) == len(body)
    # The leading code block was untouched and still decodes.
    first_start, first_end = decoded.result.block_byte_ranges[0]
    assert decoded.body[first_start:first_end] == body[first_start:first_end]


def test_scrambler_off_seed_zero_still_round_trips() -> None:
    body = bytes(500)  # all zeros: the case the scrambler exists for
    burst = encode_burst_v5(body, mcs_entry(12), scrambler_seed=0)

    decoded = decode_burst(_framed(burst.samples))

    assert decoded.fcs_ok and decoded.body == body


def test_scrambler_whitens_a_constant_payload() -> None:
    body = bytes(4000)
    scrambled = encode_burst_v5(body, mcs_entry(12), scrambler_seed=77).samples[560:]
    plain = encode_burst_v5(body, mcs_entry(12), scrambler_seed=0).samples[560:]

    def papr(samples: np.ndarray) -> float:
        power = np.abs(samples) ** 2
        return float(10 * np.log10(power.max() / power.mean()))

    assert papr(scrambled) < papr(plain)


@pytest.mark.parametrize(
    "rx",
    [
        V5RxConfig(ltf_average=False),
        V5RxConfig(channel_smoothing=False),
        V5RxConfig(llr_noise_source="header"),
        V5RxConfig(llr_noise_source="ltf"),
        V5RxConfig(turbo_max_iterations=4),
        V5RxConfig(legacy_acquisition=False),
    ],
)
def test_receiver_ablation_switches_still_decode(rx: V5RxConfig) -> None:
    config = BurstConfig(v5=rx)
    body = _body(1200, 7)
    burst = encode_burst_v5(body, mcs_entry(12), config, scrambler_seed=9)
    stream = _awgn(_framed(burst.samples, before=100), 24.0, np.random.default_rng(8), 0.8)

    decoded = decode_burst(stream, config)

    assert decoded.fcs_ok and decoded.body == body


def test_v5_acquisition_switch_ignores_v5_bursts() -> None:
    config = BurstConfig(v5=V5RxConfig(v5_acquisition=False))
    burst = encode_burst_v5(_body(300, 9), mcs_entry(8), scrambler_seed=1)

    with pytest.raises(BurstDecodeError, match="no unambiguous preamble"):
        decode_burst(_framed(burst.samples), config)


def test_signalled_psdu_above_the_receive_limit_is_a_header_failure() -> None:
    burst = encode_burst_v5(_body(3000, 10), mcs_entry(14), scrambler_seed=2)
    config = BurstConfig(v5=V5RxConfig(max_psdu_bytes=1000))

    with pytest.raises(BurstDecodeError, match="fixed signaling header decode failed"):
        decode_burst(_framed(burst.samples), config)


def test_signalled_burst_longer_than_max_burst_samples_is_rejected() -> None:
    burst = encode_burst_v5(_body(3000, 11), mcs_entry(8), scrambler_seed=2)
    config = BurstConfig(max_burst_samples=20_000)

    with pytest.raises(BurstDecodeError, match="signaled burst exceeds"):
        decode_burst(_framed(burst.samples), config)


def test_encoder_refuses_legacy_entries_bad_seeds_and_short_bodies() -> None:
    with pytest.raises(BurstValidationError):
        encode_burst_v5(b"x" * 100, mcs_entry(0), scrambler_seed=1)
    with pytest.raises(BurstValidationError):
        encode_burst_v5(b"x" * 100, mcs_entry(8), scrambler_seed=128)
    with pytest.raises(BurstValidationError):
        encode_burst_v5(b"x" * 14, mcs_entry(8), scrambler_seed=1)


def test_legacy_burst_still_returns_a_decoded_frame() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QAM16, 3, b"legacy payload")
    burst = encode_burst(frame, wire_version=2)

    decoded = decode_burst(_framed(burst.samples, before=300, after=300))

    assert isinstance(decoded, DecodedBurst) and decoded.frame == frame


def test_legacy_encoder_refuses_64qam_frames() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QAM64, 3, b"v5 only")

    with pytest.raises(BurstValidationError, match="wire v5"):
        encode_burst(frame, wire_version=2)


def test_legacy_headers_cannot_name_64qam() -> None:
    with pytest.raises(BurstHeaderValidationError):
        BurstHeader(1, MCS.QAM64, 1000)
    with pytest.raises(BurstHeaderValidationError):
        TurboBurstHeader(2, MCS.QAM64, 1000)
    prefix = _PREFIX.pack(b"OF", 2, int(MCS.QAM64), 1000)
    raw = prefix + _CRC.pack(zlib.crc32(prefix) & 0xFFFFFFFF)
    bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8)).astype(bool)
    with pytest.raises(BurstHeaderDecodeError, match="unknown MCS 2"):
        _parse_header_bits(bits)


def test_legacy_header_list_holds_no_64qam_header() -> None:
    config = BurstConfig()
    candidates = _header_candidates(
        config.header_list_max_payload_bytes, config.max_coded_payload_bits
    )

    assert {header.mcs for header in candidates.headers} == {MCS.QPSK, MCS.QAM16}


def test_diagnostics_refuse_a_wire_version_that_does_not_match_the_acquisition() -> None:
    burst = encode_burst_v5(_body(100, 12), mcs_entry(8), scrambler_seed=3)
    decoded = decode_burst(_framed(burst.samples))

    with pytest.raises(BurstDecodeError):
        replace(decoded.diagnostics, wire_version=2)


def test_sig_survives_well_below_the_payload_operating_point() -> None:
    """At 1 dB wideband SNR (about 2 dB per carrier) the SIG must still decode.

    The SIG equalizer fits only a common phase: fitting the legacy line
    through four noisy pilot phases cost about 3 dB of SIG robustness here
    (5 % SIG failures at 2 dB with it, none without).
    """

    rng = np.random.default_rng(20261102)
    header_failures = 0
    for trial in range(100):
        burst = encode_burst_v5(_body(15, trial), mcs_entry(8), scrambler_seed=1 + trial)
        stream = _framed(burst.samples, before=300, after=300)
        stream = stream * np.exp(2j * np.pi * 0.13 * np.arange(stream.size) / 64)
        try:
            decode_burst(_awgn(stream, 1.0, rng, 52 / 64))
        except BurstDecodeError as error:
            header_failures += "header" in str(error)
    assert header_failures <= 3
