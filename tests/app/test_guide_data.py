"""guide/data.js carries the aggregation caps tx_app resolves at each sample rate."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from ofdm_link.radio.uhd import SUPPORTED_SAMPLE_RATES

ROOT = Path(__file__).resolve().parents[2]
# guide/ is not an installed package: load the script by path, from any cwd.
_spec = importlib.util.spec_from_file_location("guide_build_data", ROOT / "guide" / "build_data.py")
build_data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_data)


def _guide_data() -> dict:
    text = (ROOT / "guide" / "data.js").read_text(encoding="utf-8")
    return json.loads(text.split("window.GUIDE_DATA = ", 1)[1].rstrip().rstrip(";"))


def test_data_js_rates_are_current_with_the_profiles():
    rates = _guide_data()["rates"]
    assert rates == build_data._rate_limits(build_data._resolve([]))
    assert [r["sampleRate"] for r in rates] == sorted(float(r) for r in SUPPORTED_SAMPLE_RATES)


def test_the_packet_lab_uses_5_ms_at_15_36_msps():
    by_rate = {r["sampleRate"]: r for r in _guide_data()["rates"]}
    assert by_rate[15_360_000.0]["maxBurstAirtimeMs"] == 5.0
    assert by_rate[15_360_000.0]["profile"] == "configs/profiles/b210_2p45ghz_15p36msps.yaml"
    assert by_rate[5_000_000.0]["isDefault"] and by_rate[5_000_000.0]["maxBurstAirtimeMs"] == 8.0
