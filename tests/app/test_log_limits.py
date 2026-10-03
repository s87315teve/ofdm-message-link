"""Size caps that keep a long demo run from filling the disk."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ofdm_message_link.log_limits import RotatingLog, max_bytes_from_mb, prune_runs

_ROOT = Path(__file__).resolve().parents[2]


def _total(path: Path) -> int:
    return sum(p.stat().st_size for p in path.parent.glob(path.name + "*"))


def test_rotation_keeps_every_file_under_the_cap_and_lines_whole(tmp_path):
    path = tmp_path / "rx_stats.jsonl"
    log = RotatingLog(path, max_bytes=1000, backups=2)
    records = [json.dumps({"n": n, "pad": "x" * 40}) + "\n" for n in range(500)]
    for record in records:
        log.write(record)
    log.close()

    files = [path, path.with_name("rx_stats.jsonl.1"), path.with_name("rx_stats.jsonl.2")]
    assert all(f.stat().st_size <= 1000 for f in files)
    assert not path.with_name("rx_stats.jsonl.3").exists()
    assert _total(path) <= 3 * 1000
    # Every kept line parses, and the newest record is last in PATH.
    kept = [json.loads(line) for f in reversed(files) for line in f.read_text().splitlines()]
    numbers = [record["n"] for record in kept]
    assert numbers == sorted(numbers) and numbers[-1] == 499
    assert log.rotations > 0


def test_no_cap_appends_forever_like_a_plain_file(tmp_path):
    path = tmp_path / "log"
    path.write_text("earlier run\n")
    log = RotatingLog(path, max_bytes=None)
    for _ in range(1000):
        log.write("0123456789\n")
    log.close()
    assert path.read_text().startswith("earlier run\n")
    assert path.stat().st_size == 12 + 11_000
    assert list(tmp_path.iterdir()) == [path]


def test_zero_backups_truncates_instead_of_keeping_a_copy(tmp_path):
    path = tmp_path / "ffplay.log"
    log = RotatingLog(path, max_bytes=100, backups=0)
    for n in range(50):
        log.write(f"line {n:04d}\n")
    log.close()
    assert list(tmp_path.iterdir()) == [path]
    assert path.stat().st_size <= 100
    assert path.read_text().splitlines()[-1] == "line 0049"


def test_an_oversized_record_is_written_whole_to_a_fresh_file(tmp_path):
    path = tmp_path / "log"
    log = RotatingLog(path, max_bytes=10, backups=1)
    log.write("small\n")
    log.write("x" * 50 + "\n")
    log.close()
    assert path.read_text() == "x" * 50 + "\n"
    assert path.with_name("log.1").read_text() == "small\n"


def test_reopening_continues_from_the_existing_size(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(b"a" * 90)
    log = RotatingLog(path, max_bytes=100, backups=1)
    assert log.size == 90
    log.write(b"b" * 20)
    log.close()
    assert path.read_bytes() == b"b" * 20
    assert path.with_name("log.1").read_bytes() == b"a" * 90


@pytest.mark.parametrize("value, expected", [(0, None), (1, 1024 * 1024), (0.5, 524288)])
def test_megabytes_convert_with_zero_meaning_no_cap(value, expected):
    assert max_bytes_from_mb(value) == expected


def test_negative_caps_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        max_bytes_from_mb(-1)
    with pytest.raises(ValueError):
        RotatingLog(tmp_path / "log", max_bytes=0)
    with pytest.raises(ValueError):
        RotatingLog(tmp_path / "log", max_bytes=10, backups=-1)


def test_command_line_copies_a_stream_into_a_capped_file(tmp_path):
    """The form the demo script pipes rx/tx/ffplay/ffmpeg output through."""

    path = tmp_path / "ffplay.log"
    payload = b"".join(b"status line %06d\r" % n for n in range(200_000))  # about 3.6 MiB
    result = subprocess.run(
        [sys.executable, "-m", "ofdm_message_link.log_limits",
         "copy", "--max-mb", "1", "--backups", "1", str(path)],
        input=payload,
        cwd=_ROOT,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert path.stat().st_size <= 1024 * 1024
    assert path.with_name("ffplay.log.1").stat().st_size <= 1024 * 1024
    assert not path.with_name("ffplay.log.2").exists()
    # The newest output is kept.
    assert path.read_bytes().endswith(b"status line 199999\r")


def _runs(parent: Path, names: list[str]) -> None:
    for name in names:
        (parent / name).mkdir(parents=True)
        (parent / name / "rx.log").write_text(name)


def test_prune_keeps_the_newest_runs_and_nothing_else_is_touched(tmp_path):
    names = [f"20260923-10{minute:02d}00" for minute in range(8)]
    _runs(tmp_path, names)
    (tmp_path / "notes").mkdir()
    (tmp_path / "20260923-1000.txt").write_text("not a run")
    current = tmp_path / names[-1]

    removed = prune_runs(tmp_path, keep=3, current=current)

    assert sorted(p.name for p in removed) == names[:5]
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["20260923-1000.txt", *names[5:], "notes"]


def test_prune_never_deletes_the_current_run_even_when_it_is_older(tmp_path):
    names = ["20260923-100000", "20260923-110000", "20260923-120000"]
    _runs(tmp_path, names)
    removed = prune_runs(tmp_path, keep=2, current=tmp_path / names[0])
    assert [p.name for p in removed] == ["20260923-110000"]
    assert (tmp_path / names[0]).is_dir() and (tmp_path / names[2]).is_dir()


def test_prune_with_zero_keeps_everything_and_a_missing_parent_is_fine(tmp_path):
    _runs(tmp_path, ["20260923-100000", "20260923-110000"])
    assert prune_runs(tmp_path, keep=0) == []
    assert len(list(tmp_path.iterdir())) == 2
    assert prune_runs(tmp_path / "absent", keep=1) == []
    with pytest.raises(ValueError):
        prune_runs(tmp_path, keep=-1)


def test_prune_command_line_reports_what_it_removed(tmp_path):
    _runs(tmp_path, ["20260923-100000", "20260923-110000", "20260923-120000"])
    result = subprocess.run(
        [sys.executable, "-m", "ofdm_message_link.log_limits", "prune-runs",
         "--keep", "1", "--current", str(tmp_path / "20260923-120000"), str(tmp_path)],
        cwd=_ROOT, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("removed old run") == 2
    assert [p.name for p in tmp_path.iterdir()] == ["20260923-120000"]
