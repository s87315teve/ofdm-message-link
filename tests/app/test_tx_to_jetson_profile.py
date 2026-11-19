"""The add-on profile for a PC transmitting to a Jetson changes only the burst interval."""

from __future__ import annotations

from pathlib import Path

import pytest

from ofdm_link.config import load_config

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / "configs" / "default.yaml"
TX_TO_JETSON = ROOT / "configs" / "profiles" / "tx_to_jetson.yaml"


@pytest.mark.parametrize("base", ["ota_2p45ghz.yaml", "duplex_fdd_2420_2460.yaml"])
def test_profile_sets_a_25_ms_burst_interval_and_nothing_else(base: str) -> None:
    profile = ROOT / "configs" / "profiles" / base
    plain = load_config(DEFAULT, [profile])
    with_tx = load_config(DEFAULT, [profile, TX_TO_JETSON])

    assert with_tx.mac.aggregation.min_burst_interval_ms == 25.0
    # The measured setting also had no max wait; that is the program default.
    assert with_tx.mac.aggregation.max_wait_ms == 0.0
    expected = plain.to_dict()
    expected["mac"]["aggregation"]["min_burst_interval_ms"] = 25.0
    assert with_tx.to_dict() == expected
