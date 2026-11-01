"""Command line of ``node_app``: one bidirectional node per Pluto.

A node transmits on its own frequency and receives the peer's at the same
time (FDD).  Everything a node needs comes from one profile with a
``duplex`` section and its node id: the two frequencies, the ports, the
scrambler seed range.  The flags shared with tx_app and rx_app come from
:mod:`.options` and keep their meaning; ``--gain`` is refused because a node
has two gains (``--tx-gain`` and ``--rx-gain``).

Ports, ``base = 52200 + 10 * node_id`` (no clash with the one-way apps'
52001/52002, the scripts' 52012/52022, or the UDP sample transport 52101):

======  =======  ======  =======  ====================
node    ingress  egress  control  UDP sample transport
======  =======  ======  =======  ====================
1       52211    52212   52213    52214
2       52221    52222   52223    52224
======  =======  ======  =======  ====================

``egress`` is where this node delivers what it received; with ``--transport
udp`` a node sends its samples to the peer's sample port, which makes a
radio-free duplex link on one PC.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

from ofdm_link.phy.mcs_table import McsTableEntry, mcs_entry

from . import log_limits, options
from .link import node_seed_range

DEFAULT_NODE_OVERLAY = "configs/profiles/duplex_fdd_2420_2460.yaml"
NODE_IDS = (1, 2)
PORT_BASE = 52200
DEFAULT_TX_GAIN_DB = -89.75
DEFAULT_RX_GAIN_DB = 15.0
MIN_TX_GAIN_DB = -89.75
MAX_TX_GAIN_DB = 0.0
DEFAULT_DECODE_WORKERS = 3
DEFAULT_LINK_ADAPTATION = "olla"
DEFAULT_REPORTER = "default"
ISM_BAND_HZ = (2_400_000_000.0, 2_483_500_000.0)
# Scrambler seeds per node: a free PHY-level direction id (design section 3.1).
SEED_RANGES = {node_id: node_seed_range(node_id) for node_id in NODE_IDS}


@dataclass(frozen=True, slots=True)
class NodePorts:
    ingress: int
    egress: int
    control: int
    udp_sample: int


def node_ports(node_id: int) -> NodePorts:
    if node_id not in NODE_IDS:
        raise ValueError(f"node id must be one of {NODE_IDS}")
    base = PORT_BASE + 10 * node_id
    return NodePorts(ingress=base + 1, egress=base + 2, control=base + 3, udp_sample=base + 4)


def peer_id(node_id: int) -> int:
    return 2 if node_id == 1 else 1


def parse_gain_profile(text: str) -> tuple[tuple[float, float], ...]:
    """``"0:-5,30:-20,60:-5"`` → ((0.0, -5.0), (30.0, -20.0), (60.0, -5.0)).

    Times are seconds from ready, non-negative and increasing; gains are a
    Pluto TX gain, -89.75 to 0 dB.
    """

    steps: list[tuple[float, float]] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        when, colon, gain = part.partition(":")
        if not colon:
            raise argparse.ArgumentTypeError(f"'{part}' is not t:gain")
        try:
            t, g = float(when), float(gain)
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{part}' is not t:gain in numbers") from None
        if not (math.isfinite(t) and t >= 0.0):
            raise argparse.ArgumentTypeError(f"time {when} must be >= 0 s")
        if not MIN_TX_GAIN_DB <= g <= MAX_TX_GAIN_DB:
            raise argparse.ArgumentTypeError(
                f"gain {gain} dB is outside {MIN_TX_GAIN_DB:g}..{MAX_TX_GAIN_DB:g} dB"
            )
        if steps and t <= steps[-1][0]:
            raise argparse.ArgumentTypeError("gain-profile times must increase")
        steps.append((t, g))
    if not steps:
        raise argparse.ArgumentTypeError("the gain profile is empty")
    return tuple(steps)


class GainSchedule:
    """The due steps of a gain profile, each returned once."""

    def __init__(self, steps: tuple[tuple[float, float], ...] = ()) -> None:
        self._steps = list(steps)
        self._next = 0

    def due(self, elapsed_s: float) -> list[tuple[float, float]]:
        steps = []
        while self._next < len(self._steps) and self._steps[self._next][0] <= elapsed_s:
            steps.append(self._steps[self._next])
            self._next += 1
        return steps

    @property
    def finished(self) -> bool:
        return self._next >= len(self._steps)


def _tx_gain(value: str) -> float:
    gain = float(value)
    if not MIN_TX_GAIN_DB <= gain <= MAX_TX_GAIN_DB:
        raise argparse.ArgumentTypeError(
            f"must be {MIN_TX_GAIN_DB:g} to {MAX_TX_GAIN_DB:g} dB (a Pluto's attenuator)"
        )
    return gain


def _port(value: str) -> int:
    port = int(value)
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be 0-65535")
    return port


def _positive(value: str) -> float:
    parsed = float(value)
    if not (math.isfinite(parsed) and parsed > 0):
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _duty(value: str) -> float:
    parsed = float(value)
    if not 0.05 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0.05 and 1")
    return parsed


def _kernel_buffers(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 64:
        raise argparse.ArgumentTypeError("must be 1-64")
    return parsed


def _decode_workers(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 64:
        raise argparse.ArgumentTypeError("must be between 1 and 64")
    return parsed


def _v5_index(value: str) -> int:
    index = int(value)
    try:
        entry = mcs_entry(index)
    except Exception:
        raise argparse.ArgumentTypeError(f"{value} is not an MCS index") from None
    if entry.family != "v5":
        raise argparse.ArgumentTypeError("must be a wire-v5 index, 8-17")
    return index


# Help texts of the shared tx_app/rx_app options that mean something else here.
_NODE_HELP = {
    "--overlay": (
        "configuration overlay, repeatable; it needs a duplex section "
        f"(default: {DEFAULT_NODE_OVERLAY})"
    ),
    "--transport": (
        "udp (default) is a radio-free duplex link through localhost sample ports; pluto "
        "drives one ADALM-Pluto for both directions; uhd is refused"
    ),
    "--udp-port": "not used by node_app: the sample ports are --udp-listen-port/--udp-peer-port",
    "--gain": "refused: a node has two gains, use --tx-gain and --rx-gain",
}


def _set_help(parser: argparse.ArgumentParser, helps: dict[str, str]) -> None:
    for action in parser._actions:
        for flag in action.option_strings:
            if flag in helps:
                action.help = helps[flag]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ofdm-message-node",
        description=(
            "One node of a bidirectional OFDM link: transmits and receives at once on "
            "one Pluto (FDD), with feedback and link adaptation. GUI or --headless."
        ),
    )
    options.add_common_arguments(parser)
    _set_help(parser, _NODE_HELP)
    node = parser.add_argument_group("node")
    node.add_argument("--node-id", type=int, choices=NODE_IDS, required=True,
                      help="which entry of duplex.nodes this process is; sets the ports")
    node.add_argument("--tx-freq", type=float, default=None, metavar="HZ",
                      help="transmit frequency (default: the profile's for this node)")
    node.add_argument("--rx-freq", type=float, default=None, metavar="HZ",
                      help="receive frequency (default: the profile's for this node)")
    node.add_argument("--tx-gain", type=_tx_gain, default=DEFAULT_TX_GAIN_DB,
                      help=f"Pluto TX gain in dB, -89.75 to 0 (default {DEFAULT_TX_GAIN_DB:g})")
    node.add_argument("--rx-gain", type=float, default=DEFAULT_RX_GAIN_DB,
                      help=f"RX gain in dB (default {DEFAULT_RX_GAIN_DB:g})")
    node.add_argument("--rx-analog-bw", type=_positive, default=None, metavar="HZ",
                      help="AD9361 RX analog filter bandwidth (default: the sample rate)")
    node.add_argument("--rx-kernel-buffers", type=_kernel_buffers, default=4, metavar="N",
                      help="libiio RX kernel buffers (default 4; 8 rides out longer USB stalls)")
    node.add_argument("--tx-duty-cap", type=_duty, default=0.7, metavar="D",
                      help=("pluto: keep the TX duty at or below D (default 0.7: above it USB 2.0 "
                            "cannot also carry the 5 MS/s RX stream); 1 = no cap"))
    node.add_argument("--peak-amplitude", type=float, default=0.7,
                      help="scale each burst to this peak magnitude (default: 0.7)")
    node.add_argument("--headless", action="store_true",
                      help="no window: implies --auto-start; pluto needs --serial")
    node.add_argument("--duration", type=_positive, default=None, metavar="S",
                      help="stop S seconds after ready (headless)")
    node.add_argument("--gain-profile", type=parse_gain_profile, default=None,
                      metavar="T:DB,...",
                      help="scheduled TX gain, seconds from ready, e.g. '0:-5,30:-20,60:-5'")
    node.add_argument("--control-port", type=_port, default=None, metavar="P",
                      help="localhost UDP JSON control socket (default: per node; 0 = off)")
    node.add_argument("--decode-workers", type=_decode_workers,
                      default=DEFAULT_DECODE_WORKERS,
                      help=f"payload decode worker processes (default {DEFAULT_DECODE_WORKERS})")
    node.add_argument("--snr-db", type=float, default=None,
                      help="udp transport only: add AWGN at this SNR")

    la = parser.add_argument_group("link adaptation")
    la.add_argument("--link-adaptation", default=DEFAULT_LINK_ADAPTATION, metavar="SPEC",
                    help=("olla (two-loop, the default), fixed (stays at --mcs-index), arf, "
                          "package.module:Class or path/file.py:Class "
                          f"(default: {DEFAULT_LINK_ADAPTATION}, starting at --mcs-index)"))
    la.add_argument("--link-adaptation-arg", action="append", default=[], metavar="K=V",
                    help="keyword argument for the LA class, repeatable")
    la.add_argument("--feedback-reporter", default=DEFAULT_REPORTER, metavar="SPEC",
                    help=f"RX-side measurement and reporting module (default: {DEFAULT_REPORTER})")
    la.add_argument("--feedback-reporter-arg", action="append", default=[], metavar="K=V",
                    help="keyword argument for the reporter class, repeatable")
    la.add_argument("--control-mcs-index", type=_v5_index, default=None, metavar="N",
                    help="MCS of control-only bursts (default: the profile's, else 8)")
    la.add_argument("--la-log", default=None, metavar="PATH",
                    help="append every LA decision and feedback report as JSON lines")

    files = parser.add_argument_group("logs and ports")
    files.add_argument("--stats-log", default=None, metavar="PATH",
                       help="append one JSON stats line every 0.5 s")
    files.add_argument("--stats-log-max-mb", type=float, default=log_limits.DEFAULT_MAX_MB,
                       metavar="MB",
                       help=("rotate the stats log when it would pass this many MiB; 0 = no cap "
                             f"(default: {log_limits.DEFAULT_MAX_MB:g})"))
    files.add_argument("--stats-log-backups", type=int, default=log_limits.DEFAULT_BACKUPS,
                       metavar="N",
                       help=f"rotated stats logs to keep (default: {log_limits.DEFAULT_BACKUPS})")
    files.add_argument("--ingress-port", type=_port, default=None,
                       help="localhost UDP port with bytes to send (default: per node)")
    files.add_argument("--egress-host", default="127.0.0.1",
                       help="host received datagrams go to (default: 127.0.0.1)")
    files.add_argument("--egress-port", type=_port, default=None,
                       help="where received datagrams go (default: per node)")
    files.add_argument("--udp-peer-port", type=_port, default=None,
                       help="udp transport: the peer's sample port (default: the peer's base+4)")
    files.add_argument("--udp-listen-port", type=_port, default=None,
                       help="udp transport: this node's sample port (default: base+4)")

    rf = parser.add_argument_group("RF safety gate")
    rf.add_argument("--enable-rf", action="store_true",
                    help="required for --transport pluto; this process will key a transmitter")
    rf.add_argument("--acknowledgement", default=None,
                    help=f"must be exactly {options.RF_ACKNOWLEDGEMENT!r}")
    options.add_aggregation_arguments(parser)
    return parser


@dataclass(frozen=True)
class NodeSetup:
    """Everything resolved from the command line, before any process starts."""

    resolved: options.ResolvedOptions
    node_id: int
    peer_id: int
    mode: str
    tx_frequency: float
    rx_frequency: float
    ports: NodePorts
    ingress_port: int
    egress: tuple[str, int]
    control_port: int
    udp_listen_port: int
    udp_peer_port: int
    control_entry: McsTableEntry
    seed_range: tuple[int, int]
    gain_profile: tuple[tuple[float, float], ...]
    warnings: tuple[str, ...]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.gain is not None:
        parser.error("--gain is ambiguous for a node; use --tx-gain and --rx-gain")
    if args.overlay is None:
        args.overlay = [DEFAULT_NODE_OVERLAY]
    if args.headless:
        args.auto_start = True
        if args.transport == "pluto" and not args.serial:
            parser.error("--headless with --transport pluto needs --serial")
    if args.transport == "uhd":
        parser.error("node_app drives a Pluto (--transport pluto) or the udp simulation")
    if args.snr_db is not None and args.transport != "udp":
        parser.error("--snr-db applies to the udp transport only")
    return args


def resolve_node(args: argparse.Namespace) -> NodeSetup:
    resolved = options.resolve(args)
    duplex = resolved.config.duplex
    if duplex is None:
        raise SystemExit(
            "the configuration has no duplex section; add "
            f"--overlay {DEFAULT_NODE_OVERLAY}"
        )
    entry = duplex.node(args.node_id)
    tx = float(entry.tx_frequency if args.tx_freq is None else args.tx_freq)
    rx = float(entry.rx_frequency if args.rx_freq is None else args.rx_freq)
    rate = float(resolved.profile.sample_rate)
    if duplex.mode == "fdd" and abs(tx - rx) < rate:
        raise SystemExit("FDD needs the TX and RX frequencies at least one sample rate apart")
    warnings = []
    for name, frequency in (("TX", tx), ("RX", rx)):
        if frequency - rate / 2 < ISM_BAND_HZ[0] or frequency + rate / 2 > ISM_BAND_HZ[1]:
            warnings.append(
                f"{name} {frequency / 1e6:.3f} MHz +- {rate / 2e6:g} MHz leaves the "
                "2400-2483.5 MHz ISM band"
            )
    ports = node_ports(args.node_id)
    peer = peer_id(args.node_id)
    control_index = (
        duplex.control_mcs_index if args.control_mcs_index is None else args.control_mcs_index
    )
    return NodeSetup(
        resolved=resolved,
        node_id=args.node_id,
        peer_id=peer,
        mode=duplex.mode,
        tx_frequency=tx,
        rx_frequency=rx,
        ports=ports,
        ingress_port=ports.ingress if args.ingress_port is None else args.ingress_port,
        egress=(args.egress_host,
                ports.egress if args.egress_port is None else args.egress_port),
        control_port=ports.control if args.control_port is None else args.control_port,
        udp_listen_port=(
            ports.udp_sample if args.udp_listen_port is None else args.udp_listen_port
        ),
        udp_peer_port=(
            node_ports(peer).udp_sample if args.udp_peer_port is None else args.udp_peer_port
        ),
        control_entry=mcs_entry(control_index),
        seed_range=SEED_RANGES[args.node_id],
        gain_profile=args.gain_profile or (),
        warnings=tuple(warnings),
    )


def warn(setup: NodeSetup) -> None:
    for line in setup.warnings:
        print(f"node_app: warning: {line}", file=sys.stderr)


def check_paths(args: argparse.Namespace) -> None:
    for path in (args.stats_log, args.la_log, args.ready_file):
        if path and not Path(path).parent.exists():
            raise SystemExit(f"directory of {path} does not exist")


__all__ = [
    "DEFAULT_NODE_OVERLAY",
    "GainSchedule",
    "NodePorts",
    "NodeSetup",
    "build_parser",
    "node_ports",
    "parse_args",
    "parse_gain_profile",
    "peer_id",
    "resolve_node",
]
