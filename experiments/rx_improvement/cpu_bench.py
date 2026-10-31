"""Per-burst decode time of whichever receiver is on PYTHONPATH, at three SNRs.

One 900-byte MCS 0 burst, 30 noisy copies per SNR, decoded one after another
on one core.  Run it once per receiver on an otherwise idle machine:

    PYTHONPATH=src python experiments/rx_improvement/cpu_bench.py
    PYTHONPATH=<v1.0.0>/src python experiments/rx_improvement/cpu_bench.py
"""

from __future__ import annotations

import time

import numpy as np

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MCS,
    TURBO_BURST_HEADER_VERSION,
    BurstDecodeError,
    Frame,
    FrameKind,
    decode_burst,
    encode_burst,
)

COPIES = 30


def main() -> None:
    payload = np.random.default_rng(1).integers(0, 256, 900, dtype=np.uint8).tobytes()
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, 1, payload)
    tx = encode_burst(frame, wire_version=TURBO_BURST_HEADER_VERSION).samples
    padded = np.concatenate((np.zeros(300), tx, np.zeros(300)))
    rng = np.random.default_rng(0)
    for snr in (20, 6, 3):
        variance = np.mean(np.abs(tx) ** 2) / 10 ** (snr / 10)
        copies = [
            (
                padded
                + np.sqrt(variance / 2)
                * (rng.standard_normal(padded.size) + 1j * rng.standard_normal(padded.size))
            ).astype(np.complex64)
            for _ in range(COPIES)
        ]
        delivered = 0
        started = time.perf_counter()
        for samples in copies:
            try:
                delivered += decode_burst(samples).frame == frame
            except BurstDecodeError:
                pass
        per_burst = (time.perf_counter() - started) / COPIES
        print(f"snr {snr:2d} dB: {per_burst * 1e3:6.2f} ms/burst, delivered {delivered}/{COPIES}")


if __name__ == "__main__":
    main()
