"""S3: channel-estimator ablations on v5 MCS 8 and MCS 12 (943 B PSDU body), AWGN and multipath.

    PYTHONPATH=src python experiments/tx_v5/sim_s3_estimator.py [--trials 400] [--topup 1500] [--mcs 8 12]

Variants (phy.rx toggles, V5RxConfig): baseline (ltf_average, channel_smoothing, llr max);
no_avg (ltf_average off); no_smooth (channel_smoothing off); neither; llr_header; llr_ltf.
Noise streams are paired across variants.  Reports the 1% whole-burst-loss SNR per variant
and its delta vs baseline.  Writes results/sim/s3_estimator.json.
"""
import argparse

from sim_common import *  # noqa: F401,F403

VARIANTS = {
    "baseline": {},
    "no_avg": {"ltf_average": False},
    "no_smooth": {"channel_smoothing": False},
    "neither": {"ltf_average": False, "channel_smoothing": False},
    "llr_header": {"llr_noise_source": "header"},
    "llr_ltf": {"llr_noise_source": "ltf"},
}
START = {8: -2.0, 12: 5.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=400)
    ap.add_argument("--topup", type=int, default=1500)
    ap.add_argument("--mcs", type=int, nargs="+", default=[8, 12])
    a = ap.parse_args()
    out = {}
    for mcs in a.mcs:
        for mp in (False, True):
            base = None
            for name, kw in VARIANTS.items():
                rows = sweep_curve(v5_maker(mcs), rx_cfg(**kw), START[mcs], mp, a.trials, a.topup,
                                   seed=3, label=f"mcs{mcs}:{name}")
                s = snr_at([r["snr"] for r in rows], [r["loss"] for r in rows], 0.01, a.topup)
                base = s if name == "baseline" else base
                out[f"mcs{mcs}_{'mp' if mp else 'awgn'}_{name}"] = {
                    "rows": rows, "snr_1pct": s, "delta_vs_baseline": None if s is None or base is None else s - base}
                print("==", mcs, "mp" if mp else "awgn", name, s, flush=True)
                save("s3_estimator.json", out)


main()
