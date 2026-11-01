"""Count decoded frames and corrupt/error lines of the M4 video consumers; JSON to stdout.

python video_counts.py RUN_DIR
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def count_log(text: str) -> dict:
    lines = text.splitlines()
    corrupt = sum(1 for line in lines if re.search(r"corrupt", line, re.I))
    errors = sum(
        1
        for line in lines
        if re.search(r"error|invalid|missing|concealing", line, re.I)
        and not re.search(r"corrupt", line, re.I)
    )
    return {"corrupt_lines": corrupt, "error_lines": errors}


def last_progress(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key in ("frame", "drop_frames", "dup_frames", "out_time_us", "total_size"):
            out[key] = value.strip()
    return {k: int(v) for k, v in out.items() if re.fullmatch(r"-?\d+", v)}


def main(run: Path) -> dict:
    result = {}
    for direction in ("AB", "BA"):
        rx_log = run / f"video_{direction}_rx.log"
        prog = run / f"video_{direction}_progress.txt"
        entry = count_log(rx_log.read_text(errors="replace")) if rx_log.exists() else {}
        entry["progress"] = last_progress(prog.read_text(errors="replace")) if prog.exists() else {}
        entry["frames"] = entry["progress"].get("frame")
        result[direction] = entry
    return result


if __name__ == "__main__":
    print(json.dumps(main(Path(sys.argv[1])), indent=1))
