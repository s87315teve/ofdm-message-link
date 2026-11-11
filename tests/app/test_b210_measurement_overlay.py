"""The B210 measurement overlay switches only the legacy detector off."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from stream_fixtures import MEASUREMENT, PROFILE, deliver, profile, v5_stream

from ofdm_link.config import load_config

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def legacy_on_overlay():
    """The overlay that turns the legacy detector back on (v1.0 behaviour)."""

    return "configs/profiles/legacy_rx.yaml"


def test_v2_default_has_legacy_acquisition_off():
    assert not load_config("configs/default.yaml").phy.rx.legacy_acquisition
    assert load_config("configs/default.yaml").phy.rx.v5_acquisition


def test_overlay_turns_legacy_acquisition_off_and_nothing_else(legacy_on_overlay):
    base = load_config("configs/default.yaml", [PROFILE, legacy_on_overlay])
    measured = load_config("configs/default.yaml", [PROFILE, legacy_on_overlay, MEASUREMENT])
    assert base.phy.rx.legacy_acquisition and not measured.phy.rx.legacy_acquisition
    assert measured.phy.rx.v5_acquisition
    expected = base.to_dict()
    expected["phy"]["rx"]["legacy_acquisition"] = False
    assert measured.to_dict() == expected
    # Over the v2.0 default the overlay changes nothing.
    plain = load_config("configs/default.yaml", [PROFILE])
    assert load_config("configs/default.yaml", [PROFILE, MEASUREMENT]).to_dict() == plain.to_dict()


def test_same_delivery_with_and_without_the_legacy_detector(legacy_on_overlay):
    on, off = profile(legacy_on_overlay), profile(MEASUREMENT)
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
