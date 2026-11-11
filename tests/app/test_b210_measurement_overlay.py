"""The B210 measurement overlay switches only the legacy detector off."""

from __future__ import annotations

import subprocess
from pathlib import Path

from stream_fixtures import MEASUREMENT, PROFILE, deliver, profile, v5_stream

from ofdm_link.config import load_config

ROOT = Path(__file__).resolve().parents[2]


def test_overlay_turns_legacy_acquisition_off_and_nothing_else():
    base = load_config("configs/default.yaml", [PROFILE])
    measured = load_config("configs/default.yaml", [PROFILE, MEASUREMENT])
    assert base.phy.rx.legacy_acquisition and not measured.phy.rx.legacy_acquisition
    assert measured.phy.rx.v5_acquisition
    expected = base.to_dict()
    expected["phy"]["rx"]["legacy_acquisition"] = False
    assert measured.to_dict() == expected


def test_library_default_keeps_legacy_acquisition_on():
    assert load_config("configs/default.yaml").phy.rx.legacy_acquisition


def test_same_delivery_with_and_without_the_legacy_detector():
    on, off = profile(), profile(MEASUREMENT)
    for dc in (0.0, 0.003 + 0.002j):
        stream, payloads = v5_stream(on, seed=11, dc=dc)
        delivered_on = deliver(on, stream)
        delivered_off = deliver(off, stream)
        assert delivered_on == delivered_off
        assert len(delivered_off) == len(payloads)


def test_run_duplex_adds_the_overlay_for_uhd_only():
    script = ROOT / "experiments" / "duplex_fdd" / "run_duplex.sh"

    def dry(*args: str) -> str:
        return subprocess.run(
            ["bash", str(script), "--dry-run", "--out", "/nonexistent", *args],
            capture_output=True, text=True, check=True, cwd=ROOT,
        ).stdout

    assert MEASUREMENT in dry("--transport", "uhd")
    assert MEASUREMENT not in dry("--transport", "uhd", "--legacy-acquisition")
    assert MEASUREMENT not in dry()
