"""Legacy (v1-v4) receive results must not move when the v5 receiver changes.

``legacy_rx_golden.txt`` was produced by the tx-v5 receiver (8aa8a8f) on the
stream below; the v5 receiver fixes after it (acquisition under DC and tones,
timing tracking, FFT backoff, header-candidate handling) must leave every
legacy acquisition, decode and sample-clock estimate exactly as it was, with
the v5 detector on or off.  The channel -- windowed-sinc SFO, CFO, AWGN -- is
built here so a change to the simulator cannot move it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    BurstConfig,
    DecodedBurst,
    Frame,
    FrameKind,
    V5RxConfig,
    encode_burst,
    make_dual_version_frame_decoder,
)
from ofdm_link.phy.streaming import StreamingBurstDecoder

GOLDEN = Path(__file__).with_name("legacy_rx_golden.txt")


def _resample(samples: np.ndarray, ppm: float, taps: int = 32) -> np.ndarray:
    ratio = 1.0 + ppm * 1e-6
    positions = np.arange(int(samples.size / ratio) - taps) * ratio
    base = np.floor(positions).astype(int)
    fraction = positions - base
    out = np.zeros(positions.size, complex)
    for k in range(-taps // 2 + 1, taps // 2 + 1):
        index = base + k
        valid = (index >= 0) & (index < samples.size)
        t = k - fraction
        weight = np.sinc(t) * (0.54 + 0.46 * np.cos(2 * np.pi * t / taps))
        out[valid] += weight[valid] * samples[index[valid]]
    return out


def _stream(seed: int, snr_db: float, ppm: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    parts = [np.zeros(300)]
    plan = [
        (1, MCS.QPSK, 300),
        (2, MCS.QAM16, 996),
        (3, MCS.QPSK, 50),
        (4, MCS.QAM16, 1400),
        (2, MCS.QAM16, 1100),
        (4, MCS.QPSK, 1272),
        (1, MCS.QAM16, 600),
    ]
    for index, (wire, mcs, size) in enumerate(plan):
        payload = rng.integers(0, 256, size, dtype=np.uint8).tobytes()
        frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, mcs, index, payload)
        parts += [
            encode_burst(frame, wire_version=wire).samples,
            np.zeros(256 + int(rng.integers(0, 900))),
        ]
    stream = _resample(np.concatenate(parts), ppm)
    stream = stream * np.exp(2j * np.pi * 0.37 * np.arange(stream.size) / 64 + 0.4j)
    variance = 0.8 / 10 ** (snr_db / 10)
    noise = rng.standard_normal(stream.size) + 1j * rng.standard_normal(stream.size)
    return (0.7 * stream + np.sqrt(variance / 2) * noise).astype(np.complex64)


def _describe(burst: DecodedBurst) -> str:
    diagnostics = burst.diagnostics
    tracking = diagnostics.sample_clock_tracking
    return (
        f"    {burst.burst_start}-{burst.burst_end} v{diagnostics.wire_version} "
        f"{hashlib.sha256(burst.frame.payload).hexdigest()[:12]} "
        f"cfo {diagnostics.acquisition.normalized_cfo:.4f} "
        f"sfo {tracking.estimated_sfo_ppm:.2f} applied {tracking.applied_correction_ppm:.2f} "
        f"evm-ish {float(np.mean(np.abs(burst.payload_symbols) ** 2)):.4f}"
    )


def test_legacy_stream_results_match_the_tx_v5_receiver() -> None:
    frame_decoder = make_dual_version_frame_decoder(accept_all_wire_versions=True)
    lines: list[str] = []
    for seed, snr, ppm in ((1, 30.0, 0.0), (2, 18.0, 35.0), (3, 12.0, -45.0), (4, 8.0, 20.0)):
        stream = _stream(seed, snr, ppm)
        for label, config in (
            ("dual", BurstConfig()),
            ("legacy-only", BurstConfig(v5=V5RxConfig(v5_acquisition=False))),
        ):
            decoder = StreamingBurstDecoder(config, frame_decoder=frame_decoder)
            decoded = []
            for start in range(0, stream.size, 5000):
                decoded += decoder.feed(stream[start : start + 5000])
            counts = decoder.snapshot()
            lines.append(
                f"seed {seed} snr {snr} ppm {ppm} {label}: "
                f"legacy cands {counts.detected_legacy_candidates} "
                f"v5 cands {counts.detected_v5_candidates} "
                f"hdr ok {counts.header_decode_success} fail {counts.header_decode_failure} "
                f"crc {counts.crc_failures}"
            )
            assert all(isinstance(burst, DecodedBurst) for burst in decoded)
            lines.extend(_describe(burst) for burst in decoded)

    assert lines == GOLDEN.read_text().splitlines()
