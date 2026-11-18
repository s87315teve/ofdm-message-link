"""Run the unmodified tx_app/rx_app over the air and sweep UDP load per MCS.

RX starts once and stays up (it reads the MCS from each burst header); TX is
restarted for every MCS because --mcs-index is a launch option.  This keys the
transmitting USRP, so it refuses to run without --enable-rf; --dry-run prints
the two app commands instead and starts nothing.

The aggregation options are tx_app's own (same names, ranges and defaults:
unset means mac.aggregation from the configuration) and are passed to every
TX started here, e.g. ``--aggregation off`` for one datagram per burst.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PY = sys.executable
ACK = "I acknowledge that this process will transmit RF"
# Airtime-only burst rate per wire-v5 MCS (the only indices the TX accepts) for a
# 968-byte datagram at 5 MS/s (airtime.py); --fractions scales these.  The real
# ceiling is lower: see README.md section 4.
CAP = {
    8: 238.5, 9: 351.1, 10: 463.0, 12: 672.0, 13: 868.1, 14: 961.5,
    15: 961.5, 16: 1225.5, 17: 1358.7,
}
DEFAULT_MCS = (8, 9, 10, 12, 13, 14)
# tx_app's aggregation options: (argparse dest, flag).
AGGREGATION_FLAGS = (
    ("aggregation", "--aggregation"),
    ("aggregation_max_subframes", "--aggregation-max-subframes"),
    ("aggregation_max_wait_ms", "--aggregation-max-wait-ms"),
    ("min_burst_interval_ms", "--min-burst-interval-ms"),
    ("max_burst_airtime_ms", "--max-burst-airtime-ms"),
)


def env() -> dict:
    e = dict(os.environ)
    e.update(QT_QPA_PLATFORM="offscreen", PYTHONUNBUFFERED="1")
    e.setdefault("UHD_IMAGES_DIR", "/tmp/ofdm_uhd_4_10_images")
    return e


def wait_ready(path: Path, proc: subprocess.Popen, name: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited with {proc.returncode} before its radio started")
        if time.monotonic() > deadline:
            raise RuntimeError(f"{name} radio did not start within {timeout} s")
        time.sleep(0.25)


def stop(proc: subprocess.Popen | None, grace: float = 10) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)


def last_stats(path: Path) -> dict:
    lines = path.read_text().splitlines() if path.exists() else []
    for line in reversed(lines):
        record = json.loads(line)
        if "receiver" in record:
            return record
    return {}


def flatten(d, prefix="") -> dict:
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(d, (int, float)) and not isinstance(d, bool):
        out[prefix[:-1]] = d
    return out


def delta(before: dict, after: dict) -> dict:
    b, a = flatten(before), flatten(after)
    return {
        k: a[k] - b.get(k, 0) for k in a if isinstance(a[k], (int, float)) and a[k] != b.get(k, 0)
    }


def tree_cpu_s(pid: int) -> float:
    """User+system CPU seconds of pid and all its live descendants."""
    total, tick = 0.0, os.sysconf("SC_CLK_TCK")
    todo = [pid]
    while todo:
        p = todo.pop()
        try:
            fields = Path(f"/proc/{p}/stat").read_text().rsplit(")", 1)[1].split()
            total += (int(fields[11]) + int(fields[12])) / tick
            for t in Path(f"/proc/{p}/task").iterdir():
                kids = (t / "children").read_text().split()
                todo.extend(int(k) for k in kids)
        except (FileNotFoundError, ProcessLookupError, IndexError):
            pass
    return total


def uhd_marks(text: str) -> dict:
    counts = {"U": 0, "L": 0, "O": 0, "S": 0}
    for token in re.findall(r"(?<![A-Za-z\[])([ULOS]+)(?![A-Za-z\]])", text):
        for ch in token:
            counts[ch] += 1
    return counts


def aggregation_args(args: argparse.Namespace) -> list[str]:
    """The tx_app aggregation options given on this command line, in tx_app's spelling."""

    out: list[str] = []
    for dest, flag in AGGREGATION_FLAGS:
        value = getattr(args, dest)
        if value is not None:
            out += [flag, f"{value:g}" if isinstance(value, float) else str(value)]
    return out


