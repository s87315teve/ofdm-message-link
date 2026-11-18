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
from .transport import DEFAULT_PEAK_AMPLITUDE, DEFAULT_TX_PORT, ReceiveFeed, TransportError
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

    add_performance_arguments(parser)


def _cpu_affinity(value: str) -> str:
    from ofdm_link.runtime.cpu_affinity import CpuAffinityError, check_spec

    try:
        return check_spec(value)
    except CpuAffinityError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _cpu_cores(value: str) -> int:
    from ofdm_link.runtime.cpu_budget import MAX_CPU_CORES

    parsed = int(value)
    if not 0 <= parsed <= MAX_CPU_CORES:
        raise argparse.ArgumentTypeError(
            f"must be between 0 (the CPUs this process may run on) and {MAX_CPU_CORES}"
        )
    return parsed


def rx_min_feed_samples_argument(value: str) -> int:
    """``--rx-min-feed-samples``: the bound ``acceleration.rx_min_feed_samples`` has."""

    from ofdm_link.config import MAX_RX_MIN_FEED_SAMPLES

    parsed = int(value)
    if not 0 <= parsed <= MAX_RX_MIN_FEED_SAMPLES:
        raise argparse.ArgumentTypeError(f"must be between 0 and {MAX_RX_MIN_FEED_SAMPLES}")
    return parsed


def rx_max_merge_wait_ms_argument(value: str) -> float:
    """``--rx-max-merge-wait-ms``: the bound ``acceleration.rx_max_merge_wait_ms`` has."""

    from ofdm_link.config import MAX_RX_MERGE_WAIT_MS

    return _milliseconds(0.0, MAX_RX_MERGE_WAIT_MS)(value)


def add_receive_feed_arguments(group) -> None:
    """``--rx-min-feed-samples`` and ``--rx-max-merge-wait-ms`` (rx_app, node_app)."""

    default = ReceiveFeed()
    group.add_argument(
        "--rx-min-feed-samples",
        type=rx_min_feed_samples_argument,
        default=None,
        metavar="N",
        help=(
            "merge short transport chunks into decoder feeds of at least N samples, 0-131072; "
            "0 feeds every chunk as it comes (default: acceleration.rx_min_feed_samples in "
            f"the configuration, else {default.min_samples}; 65536 on a Jetson)"
        ),
    )
    group.add_argument(
        "--rx-max-merge-wait-ms",
        type=rx_max_merge_wait_ms_argument,
        default=None,
        metavar="MS",
        help=(
            "wait at most MS for the chunks that complete a feed, 0-20 (default: "
            "acceleration.rx_max_merge_wait_ms in the configuration, else "
            f"{default.max_wait_s * 1e3:g}; 15 on a Jetson); --events host times can "
            "come up to this much later"
        ),
    )


def decode_workers_argument(value: str) -> int:
    """``--decode-workers``: the bound ``acceleration.decode_workers`` also has."""

    from ofdm_link.config import MAX_DECODE_WORKERS

    parsed = int(value)
    if not 1 <= parsed <= MAX_DECODE_WORKERS:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_DECODE_WORKERS}")
    return parsed


DEFAULT_REALTIME_PRIORITY = 15


