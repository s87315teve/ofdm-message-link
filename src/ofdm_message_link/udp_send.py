"""Minimal third-party sender: hand bytes to a running transmitter app.

This is deliberately as small as a socket program can be.  It proves the
layering: the application does not import the PHY, does not know the MCS and
does not know whether a radio is involved.  It writes bytes to a UDP port.

    python -m ofdm_message_link.udp_send "Hello from USRP B210!"
    python -m ofdm_message_link.udp_send --file photo.jpg
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ofdm-message-udp-send")
    parser.add_argument("message", nargs="*", help="text to transmit")
    parser.add_argument("--file", default=None, help="send this file's bytes instead")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=52001)
    args = parser.parse_args(argv)

    if args.file:
        payload = Path(args.file).read_bytes()
    elif args.message:
        payload = " ".join(args.message).encode("utf-8")
    else:
        parser.error("give a message or --file")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(payload, (args.host, args.port))
    print(f"handed {len(payload)} bytes to {args.host}:{args.port}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