def rx_command(args: argparse.Namespace, overlays: list[str], rx_ready: Path,
               stats_log: Path) -> list[str]:
    return [
        PY, "-m", "ofdm_message_link.rx_app", *overlays,
        "--transport", "uhd", "--serial", args.rx_serial, "--channel", "0",
        "--antenna", "TX/RX", "--gain", str(args.rx_gain), "--auto-start",
        "--ready-file", str(rx_ready), "--egress-port", str(args.egress_port),
        "--stats-log", str(stats_log), "--stats-log-max-mb", "0",
    ]


def tx_command(args: argparse.Namespace, overlays: list[str], tx_ready: Path,
               mcs: int) -> list[str]:
    return [
        PY, "-m", "ofdm_message_link.tx_app", *overlays,
        "--transport", "uhd", "--serial", args.tx_serial, "--channel", "0",
        "--antenna", "TX/RX", "--gain", str(args.tx_gain), "--auto-start",
        "--ready-file", str(tx_ready), "--mcs-index", str(mcs),
        *aggregation_args(args),
        "--enable-rf", "--acknowledgement", ACK,
    ]


def build_parser() -> argparse.ArgumentParser:
    from ofdm_message_link import options

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--mcs",
        default=",".join(str(m) for m in DEFAULT_MCS),
        help=(
            "comma-separated wire-v5 indices for tx_app --mcs-index (8-17; 0-7 are refused). "
            f"Default {','.join(str(m) for m in DEFAULT_MCS)}"
        ),
    )
    ap.add_argument("--fractions", default="0.5,0.8,0.9,0.95,1.0,1.1")
    ap.add_argument(
        "--rates", default="", help="explicit rates (overrides --fractions), same for every MCS"
    )
    ap.add_argument("--duration", type=float, default=20)
    ap.add_argument("--size", type=int, default=968)
    ap.add_argument("--tx-serial", required=True, help="serial of the transmitting USRP")
    ap.add_argument("--rx-serial", required=True, help="serial of the receiving USRP")
    ap.add_argument(
        "--overlay",
        action="append",
        default=[],
        metavar="PATH",
        help="configuration overlay passed to both apps, repeatable (default: the apps' default)",
    )
    ap.add_argument("--tx-gain", type=float, default=5)
    ap.add_argument("--rx-gain", type=float, default=15)
    ap.add_argument("--egress-port", type=int, default=52022)
    ap.add_argument("--settle", type=float, default=3)
    ap.add_argument(
        "--enable-rf",
        action="store_true",
        help="required: every TX app started here transmits RF",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the RX command and one TX command per MCS, then exit (no radio, no files)",
    )
    options.add_aggregation_arguments(ap)
    return ap


