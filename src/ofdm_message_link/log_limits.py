"""Size-capped log files for long demo runs.

A demo left running for hours must not fill the disk.  :class:`RotatingLog`
keeps one file at or below ``max_bytes`` and, when the next write would pass
that, renames it to ``PATH.1`` (older copies shift to ``PATH.2`` ...) and
starts a new ``PATH``.  At most ``backups`` old copies are kept, so the whole
log never exceeds ``(backups + 1) * max_bytes`` plus one record.  A record is
never split across two files, so every line of a JSON-lines log still parses.

Run as a module it serves ``scripts/run_ota_video_demo.sh``: ``copy`` bounds
the rx/tx/ffplay/ffmpeg output, and ``prune-runs`` deletes all but the newest
run directories::

    ffplay ... 2>&1 | python -m ofdm_message_link.log_limits \\
        copy --max-mb 4 --backups 1 ffplay.log
    python -m ofdm_message_link.log_limits \\
        prune-runs --keep 5 --current /tmp/ofdm_ota_video_demo/20250101-101500 \\
        /tmp/ofdm_ota_video_demo

This module imports nothing beyond the standard library, so the copying
process stays small.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import sys
from pathlib import Path

DEFAULT_MAX_MB = 16.0
DEFAULT_BACKUPS = 1
_MIB = 1024 * 1024
# The run directory names the demo script creates: date +%Y%m%d-%H%M%S.
RUN_NAME = re.compile(r"\d{8}-\d{6}")


def max_bytes_from_mb(value: float) -> int | None:
    """Megabytes (MiB) as bytes; 0 means no cap."""

    if value < 0:
        raise ValueError("the log size cap must be 0 (no cap) or positive")
    return None if value == 0 else max(1, int(value * _MIB))


class RotatingLog:
    """An append-only log file that rotates at ``max_bytes``.

    ``max_bytes=None`` keeps the plain append-forever behaviour.  Text is
    written as UTF-8; ``write`` also accepts bytes.
    """

    def __init__(self, path: str | os.PathLike[str], *, max_bytes: int | None,
                 backups: int = DEFAULT_BACKUPS) -> None:
        if max_bytes is not None and max_bytes < 1:
            raise ValueError("max_bytes must be positive or None")
        if backups < 0:
            raise ValueError("backups must be zero or positive")
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self.rotations = 0
        self._handle = self.path.open("ab")
        self._size = self._handle.tell()

    @property
    def size(self) -> int:
        """Bytes in the current file."""

        return self._size

    def write(self, data: str | bytes) -> int:
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        if (
            self.max_bytes is not None
            and self._size > 0
            and self._size + len(raw) > self.max_bytes
        ):
            self._rotate()
        self._handle.write(raw)
        self._size += len(raw)
        return len(data)

    def flush(self) -> None:
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    @property
    def closed(self) -> bool:
        return self._handle.closed

    def _rotate(self) -> None:
        self._handle.close()
        if self.backups == 0:
            self.path.unlink(missing_ok=True)
        else:
            for index in range(self.backups - 1, 0, -1):
                older = self.path.with_name(f"{self.path.name}.{index}")
                if older.exists():
                    older.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        self._handle = self.path.open("ab")
        self._size = 0
        self.rotations += 1


def copy_stream(source, log: RotatingLog, chunk_bytes: int = 65536) -> None:
    """Copy a binary stream into ``log`` until end of file."""

    fd = source.fileno()
    while True:
        chunk = os.read(fd, chunk_bytes)
        if not chunk:
            return
        log.write(chunk)
        log.flush()


def prune_runs(parent: Path, keep: int, current: Path | None = None) -> list[Path]:
    """Delete all but the newest ``keep`` run directories under ``parent``.

    Only directories named like the script's timestamps are candidates, so
    nothing else under ``parent`` is ever removed; ``current`` is always kept
    and counts towards ``keep``.  ``keep=0`` deletes nothing.  Returns the
    deleted directories.
    """

    if keep < 0:
        raise ValueError("keep must be zero or positive")
    if keep == 0 or not parent.is_dir():
        return []
    runs = sorted(
        (p for p in parent.iterdir()
         if p.is_dir() and not p.is_symlink() and RUN_NAME.fullmatch(p.name)),
        key=lambda p: p.name,
        reverse=True,
    )
    this_run = None if current is None else current.resolve()
    others = [p for p in runs if p.resolve() != this_run]
    # The current run takes one of the ``keep`` places when it is among them.
    removed = others[keep - (len(others) < len(runs)):]
    for path in removed:
        shutil.rmtree(path)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ofdm_message_link.log_limits",
        description="Bound the demo's log files and run directories.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prune = commands.add_parser("prune-runs", help="keep only the newest run directories")
    prune.add_argument("parent", type=Path, help="directory holding one directory per run")
    prune.add_argument("--keep", type=int, required=True, help="run directories to keep; 0 = all")
    prune.add_argument("--current", type=Path, default=None, help="this run; never deleted")
    copy = commands.add_parser("copy", help="copy standard input into a capped log file")
    copy.add_argument("path", help="log file to write")
    copy.add_argument(
        "--max-mb",
        type=float,
        default=DEFAULT_MAX_MB,
        help=(
            "rotate when the file would pass this many MiB; 0 = no cap "
            f"(default: {DEFAULT_MAX_MB:g})"
        ),
    )
    copy.add_argument(
        "--backups",
        type=int,
        default=DEFAULT_BACKUPS,
        help=f"rotated copies to keep as PATH.1, PATH.2 ... (default: {DEFAULT_BACKUPS})",
    )
    args = parser.parse_args(argv)
    if args.command == "prune-runs":
        try:
            removed = prune_runs(args.parent, args.keep, args.current)
        except ValueError as error:
            parser.error(str(error))
        for path in removed:
            print(f"removed old run {path}")
        return 0
    try:
        log = RotatingLog(args.path, max_bytes=max_bytes_from_mb(args.max_mb), backups=args.backups)
    except ValueError as error:
        parser.error(str(error))
    # Ctrl-C reaches the whole process group; the writer decides when to stop,
    # and this copy ends at its end of file so no last message is lost.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        copy_stream(sys.stdin.buffer, log)
    finally:
        log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
