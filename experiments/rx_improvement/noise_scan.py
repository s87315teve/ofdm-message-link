"""How high signal-free air scores against the preamble detector.

Receives only -- nothing is transmitted -- and runs acquisition with loose
thresholds over every chunk, keeping the distribution of the coherent
correlation that noise and other users of the band reach.  A correlation
threshold is safe while it sits clearly above the maximum seen here.

    PYTHONPATH=src python experiments/rx_improvement/noise_scan.py --seconds 120 out.json
"""

from __future__ import annotations

import argparse
import json
import time

import numpy as np

from ofdm_link.phy.sync import SyncConfig, _acquisition_candidates
from ofdm_message_link import pluto
from ofdm_message_link.link import build_phy_profile, load_link_config
from ofdm_message_link.options import DEFAULT_CONFIG, DEFAULT_OVERLAY

LOOSE = SyncConfig(detection_threshold=0.10, correlation_threshold=0.05)
EDGES = (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--rx-gain", type=float, default=15.0)
    parser.add_argument("--serial")
    args = parser.parse_args()

    devices = {device.serial: device for device in pluto.discover()}
    serial = args.serial or sorted(devices)[-1]
    profile = build_phy_profile(load_link_config(DEFAULT_CONFIG, [DEFAULT_OVERLAY]))
    source = pluto.PlutoSampleSource(
        pluto.PlutoSettings(
            devices[serial].address,
            serial,
            int(profile.sample_rate),
            float(profile.center_frequency),
            args.rx_gain,
        )
    )
    confidences: list[float] = []
    metrics: list[float] = []
    samples = 0
    tail = np.zeros(0, dtype=np.complex64)
    source.start()
    started = time.monotonic()
    try:
        while time.monotonic() - started < args.seconds:
            chunk = source.recv(0.5)
            if chunk is None:
                continue
            block = np.concatenate((tail, chunk))
            samples += chunk.size
            for candidate in _acquisition_candidates(block, LOOSE):
                confidences.append(candidate.confidence)
                metrics.append(candidate.metric)
            tail = block[-(LOOSE.preamble_length - 1) :]
        overflow = source.snapshot()["rx_overflow_events"]
    finally:
        source.stop()

    seconds = samples / profile.sample_rate
    confidence = np.asarray(confidences)
    result = {
        "serial": serial,
        "rx_gain_db": args.rx_gain,
        "center_frequency_hz": profile.center_frequency,
        "seconds": seconds,
        "rx_overflow_events": overflow,
        "candidates_per_second": len(confidences) / seconds,
        "max_confidence": float(confidence.max()) if confidence.size else None,
        "max_metric": float(max(metrics)) if metrics else None,
        "candidates_above_confidence": {
            f"{edge:.2f}": int(np.count_nonzero(confidence >= edge)) for edge in EDGES
        },
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with open(args.output, "w") as handle:
        json.dump(result, handle, indent=1)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
