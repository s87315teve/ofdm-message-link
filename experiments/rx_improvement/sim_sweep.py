"""Simulated burst success versus SNR for whichever receiver is on PYTHONPATH.

Run it once against this tree and once against a v1.0.0 checkout with the same
arguments; the channel, the frames and the noise are seeded identically, so the
two runs see the same samples.

    PYTHONPATH=src python experiments/rx_improvement/sim_sweep.py 200 new.json
    PYTHONPATH=<v1.0.0>/src python experiments/rx_improvement/sim_sweep.py 200 old.json

The SNR is the burst's mean sample power over the complex noise variance, the
same definition the udp transport's ``--snr-db`` uses.  When the receiver
exposes a preamble SNR estimate, its per-burst mean and spread are recorded too.
"""

from __future__ import annotations

import json
import sys

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
    sync,
)
from ofdm_link.phy.ofdm import OfdmNumerology
from ofdm_link.phy.sync import acquire_preamble

TAPS = np.array([0.82 + 0.13j, 0, 0.21 - 0.17j, 0, 0.08j])
TAPS = TAPS / np.linalg.norm(TAPS)
SAMPLE_RATE = 5e6
CFO_HZ = 10e3
NUMEROLOGY = OfdmNumerology()


def preamble_snr_db(rx, acquisition):
    """The receiver's own SNR reading, or None for a v1.0.0 receiver."""

    measure = getattr(sync, "measure_preamble", None)
    if measure is None or acquisition is None:
        return None
    from ofdm_link.phy.streaming import link_snr_db

    return link_snr_db(measure(rx, acquisition, NUMEROLOGY.active_carriers), NUMEROLOGY)


def frame(seq: int, mcs: MCS) -> Frame:
    rng = np.random.default_rng(seq)
    payload = rng.integers(0, 256, 900, dtype=np.uint8).tobytes()
    return Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, mcs, seq & 0xFFFF, payload)


def channel(tx, snr_db, rng, multipath):
    x = np.concatenate((np.zeros(600), tx, np.zeros(600))).astype(np.complex128)
    if multipath:
        x = np.convolve(x, TAPS)[: x.size]
    x *= np.exp(2j * np.pi * CFO_HZ / SAMPLE_RATE * np.arange(x.size))
    power = np.mean(np.abs(tx) ** 2)
    variance = power / 10 ** (snr_db / 10)
    noise = rng.standard_normal(x.size) + 1j * rng.standard_normal(x.size)
    return (x + np.sqrt(variance / 2) * noise).astype(np.complex64)


def stage(error: BurstDecodeError) -> str:
    text = str(error)
    if "preamble" in text or "acquisition" in text:
        return "acq"
    if "header" in text:
        return "header"
    return "payload"


def main() -> None:
    trials = int(sys.argv[1])
    out: dict[str, list[dict]] = {}
    for mcs, snrs in ((MCS.QPSK, range(-2, 13)), (MCS.QAM16, range(3, 19))):
        for multipath in (False, True):
            rows = []
            for snr in snrs:
                rng = np.random.default_rng(9000 + 31 * snr + (7 if multipath else 0))
                counts = {"ok": 0, "acq": 0, "header": 0, "payload": 0}
                estimates = []
                for trial in range(trials):
                    f = frame(trial, mcs)
                    tx = encode_burst(f, wire_version=TURBO_BURST_HEADER_VERSION).samples
                    rx = channel(tx, snr, rng, multipath)
                    reading = preamble_snr_db(rx, acquire_preamble(rx))
                    if reading is not None:
                        estimates.append(reading)
                    try:
                        decoded = decode_burst(rx)
                    except BurstDecodeError as error:
                        counts[stage(error)] += 1
                        continue
                    counts["ok" if decoded.frame == f else "payload"] += 1
                row = {"snr": snr, **{k: v / trials for k, v in counts.items()}}
                if estimates:
                    row["est_mean"] = float(np.mean(estimates))
                    row["est_std"] = float(np.std(estimates))
                rows.append(row)
                print(mcs.name, "mp" if multipath else "awgn", row, flush=True)
            out[f"{mcs.name}_{'multipath' if multipath else 'awgn'}"] = rows
    with open(sys.argv[2], "w") as handle:
        json.dump(out, handle, indent=1)


if __name__ == "__main__":
    main()
