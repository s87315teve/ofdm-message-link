"""Per-segment and per-gain-step tables from summarize_duplex.py's summary.json files.

    python experiments/duplex_fdd/segment_table.py RUN_DIR... [--direction AB]

One row per run and segment (bounded by the gain-profile steps): goodput, datagram and
subframe loss, MCS median/final and changes, receiver SNR, OLLA offset; then one row
per gain step: convergence time and frames lost in the first second.  Run
summarize_duplex.py on the directories first.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _f(value, spec=".2f"):
    return "-" if value is None else format(value, spec)


def _p(value):
    return "-" if value is None else f"{value * 100:.2f} %"


def tables(runs: list[Path], direction: str) -> str:
    seg = [
        "| run | segment s | TX dB | goodput Mb/s | dgram loss | subframe loss | MCS med / final "
        "| MCS changes | SNR dB | offset dB |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    steps = [
        "| run | step | at s | convergence s | frames lost in first 1 s |",
        "|---|---|---:|---:|---:|",
    ]
    totals = [
        "| run | sent | delivered | loss | goodput Mb/s | latency p50 / p99 ms | MCS changes |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for run in runs:
        summary = json.loads((run / "summary.json").read_text())
        res = summary["directions"].get(direction)
        if res is None:
            continue
        for s in res["segments"]:
            seg.append(
                f"| {run.name} | {s['t0']:.0f}–{s['t1']:.0f} | {_f(s.get('tx_gain_db'), 'g')} | "
                f"{_f(s['goodput_mbps'], '.3f')} | {_p(s['datagram_loss'])} | "
                f"{_p(s['subframe_loss'])} | {_f(s['mcs_median'], 'g')} / {s['mcs_final']} | "
                f"{s['mcs_changes']} | {_f(s['snr_mean_db'], '.1f')} | {_f(s['offset_db'])} |"
            )
        for st in res["steps"]:
            steps.append(
                f"| {run.name} | {st.get('from_db')} → {st.get('to_db')} dB | {st['t']:.0f} | "
                f"{_f(st.get('convergence_s'), '.1f')} | {_f(st.get('missing_first_1s'), 'g')} |"
            )
        load = res.get("load") or {}
        overall = res["overall"]
        totals.append(
            f"| {run.name} | {load.get('sent')} | {load.get('delivered')} | "
            f"{_p(load.get('loss_ratio'))} | {_f(load.get('steady_goodput_mbps'), '.3f')} | "
            f"{_f(load.get('latency_ms_p50'), '.0f')} / {_f(load.get('latency_ms_p99'), '.0f')} | "
            f"{overall['mcs_changes']} |"
        )
    return "\n".join(totals + [""] + seg + [""] + steps) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--direction", default="AB", choices=("AB", "BA"))
    args = parser.parse_args(argv)
    print(tables(args.runs, args.direction))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
