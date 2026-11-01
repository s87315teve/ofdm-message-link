"""Shared command-line handling for the transmitter and receiver apps.

Both apps must resolve the same sample timing and OFDM numerology.  The
receiver learns modulation and FEC from each protected burst header; it does
not need a matching startup MCS.  Keeping resolution in one place still makes
the two commands in the README a matched pair for the parameters that really
are shared.

The transmitter sends only wire v5, so its ``--mcs-index`` takes the v5
indices 8-17; the receiver's is a display hint and accepts the whole table.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, replace
from pathlib import Path

from ofdm_link.config import LinkConfig
from ofdm_link.phy import MCS
from ofdm_link.phy.mcs_table import (
    ALL_MCS_ENTRIES,
    McsTableEntry,
    mcs_entry,
    v5_entries,
)
from ofdm_link.radio.uhd import RF_ENABLE_ACKNOWLEDGEMENT, B210Settings

from .aggregation import AggregationLimits
from .devices import RadioSelection
from .link import (
    DEFAULT_FRAME_PAYLOAD_BYTES,
    PhyProfile,
    build_phy_profile,
    default_v5_entry,
    load_link_config,
)
from .transport import DEFAULT_PEAK_AMPLITUDE, DEFAULT_TX_PORT, TransportError
from .window_layout import parse_geometry

_MIN_BANDWIDTH_HZ = 200_000.0
_MAX_BANDWIDTH_HZ = 56_000_000.0

DEFAULT_CONFIG = "configs/default.yaml"
DEFAULT_OVERLAY = "configs/profiles/ota_2p45ghz.yaml"
DEFAULT_INGRESS_PORT = 52001
DEFAULT_EGRESS_PORT = 52002
DEFAULT_UI_FPS = 15
# Lines the Log tab keeps; older lines are dropped as new ones arrive, so a
# video demo that logs hundreds of messages a second holds a fixed amount.
DEFAULT_LOG_LINES = 2000
# Transports that drive a real radio: these get the Radio panel, and their
# transmit side the RF capability gate.
RADIO_TRANSPORTS = ("uhd", "pluto")

# Re-exported so both apps and the README quote the one phrase the project's
# own capability gate accepts, instead of a copy that can drift from it.
RF_ACKNOWLEDGEMENT = RF_ENABLE_ACKNOWLEDGEMENT


def rx_recv_frames(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 1024:
        raise argparse.ArgumentTypeError("must be between 0 (UHD default) and 1024")
    return parsed


def _log_lines(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 1_000_000:
        raise argparse.ArgumentTypeError("must be between 1 and 1000000")
    return parsed


def _max_subframes(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 64:
        raise argparse.ArgumentTypeError("must be between 1 and 64")
    return parsed


def _milliseconds(low: float, high: float, *, low_inclusive: bool = True):
    def parse(value: str) -> float:
        parsed = float(value)
        above = parsed >= low if low_inclusive else parsed > low
        if not (above and parsed <= high):
            bound = "[" if low_inclusive else "("
            raise argparse.ArgumentTypeError(f"must be in {bound}{low:g}, {high:g}] ms")
        return parsed

    return parse


def _ui_fps(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 60:
        raise argparse.ArgumentTypeError("must be between 1 and 60")
    return parsed


def add_common_arguments(
    parser: argparse.ArgumentParser,
    *,
    receiver_display_only: bool = False,
) -> None:
    link = parser.add_argument_group("link configuration")
    link.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"base configuration file (default: {DEFAULT_CONFIG})",
    )
    link.add_argument(
        "--overlay",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "configuration overlay, repeatable; "
            f"defaults to {DEFAULT_OVERLAY} when not given"
        ),
    )
    link.add_argument(
        "--mcs",
        choices=("qpsk", "qam16"),
        default=None,
        help=(
            "display hint only; RX reads the actual MCS from every burst header"
            if receiver_display_only
            else (
                "constellation shorthand: qpsk is MCS 8 (QPSK 1/3), qam16 is MCS 12 "
                "(16QAM 1/2); ignored when --mcs-index is set"
            )
        ),
    )
    entries = ALL_MCS_ENTRIES if receiver_display_only else v5_entries(include_hidden=True)
    link.add_argument(
        "--mcs-index",
        type=int,
        choices=tuple(entry.index for entry in entries),
        default=None,
        metavar="N",
        help=(
            "display hint only; RX reads the actual MCS from every burst header"
            if receiver_display_only
            else (
                "startup wire-v5 MCS index: 8, 9, 10 (QPSK 1/3, 1/2, 2/3), "
                "12, 13, 14 (16QAM 1/2, 2/3, 3/4), 15, 16, 17 (64QAM 1/2, 2/3, 3/4); "
                "11 is an experiment entry the menu hides. Default 8 unless --mcs qam16. Legacy "
                "indices 0-7 are receive-only"
            )
        ),
    )
    link.add_argument(
        "--frame-payload-bytes",
        type=int,
        default=DEFAULT_FRAME_PAYLOAD_BYTES,
        help=f"PHY frame payload size (default: {DEFAULT_FRAME_PAYLOAD_BYTES})",
    )
    link.add_argument(
        "--ui-fps",
        type=_ui_fps,
        default=DEFAULT_UI_FPS,
        help=f"plot refresh rate from 1 to 60 Hz (default: {DEFAULT_UI_FPS})",
    )
    window = parser.add_argument_group("window")
    window.add_argument(
        "--log-lines",
        type=_log_lines,
        default=DEFAULT_LOG_LINES,
        metavar="N",
        help=(
            "lines the Log tab keeps; older lines are dropped "
            f"(default: {DEFAULT_LOG_LINES})"
        ),
    )
    window.add_argument(
        "--geometry",
        type=parse_geometry,
        default=None,
        metavar="WxH+X+Y",
        help=(
            "window client size WxH with the frame's top-left corner at X+Y "
            "(default: let the window manager place it)"
        ),
    )

    transport = parser.add_argument_group("sample transport")
    transport.add_argument(
        "--transport",
        choices=("udp", *RADIO_TRANSPORTS),
        default="udp",
        help=(
            "udp is a localhost sample link needing no radio; uhd is the real link "
            "through a USRP and pluto the real link through an ADALM-Pluto"
        ),
    )
    transport.add_argument(
        "--udp-host",
        default="127.0.0.1",
        help="udp transport peer/bind address (default: 127.0.0.1)",
    )
    transport.add_argument(
        "--udp-port",
        type=int,
        default=DEFAULT_TX_PORT,
        help=f"udp transport port (default: {DEFAULT_TX_PORT})",
    )
    transport.add_argument(
        "--serial",
        default=None,
        help="select one radio by serial number (B210, NI USRP-2901, N200/N210 or Pluto)",
    )
    transport.add_argument(
        "--channel",
        type=int,
        default=None,
        help=(
            "preselect a front end: 0 is RF A and 1 is RF B on a B210/2901; "
            "an N210 or a Pluto has only 0"
        ),
    )
    transport.add_argument(
        "--antenna",
        default=None,
        help=(
            "preselect an antenna port, e.g. TX/RX or RX2 (on an N210 these are RF1 "
            "and RF2); a Pluto has one port per direction"
        ),
    )
    transport.add_argument(
        "--gain",
        type=float,
        default=None,
        help=(
            "preselect the gain in dB (transmit otherwise defaults to the minimum); "
            "a Pluto's transmit gain is its attenuator, -89.75 to 0 dB"
        ),
    )
    transport.add_argument(
        "--auto-start",
        action="store_true",
        help=(
            "start the radio as soon as the device has been probed, instead of "
            "waiting for the Start button; for scripted and reproducible runs"
        ),
    )
    transport.add_argument(
        "--ready-file",
        default=None,
        metavar="PATH",
        help=(
            "write PATH once the radio is running and delete it when the radio "
            "stops or the window closes, so a script can wait for an operator "
            "who chooses the device and gain in the window"
        ),
    )


def add_aggregation_arguments(parser: argparse.ArgumentParser) -> None:
    """Transmit-side A-MPDU aggregation controls.

    Aggregation is opportunistic: a burst carries whatever datagrams are
    queued when the radio has room for it, so at low load it adds no latency
    and at high load it shares one preamble, SIG and guard between up to
    ``--aggregation-max-subframes`` datagrams.  ``--aggregation-max-wait-ms``
    is the only knob that adds latency on purpose.
    """

    group = parser.add_argument_group(
        "aggregation",
        "Several datagrams can share one v5 burst. The caps bound the burst's "
        "airtime (timing drift, decode latency) and the latency a datagram can "
        "wait for company. Each option overrides mac.aggregation in the "
        "configuration (defaults: on, 16 subframes, 0 ms wait, 8 ms airtime, 4 ms burst interval).",
    )
    group.add_argument(
        "--aggregation",
        choices=("on", "off"),
        default=None,
        help=(
            "on packs every datagram queued when the radio is ready into one "
            "burst, within the caps below; off sends one datagram per burst, "
            "for A/B measurements"
        ),
    )
    group.add_argument(
        "--aggregation-max-subframes",
        type=_max_subframes,
        default=None,
        metavar="N",
        help="most datagrams in one burst, 1-64",
    )
    group.add_argument(
        "--aggregation-max-wait-ms",
        type=_milliseconds(0.0, 1000.0),
        default=None,
        metavar="MS",
        help=(
            "hold a burst that still has room up to MS after its oldest message "
            "arrived, waiting for more to share it. Fuller bursts at a cost of up "
            "to MS of added latency per datagram; 0 never waits, so a lone "
            "datagram leaves as soon as the radio is free"
        ),
    )
    group.add_argument(
        "--min-burst-interval-ms",
        type=_milliseconds(0.0, 50.0),
        default=None,
        metavar="MS",
        help=(
            "burst-rate cap: hold a burst until MS after the previous one started "
            "to go to the radio (start to start, so a burst longer than MS is "
            "followed at once), packing more datagrams into it meanwhile (default 4, "
            "at most 250 bursts/s). Adds at most MS of latency, and only when "
            "bursts are back to back; a datagram after an idle period leaves at "
            "once, and a full burst is never held. 0 disables the cap"
        ),
    )
    group.add_argument(
        "--max-burst-airtime-ms",
        type=_milliseconds(0.0, 100.0, low_inclusive=False),
        default=None,
        metavar="MS",
        help=(
            "longest aggregated burst on the air, guards included; a single "
            "datagram longer than this is still sent alone. Longer bursts drift "
            "further against the receiver's timing tracker, lose more level to "
            "peak scaling and decode more lumpily. Aggregates also stop at 80000 "
            "samples whatever MS says, the drift the receiver corrects at 50 ppm"
        ),
    )


def aggregation_limits(args: argparse.Namespace, config: LinkConfig) -> AggregationLimits:
    """``mac.aggregation`` with any transmit option laid over it.

    The configuration states the caps in milliseconds, like the options; this
    is the one place they become the seconds :class:`AggregationLimits` uses.
    The receiver has no such options and simply gets the configured values.
    """

    section = config.mac.aggregation

    def given(name: str, configured):
        value = getattr(args, name, None)
        return configured if value is None else value

    switch = getattr(args, "aggregation", None)
    return AggregationLimits(
        enabled=bool(section.enabled) if switch is None else switch == "on",
        max_wait_s=float(given("aggregation_max_wait_ms", section.max_wait_ms)) / 1e3,
        max_subframes=int(given("aggregation_max_subframes", section.max_subframes)),
        max_psdu_bytes=int(section.max_psdu_bytes),
        max_burst_airtime_s=(
            float(given("max_burst_airtime_ms", section.max_burst_airtime_ms)) / 1e3
        ),
        min_burst_interval_s=(
            float(given("min_burst_interval_ms", section.min_burst_interval_ms)) / 1e3
        ),
    )


@dataclass(frozen=True, slots=True)
class ResolvedOptions:
    """One consistent working point shared by both apps."""

    config: LinkConfig
    profile: PhyProfile
    mcs_entry: McsTableEntry
    transport: str
    aggregation: AggregationLimits = AggregationLimits()

    @property
    def uses_radio(self) -> bool:
        return self.transport in RADIO_TRANSPORTS


def resolve(args: argparse.Namespace) -> ResolvedOptions:
    """Load configuration and build the PHY profile both halves must share."""

    overlays = args.overlay if args.overlay else [DEFAULT_OVERLAY]
    for candidate in [args.config, *overlays]:
        if not Path(candidate).is_file():
            raise SystemExit(f"configuration file not found: {candidate}")
    config = load_link_config(args.config, overlays)

    if args.serial:
        config = replace(config, radio=replace(config.radio, device_args=f"serial={args.serial}"))
    if args.transport == "uhd" and config.radio.adapter != "uhd":
        raise SystemExit(
            "--transport uhd needs a configuration that selects the UHD adapter; "
            f"{args.config} with the given overlays selects {config.radio.adapter!r}. "
            f"Add --overlay {DEFAULT_OVERLAY}."
        )

    selected_entry = mcs_entry(args.mcs_index) if args.mcs_index is not None else None
    mcs = selected_entry.modulation if selected_entry is not None else (
        MCS[args.mcs.upper()] if args.mcs else None
    )
    try:
        profile = build_phy_profile(
            config,
            mcs=mcs,
            mcs_entry=selected_entry,
            frame_payload_bytes=args.frame_payload_bytes,
        )
    except (ValueError, TypeError) as error:
        raise SystemExit(f"invalid PHY working point: {error}") from error
    if selected_entry is None:
        selected_entry = default_v5_entry(profile.mcs)
    return ResolvedOptions(
        config=config,
        profile=profile,
        mcs_entry=selected_entry,
        transport=args.transport,
        aggregation=aggregation_limits(args, config),
    )


def settings_for(
    selection: RadioSelection,
    config: LinkConfig,
    *,
    direction: str,
) -> B210Settings:
    """Turn one operator selection into validated radio settings.

    The selection owns the device, front end, antenna, gain, frequency and
    sample rate.  Everything else -- clock source, and the antenna for the
    direction not being configured -- comes from the configuration, so a GUI
    choice cannot quietly widen what the wire contract allows.

    Analog bandwidth follows the sample rate rather than the configuration: a
    filter left at 5 MHz while the operator moves to 20 MS/s would clip the
    signal for reasons nothing on screen explains.
    """

    if direction not in {"rx", "tx"}:
        raise ValueError("direction must be rx or tx")
    bandwidth = min(max(float(selection.sample_rate), _MIN_BANDWIDTH_HZ), _MAX_BANDWIDTH_HZ)
    radio = config.radio
    return B210Settings(
        device_args=selection.device_args,
        sample_rate=int(selection.sample_rate),
        center_frequency=float(selection.center_frequency_hz),
        rx_gain=selection.device_gain_db("rx") if direction == "rx" else radio.rx_gain,
        tx_gain=selection.device_gain_db("tx") if direction == "tx" else radio.tx_gain,
        bandwidth=bandwidth,
        rx_antenna=selection.antenna if direction == "rx" else radio.rx_antenna,
        tx_antenna=selection.antenna if direction == "tx" else radio.tx_antenna,
        channel=int(selection.channel),
        clock_source=radio.clock_source,
    )


def require_rf_capability(args: argparse.Namespace):
    """Return the RF token, or exit explaining exactly what is missing."""

    from ofdm_link.radio import acknowledge_rf_transmission

    if not getattr(args, "enable_rf", False):
        raise SystemExit(
            f"--transport {args.transport} transmits RF and requires --enable-rf together with "
            f"--acknowledgement {RF_ACKNOWLEDGEMENT!r}"
        )
    acknowledgement = getattr(args, "acknowledgement", None)
    if acknowledgement != RF_ACKNOWLEDGEMENT:
        raise SystemExit(f"--acknowledgement must be exactly {RF_ACKNOWLEDGEMENT!r}")
    return acknowledge_rf_transmission(acknowledgement)


def build_sink(args: argparse.Namespace, options: ResolvedOptions, selection=None):
    """Build the transmit-side sample transport.

    ``selection`` is required for the radio and ignored for UDP.  The RF
    capability is taken at startup, before any window exists, so a process
    that was not authorised to transmit cannot become one by clicking.
    """

    from .transport import UdpSampleSink, UhdSampleSink

    peak = float(getattr(args, "peak_amplitude", DEFAULT_PEAK_AMPLITUDE))
    if options.transport == "udp":
        return UdpSampleSink(host=args.udp_host, port=args.udp_port, peak_amplitude=peak)

    token = require_rf_capability(args)
    if options.transport == "pluto":
        from . import pluto

        return pluto.PlutoSampleSink(
            pluto.settings_for(_selected(selection), direction="tx"), token, peak_amplitude=peak
        )
    if selection is None:
        from ofdm_link.radio import build_b210_settings

        settings = build_b210_settings(options.config)
    else:
        settings = settings_for(selection, options.config, direction="tx")
    return UhdSampleSink(settings, token, peak_amplitude=peak)


def build_source(args: argparse.Namespace, options: ResolvedOptions, selection=None):
    """Build the receive-side sample transport.

    Receiving transmits nothing, so no RF acknowledgement is involved.
    """

    from .transport import UdpSampleSource, UhdSampleSource

    if options.transport == "udp":
        return UdpSampleSource(
            bind_host=args.udp_host,
            port=args.udp_port,
            snr_db=getattr(args, "snr_db", None),
        )
    if options.transport == "pluto":
        from . import pluto

        return pluto.PlutoSampleSource(pluto.settings_for(_selected(selection), direction="rx"))

    if selection is None:
        from ofdm_link.radio import build_b210_settings

        try:
            settings = build_b210_settings(options.config)
        except ValueError as error:
            raise SystemExit(str(error)) from error
    else:
        settings = settings_for(selection, options.config, direction="rx")
    return UhdSampleSource(settings, recv_frames=getattr(args, "rx_recv_frames", None))


def _selected(selection: RadioSelection | None) -> RadioSelection:
    """A Pluto has no configuration-file fallback: it is always chosen in the panel."""

    if selection is None:
        raise TransportError("the pluto transport needs a device chosen in the Radio panel")
    return selection


def mark_ready(path: str | None, description: str) -> None:
    """Publish that the radio is running, atomically, if a path was given."""

    if not path:
        return
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(description + "\n")
    os.replace(temporary, path)


def clear_ready(path: str | None) -> None:
    """Withdraw :func:`mark_ready`; a missing file is already the answer."""

    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def evidence_banner(options: ResolvedOptions) -> str:
    """The claim this run is allowed to support, shown in both windows."""

    scope = (
        "real over-the-air RF"
        if options.uses_radio
        else "localhost UDP sample transport, no radio"
    )
    return (
        f"DEMO ONLY - one-way link ({scope}). No ACK, no ARQ, no TDD: lost bursts stay lost. "
        "Not throughput, reliability or RF acceptance evidence."
    )


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_EGRESS_PORT",
    "DEFAULT_INGRESS_PORT",
    "DEFAULT_OVERLAY",
    "DEFAULT_UI_FPS",
    "RF_ACKNOWLEDGEMENT",
    "ResolvedOptions",
    "TransportError",
    "add_aggregation_arguments",
    "add_common_arguments",
    "aggregation_limits",
    "build_sink",
    "build_source",
    "clear_ready",
    "evidence_banner",
    "mark_ready",
    "require_rf_capability",
    "resolve",
    "settings_for",
]
