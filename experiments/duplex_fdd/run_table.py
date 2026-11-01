"""One markdown row per direction for a set of run_duplex.sh directories.

    PYTHONPATH=src python experiments/duplex_fdd/run_table.py RUN_DIR... [--json OUT]

Per direction (AB = node 1 -> node 2): offered rate, delivered/sent and loss from
udp_load's byte-exact check, steady goodput, latency p50/p99, the sender's MCS (the
most used ``tx.active_mcs`` while data flowed) and peak 2-s airtime, the receiver's
median 2-s preamble SNR and RX overflows after the first 3 s, and the CPU of both
engines and their decode workers (cores, from the stats lines).  A direction with no
load (rate 0) is skipped.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

DIRECTIONS = {"AB": (1, 2), "BA": (2, 1)}
STARTUP_S = 3.0


def _lines(run: Path, node: int) -> list[dict]:
    path = run / f"node{node}_stats.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open() if '"tx"' in line]


def _cpu(lines: list[dict]) -> tuple[float | None, float | None]:
    after = [line for line in lines if line["elapsed_s"] >= STARTUP_S]
    if len(after) < 2:
        return None, None
    first, last = after[0], after[-1]
    span = last["elapsed_s"] - first["elapsed_s"]
    engine = (last["cpu"]["engine_cpu_s"] - first["cpu"]["engine_cpu_s"]) / span
    workers = (
        (last["cpu"].get("decode_worker_cpu_s") or 0)
        - (first["cpu"].get("decode_worker_cpu_s") or 0)
    ) / span
    return round(engine, 2), round(workers, 2)


def _overflows(lines: list[dict]) -> int | None:
    after = [line for line in lines if line["elapsed_s"] >= STARTUP_S]
    if not after:
        return None
    key = "rx_overflow_events"
    return int(after[-1]["rx"]["transport"][key] - after[0]["rx"]["transport"][key])


def direction_row(run: Path, direction: str) -> dict | None:
    load_path = run / f"load_{direction}.json"
    if not load_path.exists():
        return None
    load = json.loads(load_path.read_text())
    if not load.get("sent"):
        return None
    sender, receiver = DIRECTIONS[direction]
    tx, rx = _lines(run, sender), _lines(run, receiver)
    busy = [line for line in tx if line["tx"]["airtime_frac"] > 0.05]
    mcs = Counter(line["tx"]["active_mcs"] for line in busy).most_common(1)
    snrs = sorted(line["rx"]["snr_2s"] for line in rx if line["rx"]["snr_2s"] is not None)
    meta = json.loads((run / "meta.json").read_text()) if (run / "meta.json").exists() else {}
    gains = meta.get("tx_gain_db") or {}
    return {
        "run": run.name,
        "direction": direction,
        "tx_gain_db": gains.get(str(sender)),
        "rate": load.get("rate_offered"),
        "sent": load.get("sent"),
        "delivered": load.get("delivered"),
        "loss": load.get("loss_ratio"),
        "corrupt": load.get("corrupt"),
        "goodput_mbps": load.get("steady_goodput_mbps"),
        "latency_p50_ms": load.get("latency_ms_p50"),
        "latency_p99_ms": load.get("latency_ms_p99"),
        "mcs": mcs[0][0] if mcs else None,
        "airtime_peak": max((line["tx"]["airtime_frac"] for line in tx), default=None),
        "snr_median_db": snrs[len(snrs) // 2] if snrs else None,
        "rx_overflows": _overflows(rx),
        "cpu_engine_tx_rx": [_cpu(tx)[0], _cpu(rx)[0]],
        "cpu_workers_rx": _cpu(rx)[1],
        "decode_workers": meta.get("decode_workers"),
        "loadavg_at_start": meta.get("loadavg_at_start"),
    }


def _f(value, spec=".2f"):
    return "-" if value is None else format(value, spec)


def _percent(value):
    return None if value is None else value * 100


def markdown(rows: list[dict]) -> str:
    head = (
        "| run | dir | TX dB | rate/s | delivered/sent | loss | goodput Mb/s | lat p50/p99 ms "
        "| MCS | airtime | SNR dB | RX ovf | engine CPU tx/rx | workers rx |\n"
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    body = [
        f"| {r['run']} | {r['direction']} | {_f(r['tx_gain_db'], 'g')} | {_f(r['rate'], 'g')} | "
        f"{r['delivered']}/{r['sent']} | {_f(_percent(r['loss']))} % | "
        f"{_f(r['goodput_mbps'], '.3f')} | {_f(r['latency_p50_ms'], '.0f')}/"
        f"{_f(r['latency_p99_ms'], '.0f')} | {r['mcs']} | {_f(r['airtime_peak'])} | "
        f"{_f(r['snr_median_db'], '.1f')} | {r['rx_overflows']} | "
        f"{_f(r['cpu_engine_tx_rx'][0])}/{_f(r['cpu_engine_tx_rx'][1])} | "
        f"{_f(r['cpu_workers_rx'])} |"
        for r in rows
    ]
    return "\n".join([head, *body]) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)
    rows = [
        row
        for run in args.runs
        for direction in DIRECTIONS
        if (row := direction_row(run, direction)) is not None
    ]
    print(markdown(rows))
    if args.json is not None:
        args.json.write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
