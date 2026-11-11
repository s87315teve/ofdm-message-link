"""15.36 MS/s on a USRP B210: allowed for uhd only, with a 30.72 MHz master clock."""

from __future__ import annotations

from pathlib import Path

import pytest

from ofdm_link.config import ConfigError, load_config
from ofdm_link.radio import RF_ENABLE_ACKNOWLEDGEMENT, acknowledge_rf_transmission
from ofdm_link.radio.uhd import (
    MASTER_CLOCK_RATES,
    SUPPORTED_SAMPLE_RATES,
    B210Settings,
    block_device_args,
    create_b210_sink,
    create_b210_source,
)
from ofdm_message_link.link import build_phy_profile
from ofdm_message_link.node_engine import UhdDuplexFactory, default_uhd_tx_bandwidth
from ofdm_message_link.transport import UhdSampleSource

ROOT = Path(__file__).resolve().parents[2]
DUPLEX = "configs/profiles/duplex_fdd_2420_2460_15p36.yaml"
ONE_WAY = "configs/profiles/b210_2p45ghz_15p36msps.yaml"
RATE = 15_360_000


class _Block:
    def __init__(self, *args):
        self.args = args
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))

        return record


class _Uhd:
    def stream_args(self, **kwargs):
        return kwargs

    def usrp_sink(self, *args):
        return _Block(*args)

    def usrp_source(self, *args):
        return _Block(*args)

    def tune_request(self, target, lo_off):
        return ("tune_request", target, lo_off)


def test_the_rate_is_supported_and_12_5_is_not():
    assert RATE in SUPPORTED_SAMPLE_RATES and 12_500_000 not in SUPPORTED_SAMPLE_RATES
    # A clean integer decimation, at a master clock a B210 allows with two channels.
    clock = MASTER_CLOCK_RATES[RATE]
    assert clock % RATE == 0 and clock // RATE == 2 and clock <= 30_720_000


def test_blocks_open_with_the_master_clock_only_at_15_36():
    uhd = _Uhd()
    fast = B210Settings(device_args="serial=30FE782", sample_rate=RATE, bandwidth=RATE)
    source = create_b210_source(fast, recv_frames=1024, uhd_api=uhd)
    assert source.args[0] == "serial=30FE782,master_clock_rate=30720000,num_recv_frames=1024"
    assert ("set_samp_rate", (RATE,)) in source.calls
    sink = create_b210_sink(
        B210Settings(device_args="serial=30FE782", sample_rate=RATE, bandwidth=40e6,
                     lo_offset=8e6),
        acknowledge_rf_transmission(RF_ENABLE_ACKNOWLEDGEMENT),
        uhd_api=uhd,
    )
    assert sink.args[0] == "serial=30FE782,master_clock_rate=30720000"
    for rate in (5_000_000, 10_000_000, 20_000_000):
        settings = B210Settings(device_args="serial=30FE782", sample_rate=rate)
        assert block_device_args(settings) == "serial=30FE782"  # UHD still chooses
    assert block_device_args(B210Settings(sample_rate=RATE)) == "master_clock_rate=30720000"


def test_the_receive_buffers_scale_with_the_rate():
    source = UhdSampleSource(B210Settings(sample_rate=RATE, bandwidth=RATE))
    assert source._recv_frames == 1024 and source._chunk_samples == 4 * 65536


@pytest.mark.parametrize("profile", [DUPLEX, ONE_WAY])
def test_the_profiles_load_and_keep_bursts_inside_the_timing_correction(profile):
    config = load_config("configs/default.yaml", [profile])
    assert config.phy.sample_rate == RATE and config.radio.adapter == "uhd"
    limit_s = 80_000 / RATE
    assert config.mac.aggregation.max_burst_airtime_ms / 1e3 <= limit_s
    assert build_phy_profile(config).sample_rate == RATE


def test_the_8_ms_default_airtime_cap_is_refused_at_15_36(tmp_path):
    overlay = tmp_path / "rate.yaml"
    overlay.write_text(
        "phy:\n  sample_rate: 15360000\nradio:\n  adapter: uhd\n  bandwidth: 15360000\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="max_burst_airtime_ms|80 000|samples"):
        load_config("configs/default.yaml", [str(overlay)])


def test_other_adapters_still_refuse_it(tmp_path):
    overlay = tmp_path / "pluto.yaml"
    overlay.write_text(
        "phy:\n  sample_rate: 15360000\nmac:\n  aggregation:\n    max_burst_airtime_ms: 5.0\n"
        "radio:\n  adapter: uhd\n  bandwidth: 15360000\n",
        encoding="utf-8",
    )
    load_config("configs/default.yaml", [str(overlay)])  # uhd: accepted
    from ofdm_message_link.pluto import PlutoSettings

    def pluto(rate):
        return PlutoSettings(uri="ip:192.168.2.1", serial="1044", sample_rate=rate,
                             center_frequency=2.45e9, gain_db=-20.0)

    pluto(5_000_000)
    with pytest.raises(Exception, match="sample"):
        pluto(RATE)


def test_the_node_widens_the_tx_filter_only_at_15_36():
    assert default_uhd_tx_bandwidth(RATE) == 40e6
    for rate in (5_000_000, 10_000_000, 20_000_000):
        assert default_uhd_tx_bandwidth(rate) == 25e6
    tx, rx = UhdDuplexFactory(
        serial="30FE782", sample_rate=RATE, tx_frequency=2.42e9, rx_frequency=2.46e9,
        tx_gain_db=70.0, rx_gain_db=30.0, acknowledgement=RF_ENABLE_ACKNOWLEDGEMENT,
        tx_bandwidth=default_uhd_tx_bandwidth(RATE),
    ).settings()
    assert tx.sample_rate == rx.sample_rate == RATE
    # The LO-offset signal reaches 8 + 7.68 MHz from the LO: inside a 40 MHz filter.
    assert tx.lo_offset + RATE / 2 < tx.bandwidth / 2
    assert rx.bandwidth == RATE
