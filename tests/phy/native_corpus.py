"""Fixed-seed sample buffers for the native-versus-Python equivalence tests.

Each case is one receive buffer: v5 and legacy bursts at a chosen SNR, back
to back or spaced, with optional CFO, sample-clock offset, DC offset, CW
tone, and buffers that hold only noise or only zeros.  The SNR range and the
impairments are the ones the phase-2 stability plan named (-6 to 30 dB, DC,
tone, back-to-back bursts, +-20 ppm).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    Frame,
    FrameKind,
    encode_burst,
    encode_burst_v5,
    mcs_entry,
)


@dataclass(frozen=True)
class Case:
    name: str
    snr_db: float | None = 20.0
    v5_bursts: int = 3
    legacy_bursts: int = 0
    gap: tuple[int, int] = (300, 1500)
    cfo: float = 0.02
    sco_ppm: float = 0.0
    dc: complex = 0.0
    tone: float = 0.0
    tone_frequency: float = 0.11
    seed: int = 0
    noise_only: int = 0
    zeros: int = 0


def _warp(x: np.ndarray, ppm: float) -> np.ndarray:
    """Resample x at t = k (1 + ppm 1e-6) (linear interpolation): a sample-clock offset."""

    if ppm == 0.0:
        return x
    t = np.arange(x.size) * (1.0 + ppm * 1e-6)
    t = t[t <= x.size - 1]
    base = np.floor(t).astype(np.int64)
    frac = t - base
    nxt = np.minimum(base + 1, x.size - 1)
    return (x[base] * (1 - frac) + x[nxt] * frac).astype(np.complex128)


def build(case: Case) -> np.ndarray:
    rng = np.random.default_rng(case.seed)
    if case.zeros:
        return np.zeros(case.zeros, np.complex64)
    parts: list[np.ndarray] = [np.zeros(int(rng.integers(*case.gap)))]
    v5_mcs = (8, 12, 16, 17, 10)
    for index in range(max(case.v5_bursts, case.legacy_bursts)):
        if index < case.v5_bursts:
            body = rng.integers(0, 256, int(rng.integers(20, 1500)), dtype=np.uint8).tobytes()
            burst = encode_burst_v5(
                body, mcs_entry(v5_mcs[index % len(v5_mcs)]), scrambler_seed=1 + index % 127
            ).samples
            parts += [np.zeros(16), burst, np.zeros(64)]
            parts.append(np.zeros(int(rng.integers(*case.gap))))
        if index < case.legacy_bursts:
            payload = rng.integers(0, 256, int(rng.integers(20, 600)), dtype=np.uint8).tobytes()
            frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, index, payload)
            parts += [np.zeros(256), encode_burst(frame, wire_version=1 + index % 4).samples,
                      np.zeros(256)]
            parts.append(np.zeros(int(rng.integers(*case.gap))))
    if case.noise_only:
        parts = [np.zeros(case.noise_only)]
    stream = _warp(np.concatenate(parts).astype(np.complex128), case.sco_ppm)
    k = np.arange(stream.size)
    stream = stream * np.exp(2j * np.pi * case.cfo / 64.0 * k)
    if case.snr_db is not None:
        variance = 0.8 * 10.0 ** (-case.snr_db / 10.0)  # bursts carry ~52/64 per sample
        noise = rng.normal(scale=np.sqrt(variance / 2.0), size=(stream.size, 2))
        stream = stream + noise[:, 0] + 1j * noise[:, 1]
    stream = stream + case.dc
    if case.tone:
        stream = stream + case.tone * np.exp(2j * np.pi * case.tone_frequency * k)
    return stream.astype(np.complex64)


CASES = (
    *(Case(f"v5_{snr:+.0f}dB", snr_db=snr, seed=int(snr) + 50) for snr in
      (-6.0, -3.0, 0.0, 3.0, 6.0, 10.0, 15.0, 20.0, 25.0, 30.0)),
    Case("noiseless", snr_db=None, seed=1),
    Case("back_to_back", gap=(0, 1), v5_bursts=5, seed=2),
    Case("back_to_back_low_snr", gap=(0, 1), v5_bursts=5, snr_db=3.0, seed=3),
    Case("sco_plus_20ppm", sco_ppm=20.0, v5_bursts=4, seed=4),
    Case("sco_minus_20ppm", sco_ppm=-20.0, v5_bursts=4, seed=5),
    Case("large_cfo", cfo=1.3, seed=6),
    Case("negative_cfo", cfo=-0.9, seed=7),
    Case("dc_0.003", dc=0.003 + 0.001j, seed=8),
    Case("dc_0.03", dc=0.03 - 0.02j, seed=9),
    Case("dc_low_snr", dc=0.01, snr_db=2.0, seed=10),
    Case("tone", tone=0.05, seed=11),
    Case("tone_strong", tone=0.3, tone_frequency=-0.07, seed=12),
    Case("mixed_families", legacy_bursts=3, v5_bursts=3, seed=13),
    Case("legacy_only", legacy_bursts=3, v5_bursts=0, seed=14),
    Case("mixed_low_snr", legacy_bursts=2, v5_bursts=2, snr_db=1.0, seed=15),
    Case("noise_only", noise_only=20000, seed=16),
    Case("noise_dc", noise_only=20000, dc=0.01, seed=17),
    Case("zeros", zeros=5000),
    Case("short", v5_bursts=1, gap=(1, 2), seed=18),
)
