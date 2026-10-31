"""Mean transmitted power after the Pluto sink's peak scaling, legacy vs v5 bursts.

Run once in each tree (cwd = tree root, PYTHONPATH=src):
    python <this> legacy  > legacy.json     # improve-rx tree: MessageTransmitter(--mcs qpsk/qam16)
    python <this> v5 8    > v5.json         # current tree: v5 MCS index
Prints, over 200 random 900-byte bursts (seeds 0..199, as the captures used):
peak-to-mean ratio, mean power of the whole burst (guards excluded) after
scale_to_peak(0.7), and the mean power of the preamble part used by the SNR
estimator (legacy: the 80-sample chirp preamble; v5: the 128-sample LTF pair).
"""

from __future__ import annotations

import json
import sys

import numpy as np

from ofdm_link.phy import MCS
from ofdm_message_link.link import MessageTransmitter, build_phy_profile, load_link_config
from ofdm_message_link.options import DEFAULT_CONFIG, DEFAULT_OVERLAY

PEAK = 0.7


def main() -> None:
    family = sys.argv[1]
    config = load_link_config(DEFAULT_CONFIG, [DEFAULT_OVERLAY])
    rows = []
    if family == "legacy":
        mcs = MCS[(sys.argv[2] if len(sys.argv) > 2 else "qpsk").upper()]
        profile = build_phy_profile(config, mcs=mcs)
        tx = MessageTransmitter(profile)
        pre_g, post_g = 256, 256
        pre_slice = slice(0, 80)  # legacy chirp preamble (CP 16 + 2 x 32)
    else:
        from ofdm_link.phy.mcs_table import mcs_entry

        entry = mcs_entry(int(sys.argv[2]))
        profile = build_phy_profile(config, mcs_entry=entry)
        tx = MessageTransmitter(profile, mcs_entry=entry)
        pre_g, post_g = profile.guard_pre_samples, profile.guard_post_samples
        pre_slice = slice(192, 320)  # L1 + L2 of the LTF
    for seed in range(200):
        body = np.random.default_rng(seed).integers(0, 256, 900, dtype=np.uint8).tobytes()
        (burst,) = tx.encode_message(body)
        x = np.asarray(burst.samples, dtype=np.complex128)
        core = x[pre_g : x.size - post_g]
        p = np.abs(core) ** 2
        scale = PEAK**2 / p.max()
        rows.append(
            (10 * np.log10(p.max() / p.mean()), 10 * np.log10(scale * p.mean()),
             10 * np.log10(scale * p[pre_slice].mean()), 10 * np.log10(p[pre_slice].mean() / p.mean()))
        )
    a = np.array(rows)
    print(json.dumps({
        "family": family,
        "arg": sys.argv[2] if len(sys.argv) > 2 else None,
        "burst_samples_without_guards": int(core.size),
        "papr_db_median": float(np.median(a[:, 0])),
        "papr_db_p10_p90": [float(np.percentile(a[:, 0], 10)), float(np.percentile(a[:, 0], 90))],
        "mean_power_after_scaling_dbfs_median": float(np.median(a[:, 1])),
        "preamble_power_after_scaling_dbfs_median": float(np.median(a[:, 2])),
        "preamble_to_burst_power_db_median": float(np.median(a[:, 3])),
    }))


if __name__ == "__main__":
    main()
