"""Sample CPU of an rx_app process and its descendants from /proc (tick deltas).

    python cpu_sampler.py RX_PID OUT.jsonl [INTERVAL]   # runs until killed
    python cpu_sampler.py --summarize OUT.jsonl RX_PID
"""
from __future__ import annotations

import json
import os
import sys
import time

TICK = os.sysconf("SC_CLK_TCK")


def table():
    rows = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            raw = open(f"/proc/{name}/stat").read()
            tail = raw[raw.rindex(")") + 2 :].split()
            rows[int(name)] = (int(tail[1]), int(tail[11]) + int(tail[12]))
        except (OSError, ValueError):
            pass
    return rows


def descendants(root, rows):
    found, frontier = {root}, [root]
    while frontier:
        parent = frontier.pop()
        for pid, (ppid, _) in rows.items():
            if ppid == parent and pid not in found:
                found.add(pid)
                frontier.append(pid)
    return found


def cmdline(pid):
    try:
        return open(f"/proc/{pid}/cmdline").read().replace("\0", " ")
    except OSError:
        return ""


def sample(root, out, interval):
    with open(out, "w") as handle:
        while True:
            rows = table()
            pids = descendants(root, rows)
            record = {"t": time.monotonic(), "ticks": {str(p): rows[p][1] for p in pids if p in rows},
                      "cmd": {str(p): cmdline(p)[:80] for p in pids}}
            handle.write(json.dumps(record) + "\n")
            handle.flush()
            time.sleep(interval)


def summarize(path, root):
    recs = [json.loads(line) for line in open(path)]
    if len(recs) < 2:
        return {}
    dt = recs[-1]["t"] - recs[0]["t"]
    pct = {}
    for pid, end in recs[-1]["ticks"].items():
        start = recs[0]["ticks"].get(pid)
        if start is not None:
            pct[pid] = (end - start) / TICK / dt * 100
    workers = {p: v for p, v in pct.items() if p != str(root) and "resource_tracker" not in recs[-1]["cmd"].get(p, "")}
    return {
        "window_s": dt,
        "rx_main_cpu_pct": pct.get(str(root)),
        "n_workers": len(workers),
        "worker_cpu_mean_pct": sum(workers.values()) / len(workers) if workers else None,
        "worker_cpu_sum_pct": sum(workers.values()),
    }


if __name__ == "__main__":
    if sys.argv[1] == "--summarize":
        print(json.dumps(summarize(sys.argv[2], int(sys.argv[3]))))
    else:
        sample(int(sys.argv[1]), sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 1.0)
