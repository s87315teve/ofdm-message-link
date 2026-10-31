"""Bias and spread of the preamble SNR against the true SNR, 0-20 dB.

Both MCS, AWGN and the multipath channel of sim_sweep.py, 200 bursts per point
with random payloads.  Only bursts acquisition finds are measured, exactly as
on the air.  The true SNR is the burst's mean sample power over the noise
variance.

    PYTHONPATH=src python experiments/rx_improvement/snr_check.py
"""

from __future__ import annotations

import numpy as np

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    TURBO_BURST_HEADER_VERSION,
    Frame,
    FrameKind,
    encode_burst,
)
from ofdm_link.phy.ofdm import OfdmNumerology
from ofdm_link.phy.streaming import link_snr_db
from ofdm_link.phy.sync import acquire_preamble, measure_preamble

NUMEROLOGY = OfdmNumerology()
TAPS = np.array([0.82 + 0.13j, 0, 0.21 - 0.17j, 0, 0.08j])
TAPS = TAPS / np.linalg.norm(TAPS)
TRIALS = 200


def received(tx, snr_db, rng, multipath):
    x = np.concatenate((np.zeros(600), tx, np.zeros(600))).astype(np.complex128)
    if multipath:
        x = np.convolve(x, TAPS)[: x.size]
    x *= np.exp(2j * np.pi * 10e3 / 5e6 * np.arange(x.size))
    variance = np.mean(np.abs(tx) ** 2) / 10 ** (snr_db / 10)
    noise = rng.standard_normal(x.size) + 1j * rng.standard_normal(x.size)
    return (x + np.sqrt(variance / 2) * noise).astype(np.complex64)


def main() -> None:
    payload = np.random.default_rng(3).integers(0, 256, 900, dtype=np.uint8).tobytes()
    worst = 0.0
    for mcs in (MCS.QPSK, MCS.QAM16):
        frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, mcs, 1, payload)
        tx = encode_burst(frame, wire_version=TURBO_BURST_HEADER_VERSION).samples
        for multipath in (False, True):
            for snr in (0, 1, 2, 3, 5, 10, 15, 20):
                rng = np.random.default_rng(snr + (50 if multipath else 0))
                readings = []
                for _ in range(TRIALS):
                    rx = received(tx, snr, rng, multipath)
                    acquisition = acquire_preamble(rx)
                    if acquisition is not None:
                        measurement = measure_preamble(
                            rx, acquisition, NUMEROLOGY.active_carriers
                        )
                        readings.append(link_snr_db(measurement, NUMEROLOGY))
                bias = float(np.mean(readings)) - snr
                worst = max(worst, abs(bias))
                print(
                    f"{mcs.name:5s} {'mp' if multipath else 'awgn':4s} true {snr:2d} dB: "
                    f"acquired {len(readings) / TRIALS:4.0%}  est mean {bias:+.2f} dB  "
                    f"(std {np.std(readings):.2f})"
                )
    print(f"max |bias| = {worst:.2f} dB")


if __name__ == "__main__":
    main()
