"""Summarize live_run.sh results: loss, overflow while loaded, and the two SNRs.

    python experiments/rx_improvement/summarize_live.py <dir-of-run-dirs> live.json

Overflow is counted from the first decoded burst to the end, so the start-up
transient every run shows while the decode pool spawns is left out.  The SNR
columns are the 2-second values the window displayed, sampled every 0.5 s
while bursts were arriving: their spread is what an operator sees jump.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np


def summarize(run: Path) -> dict:
    load = json.loads((run / "load.json").read_text())
    records = [json.loads(line) for line in (run / "rx_stats.jsonl").read_text().splitlines()]
    active = [r for r in records if r["receiver"]["bursts_decoded"] > 0]

    def overflow(record):
        return (record.get("transport") or {}).get("rx_overflow_events", 0) or 0

    def series(key):
        values = [r.get(key) for r in active]
        return np.array([v for v in values if v is not None], dtype=float)

    result = {
        "run": run.name,
        "sent": load["sent"],
        "delivered": load["delivered"],
        "loss": load["loss_ratio"],
        "corrupt": load["corrupt"],
        "overflow_while_loaded": (overflow(active[-1]) - overflow(active[0])) if active else None,
    }
    for key in ("snr_2s", "evm_snr_2s"):
        values = series(key)
        if values.size:
            result[f"{key}_mean"] = float(values.mean())
            result[f"{key}_std"] = float(values.std())
    return result


def main() -> None:
    runs = sorted(path for path in Path(sys.argv[1]).iterdir() if (path / "load.json").exists())
    results = [summarize(run) for run in runs]
    for result in results:
        print(json.dumps(result))
    with open(sys.argv[2], "w") as handle:
        json.dump(results, handle, indent=1)


if __name__ == "__main__":
    main()
