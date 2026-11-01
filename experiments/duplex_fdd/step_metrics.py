"""How a link adaptation run answers each TX gain step, from the node logs.

    python experiments/duplex_fdd/step_metrics.py RUN_DIR... [--direction AB] [--json OUT]

Per gain step of the sender (``meta.json`` gain profile): subframes lost at the receiver
in the first 0.5 / 1.0 / 1.5 s; subframe loss over the rest of the segment (from 1.5 s
after the step to the next step); time from the step to the first LA decision at or
below ``--down-mcs`` (a down step) or at or above ``--up-mcs`` (an up step). Also the
whole-segment loss and, if the stats line carries the algorithm snapshot, OLLA's
``snr_drops``. Stats lines are 0.5 s apart, so the windows have that resolution.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DIRECTIONS = {"AB": (1, 2), "BA": (2, 1)}


def _stats(run: Path, node: int) -> list[dict]:
    return [
        json.loads(line)
        for line in (run / f"node{node}_stats.jsonl").open()
        if '"tx"' in line and '"elapsed_s"' in line
    ]


def _at(series: list[tuple[float, float, float]], t: float) -> tuple[float, float]:
    """(missing, decoded) of the last stats line at or before ``t``."""

    missing, decoded = series[0][1], series[0][2]
    for when, m, d in series:
        if when > t:
            break
        missing, decoded = m, d
    return missing, decoded


def _loss(series, t0: float, t1: float) -> tuple[int, float | None]:
    m0, d0 = _at(series, t0)
    m1, d1 = _at(series, t1)
    lost, ok = m1 - m0, d1 - d0
    return int(lost), (lost / (lost + ok) if lost + ok > 0 else None)


def _steps(profile: str | None) -> list[tuple[float, float]]:
    if not profile:
        return []
    return [(float(t), float(g)) for t, g in (part.split(":") for part in profile.split(","))]


def analyse(run: Path, direction: str, down_mcs: int, up_mcs: int) -> dict:
    sender, receiver = DIRECTIONS[direction]
    meta = json.loads((run / "meta.json").read_text())
    tx, rx = _stats(run, sender), _stats(run, receiver)
    # Everything on the sender's time axis (seconds from the sender's ready): the two
    # nodes became ready at different moments, and the monotonic clock is shared.
    offset = sorted(line["elapsed_s"] - line["monotonic_s"] for line in tx)[len(tx) // 2]
    series = [
        (line["monotonic_s"] + offset, line["rx"]["receiver"]["missing_bursts"],
         line["rx"]["receiver"]["bursts_decoded"])
        for line in rx
    ]
    decisions = []
    la_log = run / f"la_{sender}.jsonl"
    if la_log.exists():
        for raw in la_log.open():
            event = json.loads(raw)
            if event.get("event") == "la" and isinstance(event.get("mcs_index"), int):
                decisions.append((event["t"] + offset, event["mcs_index"]))
    steps = _steps((meta.get("gain_profile") or {}).get(str(sender)))
    end = tx[-1]["elapsed_s"]
    load_end = float((meta.get("load") or {}).get("duration_s") or end) + float(
        (meta.get("args") or {}).get("settle", 0) or 0
    )
    result = {"run": run.name, "direction": direction, "steps": []}
    for index, (t, gain) in enumerate(steps):
        if index == 0:
            continue  # the profile's first entry is the start gain, not a step
        previous = steps[index - 1][1]
        bound = steps[index + 1][0] if index + 1 < len(steps) else min(end, load_end)
        down = gain < previous
        target = (lambda m: m <= down_mcs) if down else (lambda m: m >= up_mcs)
        reach = next((when - t for when, mcs in decisions if when >= t and target(mcs)), None)
        lost_05, _ = _loss(series, t, t + 0.5)
        lost_10, _ = _loss(series, t, t + 1.0)
        lost_15, _ = _loss(series, t, t + 1.5)
        _, rest = _loss(series, t + 1.5, bound)
        whole_lost, whole = _loss(series, t, bound)
        result["steps"].append({
            "t": t, "from_db": previous, "to_db": gain, "until_s": round(bound, 1),
            "lost_first_0p5s": lost_05, "lost_first_1s": lost_10, "lost_first_1p5s": lost_15,
            "loss_after_1p5s": rest, "segment_loss": whole, "segment_lost": whole_lost,
            "time_to_target_mcs_s": None if reach is None else round(reach, 2),
            "target": f"MCS <= {down_mcs}" if down else f"MCS >= {up_mcs}",
        })
    algorithm = (tx[-1].get("la") or {}).get("algorithm") or {}
    result["snr_drops"] = algorithm.get("snr_drops")
    result["mcs_changes"] = len(decisions)
    return result


def _p(value):
    return "-" if value is None else f"{value * 100:.2f} %"


def markdown(results: list[dict]) -> str:
    lines = [
        "| run | step | lost 0.5 / 1 / 1.5 s | loss after 1.5 s | segment loss | time to target |"
        " snr_drops |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for res in results:
        for st in res["steps"]:
            reach = st["time_to_target_mcs_s"]
            lines.append(
                f"| {res['run']} | {st['from_db']:g} → {st['to_db']:g} dB at {st['t']:g} s "
                f"(until {st['until_s']:g}) | {st['lost_first_0p5s']} / {st['lost_first_1s']} / "
                f"{st['lost_first_1p5s']} | {_p(st['loss_after_1p5s'])} | "
                f"{_p(st['segment_loss'])} | "
                f"{'-' if reach is None else f'{reach:.2f} s'} ({st['target']}) | "
                f"{res['snr_drops']} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--direction", default="AB", choices=tuple(DIRECTIONS))
    parser.add_argument("--down-mcs", type=int, default=10)
    parser.add_argument("--up-mcs", type=int, default=15)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)
    results = [analyse(run, args.direction, args.down_mcs, args.up_mcs) for run in args.runs]
    print(markdown(results))
    if args.json is not None:
        args.json.write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
