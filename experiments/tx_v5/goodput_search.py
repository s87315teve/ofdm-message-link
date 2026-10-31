"""Find the max datagram rate with loss < 0.1% and zero RX overflow while loaded.

    PYTHONPATH=<anything> python goodput_search.py --root ROOT --tx-gain G --mcs M \
        [--size 900] [--seconds 30] --out DIR

Runs live_run.sh (real rx_app/tx_app from ROOT, transmits RF) repeatedly: a low
probe, then x1.5 bracketing, then bisection to ~5% resolution.  Goodput is
rate * size * 8 (UDP payload bits/s).  Every run is gated on 1-min load < 4;
a fail that coincides with load >= 4 or an overflow is re-run once.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
LOSS_LIMIT = 0.001
LOAD_LIMIT = 4.0
# Rough burst-rate ceilings (Pluto TX saturation) used only to place the first bracket.
CAPACITY = {0: 190, 1: 190, 2: 250, 3: 330, 4: 330, 5: 330, 6: 330, 7: 330}


def load1() -> float:
    return float(open("/proc/loadavg").read().split()[0])


def wait_for_load(log: list) -> float:
    waited = 0
    while load1() >= LOAD_LIMIT and waited < 600:
        time.sleep(10)
        waited += 10
    if waited:
        log.append(f"waited {waited}s for load; now {load1():.2f}")
    return load1()


def summarize(run: Path) -> dict:
    load = json.loads((run / "load.json").read_text())
    meta = json.loads((run / "meta.json").read_text())
    records = [json.loads(line) for line in (run / "rx_stats.jsonl").read_text().splitlines()]
    active = [r for r in records if r["receiver"]["bursts_decoded"] > 0]

    def overflow(r):
        return (r.get("transport") or {}).get("rx_overflow_events", 0) or 0

    snr = [r.get("snr_2s") for r in active if r.get("snr_2s") is not None]
    return {
        "rate": load["rate_offered"], "size": load["size"], "sent": load["sent"],
        "delivered": load["delivered"], "loss": load["loss_ratio"], "corrupt": load["corrupt"],
        "overflow_while_loaded": (overflow(active[-1]) - overflow(active[0])) if active else None,
        "snr_2s_mean": float(np.mean(snr)) if snr else None,
        "load_1min_at_start": meta.get("load_1min_at_start"),
        "worker_cpu_mean_pct": meta.get("worker_cpu_mean_pct"),
        "n_workers": meta.get("n_workers"),
        "worker_cpu_sum_pct": meta.get("worker_cpu_sum_pct"),
        "rx_main_cpu_pct": meta.get("rx_main_cpu_pct"),
        "sender_max_behind_ms": load.get("sender_max_behind_ms"),
    }


class Search:
    def __init__(self, args):
        self.a = args
        self.out = Path(args.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.runs: list[dict] = []
        self.n = 0

    def once(self, rate: float, note: str = "") -> dict:
        notes = [note] if note else []
        for attempt in range(3):
            load = wait_for_load(notes)
            self.n += 1
            run = self.out / f"run{self.n:02d}_r{rate:.1f}"
            env = {"PYTHON": sys.executable, **__import__("os").environ}
            proc = subprocess.run(
                [str(HERE / "live_run.sh"), self.a.root, str(self.a.tx_gain), str(self.a.mcs),
                 str(rate), str(self.a.seconds), str(run), str(self.a.size)],
                env=env, capture_output=True, text=True)
            if proc.returncode == 0 and (run / "meta.json").exists():
                break
            notes.append(f"live_run failed rc={proc.returncode} (attempt {attempt + 1}): {proc.stderr[-200:]}")
            time.sleep(15)
        else:
            raise RuntimeError("live_run failed 3 times: " + " | ".join(notes))
        s = summarize(run)
        s["dir"] = run.name
        s["passed"] = s["loss"] < LOSS_LIMIT and s["overflow_while_loaded"] == 0
        s["notes"] = notes
        print(json.dumps({k: s[k] for k in ("rate", "loss", "overflow_while_loaded", "load_1min_at_start", "passed")}), flush=True)
        self.runs.append(s)
        self.save()
        return s

    def test(self, rate: float) -> dict:
        s = self.once(rate)
        suspicious = (s["load_1min_at_start"] or 0) >= LOAD_LIMIT or (s["overflow_while_loaded"] or 0) > 0
        if not s["passed"] and suspicious:
            r = self.once(rate, "rerun of a fail with load>=4 or overflow")
            r["rerun"] = True
            s["superseded_by_rerun"] = True
            return r
        return s

    def save(self, result: dict | None = None) -> None:
        doc = {"root": self.a.root, "tx_gain_db": self.a.tx_gain, "mcs_index": self.a.mcs,
               "size": self.a.size, "seconds": self.a.seconds, "runs": self.runs}
        if result is not None:
            doc["result"] = result
        (self.out / "search.json").write_text(json.dumps(doc, indent=1))

    def run(self) -> dict:
        a = self.a
        probe = self.test(20.0)
        if probe["loss"] > 0.01:
            result = {"goodput_bps": 0.0, "max_rate": 0.0, "reason": "probe at 20/s lost > 1%"}
            self.save(result)
            return result
        lo, hi = 0.0, None
        if probe["passed"]:
            lo = 20.0
            rate = max(30.0, 0.6 * CAPACITY.get(a.mcs, 190))
        else:
            hi, rate = 20.0, 10.0
        while hi is None:
            s = self.test(rate)
            if s["passed"]:
                lo = rate
                rate *= 1.5
                if lo > 800:
                    break
            else:
                hi = rate
        while hi is not None and (hi - lo) > 0.05 * max(hi, 1.0) and hi - lo > 1.0:
            mid = round((lo + hi) / 2, 1)
            if self.test(mid)["passed"]:
                lo = mid
            else:
                hi = mid
        result = {"max_rate": lo, "first_failing_rate": hi, "goodput_bps": lo * a.size * 8}
        self.save(result)
        return result


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", required=True)
    p.add_argument("--tx-gain", type=float, required=True)
    p.add_argument("--mcs", type=int, required=True)
    p.add_argument("--size", type=int, default=900)
    p.add_argument("--seconds", type=int, default=30)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    args.root = str(Path(args.root).resolve())
    print(json.dumps(Search(args).run()))


if __name__ == "__main__":
    main()
