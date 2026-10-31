"""Configuration keys for wire v5 and what the factory builds from them."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from ofdm_link.config import (
    AggregationConfigSection,
    ConfigError,
    RxConfigSection,
    V5TxConfigSection,
    load_config,
    validate_config,
)
from ofdm_link.phy import V5_WIRE_VERSION, mcs_entry
from ofdm_link.runtime.factory import build_burst_config, select_frame_decoder_for_entry

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / "configs" / "default.yaml"


def _raw() -> dict:
    return yaml.safe_load(DEFAULT.read_text(encoding="utf-8"))


def test_default_yaml_states_the_library_defaults() -> None:
    config = load_config(DEFAULT)

    assert config.phy.v5 == V5TxConfigSection()
    assert config.phy.rx == RxConfigSection()
    assert config.mac.aggregation == AggregationConfigSection()
    assert (config.phy.v5.guard_pre_samples, config.phy.v5.guard_post_samples) == (16, 64)


@pytest.mark.parametrize("profile", sorted((ROOT / "configs" / "profiles").glob("*.yaml")))
def test_every_profile_still_loads(profile: Path) -> None:
    assert load_config(DEFAULT, [profile]).phy.rx.v5_acquisition


def test_sections_are_optional_so_older_files_keep_loading() -> None:
    raw = _raw()
    del raw["phy"]["v5"], raw["phy"]["rx"], raw["mac"]["aggregation"]

    config = validate_config(raw)

    assert config.phy.rx == RxConfigSection()
    assert config.mac.aggregation == AggregationConfigSection()


def test_burst_config_carries_every_receive_key() -> None:
    raw = _raw()
    raw["phy"]["rx"].update(
        {
            "legacy_acquisition": False,
            "v5_detection_threshold": 0.12,
            "v5_stf_threshold": 0.3,
            "v5_correlation_threshold": 0.35,
            "v5_balance_ratio": 0.6,
            "acquisition_prefilter": True,
            "ltf_average": False,
            "channel_smoothing": False,
            "llr_noise_source": "ltf",
            "max_psdu_bytes": 9000,
            "turbo_max_iterations": 4,
        }
    )

    burst = build_burst_config(validate_config(raw))

    rx = burst.v5
    assert not rx.legacy_acquisition and rx.v5_acquisition
    assert rx.acquisition.detection_threshold == 0.12
    assert rx.acquisition.stf_threshold == 0.3
    assert rx.acquisition.correlation_threshold == 0.35
    assert rx.acquisition.balance_ratio == 0.6
    assert rx.acquisition.prefilter
    assert not rx.ltf_average and not rx.channel_smoothing
    assert rx.llr_noise_source == "ltf"
    assert rx.max_psdu_bytes == 9000 and rx.turbo_max_iterations == 4


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("v5", "guard_pre_samples", 257),
        ("v5", "guard_post_samples", -1),
        ("v5", "scrambler", "yes"),
        ("v5", "unknown", 1),
        ("rx", "v5_detection_threshold", 0.0),
        ("rx", "v5_correlation_threshold", 1.5),
        ("rx", "llr_noise_source", "median"),
        ("rx", "turbo_max_iterations", 5),
        ("rx", "max_psdu_bytes", 10),
        ("rx", "unknown", True),
    ],
)
def test_bad_phy_values_are_rejected(section: str, key: str, value: object) -> None:
    raw = _raw()
    raw["phy"][section][key] = value

    with pytest.raises(ConfigError):
        validate_config(raw)


def test_both_acquisition_families_cannot_be_switched_off() -> None:
    raw = _raw()
    raw["phy"]["rx"]["legacy_acquisition"] = False
    raw["phy"]["rx"]["v5_acquisition"] = False

    with pytest.raises(ConfigError, match="acquisition family"):
        validate_config(raw)


@pytest.mark.parametrize(
    ("key", "value"),
    [("max_subframes", 0), ("max_psdu_bytes", 18), ("max_burst_airtime_ms", 0.0), ("x", 1)],
)
def test_bad_aggregation_values_are_rejected(key: str, value: object) -> None:
    raw = _raw()
    raw["mac"]["aggregation"][key] = value

    with pytest.raises(ConfigError):
        validate_config(raw)


@pytest.mark.parametrize("rate", ["1/3", "1/2", "2/3", "3/4"])
def test_rate_matched_turbo_scheme_accepts_the_v5_rates(rate: str) -> None:
    raw = copy.deepcopy(_raw())
    raw["phy"]["fec"].update({"scheme": "turbo_rm", "code_rate": rate, "decision_mode": "turbo"})
    raw["phy"]["mcs"] = "qam64"

    assert validate_config(raw).phy.fec.code_rate == rate


def test_v5_only_rates_and_64qam_need_the_rate_matched_scheme() -> None:
    raw = _raw()
    raw["phy"]["fec"]["code_rate"] = "2/3"
    with pytest.raises(ConfigError):
        validate_config(raw)
    raw = _raw()
    raw["phy"]["mcs"] = "qam64"
    with pytest.raises(ConfigError):
        validate_config(raw)


def test_a_v5_entry_selects_the_v5_family() -> None:
    config = load_config(DEFAULT)

    assert select_frame_decoder_for_entry(config, mcs_entry(13)).wire_version == V5_WIRE_VERSION
    assert select_frame_decoder_for_entry(config, mcs_entry(0)).wire_version == 2
