"""Write OUT/meta.json for a duplex run from environment variables (called by run_duplex.sh)."""

from __future__ import annotations

import json
import os
import subprocess

e = os.environ


def sh(*cmd: str) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, cwd=e["REPO"], timeout=10)
        return done.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {exc}"


def main() -> None:
    nodes = ("1", "2")
    meta = {
        "git_rev": sh("git", "rev-parse", "HEAD"),
        "git_dirty": bool(sh("git", "status", "--porcelain")),
        "serials": {n: e["SER" + n] for n in nodes},
        "overlay": e["OVERLAY"],
        "node_pids": {n: int(e["PID" + n]) for n in nodes},
        "gain_profile": {n: e["PROF" + n] or None for n in nodes},
        "tx_gain_db": {n: float(e["TXG" + n]) for n in nodes},
        "rx_gain_db": float(e["RX_GAIN"]),
        "mcs_index": {n: int(e["MCS" + n]) for n in nodes},
        "link_adaptation": {n: e["LA" + n] for n in nodes},
        "link_adaptation_args": {n: [a for a in e["LAARGS" + n].split("\n") if a] for n in nodes},
        "decode_workers": int(e["WORKERS"]),
        "load": {
            "AB": {"rate": float(e["RATE_AB"])},
            "BA": {"rate": float(e["RATE_BA"])},
            "size": int(e["SIZE"]),
            "duration_s": float(e["DURATION"]),
        },
        "loadavg_at_start": e["LOAD_START"],
        "lsusb_t": sh("lsusb", "-t"),
        "args": e["ARGV"],
        "startup_s": 3.0,
    }
    with open(os.path.join(e["OUT"], "meta.json"), "w") as handle:
        json.dump(meta, handle, indent=1)


if __name__ == "__main__":
    main()
