"""Validated configuration loading for the OFDM link.

The public interface is intentionally small: callers load one base YAML file and
zero or more overlays, then receive an immutable :class:`LinkConfig`.
"""

from __future__ import annotations

import copy
import ipaddress
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when a configuration file violates the project schema."""


@dataclass(frozen=True)
class NodeConfig:
    role: str
    node_id: int


@dataclass(frozen=True)
class NetworkConfig:
    interface_name: str
    address: str
    mtu: int


@dataclass(frozen=True)
class FecConfig:
    scheme: str
    code_rate: str
    decision_mode: str
    max_iterations: int


@dataclass(frozen=True)
class SyncConfigSection:
    """Acquisition thresholds, kept settable because they are bench-measured.

    ``detection_threshold`` gates the Schmidl--Cox repeated-half metric and
    ``correlation_threshold`` gates the coherent preamble correlation.  Their
    values follow from measurements of this radio pair, not from theory, so
    they belong in configuration rather than in a source constant.
    """

    detection_threshold: float
    correlation_threshold: float


@dataclass(frozen=True)
class V5TxConfigSection:
    """Wire-v5 transmit settings (``phy.v5``); all are experiment toggles.

    The guards are zeros around each pushed burst.  The STF absorbs the
    radio's leading transient, so 16 samples before suffice; 64 after flush
    the TX filters.  Both stay settable until the hardware guard sweep (H2)
    has confirmed them.  ``scrambler: false`` sends seed 0, which the SIG
    signals, so receivers need no matching setting.
    """

    guard_pre_samples: int = 16
    guard_post_samples: int = 64
    scrambler: bool = True


@dataclass(frozen=True)
class RxConfigSection:
    """Receive settings for the dual-family receiver (``phy.rx``).

    Everything except ``max_psdu_bytes`` and ``turbo_max_iterations`` is an
    experiment toggle or a threshold to be set from measurements: the v5
    thresholds are predictions until signal-free air has been scanned (H1).
    """

    legacy_acquisition: bool = True
    v5_acquisition: bool = True
    v5_detection_threshold: float = 0.10
    v5_stf_threshold: float = 0.20
    v5_correlation_threshold: float = 0.25
    v5_balance_ratio: float = 0.5
    acquisition_prefilter: bool = False
    ltf_average: bool = True
    channel_smoothing: bool = True
    llr_noise_source: str = "max"
    max_psdu_bytes: int = 16384
    turbo_max_iterations: int = 8
    fft_backoff_samples: int = 3


@dataclass(frozen=True)
class PhyConfig:
    sample_rate: int
    fft_len: int
    cyclic_prefix_len: int
    mcs: str
    center_frequency: int
    fec: FecConfig
    sync: SyncConfigSection
    v5: V5TxConfigSection = V5TxConfigSection()
    rx: RxConfigSection = RxConfigSection()

    @property
    def subcarrier_spacing(self) -> float:
        return self.sample_rate / self.fft_len


@dataclass(frozen=True)
class TddConfig:
    link_id: int
    beacon_duration_s: float
    downlink_duration_s: float
    guard_duration_s: float
    uplink_duration_s: float
    beacon_timeout_s: float

    @property
    def superframe_duration(self) -> float:
        return (
            self.beacon_duration_s
            + self.downlink_duration_s
            + 2 * self.guard_duration_s
            + self.uplink_duration_s
        )


@dataclass(frozen=True)
class AggregationConfigSection:
    """How a v5 transmitter packs queued datagrams into one burst.

    Opportunistic by default: whatever is queued when the radio can take the
    next burst goes into it, up to the caps, with no added wait.  The airtime
    cap bounds sample-clock drift, peak-normalization loss and decode latency
    per burst; ``enabled: false`` sends one datagram per burst (experiment).
    The 8 ms default keeps a burst at 10 MS/s within the 80 000 samples the
    receiver's timing correction covers at 50 ppm (4 samples), and
    validation refuses a cap beyond that at the configured sample rate.
    ``min_burst_interval_ms`` caps the burst rate (4 ms: at most 250/s) by
    holding a burst back until that long after the previous one, filling it
    meanwhile; it adds latency only when bursts are back to back.  0 disables.
    ``max_psdu_bytes`` may not exceed the receiver's ``phy.rx.max_psdu_bytes``.
    """

    enabled: bool = True
    max_wait_ms: float = 0.0
    max_subframes: int = 16
    max_psdu_bytes: int = 8192
    max_burst_airtime_ms: float = 8.0
    min_burst_interval_ms: float = 4.0


@dataclass(frozen=True)
class MacConfig:
    duplex: str
    arq_window: int
    max_retries: int
    max_fragment_payload: int
    max_pending_sdus: int
    retransmission_timeout_s: float
    tdd: TddConfig
    aggregation: AggregationConfigSection = AggregationConfigSection()


@dataclass(frozen=True)
class RadioConfig:
    adapter: str
    rf_enabled: bool
    device_args: str
    rx_gain: float
    tx_gain: float
    bandwidth: float
    rx_antenna: str
    tx_antenna: str
    clock_source: str


@dataclass(frozen=True)
class SimulationConfig:
    profile: str
    snr_db: float
    cfo_subcarrier_fraction: float
    sample_rate_offset_ppm: float
    amplitude_min: float
    amplitude_max: float
    taps: tuple[complex, ...]


@dataclass(frozen=True)
class GuiConfig:
    enabled: bool


@dataclass(frozen=True)
class AccelerationConfig:
    backend: str
    device: str
    decode_workers: int
    cpu_cores: int


@dataclass(frozen=True)
class LinkConfig:
    schema_version: int
    node: NodeConfig
    network: NetworkConfig
    phy: PhyConfig
    mac: MacConfig
    radio: RadioConfig
    simulation: SimulationConfig
    gui: GuiConfig
    acceleration: AccelerationConfig

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable copy suitable for logs and diagnostics."""

        result = asdict(self)
        result["simulation"]["taps"] = [[tap.real, tap.imag] for tap in self.simulation.taps]
        return result


