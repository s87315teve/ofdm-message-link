"""Paced UDP load into tx_app's ingress; byte-exact check of what rx_app egresses.

Every datagram is HEADER + body, where the body is a deterministic slice of a
seeded random block chosen by the sequence number, so the receiver can verify
each delivered datagram byte for byte without keeping a copy of what was sent.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import threading
import time

import numpy as np

MAGIC = b"UTP1"
HEADER = struct.Struct("!4sIIQ")  # magic, run id, sequence, send time (ns, monotonic)
BLOCK = np.random.default_rng(20260924).integers(0, 256, 1 << 16, dtype=np.uint8).tobytes()


def body(seq: int, size: int) -> bytes:
    start = (seq * 131) % (len(BLOCK) - size)
    return BLOCK[start : start + size]


class Receiver(threading.Thread):
    def __init__(self, port: int, run_id: int, size: int) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.sock.bind(("127.0.0.1", port))
        self.sock.settimeout(0.1)
        self.run_id, self.size = run_id, size
        self.stop = threading.Event()
        self.seen: set[int] = set()
        self.arrivals: list[float] = []
        self.latency_ms: list[float] = []
        self.corrupt = self.duplicate = self.foreign = self.reordered = 0
        self._last_seq = -1

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                data, _ = self.sock.recvfrom(1 << 16)
            except TimeoutError:
                continue
            now = time.monotonic_ns()
            if len(data) < HEADER.size or data[:4] != MAGIC:
                self.foreign += 1
                continue
            _, run_id, seq, sent_ns = HEADER.unpack_from(data)
            if run_id != self.run_id:
                self.foreign += 1
                continue
            if len(data) != self.size or data[HEADER.size :] != body(seq, self.size - HEADER.size):
                self.corrupt += 1
                continue
            if seq in self.seen:
                self.duplicate += 1
                continue
            if seq < self._last_seq:
                self.reordered += 1
            self._last_seq = max(self._last_seq, seq)
            self.seen.add(seq)
            self.arrivals.append(now / 1e9)
            self.latency_ms.append((now - sent_ns) / 1e6)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, required=True, help="datagrams per second")
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--size", type=int, default=968)
    ap.add_argument("--ingress-port", type=int, default=52001)
    ap.add_argument("--egress-port", type=int, default=52022)
    ap.add_argument("--tail", type=float, default=2.0)
    ap.add_argument("--warmup", type=float, default=2.0, help="excluded from steady-state goodput")
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    run_id = int(time.time()) & 0xFFFFFFFF
    rx = Receiver(args.egress_port, run_id, args.size)
    rx.start()
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
    body_size = args.size - HEADER.size
    total = int(round(args.rate * args.duration))
    period = 1.0 / args.rate
    send_times: list[float] = []
    max_behind = 0.0
    t0 = time.monotonic()
    for seq in range(total):
        due = t0 + seq * period
        now = time.monotonic()
        if due > now:
            time.sleep(due - now)
        else:
            max_behind = max(max_behind, now - due)
        ns = time.monotonic_ns()
        tx.sendto(
            HEADER.pack(MAGIC, run_id, seq, ns) + body(seq, body_size),
            ("127.0.0.1", args.ingress_port),
        )
        send_times.append(ns / 1e9)
    t_end = time.monotonic()
    time.sleep(args.tail)
    rx.stop.set()
    rx.join()

    send_span = t_end - t0
    delivered = len(rx.seen)
    arrivals = np.array(rx.arrivals)
    lat = np.array(rx.latency_ms) if rx.latency_ms else np.array([np.nan])
    # Steady state: datagrams that *arrived* between the end of the warm-up and
    # the end of sending, over that window.  Counting by arrival time keeps the
    # TX queue draining after the sender stops out of an overloaded point.
    steady_lo = t0 + args.warmup
    steady_seqs = [a for a in rx.arrivals if steady_lo <= a <= t_end]
    steady_sent = sum(1 for t in send_times if t >= steady_lo)
    steady_span = t_end - steady_lo
    result = {
        "rate_offered": args.rate,
        "size": args.size,
        "duration": args.duration,
        "sent": total,
        "send_span_s": send_span,
        "sender_max_behind_ms": max_behind * 1e3,
        "offered_mbps": total * args.size * 8 / send_span / 1e6,
        "delivered": delivered,
        "lost": total - delivered,
        "loss_ratio": (total - delivered) / total,
        "corrupt": rx.corrupt,
        "duplicate": rx.duplicate,
        "reordered": rx.reordered,
        "foreign": rx.foreign,
        "goodput_mbps": delivered * args.size * 8 / send_span / 1e6,
        "steady_sent": steady_sent,
        "steady_delivered": len(steady_seqs),
        "steady_goodput_mbps": len(steady_seqs) * args.size * 8 / steady_span / 1e6,
        "latency_ms_p50": float(np.nanpercentile(lat, 50)),
        "latency_ms_p99": float(np.nanpercentile(lat, 99)),
        "latency_ms_max": float(np.nanmax(lat)),
        "arrival_span_s": float(arrivals[-1] - arrivals[0]) if arrivals.size > 1 else 0.0,
        "first_lost": sorted(set(range(total)) - rx.seen)[:20],
    }
    with open(args.output, "w") as fh:
        json.dump(result, fh, indent=1)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "rate_offered",
                    "offered_mbps",
                    "delivered",
                    "lost",
                    "loss_ratio",
                    "goodput_mbps",
                    "steady_goodput_mbps",
                    "corrupt",
                    "latency_ms_p50",
                    "latency_ms_p99",
                    "sender_max_behind_ms",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
