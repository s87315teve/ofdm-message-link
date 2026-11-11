"""node_app on a USRP B210 (--transport uhd), without hardware.

The factory, the B210 gain rules of the command line and the TX LO offset are
checked against fakes; the over-the-air run is in experiments/duplex_fdd/README.md.
"""

from __future__ import annotations

import pickle

import pytest

from ofdm_link.radio import acknowledge_rf_transmission
from ofdm_link.radio.uhd import (
    RF_ENABLE_ACKNOWLEDGEMENT,
    B210Settings,
    create_b210_sink,
    create_b210_source,
)
from ofdm_message_link import node_app, node_options
from ofdm_message_link.node_engine import (
    DEFAULT_UHD_TX_BANDWIDTH_HZ,
    DEFAULT_UHD_TX_LO_OFFSET_HZ,
    UhdDuplexFactory,
)

ACK = RF_ENABLE_ACKNOWLEDGEMENT


# --------------------------------------------------------------------------
# radio layer: the TX LO offset
# --------------------------------------------------------------------------


class _Block:
    def __init__(self, *args):
        self.args = args
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))

        return record


class _Uhd:
    def __init__(self):
        self.tunes = []

    def stream_args(self, **kwargs):
        return kwargs

    def usrp_sink(self, *args):
        return _Block(*args)

    def usrp_source(self, *args):
        return _Block(*args)

    def tune_request(self, target, lo_off):
        self.tunes.append((target, lo_off))
        return ("tune_request", target, lo_off)


def _center(block):
    return [args[0] for name, args in block.calls if name == "set_center_freq"]


def test_a_lo_offset_tunes_through_a_tune_request():
    uhd = _Uhd()
    settings = B210Settings(device_args="serial=30FE782", sample_rate=5_000_000,
                            center_frequency=2.42e9, tx_gain=65.0, bandwidth=25e6,
                            lo_offset=8e6)
    sink = create_b210_sink(settings, acknowledge_rf_transmission(ACK), uhd_api=uhd)
    assert _center(sink) == [("tune_request", 2.42e9, 8e6)]
    assert uhd.tunes == [(2.42e9, 8e6)]
    assert ("set_bandwidth", (25e6, 0)) in sink.calls


def test_without_a_lo_offset_the_block_tunes_directly():
    uhd = _Uhd()
    settings = B210Settings(device_args="serial=3150380", sample_rate=5_000_000,
                            center_frequency=2.46e9, channel=1)
    source = create_b210_source(settings, uhd_api=uhd)
    assert _center(source) == [2.46e9] and uhd.tunes == []


@pytest.mark.parametrize("offset", [29e6, -29e6, float("nan")])
def test_a_lo_offset_outside_28_mhz_is_refused(offset):
    with pytest.raises(ValueError):
        B210Settings(lo_offset=offset)


# --------------------------------------------------------------------------
# UhdDuplexFactory
# --------------------------------------------------------------------------


def _factory(**overrides):
    values = dict(serial="30FE782", sample_rate=5_000_000, tx_frequency=2.42e9,
                  rx_frequency=2.46e9, tx_gain_db=65.0, rx_gain_db=30.0, acknowledgement=ACK)
    values.update(overrides)
    return UhdDuplexFactory(**values)


def test_the_factory_puts_tx_on_rf_a_and_rx_on_rf_b_by_default():
    tx, rx = _factory().settings()
    assert tx.device_args == rx.device_args == "serial=30FE782"
    assert (tx.channel, rx.channel) == (0, 1)
    assert (tx.center_frequency, rx.center_frequency) == (2.42e9, 2.46e9)
    assert tx.lo_offset == DEFAULT_UHD_TX_LO_OFFSET_HZ == 8e6 and rx.lo_offset == 0.0
    assert tx.bandwidth == DEFAULT_UHD_TX_BANDWIDTH_HZ == 25e6 and rx.bandwidth == 5e6
    assert (tx.tx_gain, rx.rx_gain) == (65.0, 30.0)
    assert tx.tx_antenna == "TX/RX" and rx.rx_antenna == "TX/RX"


