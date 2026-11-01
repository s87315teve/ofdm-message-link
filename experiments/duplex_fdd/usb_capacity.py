"""M0: does USB 2.0 carry a Pluto's receive stream while its own transmitter pushes bursts?

A duplex node receives 5 MS/s (20 MB/s) and transmits on the same USB device,
and both Plutos and the webcam share one USB 2.0 bus here.  Measured already
(commander's probe, scratchpad usbprobe/probe1.json): both Plutos receiving
at once for 30 s, 0 overflows each; a second libiio context on the same Pluto
fails with EBUSY, so a node must share one context
(:class:`~ofdm_message_link.pluto.SharedPlutoContext`).

This measures the rest of design §6.3 M0, with the transmitter at -89.75 dB
(almost no RF), random-noise bursts of 6.9 ms:

    per-pluto   each Pluto alone: RX streaming while its own TX pushes at
                duty 0 (sink open, idle), 0.5 and 0.9; --seconds each (60)
    both        both Plutos at once, duty 0.9, with 4 and then 8 RX kernel buffers
    double-open (--probe-double-open) a second context while the first streams

Overflows are polled from the FPGA flag every 0.25 s, so a count is a lower
bound.  The first 3 s after the receiver starts are reported apart (known
start-up overflows).

    PYTHONPATH=src python experiments/duplex_fdd/usb_capacity.py run \\
        --out experiments/duplex_fdd/results/usb \\
        --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF'
    PYTHONPATH=src python experiments/duplex_fdd/usb_capacity.py summarize \\
        experiments/duplex_fdd/results/usb
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hw_common  # noqa: E402
from hw_common import Pacer  # noqa: E402

SAMPLE_RATE = hw_common.SAMPLE_RATE
MIN_TX_GAIN_DB = -89.75
BURST_SAMPLES = 34_500  # 6.9 ms, the length of an MCS 14 8-KB aggregate
STARTUP_S = 3.0
# Node A transmits on 2420 and receives on 2460; node B the reverse.
FREQUENCIES = {
    hw_common.NODE_A_SERIAL: (2.460e9, 2.420e9),
    hw_common.NODE_B_SERIAL: (2.420e9, 2.460e9),
}
ALLOWED_OVERFLOWS_PER_MIN = 0.5


class Drain(threading.Thread):
    """Count what a running source delivers, as fast as it delivers it."""

    def __init__(self, source) -> None:
        super().__init__(daemon=True, name="drain")
        self.source = source
        self.samples = 0
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            chunk = self.source.recv(0.1)
            if chunk is not None:
                self.samples += int(chunk.size)

    def close(self) -> None:
        self.stop_event.set()
        self.join(timeout=2.0)


class _DryEndpoint:
    """--dry-run stand-in for a source/sink pair."""

    def __init__(self) -> None:
        self.started = time.monotonic()

    def send(self, samples) -> bool:
        time.sleep(samples.size / SAMPLE_RATE)
        return True

    def recv(self, timeout: float):
        time.sleep(0.013)
        return np.zeros(65536, dtype=np.complex64)

    def snapshot(self) -> dict:
        return {"rx_overflow_events": 0}


class Node:
    """One Pluto with RX and TX on one shared context."""

    def __init__(self, serial: str, device, token, kernel_buffers: int, dry: bool) -> None:
        self.serial = serial
        self.device = device
        self.token = token
        self.kernel_buffers = kernel_buffers
        self.dry = dry
        self.shared = self.source = self.sink = self.drain = None
        self.started = 0.0
        self.startup_overflows = 0

    def open(self) -> None:
        f_rx, f_tx = FREQUENCIES.get(self.serial, (2.460e9, 2.420e9))
        if self.dry:
            self.source = self.sink = _DryEndpoint()
        else:
            from ofdm_message_link import pluto

            address = self.device.address
            self.shared = pluto.SharedPlutoContext(
                address, self.serial, rx_kernel_buffers=self.kernel_buffers
            )
            try:
                self.shared.configure(SAMPLE_RATE)
                self.sink = pluto.PlutoSampleSink(
                    pluto.PlutoSettings(address, self.serial, SAMPLE_RATE, f_tx, MIN_TX_GAIN_DB),
                    self.token,
                    open_context=self.shared.factory(),
                )
                self.sink.start()
                self.source = pluto.PlutoSampleSource(
                    pluto.PlutoSettings(address, self.serial, SAMPLE_RATE, f_rx, 15.0),
                    open_context=self.shared.factory(),
                )
                self.source.start()
            except BaseException:
                self.close()
                raise
        self.started = time.monotonic()
        self.drain = Drain(self.source)
        self.drain.start()

    def overflows(self) -> int:
        return int(self.source.snapshot()["rx_overflow_events"])

    def settle(self) -> None:
        """Wait out the start-up window and note what it overflowed."""

        remaining = self.started + STARTUP_S - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self.startup_overflows = self.overflows()

    def snapshot(self) -> dict:
        if self.dry:
            return {"dry_run": True}
        snapshot = self.shared.snapshot()
        snapshot["source"] = self.source.snapshot()
        snapshot["sink"] = self.sink.snapshot()
        return snapshot

    def close(self) -> None:
        # Transmit first: its attenuator goes back to the minimum output.
        for closer in (
            lambda: self.sink and not self.dry and self.sink.stop(),
            lambda: self.drain and self.drain.close(),
            lambda: self.source and not self.dry and self.source.stop(),
            lambda: self.shared and self.shared.close(),
        ):
            try:
                closer()
            except Exception as error:  # noqa: BLE001 - closing must go on
                print(f"close: {error}", file=sys.stderr)


def noise_bursts(count: int = 4) -> list[np.ndarray]:
    rng = np.random.default_rng(11)
    return [
        (
            (rng.standard_normal(BURST_SAMPLES) + 1j * rng.standard_normal(BURST_SAMPLES)) * 0.25
        ).astype(np.complex64)
        for _ in range(count)
    ]


def measure(nodes: list[Node], duty: float, seconds: float, label: str) -> dict:
    """Run every node's TX at ``duty`` for ``seconds`` and count RX overflows."""

    bursts = noise_bursts()
    for node in nodes:
        node.settle()
    pacers = [Pacer(node.sink, bursts, duty=duty) if duty > 0 else None for node in nodes]
    before = {node.serial: (node.overflows(), node.drain.samples) for node in nodes}
    cpu_before = os.times()
    load = hw_common.loadavg()
    started = time.monotonic()
    for pacer in pacers:
        if pacer is not None:
            pacer.start()
    time.sleep(seconds)
    for pacer in pacers:
        if pacer is not None:
            pacer.finish()
    elapsed = time.monotonic() - started
    cpu_after = os.times()
    record = {
        "kind": "phase",
        "phase": label,
        "duty_requested": duty,
        "seconds": round(elapsed, 3),
        "loadavg": load,
        "process_cpu_s": round(
            (cpu_after.user - cpu_before.user) + (cpu_after.system - cpu_before.system), 2
        ),
        "nodes": {},
    }
    for node, pacer in zip(nodes, pacers, strict=True):
        overflow_before, samples_before = before[node.serial]
        events = node.overflows() - overflow_before
        record["nodes"][node.serial[-6:]] = {
            "serial": node.serial,
            "rx_kernel_buffers": node.kernel_buffers,
            "startup_overflows_3s": node.startup_overflows,
            "overflow_events": events,
            "overflows_per_min": round(events / elapsed * 60.0, 3),
            "rx_samples_per_s": round((node.drain.samples - samples_before) / elapsed, 1),
            "tx_bursts": None if pacer is None else pacer.sent,
            "tx_duty_achieved": None if pacer is None else pacer.duty_achieved,
            "tx_max_push_ms": None if pacer is None else round(pacer.max_send_s * 1e3, 2),
            "tx_error": None if pacer is None else pacer.error,
            "device": node.snapshot(),
        }
    return record


