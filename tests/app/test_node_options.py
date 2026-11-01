"""node_app's command line: ports per node, the gain profile, refused flags."""

from __future__ import annotations

import argparse
import errno
import socket

import pytest

from ofdm_message_link import node_options
from ofdm_message_link.tx_app import IngressListener

ONE_WAY_PORTS = {52001, 52002, 52012, 52022, 52101}


def test_ports_per_node_id():
    one, two = node_options.node_ports(1), node_options.node_ports(2)
    assert (one.ingress, one.egress, one.control, one.udp_sample) == (52211, 52212, 52213, 52214)
    assert (two.ingress, two.egress, two.control, two.udp_sample) == (52221, 52222, 52223, 52224)
    every = {*vars_of(one), *vars_of(two)}
    assert len(every) == 8 and not every & ONE_WAY_PORTS


def vars_of(ports):
    return (ports.ingress, ports.egress, ports.control, ports.udp_sample)


def test_unknown_node_id_is_refused():
    with pytest.raises(ValueError):
        node_options.node_ports(3)


def test_a_second_ingress_on_the_same_port_fails_with_eaddrinuse():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    first = IngressListener("127.0.0.1", port, lambda *_: True, reuse_address=False)
    first.start()
    try:
        second = IngressListener("127.0.0.1", port, lambda *_: True, reuse_address=False)
        with pytest.raises(OSError) as raised:
            second.start()
        assert raised.value.errno == errno.EADDRINUSE
    finally:
        first.stop()


def test_gain_profile_parses_in_order():
    assert node_options.parse_gain_profile("0:-5,30:-20, 60:-5") == (
        (0.0, -5.0),
        (30.0, -20.0),
        (60.0, -5.0),
    )


@pytest.mark.parametrize(
    "text",
    ["", "5", "0:-5,0:-10", "10:-5,5:-6", "0:3", "0:-90", "-1:-5", "a:b"],
)
def test_bad_gain_profiles_are_refused(text):
    with pytest.raises(argparse.ArgumentTypeError):
        node_options.parse_gain_profile(text)


def test_gain_schedule_returns_each_step_once():
    schedule = node_options.GainSchedule(((0.0, -5.0), (30.0, -20.0), (60.0, -5.0)))
    assert schedule.due(0.0) == [(0.0, -5.0)]
    assert schedule.due(29.9) == []
    assert schedule.due(75.0) == [(30.0, -20.0), (60.0, -5.0)]
    assert schedule.due(100.0) == [] and schedule.finished


def test_gain_is_refused_as_ambiguous(capsys):
    with pytest.raises(SystemExit):
        node_options.parse_args(["--node-id", "1", "--gain", "-10"])
    assert "--tx-gain" in capsys.readouterr().err


def test_headless_pluto_needs_a_serial():
    with pytest.raises(SystemExit):
        node_options.parse_args(["--node-id", "1", "--headless", "--transport", "pluto"])


def test_defaults_resolve_from_the_fdd_profile():
    args = node_options.parse_args(["--node-id", "2", "--headless"])
    assert args.auto_start and args.overlay == [node_options.DEFAULT_NODE_OVERLAY]
    setup = node_options.resolve_node(args)
    assert (setup.tx_frequency, setup.rx_frequency) == (2.46e9, 2.42e9)
    assert setup.ingress_port == 52221 and setup.egress == ("127.0.0.1", 52222)
    assert setup.control_port == 52223
    assert (setup.udp_listen_port, setup.udp_peer_port) == (52224, 52214)
    assert setup.seed_range == (64, 127) and setup.control_entry.index == 8
    assert args.tx_gain == -89.75 and args.decode_workers == 3
    assert not setup.warnings


def test_out_of_band_frequency_warns():
    args = node_options.parse_args(["--node-id", "1", "--tx-freq", "2401e6"])
    assert any("ISM" in line for line in node_options.resolve_node(args).warnings)


def test_a_profile_without_duplex_is_refused():
    args = node_options.parse_args(
        ["--node-id", "1", "--overlay", "configs/profiles/ota_2p45ghz.yaml"]
    )
    with pytest.raises(SystemExit, match="duplex"):
        node_options.resolve_node(args)