def test_the_factory_swaps_front_ends_and_takes_overrides():
    tx, rx = _factory(rx_channel=0, rx_antenna="RX2", rx_bandwidth=4e6, tx_lo_offset=0.0,
                      tx_bandwidth=10e6).settings()
    assert (tx.channel, rx.channel) == (1, 0) and rx.rx_antenna == "RX2"
    assert rx.bandwidth == 4e6 and tx.lo_offset == 0.0 and tx.bandwidth == 10e6


@pytest.mark.parametrize("overrides", [
    {"tx_gain_db": -5.0},       # a Pluto gain
    {"tx_gain_db": 95.0},
    {"rx_gain_db": 80.0},
    {"rx_channel": 2},
    {"sample_rate": 4_000_000},
    {"serial": "serial=30FE782"},
])
def test_the_factory_refuses_settings_a_b210_cannot_take(overrides):
    with pytest.raises((ValueError, TypeError)):
        _factory(**overrides).settings()


def test_the_factory_pickles_and_builds_sink_first_with_the_rf_token(monkeypatch):
    from ofdm_message_link import transport

    factory = _factory()
    assert pickle.loads(pickle.dumps(factory)) == factory
    built = []

    class Sink:
        def __init__(self, settings, token, *, peak_amplitude):
            built.append(("sink", settings.channel, settings.lo_offset, peak_amplitude))
            assert token is not None

    class Source:
        def __init__(self, settings, *, recv_frames):
            built.append(("source", settings.channel, recv_frames))

    monkeypatch.setattr(transport, "UhdSampleSink", Sink)
    monkeypatch.setattr(transport, "UhdSampleSource", Source)
    endpoint = factory()
    assert built == [("sink", 0, 8e6, 0.7), ("source", 1, None)]
    assert endpoint.shared is None and endpoint.snapshot() is None
    assert "30FE782" in endpoint.description and "RF B" in endpoint.description


def test_the_factory_refuses_a_wrong_acknowledgement():
    with pytest.raises(ValueError):
        _factory(acknowledgement="yes")()


# --------------------------------------------------------------------------
# the command line
# --------------------------------------------------------------------------


def _uhd_args(*extra):
    return ["--node-id", "1", "--transport", "uhd", "--headless", "--serial", "30FE782", *extra]


def test_uhd_is_accepted_with_b210_default_gains():
    args = node_options.parse_args(_uhd_args())
    assert args.transport == "uhd"
    assert (args.tx_gain, args.rx_gain) == (0.0, 30.0)


def test_pluto_and_udp_keep_their_gain_defaults():
    for transport in ("udp", "pluto"):
        args = node_options.parse_args(
            ["--node-id", "1", "--transport", transport, "--serial", "x"]
        )
        assert (args.tx_gain, args.rx_gain) == (-89.75, 15.0)


@pytest.mark.parametrize("flags", [
    ["--tx-gain", "-5"],          # a Pluto attenuation is not a B210 gain
    ["--tx-gain", "90"],
    ["--rx-gain", "77"],
    ["--gain-profile", "0:65,30:-20"],
    ["--channel", "2"],
    ["--antenna", "RX1"],
])
def test_uhd_refuses_values_outside_a_b210s_range(flags):
    with pytest.raises(SystemExit):
        node_options.parse_args(_uhd_args(*flags))


def test_a_pluto_gain_stays_refused_on_a_pluto():
    with pytest.raises(SystemExit):
        node_options.parse_args(["--node-id", "1", "--tx-gain", "65"])
    with pytest.raises(SystemExit):
        node_options.parse_args(["--node-id", "1", "--gain-profile", "0:65"])


def test_uhd_gain_profile_uses_the_b210_range():
    args = node_options.parse_args(_uhd_args("--tx-gain", "70", "--gain-profile", "0:70,30:40"))
    assert args.gain_profile == ((0.0, 70.0), (30.0, 40.0))


def test_headless_uhd_needs_a_serial():
    with pytest.raises(SystemExit):
        node_options.parse_args(["--node-id", "1", "--transport", "uhd", "--headless"])


