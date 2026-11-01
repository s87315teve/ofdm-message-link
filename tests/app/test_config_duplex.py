"""The optional ``duplex`` configuration section that node_app reads."""

from __future__ import annotations

import copy

import pytest

from ofdm_link.config import ConfigError, _read_yaml, load_config, validate_config

BASE = "configs/default.yaml"
FDD = "configs/profiles/duplex_fdd_2420_2460.yaml"


def _raw(duplex):
    raw = _read_yaml(BASE)
    raw = copy.deepcopy(raw)
    raw["phy"]["sample_rate"] = 5_000_000
    raw["duplex"] = duplex
    return raw


def _fdd(**overrides):
    section = {
        "mode": "fdd",
        "control_mcs_index": 8,
        "nodes": [
            {"node_id": 1, "tx_frequency": 2_420_000_000, "rx_frequency": 2_460_000_000},
            {"node_id": 2, "tx_frequency": 2_460_000_000, "rx_frequency": 2_420_000_000},
        ],
    }
    section.update(overrides)
    return section


@pytest.mark.parametrize("profile", [FDD, "configs/profiles/duplex_fdd_2405_2475.yaml"])
def test_fdd_profiles_load(profile):
    config = load_config(BASE, [profile])
    assert config.duplex.mode == "fdd"
    node = config.duplex.node(1)
    peer = config.duplex.peer_of(1)
    assert node.tx_frequency == peer.rx_frequency and node.rx_frequency == peer.tx_frequency
    assert config.to_dict()["duplex"]["nodes"][0]["node_id"] == 1


def test_configurations_without_the_section_have_none():
    assert load_config(BASE, ["configs/profiles/ota_2p45ghz.yaml"]).duplex is None


def test_lbt_is_accepted_with_one_frequency():
    config = validate_config(
        _raw(
            {
                "mode": "lbt",
                "nodes": [
                    {"node_id": 1, "tx_frequency": 2_440_000_000, "rx_frequency": 2_440_000_000},
                    {"node_id": 2, "tx_frequency": 2_440_000_000, "rx_frequency": 2_440_000_000},
                ],
                "lbt": {"slot_ms": 3.0, "cw_min": 4, "cw_max": 64},
            }
        )
    )
    assert config.duplex.mode == "lbt" and config.duplex.lbt.cw_max == 64
    assert config.duplex.control_mcs_index == 8


def test_swapped_frequencies_are_refused():
    section = _fdd()
    section["nodes"][1] = {
        "node_id": 2,
        "tx_frequency": 2_420_000_000,
        "rx_frequency": 2_460_000_000,
    }
    with pytest.raises(ConfigError, match="other node's rx_frequency"):
        validate_config(_raw(section))


def test_spacing_below_the_sample_rate_is_refused():
    section = _fdd(
        nodes=[
            {"node_id": 1, "tx_frequency": 2_440_000_000, "rx_frequency": 2_443_000_000},
            {"node_id": 2, "tx_frequency": 2_443_000_000, "rx_frequency": 2_440_000_000},
        ]
    )
    with pytest.raises(ConfigError, match="one sample"):
        validate_config(_raw(section))


def test_lbt_with_two_frequencies_is_refused():
    with pytest.raises(ConfigError, match="lbt needs"):
        validate_config(_raw(_fdd(mode="lbt")))


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda s: s.update(colour="red"), "unknown key"),
        (lambda s: s["nodes"][0].update(gain=3), "unknown key"),
        (lambda s: s["nodes"][1].update(node_id=1), "different node_id"),
        (lambda s: s.update(control_mcs_index=3), "between 8 and 17"),
        (lambda s: s["nodes"].pop(), "exactly two"),
        (lambda s: s.update(lbt={"slot_ms": 3}), "mode lbt only"),
    ],
)
def test_bad_sections_are_refused(mutate, message):
    section = _fdd()
    mutate(section)
    with pytest.raises(ConfigError, match=message):
        validate_config(_raw(section))
