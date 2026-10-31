"""Record raw Pluto-to-Pluto bursts at a sweep of TX gains, for offline A/B decoding.

One Pluto transmits a fixed number of app-framed bursts (the same
``MessageTransmitter`` tx_app uses) at a fixed rate; the other records the raw
samples around them.  Each TX gain becomes one ``.npy`` of interleaved int16 --
exactly what the AD936x delivered -- plus a ``.json`` describing it.  Decoding
happens later, with ``offline_decode.py``, so two receivers can be compared on
identical samples.

This transmits RF and takes the same explicit acknowledgement as the apps:

    PYTHONPATH=src python experiments/rx_improvement/ota_capture.py \\
        --out <dir> --mcs qpsk --tx-gains -25 -22.5 -20 ... 0 \\
        --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF'
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np

from ofdm_link.phy import MCS
from ofdm_message_link import pluto
from ofdm_message_link.link import MessageTransmitter, build_phy_profile, load_link_config
from ofdm_message_link.options import (
    DEFAULT_CONFIG,
    DEFAULT_OVERLAY,
    require_rf_capability,
)

RX_FULL_SCALE = 2048.0


class Recorder(threading.Thread):
    """Pull chunks off a running source and keep them while armed."""

    def __init__(self, source: pluto.PlutoSampleSource) -> None:
        super().__init__(daemon=True)
        self.source = source
        self.stop = threading.Event()
        self.armed = threading.Event()
        self.chunks: list[np.ndarray] = []
        self.lock = threading.Lock()

    def run(self) -> None:
        while not self.stop.is_set():
            chunk = self.source.recv(0.1)
            if chunk is None or not self.armed.is_set():
                continue
            with self.lock:
                self.chunks.append(chunk)

    def take(self) -> np.ndarray:
        with self.lock:
            chunks, self.chunks = self.chunks, []
        if not chunks:
            return np.zeros(0, dtype=np.int16)
        samples = np.concatenate(chunks).view(np.float32) * RX_FULL_SCALE
        return np.rint(samples).astype(np.int16)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--mcs", choices=("qpsk", "qam16"), default="qpsk")
    parser.add_argument("--tx-gains", nargs="+", type=float, required=True)
    parser.add_argument("--rx-gain", type=float, default=15.0)
    parser.add_argument("--bursts", type=int, default=400)
    parser.add_argument("--rate", type=float, default=100.0, help="bursts per second")
    parser.add_argument("--payload-bytes", type=int, default=900)
    parser.add_argument("--tx-serial")
    parser.add_argument("--rx-serial")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--overlay", default=DEFAULT_OVERLAY)
    parser.add_argument("--enable-rf", action="store_true")
    parser.add_argument("--acknowledgement")
    args = parser.parse_args()
    args.transport = "pluto"
    token = require_rf_capability(args)
    if max(args.tx_gains) > 0.0:
        raise SystemExit("TX gain above 0 dB is not a Pluto setting")

    devices = {device.serial: device for device in pluto.discover()}
    if len(devices) < 2:
        raise SystemExit(f"need two Plutos, found {len(devices)}")
    serials = sorted(devices)
    tx_serial = args.tx_serial or serials[0]
    rx_serial = args.rx_serial or next(s for s in serials if s != tx_serial)

    config = load_link_config(args.config, [args.overlay])
    profile = build_phy_profile(config, mcs=MCS[args.mcs.upper()])
    rate = int(profile.sample_rate)
    centre = float(profile.center_frequency)

    def settings(serial: str, gain: float) -> pluto.PlutoSettings:
        return pluto.PlutoSettings(devices[serial].address, serial, rate, centre, gain)

    source = pluto.PlutoSampleSource(settings(rx_serial, args.rx_gain))
    sink = pluto.PlutoSampleSink(settings(tx_serial, min(args.tx_gains)), token)
    args.out.mkdir(parents=True, exist_ok=True)
    source.start()
    recorder = Recorder(source)
    recorder.start()
    sink.start()
    try:
        for gain in args.tx_gains:
            readback = sink.set_gain(gain)
            transmitter = MessageTransmitter(profile)
            bursts = [
                transmitter.encode_message(
                    np.random.default_rng(index).integers(
                        0, 256, args.payload_bytes, dtype=np.uint8
                    ).tobytes()
                )
                for index in range(args.bursts)
            ]
            if any(len(burst) != 1 for burst in bursts):
                raise SystemExit("payload does not fit one burst")
            time.sleep(0.3)
            overflow_before = source.snapshot()["rx_overflow_events"]
            recorder.armed.set()
            time.sleep(0.2)
            started = time.monotonic()
            for index, (burst,) in enumerate(bursts):
                due = started + index / args.rate
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                sink.send(burst.samples)
            time.sleep(0.5)
            recorder.armed.clear()
            samples = recorder.take()
            overflow = source.snapshot()["rx_overflow_events"] - overflow_before
            stem = f"{args.mcs}_tx{gain:+05.1f}".replace(".", "p")
            np.save(args.out / f"{stem}.npy", samples)
            meta = {
                "mcs": args.mcs,
                "tx_gain_db": gain,
                "tx_gain_readback_db": readback,
                "rx_gain_db": args.rx_gain,
                "bursts_sent": args.bursts,
                "burst_rate_per_s": args.rate,
                "payload_bytes": args.payload_bytes,
                "sample_rate_sps": rate,
                "center_frequency_hz": centre,
                "tx_serial": tx_serial,
                "rx_serial": rx_serial,
                "rx_overflow_events": overflow,
                "samples": int(samples.size // 2),
                "send_duration_s": time.monotonic() - started,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            (args.out / f"{stem}.json").write_text(json.dumps(meta, indent=1))
            print(json.dumps(meta), flush=True)
    finally:
        sink.stop()
        recorder.stop.set()
        recorder.join(timeout=2)
        source.stop()


if __name__ == "__main__":
    sys.exit(main())