def test_uhd_only_flags_are_refused_on_other_transports():
    for flag in ("--tx-lo-offset", "--tx-analog-bw"):
        with pytest.raises(SystemExit):
            node_options.parse_args(["--node-id", "1", flag, "1e6"])


def test_uhd_factory_from_the_command_line():
    args = node_options.parse_args(_uhd_args(
        "--tx-gain", "68", "--rx-gain", "28", "--enable-rf", "--acknowledgement", ACK,
    ))
    setup = node_options.resolve_node(args)
    factory = node_app.uhd_factory(args, setup, serial=args.serial,
                                   tx_gain_db=args.tx_gain, rx_gain_db=args.rx_gain)
    assert factory.serial == "30FE782" and factory.rx_channel == 1
    assert (factory.tx_frequency, factory.rx_frequency) == (2.42e9, 2.46e9)
    assert (factory.tx_gain_db, factory.rx_gain_db) == (68.0, 28.0)
    assert factory.tx_lo_offset == 8e6 and factory.tx_bandwidth == 25e6


def test_uhd_factory_takes_the_lo_offset_and_front_end_flags():
    args = node_options.parse_args(_uhd_args(
        "--node-id", "2", "--tx-lo-offset", "0", "--tx-analog-bw", "10e6", "--channel", "0",
        "--antenna", "RX2",
    ))
    setup = node_options.resolve_node(args)
    factory = node_app.uhd_factory(args, setup, serial=args.serial,
                                   tx_gain_db=args.tx_gain, rx_gain_db=args.rx_gain)
    assert (factory.tx_frequency, factory.rx_frequency) == (2.46e9, 2.42e9)
    assert factory.tx_lo_offset == 0.0 and factory.tx_bandwidth == 10e6
    assert factory.rx_channel == 0 and factory.rx_antenna == "RX2"



# --------------------------------------------------------------------------
# TX analog filter: wide enough for the LO-shifted signal at every rate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rate", "old", "new"),
    [(5_000_000, 25e6, 25e6), (10_000_000, 25e6, 29e6), (15_360_000, None, 35e6),
     (20_000_000, 25e6, 40e6)],
)
def test_the_default_tx_filter_covers_the_shifted_band(rate, old, new):
    from ofdm_message_link.node_engine import default_uhd_tx_bandwidth

    width = default_uhd_tx_bandwidth(rate)
    assert width == new
    # Half-width covers the LO offset plus half the sampled band.
    assert width / 2 >= DEFAULT_UHD_TX_LO_OFFSET_HZ + rate / 2
    if old is not None and old < new:
        assert old / 2 < DEFAULT_UHD_TX_LO_OFFSET_HZ + rate / 2  # the old 25 MHz cut the edge


def test_the_filter_follows_the_lo_offset_and_never_drops_below_25_mhz():
    from ofdm_message_link.node_engine import default_uhd_tx_bandwidth

    assert default_uhd_tx_bandwidth(10_000_000, 0.0) == 25e6
    assert default_uhd_tx_bandwidth(10_000_000, 12e6) == 38e6
    assert default_uhd_tx_bandwidth(20_000_000, 28e6) == 56e6  # capped at the AD9361 maximum


def test_node_app_uses_the_rate_default_and_the_override_reproduces_25_mhz():
    def factory(*extra):
        args = node_options.parse_args(
            ["--node-id", "1", "--transport", "uhd", "--serial", "30FE782", "--enable-rf",
             "--acknowledgement", ACK, "--overlay", "configs/profiles/duplex_fdd_2420_2460.yaml",
             "--overlay", str(_rate_overlay(10_000_000)), *extra])
        setup = node_options.resolve_node(args)
        return node_app.uhd_factory(args, setup, serial="30FE782", tx_gain_db=70.0,
                                    rx_gain_db=30.0)

    assert factory().tx_bandwidth == 29e6
    assert factory("--tx-analog-bw", "25e6").tx_bandwidth == 25e6
    assert factory("--tx-lo-offset", "0").tx_bandwidth == 25e6


def _rate_overlay(rate):
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp()) / "rate.yaml"
    path.write_text(f"phy:\n  sample_rate: {rate}\nradio:\n  bandwidth: {rate}\n",
                    encoding="utf-8")
    return path
