"""Record raw Pluto-to-Pluto wire-v5 bursts at a sweep of TX gains, for offline decoding.

Like ``experiments/rx_improvement/ota_capture.py`` but the transmitter is the
current v5 ``MessageTransmitter`` with an explicit MCS index (8-17), optional
guards and scrambler switch, and random or all-zero datagram bodies.  One
datagram per burst (no aggregation), so loss is per burst.  Each TX gain
becomes one ``.npy`` of interleaved int16 RX samples plus a ``.json``.

    PYTHONPATH=src python experiments/tx_v5/ota_capture_v5.py --out <dir> --mcs-index 8 \\
        --tx-gains -30 -27.5 ... 0 [--guards 16 64] [--scrambler on|off] [--payload random|zeros] \\
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

from ofdm_link.phy.mcs_table import mcs_entry
from ofdm_message_link import pluto
from ofdm_message_link.link import MessageTransmitter, build_phy_profile, load_link_config
from ofdm_message_link.options import DEFAULT_CONFIG, DEFAULT_OVERLAY, require_rf_capability

RX_FULL_SCALE = 2048.0
TX_SERIAL = "104473541196000419000c00f2ff1e6e77"
RX_SERIAL = "10447354119600041b000b00f2ff1ef9c6"


def loadavg() -> list[float]:
    return [float(v) for v in open("/proc/loadavg").read().split()[:3]]


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


def open_radios(args, token, rate, centre, first_gain):
    """Discover and start both Plutos; up to 3 retries on failure."""

    last = None
    for attempt in range(4):
        try:
            devices = {device.serial: device for device in pluto.discover()}
            for serial in (args.tx_serial, args.rx_serial):
                if serial not in devices:
                    raise RuntimeError(f"Pluto {serial} not found (have {sorted(devices)})")

            def settings(serial: str, gain: float) -> pluto.PlutoSettings:
                return pluto.PlutoSettings(devices[serial].address, serial, rate, centre, gain)

            source = pluto.PlutoSampleSource(settings(args.rx_serial, args.rx_gain))
            sink = pluto.PlutoSampleSink(settings(args.tx_serial, first_gain), token)
            source.start()
            recorder = Recorder(source)
            recorder.start()
            try:
                sink.start()
            except Exception:
                recorder.stop.set()
                recorder.join(2)
                source.stop()
                raise
            return source, sink, recorder, attempt
        except Exception as error:  # noqa: BLE001 - Pluto hangs surface as many types
            last = error
            print(f"radio open failed (attempt {attempt}): {error}", file=sys.stderr, flush=True)
            time.sleep(8)
    raise SystemExit(f"Plutos unavailable after retries: {last}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--mcs-index", type=int, required=True)
    parser.add_argument("--tx-gains", nargs="+", type=float, required=True)
    parser.add_argument("--guards", nargs=2, type=int, metavar=("PRE", "POST"))
    parser.add_argument("--scrambler", choices=("on", "off"), default="on")
    parser.add_argument("--payload", choices=("random", "zeros"), default="random")
    parser.add_argument("--tag", default="", help="extra stem suffix, e.g. g0_32")
    parser.add_argument("--rx-gain", type=float, default=15.0)
    parser.add_argument("--bursts", type=int, default=400)
    parser.add_argument("--rate", type=float, default=100.0, help="bursts per second")
    parser.add_argument("--payload-bytes", type=int, default=900)
    parser.add_argument("--tx-serial", default=TX_SERIAL)
    parser.add_argument("--rx-serial", default=RX_SERIAL)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--overlay", default=DEFAULT_OVERLAY)
    parser.add_argument("--max-load", type=float, default=4.0)
    parser.add_argument("--enable-rf", action="store_true")
    parser.add_argument("--acknowledgement")
    args = parser.parse_args()
    args.transport = "pluto"
    token = require_rf_capability(args)
    if max(args.tx_gains) > 0.0:
        raise SystemExit("TX gain above 0 dB is not a Pluto setting")

    entry = mcs_entry(args.mcs_index)
    if entry.family != "v5":
        raise SystemExit(f"MCS {args.mcs_index} is not a v5 entry")
    config = load_link_config(args.config, [args.overlay])
    guard_kw = {}
    if args.guards:
        guard_kw = dict(guard_pre_samples=args.guards[0], guard_post_samples=args.guards[1])
    profile = build_phy_profile(config, mcs_entry=entry, **guard_kw)
    scrambler = args.scrambler == "on"
    rate = int(profile.sample_rate)
    centre = float(profile.center_frequency)
    args.out.mkdir(parents=True, exist_ok=True)

    load = loadavg()
    if load[0] >= args.max_load:
        raise SystemExit(f"1-min load {load[0]} >= {args.max_load}; not capturing")

    source, sink, recorder, open_retries = open_radios(
        args, token, rate, centre, min(args.tx_gains)
    )
    try:
        for gain in args.tx_gains:
            load = loadavg()
            if load[0] >= args.max_load:
                print(f"load {load[0]} high, waiting", file=sys.stderr, flush=True)
                for _ in range(60):
                    time.sleep(5)
                    load = loadavg()
                    if load[0] < args.max_load:
                        break
            readback = sink.set_gain(gain)
            transmitter = MessageTransmitter(profile, mcs_entry=entry, scrambler=scrambler)
            bursts = []
            for index in range(args.bursts):
                if args.payload == "zeros":
                    body = bytes(args.payload_bytes)
                else:
                    body = (
                        np.random.default_rng(index)
                        .integers(0, 256, args.payload_bytes, dtype=np.uint8)
                        .tobytes()
                    )
                bursts.append(transmitter.encode_message(body))
            if any(len(burst) != 1 for burst in bursts):
                raise SystemExit("payload does not fit one burst")
            burst_samples = int(bursts[0][0].sample_count)
            tx_peak_to_rms_db = float(
                np.median(
                    [
                        10
                        * np.log10(
                            np.max(np.abs(b.samples) ** 2) / np.mean(np.abs(b.samples) ** 2)
                        )
                        for (b,) in bursts[:50]
                    ]
                )
            )
            time.sleep(0.3)
            overflow_before = source.snapshot()["rx_overflow_events"]
            recorder.armed.set()
            time.sleep(0.2)
            started_wall = time.time()
            started = time.monotonic()
            late = 0
            for index, (burst,) in enumerate(bursts):
                due = started + index / args.rate
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                elif delay < -0.005:
                    late += 1
                sink.send(burst.samples)
            send_duration = time.monotonic() - started
            time.sleep(0.5)
            recorder.armed.clear()
            samples = recorder.take()
            overflow = source.snapshot()["rx_overflow_events"] - overflow_before
            stem = f"v5m{args.mcs_index}_tx{gain:+06.2f}".replace(".", "p")
            if args.tag:
                stem += f"_{args.tag}"
            np.save(args.out / f"{stem}.npy", samples)
            meta = {
                "family": "v5",
                "mcs": entry.modulation.name.lower(),
                "mcs_index": args.mcs_index,
                "code_rate": str(entry.rate),
                "tx_gain_db": gain,
                "tx_gain_readback_db": readback,
                "rx_gain_db": args.rx_gain,
                "bursts_sent": args.bursts,
                "burst_rate_per_s": args.rate,
                "payload_bytes": args.payload_bytes,
                "payload": args.payload,
                "scrambler": scrambler,
                "guards": [profile.guard_pre_samples, profile.guard_post_samples],
                "aggregation": False,
                "burst_samples_with_guards": burst_samples,
                "psdu_bytes": int(bursts[0][0].psdu_bytes),
                "tx_peak_to_mean_db_median": tx_peak_to_rms_db,
                "sample_rate_sps": rate,
                "center_frequency_hz": centre,
                "tx_serial": args.tx_serial,
                "rx_serial": args.rx_serial,
                "rx_overflow_events": overflow,
                "late_sends": late,
                "samples": int(samples.size // 2),
                "send_duration_s": send_duration,
                "send_started_unix": started_wall,
                "loadavg_at_start": load,
                "radio_open_retries": open_retries,
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "stem": stem,
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
