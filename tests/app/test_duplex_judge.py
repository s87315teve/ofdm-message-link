"""judge_run.py (duplex soak verdict by per-minute trend) on synthetic run directories."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "judge_run", ROOT / "experiments" / "duplex_fdd" / "judge_run.py"
)
jr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jr)


def test_a_single_spike_is_not_a_rising_trend():
    assert not jr.keeps_rising([18, 7, 0, 5, 43], factor=2.0, min_excess=3)
    # 18 is also well above the median (6) of the other minutes
    assert jr.spikes([18, 7, 0, 5, 43], factor=2.0, min_excess=3) == [0, 4]


def test_three_rising_minutes_well_above_the_rest_fail():
    # node 2 ingress drops of the 2026-11-11 10 MS/s 900/s run (29 MHz filter)
    assert jr.keeps_rising([31, 130, 67, 84, 190], factor=2.0, min_excess=3)
    # rising, but the last minute is not twice the median of the earlier ones
    assert not jr.keeps_rising([100, 110, 101, 105, 120], factor=2.0, min_excess=3)
    # rising and doubling, but by fewer than 3 events
    assert not jr.keeps_rising([0, 0, 0, 1, 2], factor=2.0, min_excess=3)
    # rose and then fell in the last minute (MCS 17 1300/s, node 2)
    assert not jr.keeps_rising([24, 98, 207, 355, 279], factor=2.0, min_excess=3)


def test_per_minute_counts_complete_minutes_only():
    times = [float(t) for t in range(0, 171, 10)]  # 2 complete minutes and a part
    values = [t // 10 for t in range(0, 171, 10)]
    assert jr.per_minute(times, values) == [6, 6]


def _stats(t, ingress, late):
    return {"monotonic_s": t, "tx": {"ingress_datagrams": ingress,
                                     "transport": {"uhd_faults": {"tx_time_error": late}}},
            "rx": {"transport": {}}}


def _write_run(run: Path, *, loss=0.002, late_per_minute=(1, 0, 2, 0, 1), buckets=None):
    run.mkdir()
    buckets = buckets or [[10 * i, 100, 100.0, 150.0] for i in range(30)]
    for direction in ("AB", "BA"):
        (run / f"load_{direction}.json").write_text(json.dumps({
            "loss_ratio": loss, "goodput_mbps": 9.3, "duration": 300.0,
            "latency_ms_p50": 100, "latency_ms_p95": 150, "latency_ms_p99": 200,
            "latency_buckets_10s": buckets}))
    for node in ("1", "2"):
        lines, late = [], 0
        for step in range(0, 302):
            minute = min(step // 60, 4)
            if step % 60 == 30:
                late += late_per_minute[minute]
            lines.append(_stats(1000.0 + step, step, late))
        (run / f"node{node}_stats.jsonl").write_text("\n".join(map(json.dumps, lines)) + "\n")


def test_a_quiet_run_passes_and_writes_judge_json(tmp_path):
    run = tmp_path / "quiet"
    _write_run(run)
    assert jr.main([str(run)]) == 0
    result = json.loads((run / "judge.json").read_text())
    assert result["verdict"] == "PASS"
    assert result["node1"]["per_minute"]["tx_late"] == [1, 0, 2, 0, 1]


def test_loss_rising_counters_latency_drift_and_crashes_fail(tmp_path):
    lossy = tmp_path / "lossy"
    _write_run(lossy, loss=0.0101)
    assert any("loss" in why for why in jr.judge_run(lossy)["why"])

    rising = tmp_path / "rising"
    _write_run(rising, late_per_minute=(2, 1, 5, 9, 20))
    assert any("tx_late keeps rising" in why for why in jr.judge_run(rising)["why"])

    drifting = tmp_path / "drifting"
    p50 = [100, 100, 120, 150, 190]
    _write_run(drifting, buckets=[[10 * i, 100, float(p50[i // 6]), 200.0] for i in range(30)])
    assert any("latency p50 keeps rising" in why for why in jr.judge_run(drifting)["why"])

    crashed = tmp_path / "crashed"
    _write_run(crashed)
    (crashed / "load_BA.json").unlink()
    assert jr.main([str(crashed)]) == 1
    assert "BA: no load result" in jr.judge_run(crashed)["why"][0]
