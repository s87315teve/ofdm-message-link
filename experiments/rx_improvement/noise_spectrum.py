"""Where a capture's noise sits: under the data carriers or in the guard band.

Each capture from ota_capture.py starts with 0.2 s of air recorded before the
first burst.  This averages that stretch over 64-point FFTs and compares the
noise on the 52 active carriers with the noise in the guard band.  A
time-domain noise estimate counts both; decoding sees only the first.

    PYTHONPATH=src python experiments/rx_improvement/noise_spectrum.py caps/*.npy
"""

from __future__ import annotations

import sys

import numpy as np

from ofdm_link.phy.ofdm import OfdmNumerology

RX_FULL_SCALE = 2048.0
LEAD_IN_SAMPLES = 800_000  # 0.16 s at 5 MS/s, inside the 0.2 s before the first burst


def main() -> None:
    numerology = OfdmNumerology()
    active = np.asarray(numerology.active_carriers) % numerology.fft_size
    guard = np.setdiff1d(np.arange(1, numerology.fft_size), active)
    for path in sys.argv[1:]:
        raw = np.load(path)[: 2 * LEAD_IN_SAMPLES].astype(np.float32) / RX_FULL_SCALE
        samples = raw.view(np.complex64)
        blocks = samples[: samples.size // 64 * 64].reshape(-1, 64)
        power = np.mean(np.abs(np.fft.fft(blocks, axis=1, norm="ortho")) ** 2, axis=0)
        in_band, out_of_band, full = power[active].mean(), power[guard].mean(), power.mean()
        print(
            f"{path}: guard / data carriers {10 * np.log10(out_of_band / in_band):+.1f} dB, "
            f"whole band / data carriers {10 * np.log10(full / in_band):+.2f} dB"
        )


if __name__ == "__main__":
    main()
