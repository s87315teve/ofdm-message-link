"""Minimal third-party consumer: print what a running receiver app delivers.

    python -m ofdm_message_link.udp_recv
"""

from __future__ import annotations

import argparse
import socket


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ofdm-message-udp-recv")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=52002)
    parser.add_argument("--binary", action="store_true", help="print byte counts, not text")
    args = parser.parse_args(argv)

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((args.host, args.port))
        print(f"listening on {args.host}:{args.port}")
        while True:
            payload, _ = sock.recvfrom(1 << 16)
            if args.binary:
                print(f"<{len(payload)} bytes>")
            else:
                print(payload.decode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
