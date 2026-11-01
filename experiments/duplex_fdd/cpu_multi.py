"""Sample CPU ticks of several process trees (one per node) into one JSONL file.

    python cpu_multi.py OUT.jsonl INTERVAL NAME=PID [NAME=PID ...]   # runs until killed
    python cpu_multi.py --summarize OUT.jsonl            # per node: main and worker CPU %

Reuses the /proc walking of experiments/tx_v5/cpu_sampler.py.  One record per
interval: {"t": monotonic, "nodes": {NAME: {"root": PID, "ticks": {pid: ticks},
"cmd": {pid: cmdline}}}}.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tx_v5"))
import cpu_sampler  # noqa: E402


def sample(out: str, interval: float, roots: dict[str, int]) -> None:
    with open(out, "w") as handle:
        while True:
            rows = cpu_sampler.table()
            nodes = {}
            for name, root in roots.items():
                pids = cpu_sampler.descendants(root, rows)
                nodes[name] = {
                    "root": root,
                    "ticks": {str(p): rows[p][1] for p in pids if p in rows},
                    "cmd": {str(p): cpu_sampler.cmdline(p)[:80] for p in pids},
                }
            handle.write(json.dumps({"t": time.monotonic(), "nodes": nodes}) + "\n")
            handle.flush()
            time.sleep(interval)


def summarize(path: str) -> dict:
    recs = [json.loads(line) for line in open(path) if line.strip()]
    if len(recs) < 2:
        return {}
    dt = recs[-1]["t"] - recs[0]["t"]
    out: dict = {"window_s": dt}
    for name, last in recs[-1]["nodes"].items():
        first = recs[0]["nodes"].get(name, {})
        pct = {}
        for pid, end in last["ticks"].items():
            start = first.get("ticks", {}).get(pid)
            if start is not None and dt > 0:
                pct[pid] = (end - start) / cpu_sampler.TICK / dt * 100
        root = str(last["root"])
        workers = {
            p: v
            for p, v in pct.items()
            if p != root and "resource_tracker" not in last["cmd"].get(p, "")
        }
        out[name] = {
            "main_cpu_pct": pct.get(root),
            "n_workers": len(workers),
            "worker_cpu_sum_pct": sum(workers.values()),
            "total_cpu_pct": sum(pct.values()),
        }
    return out


if __name__ == "__main__":
    if sys.argv[1] == "--summarize":
        print(json.dumps(summarize(sys.argv[2])))
    else:
        roots = {a.split("=")[0]: int(a.split("=")[1]) for a in sys.argv[3:]}
        sample(sys.argv[1], float(sys.argv[2]), roots)