def main(argv: list[str] | None = None) -> None:
    ap = build_parser()
    args = ap.parse_args(argv)
    mcs_list = [int(m) for m in args.mcs.split(",")]
    unknown = [m for m in mcs_list if m not in CAP]
    if not args.rates and unknown:
        ap.error(f"--fractions needs an airtime cap; none for MCS {unknown} (use --rates)")

    overlays = [part for path in args.overlay for part in ("--overlay", path)]
    out = Path(args.out)
    if args.dry_run:
        print(shlex.join(rx_command(args, overlays, out / "rx.ready", out / "rx_stats.jsonl")))
        for mcs in mcs_list:
            print(shlex.join(tx_command(args, overlays, out / "tx.ready", mcs)))
        return
    if not args.enable_rf:
        ap.error("refusing to transmit: pass --enable-rf to acknowledge that this keys the USRP")

    out.mkdir(parents=True, exist_ok=True)
    stats_log = out / "rx_stats.jsonl"
    rx_ready, tx_ready = out / "rx.ready", out / "tx.ready"
    for f in (rx_ready, tx_ready):
        f.unlink(missing_ok=True)
    results_path = out / "results.jsonl"
    meta = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "args": vars(args)}
    (out / "meta.json").write_text(json.dumps(meta, indent=1))

    rx = tx = None
    rx_log = open(out / "rx.log", "ab")
    try:
        rx = subprocess.Popen(
            rx_command(args, overlays, rx_ready, stats_log),
            cwd=REPO,
            env=env(),
            stdout=rx_log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        wait_ready(rx_ready, rx, "RX")
        print("RX ready:", rx_ready.read_text().strip(), flush=True)
        time.sleep(2)

        for mcs in mcs_list:
            tx_ready.unlink(missing_ok=True)
            tx_log_path = out / f"tx-mcs{mcs}.log"
            tx_log = open(tx_log_path, "ab")
            tx = subprocess.Popen(
                tx_command(args, overlays, tx_ready, mcs),
                cwd=REPO,
                env=env(),
                stdout=tx_log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            wait_ready(tx_ready, tx, f"TX MCS {mcs}")
            print(f"TX MCS {mcs} ready:", tx_ready.read_text().strip(), flush=True)
            time.sleep(args.settle)

            if args.rates:
                rates = [float(r) for r in args.rates.split(",")]
            else:
                rates = [round(CAP[mcs] * float(f)) for f in args.fractions.split(",")]
            for rate in rates:
                tag = f"m{mcs}-r{int(rate)}-{time.strftime('%H%M%S')}"
                before = last_stats(stats_log)
                tx_mark = tx_log_path.stat().st_size
                rx_mark = (out / "rx.log").stat().st_size
                cpu_rx0, cpu_tx0, w0 = tree_cpu_s(rx.pid), tree_cpu_s(tx.pid), time.monotonic()
                load = subprocess.run(
                    [
                        PY,
                        str(HERE / "udp_load.py"),
                        "--rate",
                        str(rate),
                        "--duration",
                        str(args.duration),
                        "--size",
                        str(args.size),
                        "--egress-port",
                        str(args.egress_port),
                        "--output",
                        str(out / f"{tag}.json"),
                    ],
                    capture_output=True,
                    text=True,
                )
                wall = time.monotonic() - w0
                cpu_rx, cpu_tx = (
                    (tree_cpu_s(rx.pid) - cpu_rx0) / wall,
                    (tree_cpu_s(tx.pid) - cpu_tx0) / wall,
                )
                time.sleep(1.0)  # let one more stats line land
                after = last_stats(stats_log)
                if load.returncode != 0:
                    print(load.stderr, file=sys.stderr)
                    raise RuntimeError(f"load generator failed at {tag}")
                result = json.loads((out / f"{tag}.json").read_text())
                with open(tx_log_path, "rb") as fh:
                    fh.seek(tx_mark)
                    tx_text = fh.read().decode(errors="replace")
                with open(out / "rx.log", "rb") as fh:
                    fh.seek(rx_mark)
                    rx_text = fh.read().decode(errors="replace")
                record = {
                    "tag": tag,
                    "mcs": mcs,
                    "rate": rate,
                    **result,
                    "rx_delta": delta(before, after),
                    "snr_mean_after": after.get("mean_effective_snr_db"),
                    "snr_2s_after": after.get("snr_2s"),
                    "link_state_after": after.get("link_state"),
                    "tx_uhd_marks": uhd_marks(tx_text),
                    "rx_uhd_marks": uhd_marks(rx_text),
                    "tx_log_tail": tx_text[-400:],
                    "rx_log_tail": rx_text[-400:],
                    "cpu_cores_rx": cpu_rx,
                    "cpu_cores_tx": cpu_tx,
                    "decode_pool_after": (after.get("transport") or {}).get("decode_pool"),
                    "tx_alive": tx.poll() is None,
                    "rx_alive": rx.poll() is None,
                }
                with open(results_path, "a") as fh:
                    fh.write(json.dumps(record) + "\n")
                d = record["rx_delta"]
                print(
                    f"MCS {mcs} rate {rate:>6}: offered {result['offered_mbps']:.3f} Mb/s  "
                    f"goodput {result['goodput_mbps']:.3f} "
                    f"(steady {result['steady_goodput_mbps']:.3f})  "
                    f"lost {result['lost']}/{result['sent']}  corrupt {result['corrupt']}  "
                    f"missing {d.get('receiver.missing_bursts', 0)}  "
                    f"snr {record['snr_mean_after']}  cpu rx {cpu_rx:.2f} tx {cpu_tx:.2f}  "
                    f"lat p50 {result['latency_ms_p50']:.0f} ms  tx {record['tx_uhd_marks']}",
                    flush=True,
                )
                if tx.poll() is not None or rx.poll() is not None:
                    raise RuntimeError("an app exited during the sweep")
                time.sleep(2)
            stop(tx)
            tx = None
            tx_log.close()
            time.sleep(2)
    finally:
        stop(tx)
        stop(rx)
        rx_log.close()


if __name__ == "__main__":
    main()
