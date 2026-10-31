"""Decode recorded captures with whichever receiver is on PYTHONPATH.

Feeds each ``.npy`` written by ``ota_capture.py`` through the app's own
``MessageReceiver`` in Pluto-sized chunks, exactly as rx_app would, and reports
how many of the transmitted bursts came out.  Run it once per receiver:

    PYTHONPATH=src python experiments/rx_improvement/offline_decode.py <dir> new.json
    PYTHONPATH=<v1.0.0>/src python experiments/rx_improvement/offline_decode.py <dir> old.json

``--overlay`` replaces the default 2.45 GHz profile, for example to try other
acquisition thresholds; ``--only`` restricts the files with a glob.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from ofdm_link.phy import MCS
from ofdm_message_link.link import MessageReceiver, build_phy_profile, load_link_config
from ofdm_message_link.options import DEFAULT_CONFIG, DEFAULT_OVERLAY

CHUNK = 65536
RX_FULL_SCALE = 2048.0


def decode(path: Path, overlays: list[str]) -> dict:
    meta = json.loads(path.with_suffix(".json").read_text())
    config = load_link_config(DEFAULT_CONFIG, overlays)
    profile = build_phy_profile(config, mcs=MCS[meta["mcs"].upper()])
    receiver = MessageReceiver(profile, max_observations=1 << 16)
    raw = np.load(path).astype(np.float32) / RX_FULL_SCALE
    samples = raw.view(np.complex64)
    sequences: set[int] = set()
    evm_snr: list[float] = []
    preamble_snr: list[float] = []
    for start in range(0, samples.size, CHUNK):
        receiver.feed(samples[start : start + CHUNK])
        for observation in receiver.drain_observations():
            if observation.is_ours:
                sequences.add(observation.sequence)
            if observation.effective_snr_db is not None:
                evm_snr.append(observation.effective_snr_db)
        drain = getattr(receiver, "drain_link_snr_db", None)
        if drain is not None:
            preamble_snr.extend(drain())
    snapshot = receiver.decoder_snapshot()
    sent = meta["bursts_sent"]
    result = {
        "file": path.name,
        "mcs": meta["mcs"],
        "tx_gain_db": meta["tx_gain_db"],
        "sent": sent,
        "decoded": len(sequences),
        "loss": 1.0 - len(sequences) / sent,
        "rx_overflow_events": meta["rx_overflow_events"],
        "candidates": snapshot.detected_burst_candidates,
        "header_ok": snapshot.header_decode_success,
        "header_fail": snapshot.header_decode_failure,
        "payload_fail": snapshot.payload_decode_failure,
        "evm_snr_db_median": float(np.median(evm_snr)) if evm_snr else None,
    }
    if preamble_snr:
        result["preamble_snr_db_median"] = float(np.median(preamble_snr))
        result["preamble_snr_db_iqr"] = float(np.subtract(*np.percentile(preamble_snr, [75, 25])))
        result["preamble_snr_count"] = len(preamble_snr)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("output")
    parser.add_argument("--overlay", action="append")
    parser.add_argument("--only", default="*.npy")
    args = parser.parse_args()
    overlays = args.overlay or [DEFAULT_OVERLAY]
    results = []
    for path in sorted(args.directory.glob(args.only)):
        result = decode(path, overlays)
        results.append(result)
        print(json.dumps(result), flush=True)
    with open(args.output, "w") as handle:
        json.dump(results, handle, indent=1)


if __name__ == "__main__":
    main()