def probe_double_open(devices, serial: str, dry: bool) -> dict:
    """A second context on a Pluto whose first context streams RX."""

    if dry:
        return {"kind": "double_open", "dry_run": True}
    from ofdm_message_link import pluto

    device = devices[serial]
    source = pluto.PlutoSampleSource(
        pluto.PlutoSettings(device.address, serial, SAMPLE_RATE, 2.44e9, 15.0)
    )
    source.start()
    drain = Drain(source)
    drain.start()
    result: dict[str, object] = {"kind": "double_open", "serial": serial}
    try:
        time.sleep(1.0)
        try:
            second = pluto.IioContext(device.address)
        except OSError as error:
            result.update(second_open="failed", error=str(error))
        else:
            result.update(second_open="succeeded")
            before = drain.samples
            time.sleep(2.0)
            result["first_stream_samples_per_s_after"] = (drain.samples - before) / 2.0
            result["first_stream_error"] = source.snapshot()["stream_error"]
            second.close()
    finally:
        drain.close()
        source.stop()
    return result


def run(args) -> int:
    token = None if args.dry_run else hw_common.acknowledge(args)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    records = out / "records.jsonl"
    meta = hw_common.base_meta(args)
    meta["burst_samples"] = BURST_SAMPLES
    meta["tx_gain_db"] = MIN_TX_GAIN_DB
    logger = hw_common.LoadLogger(out / "loadavg.log").start()
    serials = (args.node_a, args.node_b)
    try:
        lock = hw_common.hardware_lock() if not args.dry_run else _Null()
        with lock:
            meta["loadavg_gate"] = (
                hw_common.loadavg() if args.dry_run else hw_common.wait_for_load(args.max_load)
            )
            (out / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
            devices = (
                {}
                if args.dry_run
                else hw_common.with_retries("discover", lambda: hw_common.find_plutos(serials))[0]
            )
            if args.probe_double_open:
                hw_common.append_jsonl(
                    records, probe_double_open(devices, args.node_a, args.dry_run)
                )
            if "per-pluto" in args.parts:
                for serial in serials:
                    if not args.dry_run:
                        hw_common.wait_for_load(args.max_load)
                    node = open_nodes([serial], devices, token, args.kernel_buffers, args.dry_run)[
                        0
                    ]
                    try:
                        for duty in args.duties:
                            record = measure(
                                [node], duty, args.seconds, f"single_{serial[-6:]}_d{duty:g}"
                            )
                            hw_common.append_jsonl(records, record)
                            print(_line(record), flush=True)
                    finally:
                        node.close()
            if "both" in args.parts:
                for buffers in args.both_kernel_buffers:
                    if not args.dry_run:
                        hw_common.wait_for_load(args.max_load)
                    nodes = open_nodes(list(serials), devices, token, buffers, args.dry_run)
                    try:
                        record = measure(
                            nodes,
                            args.both_duty,
                            args.seconds,
                            f"both_d{args.both_duty:g}_kb{buffers}",
                        )
                        hw_common.append_jsonl(records, record)
                        print(_line(record), flush=True)
                    finally:
                        for node in nodes:
                            node.close()
    finally:
        logger.stop()
        meta["max_load1"] = logger.max_load1
        meta["invalid_load"] = logger.invalid
        meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        (out / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
    return summarize(argparse.Namespace(directory=out))


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def open_nodes(serials, devices, token, kernel_buffers, dry) -> list[Node]:
    def attempt():
        nodes = []
        try:
            for serial in serials:
                node = Node(serial, devices.get(serial), token, kernel_buffers, dry)
                node.open()
                nodes.append(node)
        except BaseException:
            for node in nodes:
                node.close()
            raise
        return nodes

    return hw_common.with_retries(f"open {[s[-6:] for s in serials]}", attempt)[0]


def _line(record: dict) -> str:
    parts = [f"{record['phase']:<24}"]
    for name, node in record["nodes"].items():
        parts.append(
            f"{name}: ovf {node['overflow_events']} ({node['overflows_per_min']}/min) "
            f"rx {node['rx_samples_per_s'] / 1e6:.3f} MS/s duty {node['tx_duty_achieved']}"
        )
    parts.append(f"cpu {record['process_cpu_s']} s load {record['loadavg'][0]}")
    return "  ".join(parts)


def summarize(args) -> int:
    directory: Path = args.directory
    records = hw_common.read_jsonl(directory / "records.jsonl")
    meta_path = directory / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    rows = []
    worst = 0.0
    for record in records:
        if record.get("kind") != "phase":
            continue
        for name, node in record["nodes"].items():
            worst = max(worst, node["overflows_per_min"])
            rows.append(
                {
                    "phase": record["phase"],
                    "pluto": name,
                    **{
                        key: node[key]
                        for key in (
                            "rx_kernel_buffers",
                            "overflow_events",
                            "overflows_per_min",
                            "startup_overflows_3s",
                            "rx_samples_per_s",
                            "tx_duty_achieved",
                            "tx_max_push_ms",
                        )
                    },
                }
            )
    verdict = (
        f"USB carries duplex streaming: overflows <= {ALLOWED_OVERFLOWS_PER_MIN}/min in every phase"
        if rows and worst <= ALLOWED_OVERFLOWS_PER_MIN
        else f"USB contention: up to {worst}/min overflows; see the table"
    )
    result = {
        "meta": meta,
        "records": records,
        "table": rows,
        "worst_overflows_per_min": worst,
        "verdict": verdict,
    }
    (directory / "usb_capacity.json").write_text(json.dumps(result, indent=1, default=str))
    lines = [
        "# M0 USB capacity",
        "",
        verdict,
        "",
        "| phase | Pluto | kernel buffers | overflows | /min | start-up (3 s) | RX MS/s "
        "| TX duty | max push ms |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['phase']} | {row['pluto']} | {row['rx_kernel_buffers']} | "
            f"{row['overflow_events']} | {row['overflows_per_min']} | "
            f"{row['startup_overflows_3s']} | {row['rx_samples_per_s'] / 1e6:.3f} | "
            f"{row['tx_duty_achieved']} | {row['tx_max_push_ms']} |"
        )
    double = [record for record in records if record.get("kind") == "double_open"]
    if double:
        lines += ["", f"Double open: `{json.dumps(double)}`"]
    lines += ["", "```", str(meta.get("lsusb_t", "")).rstrip(), "```"]
    (directory / "usb_summary.md").write_text("\n".join(lines) + "\n")
    print(verdict)
    return 0


def _floats(text: str) -> list[float]:
    return [float(value) for value in text.split(",") if value]


def _ints(text: str) -> list[int]:
    return [int(value) for value in text.split(",") if value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--out", type=Path, required=True)
    run_parser.add_argument("--node-a", default=hw_common.NODE_A_SERIAL)
    run_parser.add_argument("--node-b", default=hw_common.NODE_B_SERIAL)
    run_parser.add_argument("--seconds", type=float, default=60.0)
    run_parser.add_argument("--duties", type=_floats, default=[0.0, 0.5, 0.9])
    run_parser.add_argument(
        "--kernel-buffers",
        type=int,
        default=4,
        help="RX kernel buffers for the per-Pluto part (default 4)",
    )
    run_parser.add_argument("--both-duty", type=float, default=0.9)
    run_parser.add_argument("--both-kernel-buffers", type=_ints, default=[4, 8])
    run_parser.add_argument(
        "--parts", type=lambda text: text.split(","), default=["per-pluto", "both"]
    )
    run_parser.add_argument("--probe-double-open", action="store_true")
    run_parser.add_argument("--dry-run", action="store_true")
    hw_common.add_rf_arguments(run_parser)
    summary_parser = commands.add_parser("summarize")
    summary_parser.add_argument("directory", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args) if args.command == "run" else summarize(args)


if __name__ == "__main__":
    raise SystemExit(main())
