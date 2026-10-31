"""H1: how high signal-free air scores against the v5 (and legacy) preamble detectors.

Phase ``record``: TX off, records ``--seconds`` of raw RX IQ (int16) in
segments to a scratch directory, so every sample is processed (live
processing could not keep up in improve-rx).  Phase ``analyze``: scans the
recordings in overlapping blocks and reports

* v5: max ``M128`` over every offset; per NMS peak (M128 >= the configured
  detection threshold) the coherent STF score ``c_stf`` and the LTF-pair score
  ``c_pair``; the same with a loose M128 gate (0.05); candidates/s that pass
  every configured v5 stage (incl. the balance test); optionally with the
  acquisition prefilter.
* legacy: max coherent confidence at loose thresholds (0.10 / 0.05) and
  candidates/s at the configured thresholds.
* ``acquire_all`` at the configured thresholds: candidates/s per family.

    PYTHONPATH=src python experiments/tx_v5/noise_scan_v5.py record --dir <scratch> --seconds 90
    PYTHONPATH=src python experiments/tx_v5/noise_scan_v5.py analyze --dir <scratch> --out noise_scan.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from ofdm_link.runtime.factory import build_burst_config
from ofdm_link.phy.sync import (
    SyncConfig,
    _acquisition_candidates,
    _shared_correlation,
    acquire_all,
)
from ofdm_link.phy.sync_v5 import (
    V5Acquisition,
    _steady_mean,
    prefilter_for_acquisition,
    v5_candidates,
)
from ofdm_message_link import pluto
from ofdm_message_link.link import build_phy_profile, load_link_config
from ofdm_message_link.options import DEFAULT_CONFIG, DEFAULT_OVERLAY

RX_SERIAL = "10447354119600041b000b00f2ff1ef9c6"
RX_FULL_SCALE = 2048.0
BLOCK = 1 << 20
OVERLAP = 2048
EDGES_V5 = (0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30)
EDGES_LEGACY = (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)


def record(args) -> None:
    load = float(open("/proc/loadavg").read().split()[0])
    profile = build_phy_profile(load_link_config(DEFAULT_CONFIG, [DEFAULT_OVERLAY]))
    devices = {device.serial: device for device in pluto.discover()}
    source = pluto.PlutoSampleSource(
        pluto.PlutoSettings(
            devices[args.serial].address,
            args.serial,
            int(profile.sample_rate),
            float(profile.center_frequency),
            args.rx_gain,
        )
    )
    args.dir.mkdir(parents=True, exist_ok=True)
    source.start()
    try:
        time.sleep(0.5)
        for segment in range(int(np.ceil(args.seconds / args.segment))):
            chunks = []
            got = 0
            target = int(args.segment * profile.sample_rate)
            ovf0 = source.snapshot()["rx_overflow_events"]
            started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            while got < target:
                chunk = source.recv(0.5)
                if chunk is None:
                    continue
                chunks.append(np.rint(chunk.view(np.float32) * RX_FULL_SCALE).astype(np.int16))
                got += chunk.size
            ovf = source.snapshot()["rx_overflow_events"] - ovf0
            data = np.concatenate(chunks)
            np.save(args.dir / f"noise_g{args.rx_gain:g}_{segment}.npy", data)
            meta = {
                "segment": segment,
                "samples": int(data.size // 2),
                "rx_gain_db": args.rx_gain,
                "rx_overflow_events": ovf,
                "recorded_at": started,
                "loadavg_at_start": load,
                "serial": args.serial,
            }
            (args.dir / f"noise_g{args.rx_gain:g}_{segment}.json").write_text(json.dumps(meta))
            print(json.dumps(meta), flush=True)
    finally:
        source.stop()


class Acc:
    def __init__(self):
        self.values: dict[str, list[np.ndarray]] = {}
        self.max: dict[str, float] = {}

    def add(self, key: str, arr) -> None:
        arr = np.asarray(arr, dtype=float)
        if arr.size:
            self.values.setdefault(key, []).append(arr)
            self.max[key] = max(self.max.get(key, -np.inf), float(arr.max()))

    def get(self, key: str) -> np.ndarray:
        parts = self.values.get(key)
        return np.concatenate(parts) if parts else np.zeros(0)


def summary(arr: np.ndarray, edges, seconds: float) -> dict:
    return {
        "count": int(arr.size),
        "per_second": arr.size / seconds,
        "max": float(arr.max()) if arr.size else None,
        "p99": float(np.percentile(arr, 99)) if arr.size else None,
        "p50": float(np.percentile(arr, 50)) if arr.size else None,
        "count_at_or_above": {f"{e:.2f}": int(np.count_nonzero(arr >= e)) for e in edges},
    }


def analyze(args) -> None:
    config = load_link_config(DEFAULT_CONFIG, [DEFAULT_OVERLAY])
    rx = config.phy.rx
    bc = build_burst_config(config)
    legacy_cfg: SyncConfig = bc.sync
    v5_cfg = bc.v5.acquisition
    v5_probe = replace(v5_cfg, stf_threshold=1e-9, correlation_threshold=1e-9, balance_ratio=1e-9)
    v5_loose = replace(v5_probe, detection_threshold=0.05)
    legacy_loose = replace(legacy_cfg, detection_threshold=0.10, correlation_threshold=0.05)

    files = sorted(args.dir.glob(f"noise_g{args.rx_gain:g}_*.npy"))
    acc = Acc()
    total = 0
    started = time.monotonic()
    m128_max = 0.0
    m128_max_pf = 0.0
    m128_raw_max = 0.0
    m128_raw_max_pf = 0.0
    full_counts = {"v5": 0, "legacy": 0}
    full_counts_pf = {"v5": 0, "legacy": 0}
    v5_pass = 0
    v5_pass_pf = 0
    for path in files:
        raw = np.load(path, mmap_mode="r")
        n = raw.size // 2
        pos = 0
        while pos < n - OVERLAP:
            end = min(n, pos + BLOCK)
            block = (np.asarray(raw[2 * pos : 2 * end]).astype(np.float32) / RX_FULL_SCALE).view(
                np.complex64
            )
            last = end >= n
            limit = block.size if last else block.size - OVERLAP
            for pf in (False, True) if args.prefilter else (False,):
                values = prefilter_for_acquisition(block) if pf else block
                tag = "_pf" if pf else ""
                shared = _shared_correlation(values, 32)
                real, imag, energy = shared.window(128)
                lim = min(limit, real.size)
                num = real[:lim] ** 2 + imag[:lim] ** 2
                en = energy[:lim]
                ok = en >= v5_cfg.minimum_power * 128
                m = np.zeros_like(num)
                m[ok] = num[ok] / en[ok] ** 2
                # the fixed detector's metric: steady (DC/CW) lag-32 part removed
                sr, si = _steady_mean(shared, lim)
                cr = real[:lim] - 128 * sr
                ci = imag[:lim] - 128 * si
                ce = en - 128 * np.hypot(sr, si)
                okc = ok & (ce > 0)
                mc = np.zeros_like(num)
                mc[okc] = (cr[okc] ** 2 + ci[okc] ** 2) / ce[okc] ** 2
                if pf:
                    m128_max_pf = max(m128_max_pf, float(mc.max()))
                    m128_raw_max_pf = max(m128_raw_max_pf, float(m.max()))
                else:
                    m128_max = max(m128_max, float(mc.max()))
                    m128_raw_max = max(m128_raw_max, float(m.max()))
                    acc.add("m128_p999_block", [np.percentile(mc, 99.9)])
                for name, cfg in (("probe", v5_probe), ("loose", v5_loose)):
                    cands = [c for c in v5_candidates(values, shared, cfg) if c.stf_start < limit]
                    acc.add(f"v5_{name}_m128{tag}", [c.metric for c in cands])
                    acc.add(f"v5_{name}_cstf{tag}", [c.stf_confidence for c in cands])
                    acc.add(f"v5_{name}_cpair{tag}", [c.confidence for c in cands])
                    if name == "probe":
                        acc.add(
                            f"v5_probe_cpair_stfpass{tag}",
                            [c.confidence for c in cands if c.stf_confidence >= v5_cfg.stf_threshold],
                        )
                # v5 at the configured thresholds (balance test included)
                passed = [c for c in v5_candidates(values, shared, v5_cfg) if c.stf_start < limit]
                if pf:
                    v5_pass_pf += len(passed)
                else:
                    v5_pass += len(passed)
                acc.add(f"v5_configured_cpair{tag}", [c.confidence for c in passed])
                # legacy, loose and configured
                legacy_l = [
                    c
                    for c in _acquisition_candidates(values, legacy_loose, shared=shared)
                    if c.preamble_start < limit
                ]
                acc.add(f"legacy_loose_conf{tag}", [c.confidence for c in legacy_l])
                acc.add(f"legacy_loose_metric{tag}", [c.metric for c in legacy_l])
                v5_full = replace(v5_cfg, prefilter=pf)
                found = [
                    c for c in acquire_all(block, legacy_cfg, v5_full) if c.preamble_start < limit
                ]
                counts = full_counts_pf if pf else full_counts
                for c in found:
                    counts["v5" if isinstance(c, V5Acquisition) else "legacy"] += 1
            total += limit
            pos += limit
        print(f"{path.name}: {total / 5e6:.1f} s processed, {time.monotonic() - started:.0f} s",
              file=sys.stderr, flush=True)
    seconds = total / float(config.phy.sample_rate)
    metas = [json.loads(p.with_suffix(".json").read_text()) for p in files]
    result = {
        "files": [p.name for p in files],
        "captures": metas,
        "rx_gain_db": args.rx_gain,
        "seconds_processed": seconds,
        "configured": {
            "v5_detection_threshold": rx.v5_detection_threshold,
            "v5_stf_threshold": rx.v5_stf_threshold,
            "v5_correlation_threshold": rx.v5_correlation_threshold,
            "v5_balance_ratio": rx.v5_balance_ratio,
            "legacy_detection_threshold": legacy_cfg.detection_threshold,
            "legacy_correlation_threshold": legacy_cfg.correlation_threshold,
        },
        "v5": {
            "m128_max_all_offsets": m128_max,
            "m128_raw_max_all_offsets_no_steady_removal": m128_raw_max,
            "m128_per_block_p99_9_max": acc.max.get("m128_p999_block"),
            "probe_m128_ge_configured": {
                "m128": summary(acc.get("v5_probe_m128"), EDGES_V5, seconds),
                "c_stf": summary(acc.get("v5_probe_cstf"), EDGES_V5, seconds),
                "c_pair": summary(acc.get("v5_probe_cpair"), EDGES_V5, seconds),
                "c_pair_given_stf_pass": summary(acc.get("v5_probe_cpair_stfpass"), EDGES_V5, seconds),
            },
            "loose_m128_ge_0p05": {
                "m128": summary(acc.get("v5_loose_m128"), EDGES_V5, seconds),
                "c_stf": summary(acc.get("v5_loose_cstf"), EDGES_V5, seconds),
                "c_pair": summary(acc.get("v5_loose_cpair"), EDGES_V5, seconds),
            },
            "configured_candidates": v5_pass,
            "configured_candidates_per_second": v5_pass / seconds,
            "configured_cpair": summary(acc.get("v5_configured_cpair"), EDGES_V5, seconds),
        },
        "legacy": {
            "loose_confidence": summary(acc.get("legacy_loose_conf"), EDGES_LEGACY, seconds),
            "loose_metric_max": acc.max.get("legacy_loose_metric"),
        },
        "acquire_all_configured": {
            "v5_candidates": full_counts["v5"],
            "legacy_candidates": full_counts["legacy"],
            "v5_per_second": full_counts["v5"] / seconds,
            "legacy_per_second": full_counts["legacy"] / seconds,
        },
        "analysis_seconds": time.monotonic() - started,
    }
    if args.prefilter:
        result["prefilter"] = {
            "m128_max_all_offsets": m128_max_pf,
            "m128_raw_max_all_offsets_no_steady_removal": m128_raw_max_pf,
            "probe_c_stf": summary(acc.get("v5_probe_cstf_pf"), EDGES_V5, seconds),
            "probe_c_pair": summary(acc.get("v5_probe_cpair_pf"), EDGES_V5, seconds),
            "probe_c_pair_given_stf_pass": summary(acc.get("v5_probe_cpair_stfpass_pf"), EDGES_V5, seconds),
            "loose_c_pair": summary(acc.get("v5_loose_cpair_pf"), EDGES_V5, seconds),
            "legacy_loose_confidence": summary(acc.get("legacy_loose_conf_pf"), EDGES_LEGACY, seconds),
            "configured_v5_candidates": v5_pass_pf,
            "acquire_all_configured": full_counts_pf,
        }
    Path(args.out).write_text(json.dumps(result, indent=1))
    print(json.dumps(result, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("phase", choices=("record", "analyze"))
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--out")
    parser.add_argument("--seconds", type=float, default=90.0)
    parser.add_argument("--segment", type=float, default=30.0)
    parser.add_argument("--rx-gain", type=float, default=15.0)
    parser.add_argument("--serial", default=RX_SERIAL)
    parser.add_argument("--prefilter", action="store_true")
    args = parser.parse_args()
    if args.phase == "record":
        record(args)
    else:
        analyze(args)


if __name__ == "__main__":
    main()