def load_config(path: str | Path, overlays: Sequence[str | Path] = ()) -> LinkConfig:
    """Load, merge, and validate a base YAML configuration and optional overlays.

    Later overlays replace scalar values and recursively merge mappings. Lists are
    replaced as a whole. Unknown keys are rejected by :func:`validate_config`.
    """

    merged = _read_yaml(path)
    for overlay in overlays:
        merged = _deep_merge(merged, _read_yaml(overlay))
    return validate_config(merged)


def validate_config(raw: Mapping[str, Any]) -> LinkConfig:
    """Validate a decoded YAML mapping and return an immutable configuration."""

    root = _mapping(raw, "configuration")
    _only_keys(
        root,
        {
            "schema_version",
            "node",
            "network",
            "phy",
            "mac",
            "radio",
            "simulation",
            "gui",
            "acceleration",
        },
        "configuration",
    )

    schema_version = _integer(root, "schema_version", "configuration")
    if schema_version != 1:
        raise ConfigError(f"schema_version must be 1, got {schema_version}")

    node_raw = _section(root, "node", {"role", "node_id"})
    node = NodeConfig(
        role=_choice(node_raw, "role", "node", {"coordinator", "station"}),
        node_id=_bounded_int(node_raw, "node_id", "node", 0, 255),
    )

    network_raw = _section(root, "network", {"interface_name", "address", "mtu"})
    interface_name = _string(network_raw, "interface_name", "network")
    if not interface_name.startswith("ofdm") or len(interface_name) > 15:
        raise ConfigError("network.interface_name must start with 'ofdm' and be <= 15 chars")
    address = _string(network_raw, "address", "network")
    try:
        ipaddress.ip_interface(address)
    except ValueError as error:
        raise ConfigError(f"network.address is invalid: {address}") from error
    network = NetworkConfig(
        interface_name=interface_name,
        address=address,
        mtu=_bounded_int(network_raw, "mtu", "network", 576, 9000),
    )

    phy_raw = _section(
        root,
        "phy",
        {"sample_rate", "fft_len", "cyclic_prefix_len", "mcs", "center_frequency", "fec"},
        optional={"sync", "v5", "rx"},
    )
    sync = _sync_section(phy_raw)
    v5_tx = _v5_tx_section(phy_raw)
    rx = _rx_section(phy_raw)
    fec_raw = _mapping(phy_raw.get("fec"), "phy.fec")
    _only_keys(
        fec_raw,
        {"scheme", "code_rate", "decision_mode"},
        "phy.fec",
        optional={"max_iterations"},
    )
    fec = FecConfig(
        scheme=_choice(
            fec_raw,
            "scheme",
            "phy.fec",
            {"convolutional", "turbo", "uncoded", "turbo_rm"},
        ),
        code_rate=_choice(
            fec_raw, "code_rate", "phy.fec", {"1", "1/2", "1/3", "2/3", "3/4"}
        ),
        decision_mode=_choice(
            fec_raw,
            "decision_mode",
            "phy.fec",
            {"hard", "soft", "turbo"},
        ),
        max_iterations=(
            8
            if "max_iterations" not in fec_raw
            else _bounded_int(fec_raw, "max_iterations", "phy.fec", 2, 8)
        ),
    )
    if fec.max_iterations not in {2, 4, 6, 8}:
        raise ConfigError("phy.fec.max_iterations must be one of: 2, 4, 6, 8")
    if fec.scheme == "convolutional" and (
        fec.code_rate not in {"1/2", "1/3"} or fec.decision_mode not in {"hard", "soft"}
    ):
        raise ConfigError(
            "convolutional FEC requires code_rate 1/2 or 1/3 and hard or soft "
            "decision_mode"
        )
    if fec.scheme == "turbo" and (
        fec.code_rate != "1/3" or fec.decision_mode != "turbo"
    ):
        raise ConfigError("Turbo FEC requires code_rate 1/3 and turbo decision_mode")
    if fec.scheme == "turbo_rm" and (
        fec.code_rate not in {"1/3", "1/2", "2/3", "3/4"} or fec.decision_mode != "turbo"
    ):
        raise ConfigError(
            "rate-matched Turbo FEC (wire v5) requires code_rate 1/3, 1/2, 2/3 or 3/4 "
            "and turbo decision_mode"
        )
    if fec.scheme != "turbo_rm" and fec.code_rate in {"2/3", "3/4"}:
        raise ConfigError("code_rate 2/3 and 3/4 exist only for the turbo_rm scheme")
    if fec.scheme == "uncoded" and (
        fec.code_rate != "1" or fec.decision_mode not in {"hard", "soft"}
    ):
        raise ConfigError(
            "uncoded FEC requires code_rate 1 and hard or soft decision_mode"
        )
    fft_len = _bounded_int(phy_raw, "fft_len", "phy", 64, 4096)
    if fft_len & (fft_len - 1):
        raise ConfigError("phy.fft_len must be a power of two")
    cyclic_prefix_len = _bounded_int(
        phy_raw, "cyclic_prefix_len", "phy", 1, fft_len - 1
    )
    phy = PhyConfig(
        sample_rate=_bounded_int(phy_raw, "sample_rate", "phy", 100_000, 61_440_000),
        fft_len=fft_len,
        cyclic_prefix_len=cyclic_prefix_len,
        mcs=_choice(phy_raw, "mcs", "phy", {"qpsk", "qam16", "qam64"}),
        center_frequency=_bounded_int(
            phy_raw, "center_frequency", "phy", 1_000_000, 10_000_000_000
        ),
        fec=fec,
        sync=sync,
        v5=v5_tx,
        rx=rx,
    )
    if phy.mcs == "qam64" and fec.scheme != "turbo_rm":
        raise ConfigError("phy.mcs qam64 is carried only by the turbo_rm scheme (wire v5)")

    mac_raw = _section(
        root,
        "mac",
        {
            "duplex",
            "arq_window",
            "max_retries",
            "max_fragment_payload",
            "max_pending_sdus",
            "retransmission_timeout_s",
            "tdd",
        },
        optional={"aggregation"},
    )
    tdd_raw = _section(
        mac_raw,
        "tdd",
        {
            "link_id",
            "beacon_duration_s",
            "downlink_duration_s",
            "guard_duration_s",
            "uplink_duration_s",
            "beacon_timeout_s",
        },
        parent="mac",
    )
    tdd = TddConfig(
        link_id=_bounded_int(tdd_raw, "link_id", "mac.tdd", 0, (1 << 32) - 1),
        beacon_duration_s=_positive_number(tdd_raw, "beacon_duration_s", "mac.tdd"),
        downlink_duration_s=_positive_number(tdd_raw, "downlink_duration_s", "mac.tdd"),
        guard_duration_s=_positive_number(tdd_raw, "guard_duration_s", "mac.tdd"),
        uplink_duration_s=_positive_number(tdd_raw, "uplink_duration_s", "mac.tdd"),
        beacon_timeout_s=_positive_number(tdd_raw, "beacon_timeout_s", "mac.tdd"),
    )
    if tdd.beacon_timeout_s <= tdd.superframe_duration:
        raise ConfigError("mac.tdd.beacon_timeout_s must exceed one superframe duration")
    mac = MacConfig(
        duplex=_choice(mac_raw, "duplex", "mac", {"tdd", "fdd"}),
        arq_window=_bounded_int(mac_raw, "arq_window", "mac", 1, 32),
        max_retries=_bounded_int(mac_raw, "max_retries", "mac", 0, 64),
        max_fragment_payload=_bounded_int(
            mac_raw, "max_fragment_payload", "mac", 1, 65529
        ),
        max_pending_sdus=_bounded_int(mac_raw, "max_pending_sdus", "mac", 1, 65535),
        retransmission_timeout_s=_positive_number(
            mac_raw, "retransmission_timeout_s", "mac"
        ),
        tdd=tdd,
        aggregation=_aggregation_section(mac_raw),
    )

    radio_raw = _section(
        root,
        "radio",
        {
            "adapter",
            "rf_enabled",
            "device_args",
            "rx_gain",
            "tx_gain",
            "bandwidth",
            "rx_antenna",
            "tx_antenna",
            "clock_source",
        },
    )
    device_args = radio_raw.get("device_args")
    if not isinstance(device_args, str) or (
        device_args and re.fullmatch(r"serial=[A-Za-z0-9]+", device_args) is None
    ):
        raise ConfigError("radio.device_args must be empty or serial=<serial>")
    radio = RadioConfig(
        adapter=_choice(radio_raw, "adapter", "radio", {"simulation", "uhd"}),
        rf_enabled=_boolean(radio_raw, "rf_enabled", "radio"),
        device_args=device_args,
        rx_gain=_bounded_number(radio_raw, "rx_gain", "radio", 0.0, 76.0),
        tx_gain=_bounded_number(radio_raw, "tx_gain", "radio", 0.0, 89.8),
        bandwidth=_bounded_number(
            radio_raw, "bandwidth", "radio", 200_000.0, 56_000_000.0
        ),
        rx_antenna=_choice(radio_raw, "rx_antenna", "radio", {"rx2", "tx/rx"}).upper(),
        tx_antenna=_choice(radio_raw, "tx_antenna", "radio", {"tx/rx"}).upper(),
        clock_source=_choice(radio_raw, "clock_source", "radio", {"internal"}),
    )

    simulation_raw = _section(
        root,
        "simulation",
        {
            "profile",
            "snr_db",
            "cfo_subcarrier_fraction",
            "sample_rate_offset_ppm",
            "amplitude_min",
            "amplitude_max",
            "taps",
        },
    )
    amplitude_min = _number(simulation_raw, "amplitude_min", "simulation")
    amplitude_max = _number(simulation_raw, "amplitude_max", "simulation")
    if amplitude_min <= 0 or amplitude_max < amplitude_min:
        raise ConfigError("simulation amplitude range must satisfy 0 < min <= max")
    cfo = _number(simulation_raw, "cfo_subcarrier_fraction", "simulation")
    if not 0 <= abs(cfo) <= 0.5:
        raise ConfigError("simulation.cfo_subcarrier_fraction magnitude must be <= 0.5")
    sfo = _number(simulation_raw, "sample_rate_offset_ppm", "simulation")
    if abs(sfo) > 1000:
        raise ConfigError("simulation.sample_rate_offset_ppm magnitude must be <= 1000")
    taps = _complex_taps(simulation_raw.get("taps"))
    simulation = SimulationConfig(
        profile=_string(simulation_raw, "profile", "simulation"),
        snr_db=_number(simulation_raw, "snr_db", "simulation"),
        cfo_subcarrier_fraction=cfo,
        sample_rate_offset_ppm=sfo,
        amplitude_min=amplitude_min,
        amplitude_max=amplitude_max,
        taps=taps,
    )

    gui_raw = _section(root, "gui", {"enabled"})
    gui = GuiConfig(enabled=_boolean(gui_raw, "enabled", "gui"))

    acceleration_raw = _section(
        root,
        "acceleration",
        {"backend", "device", "decode_workers", "cpu_cores"},
    )
    acceleration_device = _string(acceleration_raw, "device", "acceleration").lower()
    if not re.fullmatch(r"cpu|cuda(?::(?:0|[1-9][0-9]*))?", acceleration_device):
        raise ConfigError(
            "acceleration.device must be 'cpu', 'cuda', or 'cuda:<non-negative index>'"
        )
    acceleration = AccelerationConfig(
        backend=_choice(acceleration_raw, "backend", "acceleration", {"native", "sionna"}),
        device=acceleration_device,
        decode_workers=_bounded_int(
            acceleration_raw,
            "decode_workers",
            "acceleration",
            0,
            256,
        ),
        cpu_cores=_bounded_int(
            acceleration_raw,
            "cpu_cores",
            "acceleration",
            0,
            256,
        ),
    )
    if acceleration.backend == "native" and acceleration.device != "cpu":
        raise ConfigError("acceleration.device must be 'cpu' for the native backend")
    if fec.decision_mode in {"soft", "turbo"} and acceleration.backend != "native":
        raise ConfigError(
            f"phy.fec.decision_mode {fec.decision_mode} requires the native CPU backend; "
            "Sionna remains an optional oracle"
        )

    if radio.adapter == "uhd" and radio.rf_enabled:
        raise ConfigError(
            "radio.rf_enabled must remain false in YAML; use the explicit --enable-rf CLI guard"
        )
    if radio.adapter == "uhd" and phy.sample_rate not in {5_000_000, 10_000_000, 20_000_000}:
        raise ConfigError("phy.sample_rate must be 5, 10, or 20 MS/s for the B210 adapter")
    if radio.adapter == "uhd" and not 70_000_000 <= phy.center_frequency <= 6_000_000_000:
        raise ConfigError("phy.center_frequency must be within 70 MHz to 6 GHz for B210")
    _check_v5_link_limits(phy, mac.aggregation)

    return LinkConfig(
        schema_version=schema_version,
        node=node,
        network=network,
        phy=phy,
        mac=mac,
        radio=radio,
        simulation=simulation,
        gui=gui,
        acceleration=acceleration,
    )


