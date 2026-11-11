"""Pass/fail of node_app duplex soak runs (run_duplex.sh directories), by the per-minute trend.

    python judge_run.py RUN_DIR [RUN_DIR ...]

Prints one line per run and writes RUN_DIR/judge.json.  The rule (user, 2026-11-11):
a run passes when nothing is abnormal over its minutes, and occasional TX underflow or
late bursts are normal.

FAIL when any of these holds:
  * a load result (load_AB.json, load_BA.json) or a node's stats are missing: a crash or
    a hang;
  * a direction's datagram loss is above 1 %;
  * an event counter keeps rising: over the run window (from the first to the last stats
    line in which the sender's ingress count grows) the per-minute counts of the last
    three complete minutes rise strictly, the one before the last is already above the
    median of the earlier minutes, and the last exceeds twice that median by at least
    3 events;
  * the latency drifts up: the same test on the per-minute p50 (median of the 10 s
    bucket p50s), with 1.25 times the median and at least 20 ms above it.

An incomplete last minute is not counted.  A spike (one high minute) does not fail the
run but is listed under ``spikes`` (a minute above twice the median of the others and at
least 3 events or 20 ms above it).  The earlier rule, "the last third has at least as
many events as the first third", failed runs on a single late spike.

Counters: RX overflow, RX backpressure, TX underflow (and in packet), TX late
(``tx_time_error``), bursts rejected because the TX queue was full, and datagrams the
ingress dropped because the send queue was full.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import median
from typing import Any

LOSS_LIMIT = 0.01
TREND_MINUTES = 3
COUNT_FACTOR = 2.0
COUNT_MIN_EXCESS = 3
LATENCY_FACTOR = 1.25
LATENCY_MIN_EXCESS_MS = 20.0
MIN_WINDOW_LINES = 8

COUNTERS = {
    "rx_overflow": "rx.transport.uhd_faults.rx_overflow",
    "rx_backpressure": "rx.transport.queue_full_backpressure_events",
    "tx_underflow": "tx.transport.uhd_faults.tx_underflow",
    "tx_underflow_in_packet": "tx.transport.uhd_faults.tx_underflow_in_packet",
    "tx_late": "tx.transport.uhd_faults.tx_time_error",
    "tx_rejected": "tx.transport.bursts_rejected_queue_full",
    "ingress_dropped": "tx.ingress_dropped_queue_full",
}
DIRECTIONS = ("AB", "BA")  # node 1 -> node 2, node 2 -> node 1


def keeps_rising(series: list[float], *, factor: float, min_excess: float,
                 minutes: int = TREND_MINUTES) -> bool:
    """True when the last ``minutes`` values rise strictly and the last is well above the rest."""

    if len(series) < minutes + 1:
        return False
    tail = series[-minutes:]
    if any(later <= earlier for earlier, later in zip(tail, tail[1:])):
        return False
    base = median(series[:-1])
    # The minute before the last must already be above the earlier median: one high
    # last minute after a dip (18, 7, 0, 5, 43) is a spike, not a trend.
    return tail[-2] > base and tail[-1] > factor * base and tail[-1] - base >= min_excess


def spikes(series: list[float], *, factor: float, min_excess: float) -> list[int]:
    """Indices of minutes well above the median of the other minutes."""

    found = []
    for index, value in enumerate(series):
        others = series[:index] + series[index + 1:]
        if not others:
            continue
        base = median(others)
        if value > factor * base and value - base >= min_excess:
            found.append(index)
    return found


def per_minute(times: list[float], values: list[float]) -> list[float]:
    """Deltas of a cumulative counter per complete minute from ``times[0]``."""

    start, end = times[0], times[-1]
    out = []
    minute = 0
    while start + 60.0 * (minute + 1) <= end:
        low = _value_at(times, values, start + 60.0 * minute)
        high = _value_at(times, values, start + 60.0 * (minute + 1))
        out.append(high - low)
        minute += 1
    return out


def _value_at(times: list[float], values: list[float], t: float) -> float:
    """The counter at the last line at or before ``t``."""

    chosen = values[0]
    for time_s, value in zip(times, values, strict=True):
        if time_s > t:
            break
        chosen = value
    return chosen


def latency_per_minute(buckets: list[list[float]], duration_s: float | None) -> list[float]:
    """Median of the 10 s bucket p50s for each complete minute of the load."""

    complete = [b for b in buckets if duration_s is None or b[0] + 10.0 <= duration_s]
    out = []
    minute = 0
    while True:
        rows = [b[2] for b in complete if 60.0 * minute <= b[0] < 60.0 * (minute + 1)]
        if len(rows) < 6:
            break
        out.append(float(median(rows)))
        minute += 1
    return out


def _get(record: dict, path: str) -> float:
    value: Any = record
    for key in path.split("."):
        if not isinstance(value, dict):
            return 0
        value = value.get(key)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _window(records: list[dict]) -> list[dict]:
    """Stats lines from just before the first to the last growth of the ingress count."""

    lines = [r for r in records if "tx" in r and "monotonic_s" in r]
    counts = [_get(r, "tx.ingress_datagrams") for r in lines]
    grew = [i for i in range(1, len(lines)) if counts[i] > counts[i - 1]]
    if len(grew) < MIN_WINDOW_LINES:
        return []
    return lines[grew[0] - 1: grew[-1] + 1]


def judge_run(run: Path) -> dict[str, Any]:
    why: list[str] = []
    notes: list[str] = []
    result: dict[str, Any] = {"run": run.name}
    for direction in DIRECTIONS:
        path = run / f"load_{direction}.json"
        if not path.exists():
            why.append(f"{direction}: no load result (crash or hang?)")
            continue
        load = json.loads(path.read_text())
        loss = float(load.get("loss_ratio", 1.0))
        latency = latency_per_minute(load.get("latency_buckets_10s") or [], load.get("duration"))
        result[direction] = {
            "loss_ratio": loss,
            "goodput_mbps": load.get("goodput_mbps"),
            "latency_ms_p50_p95_p99": [load.get(f"latency_ms_p{q}") for q in (50, 95, 99)],
            "latency_p50_per_minute_ms": [round(x, 1) for x in latency],
        }
        if loss > LOSS_LIMIT:
            why.append(f"{direction} loss {100 * loss:.2f}% > {100 * LOSS_LIMIT:g}%")
        if keeps_rising(latency, factor=LATENCY_FACTOR, min_excess=LATENCY_MIN_EXCESS_MS):
            why.append(f"{direction} latency p50 keeps rising {[round(x) for x in latency]} ms")
        for index in spikes(latency, factor=LATENCY_FACTOR, min_excess=LATENCY_MIN_EXCESS_MS):
            notes.append(f"{direction} latency spike in minute {index + 1}: "
                         f"{[round(x) for x in latency]} ms")
    for node in ("1", "2"):
        records = _jsonl(run / f"node{node}_stats.jsonl")
        if not records:
            why.append(f"node{node}: no stats (crash?)")
            continue
        window = _window(records)
        if not window:
            notes.append(f"node{node}: too few stats lines with traffic to judge counters")
            continue
        times = [float(r["monotonic_s"]) for r in window]
        counts = {}
        for name, path in COUNTERS.items():
            series = per_minute(times, [_get(r, path) for r in window])
            counts[name] = series
            if keeps_rising(series, factor=COUNT_FACTOR, min_excess=COUNT_MIN_EXCESS):
                why.append(f"node{node} {name} keeps rising per minute {series}")
            for index in spikes(series, factor=COUNT_FACTOR, min_excess=COUNT_MIN_EXCESS):
                notes.append(f"node{node} {name} spike in minute {index + 1}: {series}")
        result[f"node{node}"] = {"window_s": round(times[-1] - times[0], 1),
                                 "per_minute": counts}
    result["verdict"] = "FAIL" if why else "PASS"
    result["why"] = why
    result["spikes"] = notes
    return result


def _describe(result: dict[str, Any]) -> str:
    parts = []
    for direction in DIRECTIONS:
        row = result.get(direction)
        if row:
            p50, p95, p99 = (round(x) if x is not None else None
                             for x in row["latency_ms_p50_p95_p99"])
            parts.append(f"{direction} {100 * row['loss_ratio']:.3f}% "
                         f"{row['goodput_mbps']:.2f} Mb/s p50/95/99 {p50}/{p95}/{p99} ms")
    reasons = "; ".join(result["why"]) or "nothing abnormal"
    return f"{result['verdict']} {result['run']} [{' | '.join(parts)}] :: {reasons}"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    failed = 0
    for argument in argv:
        run = Path(argument.rstrip("/"))
        result = judge_run(run)
        (run / "judge.json").write_text(json.dumps(result, indent=1) + "\n")
        print(_describe(result))
        for note in result["spikes"]:
            print(f"     note: {note}")
        failed += result["verdict"] == "FAIL"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
