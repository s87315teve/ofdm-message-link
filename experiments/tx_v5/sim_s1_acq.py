"""S1: acquisition P(detect) vs SNR (legacy preamble/legacy detector vs v5 STF+LTF/v5 detector)
and false acquisitions per 1e7 samples of white noise.

    PYTHONPATH=src python experiments/tx_v5/sim_s1_acq.py [--trials 200] [--noise-samples 30000000]

Hit = the family's own detector (sync.acquire_all with only that family enabled, default
thresholds) reports a start within +-3 samples of the true one.  Also timing error and CFO
error of the hits (10 kHz CFO).  Writes results/sim/s1_acq.json.
"""
import argparse

from ofdm_link.phy.sync import SyncConfig, acquire_all
from ofdm_link.phy.sync_v5 import V5SyncConfig
from sim_common import *  # noqa: F401,F403

START = 600


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--noise-samples", type=int, default=30_000_000)
    a = ap.parse_args()
    LEG, V5 = SyncConfig(), V5SyncConfig()
    rng0 = np.random.default_rng(1)
    _, v5tx = v5_burst(rng0, 8)
    _, legtx = legacy_burst(rng0)
    v5tx, legtx = v5tx[:2000], legtx[:2000]  # preamble + start of the header only
    out = {"detect": {}, "false": {}}
    for mp in (False, True):
        for fam, tx, args in (("legacy", legtx, (LEG, None)), ("v5", v5tx, (None, V5))):
            rows = []
            for snr in range(-6, 5):
                rng = np.random.default_rng(1000 + snr + 100 * mp)
                hit, terr, cerr = 0, [], []
                for _ in range(a.trials):
                    rx = channel(tx, snr, rng, mp)
                    found = [x for x in acquire_all(rx, *args) if abs(x.preamble_start - START) <= 3]
                    if found:
                        hit += 1
                        terr.append(found[0].preamble_start - START)
                        cerr.append(found[0].normalized_cfo * FS / 64 - CFO_HZ)
                rows.append({"snr": snr, "p": hit / a.trials,
                             "timing_mean": float(np.mean(terr)) if terr else None,
                             "cfo_rms_hz": float(np.sqrt(np.mean(np.square(cerr)))) if cerr else None})
                print(fam, "mp" if mp else "awgn", rows[-1], flush=True)
            out["detect"][f"{fam}_{'mp' if mp else 'awgn'}"] = rows
    chunk = 65536
    for fam, args in (("legacy", (LEG, None)), ("v5", (None, V5))):
        rng = np.random.default_rng(5)
        n = found = 0
        while n < a.noise_samples:
            x = ((rng.standard_normal(chunk) + 1j * rng.standard_normal(chunk)) * np.sqrt(0.5)).astype(np.complex64)
            found += len(acquire_all(x, *args))
            n += chunk
        out["false"][fam] = {"samples": n, "found": found, "per_1e7": found / n * 1e7}
        print(fam, out["false"][fam], flush=True)
    save("s1_acq.json", out)


main()
