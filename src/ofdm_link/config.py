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
class PhyConfig:
    sample_rate: int
    fft_len: int
    cyclic_prefix_len: int
    mcs: str
    center_frequency: int
    fec: FecConfig
    sync: SyncConfigSection

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
class MacConfig:
    duplex: str
    arq_window: int
    max_retries: int
    max_fragment_payload: int
    max_pending_sdus: int
    retransmission_timeout_s: float
    tdd: TddConfig


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
        optional={"sync"},
    )
    sync = _sync_section(phy_raw)
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
            {"convolutional", "turbo", "uncoded"},
        ),
        code_rate=_choice(fec_raw, "code_rate", "phy.fec", {"1", "1/2", "1/3"}),
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
        mcs=_choice(phy_raw, "mcs", "phy", {"qpsk", "qam16"}),
        center_frequency=_bounded_int(
            phy_raw, "center_frequency", "phy", 1_000_000, 10_000_000_000
        ),
        fec=fec,
        sync=sync,
    )

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
#: one acquire identically.
DEFAULT_DETECTION_THRESHOLD = 0.40
DEFAULT_CORRELATION_THRESHOLD = 0.40

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
