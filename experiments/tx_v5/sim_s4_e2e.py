"""S4: end-to-end whole-burst loss vs SNR, legacy MCS 0/4 and the v5 ladder 8-17 (900 B datagram).

    PYTHONPATH=src python experiments/tx_v5/sim_s4_e2e.py [--trials 300] [--topup 1000] [--only legacy0 v5_8 ...]

Burst = full decode_burst through acquisition, SIG/header and payload (default receiver),
AWGN and 5-tap multipath, 10 kHz CFO.  Legacy bursts carry a 900 B payload (wire v2 Turbo
1/3, QPSK = MCS 0, 16QAM = MCS 4); v5 bursts a 943 B PSDU body.  Loss is split by failing stage
(acq/hdr/pay).  Points with 0 < loss <= 5% are topped up to --topup trials.  1% SNR by
log-interpolation.  Writes results/sim/s4_e2e.json (merged with any previous run).
"""
import argparse
import json

from sim_common import *  # noqa: F401,F403
from ofdm_link.phy import v5_entries

START = {"legacy0": -3.0, "legacy4": 2.0, 8: -3.0, 9: -0.5, 10: 1.5, 11: 2.0, 12: 5.0, 13: 7.5,
         14: 8.5, 15: 10.0, 16: 13.0, 17: 14.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=300)
    ap.add_argument("--topup", type=int, default=1000)
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    path = RESULTS / "s4_e2e.json"
    out = json.load(open(path)) if path.exists() else {}
    for key in START:
        name = key if isinstance(key, str) else f"v5_{key}"
        if a.only and name not in a.only:
            continue
        make = legacy_maker(MCS.QPSK) if key == "legacy0" else legacy_maker(MCS.QAM16) if key == "legacy4" else v5_maker(key)
        for mp in (False, True):
            rows = sweep_curve(make, None, START[key], mp, a.trials, a.topup, seed=4, label=name)
            res = {"rows": rows, "snr_1pct": snr_at([r["snr"] for r in rows], [r["loss"] for r in rows], 0.01, a.topup)}
            out[f"{name}_{'mp' if mp else 'awgn'}"] = res
            print("==", name, "mp" if mp else "awgn", "1% SNR", res["snr_1pct"], flush=True)
            save("s4_e2e.json", out)


main()
