"""The experiment drivers build v2.x command lines (dry runs only; nothing transmits)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ofdm_message_link import options, tx_app

ROOT = Path(__file__).resolve().parents[2]
SWEEP = ROOT / "experiments" / "ota_udp_throughput" / "sweep.py"
DECODE_ALL = ROOT / "experiments" / "tx_v5" / "decode_all_v5.sh"


def _sweep_dry(*argv: str) -> list[list[str]]:
    out = subprocess.run(
        [sys.executable, str(SWEEP), "--dry-run", "--out", "/nonexistent",
         "--tx-serial", "TX", "--rx-serial", "RX", *argv],
        capture_output=True, text=True, check=True, cwd=ROOT,
    ).stdout
    return [line.split() for line in out.splitlines()]


def _tx_args(words: list[str]):
    """Parse a printed tx_app command with tx_app's own parser."""

    argv = words[words.index("ofdm_message_link.tx_app") + 1:]
    cut = argv.index("--acknowledgement")
    return tx_app.build_parser().parse_args([*argv[:cut], "--acknowledgement", "x"])


def test_sweep_passes_the_aggregation_options_to_tx_app():
    rx, *txs = _sweep_dry(
        "--mcs", "12,17", "--aggregation", "off", "--aggregation-max-subframes", "16",
        "--aggregation-max-wait-ms", "2", "--min-burst-interval-ms", "0",
        "--max-burst-airtime-ms", "5",
    )
    assert "ofdm_message_link.rx_app" in rx
    assert [_tx_args(words).mcs_index for words in txs] == [12, 17]
    args = _tx_args(txs[0])
    assert (args.aggregation, args.aggregation_max_subframes) == ("off", 16)
    assert (args.aggregation_max_wait_ms, args.min_burst_interval_ms) == (2.0, 0.0)
    assert args.max_burst_airtime_ms == 5.0


def test_sweep_without_aggregation_options_keeps_the_configured_defaults():
    (_, tx) = _sweep_dry("--mcs", "8")
    args = _tx_args(tx)
    assert args.aggregation is None and args.aggregation_max_subframes is None
    limits = options.resolve(args).aggregation
    assert limits.enabled and limits.max_subframes == 32 and limits.max_psdu_bytes == 16384


def test_sweep_refuses_to_transmit_without_enable_rf():
    result = subprocess.run(
        [sys.executable, str(SWEEP), "--out", "/nonexistent", "--tx-serial", "TX",
         "--rx-serial", "RX"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 2 and "--enable-rf" in result.stderr


def test_decode_all_turns_legacy_acquisition_on_only_for_the_legacy_captures():
    out = subprocess.run(
        ["bash", str(DECODE_ALL), "--dry-run"], capture_output=True, text=True, check=True,
        cwd=ROOT, env={"PATH": "/usr/bin:/bin", "CAPTURES": "/caps", "PYTHON": "py"},
    ).stdout.splitlines()
    legacy = [line for line in out if "caps_baseline" in line]
    v5 = [line for line in out if "caps_v5" in line]
    assert len(legacy) == 2 and len(v5) >= 4
    assert all("phy.rx.legacy_acquisition=true" in line for line in legacy)
    assert not any("legacy_acquisition" in line for line in v5)
