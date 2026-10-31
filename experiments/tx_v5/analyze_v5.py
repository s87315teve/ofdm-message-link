"""Summarize the v5 hardware phase-1 results (results/v5/*.json) into tables.

    python experiments/tx_v5/analyze_v5.py > experiments/tx_v5/results/v5/summary.md
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

R = Path(__file__).parent / "results"
V5 = R / "v5"


def load(name):
    p = V5 / name
    return json.loads(p.read_text()) if p.exists() else None


def pct(x):
    return f"{100 * x:.2f}%"


def f1(x):
    return "-" if x is None else f"{x:.1f}"


def crossing(rows):
    """Lowest gain such that it and every higher gain have loss < 1%."""
    rows = sorted(rows, key=lambda r: r["tx_gain_db"])
    best = None
    for r in reversed(rows):
        if r["loss"] < 0.01:
            best = r
        else:
            break
    return best, rows


def sweep():
    data = load("ota_v5.json")
    if not data:
        return {}
    by = defaultdict(list)
    for r in data:
        by[r["mcs_index"]].append(r)
    out = {}
    print("## B. v5 sweep (400 bursts/point)\n")
    for m in sorted(by):
        best, rows = crossing(by[m])
        print(f"### MCS {m}\n")
        print("| TX dB | decoded | loss | preamble miss | SIG fail | payload fail | pre SNR med (IQR, n) | EVM SNR |")
        print("|---:|---:|---:|---:|---:|---:|---|---:|")
        for r in rows:
            print(f"| {r['tx_gain_db']:g} | {r['decoded']} | {pct(r['loss'])} | {r['preamble_missed']} | "
                  f"{r['sig_failed']} | {r['payload_failed']} | {f1(r['preamble_snr_db_median'])} "
                  f"({f1(r['preamble_snr_db_iqr'])}, {r['preamble_snr_count']}) | {f1(r['evm_snr_db_median'])} |")
        print()
        if best:
            gui = math.ceil((best["preamble_snr_db_median"] + 1.0) * 2) / 2
            exact = [r["tx_gain_db"] for r in rows if abs(r["loss"] - 0.01) < 1e-9]
            out[m] = dict(lowest_pass_gain=best["tx_gain_db"], loss=best["loss"],
                          snr=best["preamble_snr_db_median"], gui_threshold=gui, exactly_1pct=exact)
    print("### Summary\n")
    print("| MCS | lowest TX gain, loss < 1% | loss there | median pre SNR there | GUI threshold (+1 dB, ceil 0.5) | points at exactly 1% |")
    print("|---:|---:|---:|---:|---:|---|")
    for m, s in out.items():
        print(f"| {m} | {s['lowest_pass_gain']:g} | {pct(s['loss'])} | {s['snr']:.1f} | {s['gui_threshold']:.1f} | {s['exactly_1pct']} |")
    print()
    return out


def legacy():
    base = {r["file"]: r for r in json.loads((R / "baseline" / "ota_baseline.json").read_text())}
    for name, label in (("legacy_on_new_rx.json", "both families"),
                        ("legacy_on_new_rx_v5acq_off.json", "v5_acquisition=false")):
        data = load(name)
        if not data:
            continue
        print(f"## C. Legacy captures on the new RX ({label})\n")
        print("| file | baseline loss | new loss | diff | base pre SNR | new pre SNR | stage split (pre/SIG/pay) |")
        print("|---|---:|---:|---:|---:|---:|---|")
        flagged = []
        for r in sorted(data, key=lambda r: (r["mcs"], r["tx_gain_db"])):
            b = base.get(r["file"])
            if not b:
                continue
            d = r["loss"] - b["loss"]
            if abs(d) > 0.01:
                flagged.append(r["file"])
            print(f"| {r['file']} | {pct(b['loss'])} | {pct(r['loss'])} | {100 * d:+.2f} | "
                  f"{f1(b.get('preamble_snr_db_median'))} | {f1(r['preamble_snr_db_median'])} | "
                  f"{r['preamble_missed']}/{r['sig_failed']}/{r['payload_failed']} |")
        print(f"\nPoints differing by > 1% absolute: {flagged or 'none'}\n")


def simple(name, title, keys):
    data = load(name)
    if not data:
        return
    print(f"## {title}\n")
    print("| case | sent | decoded | loss | 95% CI | pre/SIG/pay | pre SNR | EVM SNR | burst power dBFS | wideband SNR |")
    print("|---|---:|---:|---:|---|---|---:|---:|---:|---:|")
    for r in data:
        case = " ".join(str(r.get(k)) for k in keys)
        lo, hi = r["loss_ci95"]
        print(f"| {case} | {r['sent']} | {r['decoded']} | {pct(r['loss'])} | {pct(lo)}-{pct(hi)} | "
              f"{r['preamble_missed']}/{r['sig_failed']}/{r['payload_failed']} | {f1(r['preamble_snr_db_median'])} | "
              f"{f1(r['evm_snr_db_median'])} | {f1(r['decoded_burst_power_dbfs_median'])} | "
              f"{f1(r['decoded_burst_wideband_snr_db'])} |")
    print()


def main():
    sweep()
    legacy()
    simple("guards_low_snr.json", "D. Guards at the MCS 8 edge", ("stem",))
    simple("scrambler_ota.json", "E. Scrambler", ("stem",))
    simple("rx_ablation.json", "F. RX ablations", ("stem", "sets"))


if __name__ == "__main__":
    main()