def _realtime_priority(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 49:
        raise argparse.ArgumentTypeError("must be between 1 and 49")
    return parsed


def add_performance_arguments(parser: argparse.ArgumentParser) -> None:
    """CPU placement and scheduling switches; every one is off by default."""

    group = parser.add_argument_group("CPU placement and scheduling (all off by default)")
    group.add_argument(
        "--cpu-cores",
        type=_cpu_cores,
        default=None,
        metavar="N",
        help=(
            "NumPy/BLAS and OpenMP threads this app and its radio engine process may each "
            "use; 0 = the CPUs this process may run on. Decode workers always use one each. "
            "Default: acceleration.cpu_cores in the configuration, else 1"
        ),
    )
    group.add_argument(
        "--cpu-affinity",
        type=_cpu_affinity,
        default=None,
        metavar="CPUS",
        help=(
            "pin this app and every process it starts (engine, decode workers) to CPUS, "
            "a list like 0-7 or 2,4-5, or p-cores for the list in "
            "/sys/devices/cpu_core/cpus (pins nothing where that file does not exist). "
            "Default: no pinning"
        ),
    )
    group.add_argument(
        "--realtime",
        nargs="?",
        type=_realtime_priority,
        const=DEFAULT_REALTIME_PRIORITY,
        default=None,
        metavar="PRIO",
        help=(
            "Linux only: run the radio engine process's threads in SCHED_RR at PRIO "
            f"(1-49, default {DEFAULT_REALTIME_PRIORITY} when given without a value); decode "
            "workers stay SCHED_OTHER and RT throttling is left on. Refused on fewer than 4 "
            "usable CPUs unless --force. Default: off"
        ),
    )
    group.add_argument(
        "--force",
        action="store_true",
        help="allow --realtime with fewer than 4 usable CPUs",
    )
    group.add_argument(
        "--native-backend",
        choices=("auto", "python", "native"),
        default=None,
        help=(
            "native acquisition, header probe, Turbo kernel and transmit encode: auto (the "
            "default: native when built, else Python with one warning), python (the reference "
            "implementations) or native (fail if not built). Sets OFDM_LINK_NATIVE"
        ),
    )


def add_overload_argument(parser: argparse.ArgumentParser) -> None:
    """``--overload-protection`` for the apps that receive (rx_app, node_app)."""

    from ofdm_link.runtime.overload import MODES

    group = parser.add_argument_group("receive overload protection (off by default)")
    group.add_argument(
        "--overload-protection",
        choices=MODES,
        default="off",
        help=(
            "off (default) for every measurement run. drop: drop a burst segment when the "
            "decode pool is full instead of stalling the receiver; give-up: stop a hopeless "
            "Turbo block after 4 iterations; both. Each has its own counter (decode_pool "
            "dropped_full, decoder payload_abandoned), apart from ordinary loss and BLER"
        ),
    )


def apply_performance_options(args: argparse.Namespace) -> None:
    """Apply the CPU and receive switches in ``main``, before any engine or worker starts."""

    import sys

    from ofdm_link.phy import native_backend
    from ofdm_link.runtime import cpu_affinity, overload, realtime

    try:
        cpu_affinity.apply(getattr(args, "cpu_affinity", None))
    except cpu_affinity.CpuAffinityError as error:
        raise SystemExit(f"--cpu-affinity: {error}") from error
    priority = getattr(args, "realtime", None)
    if priority is not None:
        try:
            realtime.check_request(priority, force=bool(getattr(args, "force", False)))
        except realtime.RealtimeError as error:
            raise SystemExit(f"--realtime: {error}") from error
        realtime.request(priority)
    backend = getattr(args, "native_backend", None)
    if backend is not None:
        native_backend.set_choice(backend)
    mode = getattr(args, "overload_protection", "off")
    if mode != "off":
        overload.request(mode)
        print(
            f"[overload] protection {mode}: dropped and abandoned bursts are counted apart "
            "from ordinary loss; do not use it for measurement runs",
            file=sys.stderr,
            flush=True,
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
        "configuration (defaults: on, 32 subframes in up to 16384 bytes, 0 ms wait, "
        "8 ms airtime, 4 ms burst interval).",
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
    # NumPy/BLAS thread budget: --cpu-cores, else acceleration.cpu_cores, else 1;
    # 0 = the CPUs each process may run on.  The source is logged at startup.
    cpu_cores: int = 1
    cpu_cores_source: str = "app default"
    # Receive feed batching: the --rx-* flags, else acceleration.rx_*, else the
    # defaults; the source names where each value came from, for the log line.
    receive_feed: ReceiveFeed = ReceiveFeed()
    receive_feed_source: str = "app default"

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

    # node_app --snr-fft-advance: a measurement option on top of phy.rx.
    advance = getattr(args, "snr_fft_advance", None)
    if advance is not None:
        rx = replace(config.phy.rx, snr_fft_advance_samples=int(advance))
        config = replace(config, phy=replace(config.phy, rx=rx))

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
    cpu_cores, cpu_cores_source = _resolve_cpu_cores(args, config)
    receive_feed, receive_feed_source = _resolve_receive_feed(args, config)
    return ResolvedOptions(
        config=config,
        profile=profile,
        mcs_entry=selected_entry,
        transport=args.transport,
        aggregation=aggregation_limits(args, config),
        cpu_cores=cpu_cores,
        cpu_cores_source=cpu_cores_source,
        receive_feed=receive_feed,
        receive_feed_source=receive_feed_source,
    )


def _resolve_receive_feed(
    args: argparse.Namespace, config: LinkConfig
) -> tuple[ReceiveFeed, str]:
    """Each value from its flag, else ``acceleration``, else :class:`ReceiveFeed`'s default.

    Returns the feed and one log phrase naming each value's source.
    """

    default = ReceiveFeed()
    sources = []
    values = []
    for name, flag, configured, built_in in (
        ("rx_min_feed_samples", "--rx-min-feed-samples",
         config.acceleration.rx_min_feed_samples, default.min_samples),
        ("rx_max_merge_wait_ms", "--rx-max-merge-wait-ms",
         config.acceleration.rx_max_merge_wait_ms, default.max_wait_s * 1e3),
    ):
        given = getattr(args, name, None)
        if given is not None:
            value, source = given, flag
        elif configured is not None:
            value, source = configured, f"configuration acceleration.{name}"
        else:
            value, source = built_in, "app default"
        values.append(value)
        sources.append(f"{name} {value:g} from {source}")
    feed = ReceiveFeed(min_samples=int(values[0]), max_wait_s=float(values[1]) / 1e3)
    return feed, "; ".join(sources)


def _resolve_cpu_cores(args: argparse.Namespace, config: LinkConfig) -> tuple[int, str]:
    from ofdm_link.runtime.cpu_budget import DEFAULT_CPU_CORES

    flag = getattr(args, "cpu_cores", None)
    if flag is not None:
        return int(flag), "--cpu-cores"
    configured = config.acceleration.cpu_cores
    if configured is None:
        return DEFAULT_CPU_CORES, "app default"
    return int(configured), "configuration acceleration.cpu_cores"


def resolve_decode_workers(
    args: argparse.Namespace, config: LinkConfig, default: int
) -> int:
    """``--decode-workers``, else ``acceleration.decode_workers``, else the app's default.

    ``acceleration.decode_workers: 0`` means the app's default too: v2.0.0 to
    v2.1.2 shipped 0 documented as "auto" while nothing read the key.  The
    result is written back to ``args.decode_workers`` (and where it came from
    to ``args.decode_workers_source``) so everything that reads the parsed
    options sees the count that is actually used.
    """

    flag = getattr(args, "decode_workers", None)
    configured = config.acceleration.decode_workers
    if flag is not None:
        workers, source = int(flag), "--decode-workers"
    elif configured is None:
        workers, source = default, "app default"
    elif configured == 0:
        workers, source = default, "configuration acceleration.decode_workers: 0 = app default"
    else:
        workers, source = int(configured), "configuration acceleration.decode_workers"
    args.decode_workers = workers
    args.decode_workers_source = source
    return workers


def apply_cpu_budget(resolved: ResolvedOptions, args: argparse.Namespace | None = None):
    """Bound this process's numeric threads and log the acceleration settings.

    Call it in ``main`` after :func:`apply_performance_options`, so that
    ``cpu_cores: 0`` counts the CPUs ``--cpu-affinity`` left this process, and
    before any engine starts.  The engine gets ``resolved.cpu_cores`` as an
    argument; decode workers keep one thread each whatever the budget.  Logs
    one line with the budget, the decode worker count (when ``args`` has one)
    and where each came from.
    """

    import sys

    from ofdm_link.runtime import cpu_budget

    budget = cpu_budget.apply_cpu_budget(resolved.cpu_cores)
    cores = (
        f"{budget.cores} (0 = the CPUs this process may run on)"
        if resolved.cpu_cores == 0 else str(budget.cores)
    )
    line = f"[acceleration] cpu_cores {cores} from {resolved.cpu_cores_source}"
    source = getattr(args, "decode_workers_source", None)
    if source is not None:
        line += f"; decode_workers {args.decode_workers} from {source}"
    else:
        line += "; no decode workers (this app does not decode)"
    if hasattr(args, "rx_min_feed_samples"):
        line += f"; {resolved.receive_feed_source}"
    print(line, file=sys.stderr, flush=True)
    return budget


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
    "add_overload_argument",
    "add_performance_arguments",
    "add_receive_feed_arguments",
    "aggregation_limits",
    "apply_cpu_budget",
    "apply_performance_options",
    "decode_workers_argument",
    "build_sink",
    "build_source",
    "clear_ready",
    "evidence_banner",
    "mark_ready",
    "require_rf_capability",
    "resolve",
    "resolve_decode_workers",
    "rx_max_merge_wait_ms_argument",
    "rx_min_feed_samples_argument",
    "settings_for",
]
