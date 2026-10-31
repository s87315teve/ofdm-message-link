"""S5: scrambler effect on PAPR and on the power left after TX peak scaling.

    PYTHONPATH=src python experiments/tx_v5/sim_s5_scrambler.py [--bursts 200] [--mcs 8 12 17]

For each payload type (random / all-zero / all-0xFF / repeated 4-byte pattern /
repeated 13-byte text) and scrambler off (seed 0) vs on (random seed 1..127 per
burst), encodes whole v5 bursts (STF+LTF+SIG+payload, raw 943 B bodies, so
deliberately worse than a real A-MPDU with headers) and measures
  * per-sample PAPR CCDF (pooled over bursts): PAPR at P(>x)=1e-2, 1e-3
  * mean power after transport.scale_to_peak(peak 0.7): the number that sets
    the transmitted level (mean power = 0.49 / burst peak-to-mean ratio)
and the spread of that mean power over payload types, with and without.
Writes results/sim/s5_scrambler.json.
"""
import argparse

from sim_common import *  # noqa: F401,F403
from ofdm_message_link.transport import scale_to_peak

PAYLOADS = {
    "random": None,
    "zero": lambda n: bytes(n),
    "ff": lambda n: b"\xff" * n,
    "pat4": lambda n: (b"\x01\x23\x45\x67" * n)[:n],
    "text13": lambda n: (b"hello world! " * n)[:n],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bursts", type=int, default=200)
    ap.add_argument("--mcs", type=int, nargs="+", default=[8, 12, 17])
    a = ap.parse_args()
    out = {}
    for mcs in a.mcs:
        for scr in ("off", "on"):
            for name, gen in PAYLOADS.items():
                rng = np.random.default_rng(55 + mcs)
                pw, paprs, pooled = [], [], []
                n = a.bursts if (gen is None or scr == "on") else 3  # deterministic otherwise
                for _ in range(n):
                    body = rng.integers(0, 256, V5_BODY, dtype=np.uint8).tobytes() if gen is None else gen(V5_BODY)
                    seed = 0 if scr == "off" else int(rng.integers(1, 128))
                    tx = np.asarray(encode_burst_v5(body, mcs_entry(mcs), scrambler_seed=seed).samples)
                    p = np.abs(tx.astype(np.complex128)) ** 2
                    paprs.append(10 * np.log10(p.max() / p.mean()))
                    pooled.append(10 * np.log10(p / p.mean() + 1e-12))
                    s = scale_to_peak(tx, 0.7)
                    pw.append(10 * np.log10(np.mean(np.abs(s.astype(np.complex128)) ** 2)))
                allp = np.sort(np.concatenate(pooled))
                ccdf = lambda q: float(allp[int(len(allp) * (1 - q))])
                out[f"mcs{mcs}_{scr}_{name}"] = {
                    "mean_power_dB": float(np.mean(pw)), "mean_power_std_dB": float(np.std(pw)),
                    "burst_papr_dB": float(np.mean(paprs)),
                    "papr_1e-2": ccdf(1e-2), "papr_1e-3": ccdf(1e-3), "n": n,
                }
                r = out[f"mcs{mcs}_{scr}_{name}"]
                print(f"MCS{mcs} scr {scr:3s} {name:7s} meanP {r['mean_power_dB']:6.2f}+-{r['mean_power_std_dB']:.2f} dB  "
                      f"burstPAPR {r['burst_papr_dB']:5.2f}  CCDF1e-2 {r['papr_1e-2']:5.2f}  1e-3 {r['papr_1e-3']:5.2f}", flush=True)
        for scr in ("off", "on"):
            m = [out[f"mcs{mcs}_{scr}_{n}"]["mean_power_dB"] for n in PAYLOADS]
            out[f"mcs{mcs}_{scr}_spread_dB"] = float(max(m) - min(m))
            print(f"MCS{mcs} scr {scr}: spread over payload types {max(m) - min(m):.2f} dB")
    save("s5_scrambler.json", out)


main()