def _read_yaml(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as stream:
            decoded = yaml.safe_load(stream)
    except OSError as error:
        raise ConfigError(f"cannot read configuration {config_path}: {error}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"invalid YAML in {config_path}: {error}") from error
    if decoded is None:
        raise ConfigError(f"configuration {config_path} is empty")
    return dict(_mapping(decoded, str(config_path)))


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(dict(base))
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _section(
    parent_value: Mapping[str, Any],
    key: str,
    allowed: set[str],
    *,
    parent: str = "configuration",
    optional: set[str] | None = None,
) -> Mapping[str, Any]:
    section = _mapping(parent_value.get(key), f"{parent}.{key}")
    _only_keys(section, allowed, f"{parent}.{key}", optional=optional or set())
    return section


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{location} must be a mapping")
    return value


def _only_keys(
    value: Mapping[str, Any],
    allowed: set[str],
    location: str,
    *,
    optional: set[str] | None = None,
) -> None:
    optional_keys = optional or set()
    unknown = sorted(set(value) - allowed - optional_keys)
    if unknown:
        raise ConfigError(f"unknown key(s) in {location}: {', '.join(unknown)}")
    missing = sorted(allowed - set(value))
    if missing:
        raise ConfigError(f"missing key(s) in {location}: {', '.join(missing)}")


def _string(value: Mapping[str, Any], key: str, location: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ConfigError(f"{location}.{key} must be a non-empty string")
    return result


def _integer(value: Mapping[str, Any], key: str, location: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int):
        raise ConfigError(f"{location}.{key} must be an integer")
    return result


def _bounded_int(
    value: Mapping[str, Any], key: str, location: str, minimum: int, maximum: int
) -> int:
    result = _integer(value, key, location)
    if not minimum <= result <= maximum:
        raise ConfigError(f"{location}.{key} must be between {minimum} and {maximum}")
    return result


def _number(value: Mapping[str, Any], key: str, location: str) -> float:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ConfigError(f"{location}.{key} must be a number")
    return float(result)


def _positive_number(value: Mapping[str, Any], key: str, location: str) -> float:
    result = _number(value, key, location)
    if not math.isfinite(result) or result <= 0:
        raise ConfigError(f"{location}.{key} must be finite and positive")
    return result


#: Bench-measured defaults, used when ``phy.sync`` is absent.  They match
#: :class:`ofdm_link.phy.SyncConfig` so an unconfigured caller and a configured
#: one acquire identically.  Lowered from 0.40/0.40 once the receiver could
#: decode the bursts they were rejecting: see experiments/rx_improvement.
DEFAULT_DETECTION_THRESHOLD = 0.25
DEFAULT_CORRELATION_THRESHOLD = 0.30

#: Below this the coherent correlation stops separating a preamble from noise:
#: 140 s of measured signal-free air put the noise ceiling at 0.199.
MINIMUM_CORRELATION_THRESHOLD = 0.25


def _sync_section(phy_raw: Mapping[str, Any]) -> SyncConfigSection:
    """Validate the optional ``phy.sync`` block.

    Absent, both thresholds take their measured defaults, so every existing
    configuration keeps working and acquires exactly as the library does.
    """

    if "sync" not in phy_raw:
        return SyncConfigSection(
            detection_threshold=DEFAULT_DETECTION_THRESHOLD,
            correlation_threshold=DEFAULT_CORRELATION_THRESHOLD,
        )
    sync_raw = _mapping(phy_raw.get("sync"), "phy.sync")
    _only_keys(
        sync_raw,
        set(),
        "phy.sync",
        optional={"detection_threshold", "correlation_threshold"},
    )
    detection = (
        DEFAULT_DETECTION_THRESHOLD
        if "detection_threshold" not in sync_raw
        else _bounded_number(sync_raw, "detection_threshold", "phy.sync", 0.05, 1.0)
    )
    correlation = (
        DEFAULT_CORRELATION_THRESHOLD
        if "correlation_threshold" not in sync_raw
        else _bounded_number(
            sync_raw,
            "correlation_threshold",
            "phy.sync",
            MINIMUM_CORRELATION_THRESHOLD,
            1.0,
        )
    )
    return SyncConfigSection(
        detection_threshold=detection,
        correlation_threshold=correlation,
    )


def _v5_tx_section(phy_raw: Mapping[str, Any]) -> V5TxConfigSection:
    """Validate the optional ``phy.v5`` block; absent keys take the defaults."""

    defaults = V5TxConfigSection()
    if "v5" not in phy_raw:
        return defaults
    raw = _mapping(phy_raw.get("v5"), "phy.v5")
    _only_keys(
        raw,
        set(),
        "phy.v5",
        optional={"guard_pre_samples", "guard_post_samples", "scrambler"},
    )
    return V5TxConfigSection(
        guard_pre_samples=(
            _bounded_int(raw, "guard_pre_samples", "phy.v5", 0, 256)
            if "guard_pre_samples" in raw
            else defaults.guard_pre_samples
        ),
        guard_post_samples=(
            _bounded_int(raw, "guard_post_samples", "phy.v5", 0, 256)
            if "guard_post_samples" in raw
            else defaults.guard_post_samples
        ),
        scrambler=(
            _boolean(raw, "scrambler", "phy.v5") if "scrambler" in raw else defaults.scrambler
        ),
    )


def _rx_section(phy_raw: Mapping[str, Any]) -> RxConfigSection:
    """Validate the optional ``phy.rx`` block; absent keys take the defaults.

    The v5 coherent thresholds share the legacy floor's reasoning: below
    about 0.2 the coherent stages stop separating a preamble from noise, so
    they are bounded well away from zero.
    """

    defaults = RxConfigSection()
    if "rx" not in phy_raw:
        return defaults
    raw = _mapping(phy_raw.get("rx"), "phy.rx")
    booleans = {
        "legacy_acquisition",
        "v5_acquisition",
        "acquisition_prefilter",
        "ltf_average",
        "channel_smoothing",
    }
    numbers = {
        "v5_detection_threshold": (0.02, 1.0),
        "v5_stf_threshold": (0.10, 1.0),
        "v5_correlation_threshold": (0.10, 1.0),
        "v5_balance_ratio": (0.05, 1.0),
    }
    _only_keys(
        raw,
        set(),
        "phy.rx",
        optional=booleans
        | set(numbers)
        | {
            "llr_noise_source",
            "max_psdu_bytes",
            "turbo_max_iterations",
            "fft_backoff_samples",
        },
    )
    values: dict[str, Any] = {}
    for key in booleans & set(raw):
        values[key] = _boolean(raw, key, "phy.rx")
    for key, (low, high) in numbers.items():
        if key in raw:
            values[key] = _bounded_number(raw, key, "phy.rx", low, high)
    if "llr_noise_source" in raw:
        values["llr_noise_source"] = _choice(
            raw, "llr_noise_source", "phy.rx", {"max", "header", "ltf"}
        )
    if "max_psdu_bytes" in raw:
        values["max_psdu_bytes"] = _bounded_int(raw, "max_psdu_bytes", "phy.rx", 19, 65535)
    if "turbo_max_iterations" in raw:
        values["turbo_max_iterations"] = _bounded_int(
            raw, "turbo_max_iterations", "phy.rx", 2, 8
        )
        if values["turbo_max_iterations"] not in {2, 4, 6, 8}:
            raise ConfigError("phy.rx.turbo_max_iterations must be one of: 2, 4, 6, 8")
    if "fft_backoff_samples" in raw:
        values["fft_backoff_samples"] = _bounded_int(raw, "fft_backoff_samples", "phy.rx", 0, 8)
    section = RxConfigSection(**{**asdict(defaults), **values})
    if not (section.legacy_acquisition or section.v5_acquisition):
        raise ConfigError("phy.rx must leave at least one acquisition family enabled")
    return section


def _check_v5_link_limits(phy: PhyConfig, aggregation: AggregationConfigSection) -> None:
    """Refuse transmit caps this configuration's own receiver cannot take.

    One configuration describes both ends of the link, so an aggregate the
    transmitter may build must be one the receiver accepts: a PSDU above
    ``phy.rx.max_psdu_bytes`` is rejected as a header failure, and a burst
    longer than the receiver's timing correction covers at the worst-case
    50 ppm sample-clock offset drifts out of it.
    """

    from ofdm_link.phy.burst_v5 import V5_MAX_TRACKED_BURST_SAMPLES, WORST_CASE_SFO_PPM

    if aggregation.max_psdu_bytes > phy.rx.max_psdu_bytes:
        raise ConfigError(
            f"mac.aggregation.max_psdu_bytes ({aggregation.max_psdu_bytes}) exceeds "
            f"phy.rx.max_psdu_bytes ({phy.rx.max_psdu_bytes}): receivers would reject "
            "the largest aggregates; lower the first or raise the second"
        )
    longest_ms = V5_MAX_TRACKED_BURST_SAMPLES / phy.sample_rate * 1e3
    if aggregation.max_burst_airtime_ms > longest_ms:
        raise ConfigError(
            f"mac.aggregation.max_burst_airtime_ms must be at most {longest_ms:g} ms at "
            f"{phy.sample_rate} S/s: {V5_MAX_TRACKED_BURST_SAMPLES} samples is the longest "
            f"burst whose drift at {WORST_CASE_SFO_PPM:g} ppm the receiver's timing "
            "correction covers"
        )


def _aggregation_section(mac_raw: Mapping[str, Any]) -> AggregationConfigSection:
    """Validate the optional ``mac.aggregation`` block (wire-v5 transmitters)."""

    defaults = AggregationConfigSection()
    if "aggregation" not in mac_raw:
        return defaults
    raw = _mapping(mac_raw.get("aggregation"), "mac.aggregation")
    _only_keys(
        raw,
        set(),
        "mac.aggregation",
        optional={
            "enabled",
            "max_wait_ms",
            "max_subframes",
            "max_psdu_bytes",
            "max_burst_airtime_ms",
            "min_burst_interval_ms",
        },
    )
    values: dict[str, Any] = {}
    if "enabled" in raw:
        values["enabled"] = _boolean(raw, "enabled", "mac.aggregation")
    if "max_wait_ms" in raw:
        values["max_wait_ms"] = _bounded_number(raw, "max_wait_ms", "mac.aggregation", 0.0, 1000.0)
    if "max_subframes" in raw:
        values["max_subframes"] = _bounded_int(raw, "max_subframes", "mac.aggregation", 1, 64)
    if "max_psdu_bytes" in raw:
        values["max_psdu_bytes"] = _bounded_int(
            raw, "max_psdu_bytes", "mac.aggregation", 19, 65535
        )
    if "max_burst_airtime_ms" in raw:
        values["max_burst_airtime_ms"] = _bounded_number(
            raw, "max_burst_airtime_ms", "mac.aggregation", 0.1, 100.0
        )
    if "min_burst_interval_ms" in raw:
        values["min_burst_interval_ms"] = _bounded_number(
            raw, "min_burst_interval_ms", "mac.aggregation", 0.0, 50.0
        )
    return AggregationConfigSection(**{**asdict(defaults), **values})


def _bounded_number(
    value: Mapping[str, Any],
    key: str,
    location: str,
    minimum: float,
    maximum: float,
) -> float:
    result = _number(value, key, location)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ConfigError(f"{location}.{key} must be between {minimum:g} and {maximum:g}")
    return result


def _boolean(value: Mapping[str, Any], key: str, location: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise ConfigError(f"{location}.{key} must be true or false")
    return result


def _choice(value: Mapping[str, Any], key: str, location: str, choices: set[str]) -> str:
    result = _string(value, key, location).lower()
    if result not in choices:
        raise ConfigError(f"{location}.{key} must be one of: {', '.join(sorted(choices))}")
    return result


def _complex_taps(value: Any) -> tuple[complex, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("simulation.taps must be a non-empty list")
    taps: list[complex] = []
    for index, pair in enumerate(value):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ConfigError(f"simulation.taps[{index}] must be [real, imag]")
        real, imag = pair
        if (
            isinstance(real, bool)
            or isinstance(imag, bool)
            or not isinstance(real, (int, float))
            or not isinstance(imag, (int, float))
        ):
            raise ConfigError(f"simulation.taps[{index}] values must be numbers")
        taps.append(complex(real, imag))
    return tuple(taps)
