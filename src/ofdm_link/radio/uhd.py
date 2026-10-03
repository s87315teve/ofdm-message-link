"""Lazy, safety-gated GNU Radio UHD block construction for one B210 channel.

This adapter only configures GNU Radio ``usrp_source`` and ``usrp_sink``
blocks.  It does not create or start a flowgraph.  The implementation and its
UHD call contract is covered by fakes and exercised on the two documented
devices. Device preflight passed; OTA energy transfer has not passed.

Constructing a TX block can make hardware capable of emitting RF once a caller
connects and starts it.  Therefore the sink factory requires a token produced
by an exact runtime acknowledgement.  A YAML boolean is deliberately
insufficient.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SUPPORTED_SAMPLE_RATES = frozenset({5_000_000, 10_000_000, 20_000_000})
RF_ENABLE_ACKNOWLEDGEMENT = "I acknowledge that this process will transmit RF"
TX_LENGTH_TAG = "ofdm_tx_len"
TX_TIME_TAG = "tx_time"

_RF_TOKEN_PROOF = object()
_SERIAL_SELECTOR = re.compile(r"serial=[A-Za-z0-9]+", re.ASCII)
_SERIAL = re.compile(r"[A-Za-z0-9]+", re.ASCII)
_MIN_CENTER_FREQUENCY_HZ = 70_000_000.0
_MAX_CENTER_FREQUENCY_HZ = 6_000_000_000.0
_MIN_BANDWIDTH_HZ = 200_000.0
_MAX_BANDWIDTH_HZ = 56_000_000.0
_MAX_RX_GAIN_DB = 76.0
_MAX_TX_GAIN_DB = 89.8


class UhdUnavailableError(RuntimeError):
    """Raised when a block is requested without GNU Radio's UHD bindings."""


class RFTransmissionDisabledError(RuntimeError):
    """Raised before any UHD access when TX lacks explicit acknowledgement."""


class RFEnableToken:
    """Capability created only by :func:`acknowledge_rf_transmission`.

    This is an accidental-misconfiguration guard, not a security boundary.
    """

    __slots__ = ("__proof",)

    def __init__(self, proof: object) -> None:
        if proof is not _RF_TOKEN_PROOF:
            raise TypeError(
                "RFEnableToken must be created by acknowledge_rf_transmission"
            )
        self.__proof = proof

    def _is_valid(self) -> bool:
        return self.__proof is _RF_TOKEN_PROOF


def acknowledge_rf_transmission(acknowledgement: str) -> RFEnableToken:
    """Return a process-local TX capability after an exact typed phrase.

    Callers must collect this acknowledgement at runtime.  In particular,
    ``True`` from a YAML ``enable_rf`` key cannot satisfy this interface.
    """

    if not isinstance(acknowledgement, str):
        raise TypeError("RF acknowledgement must be a string")
    if acknowledgement != RF_ENABLE_ACKNOWLEDGEMENT:
        raise ValueError("RF transmission requires the exact acknowledgement phrase")
    return RFEnableToken(_RF_TOKEN_PROOF)


@dataclass(frozen=True, slots=True)
class B210Settings:
    """Validated single-channel B210 settings for the 5/10/20 MS/s sweep."""

    device_args: str = ""
    sample_rate: int = 10_000_000
    center_frequency: float = 2_450_000_000.0
    rx_gain: float = 20.0
    tx_gain: float = 0.0
    bandwidth: float = 10_000_000.0
    rx_antenna: str = "RX2"
    tx_antenna: str = "TX/RX"
    channel: int = 0
    clock_source: str = "internal"

    def __post_init__(self) -> None:
        if not isinstance(self.device_args, str):
            raise TypeError("device_args must be a string")
        if self.device_args and _SERIAL_SELECTOR.fullmatch(self.device_args) is None:
            raise ValueError("device_args must be empty or an exact serial=<serial> selector")
        if type(self.sample_rate) is not int:
            raise TypeError("sample_rate must be an integer")
        if self.sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise ValueError("sample_rate must be one of 5000000, 10000000, or 20000000")

        center_frequency = _finite_number("center_frequency", self.center_frequency)
        if not _MIN_CENTER_FREQUENCY_HZ <= center_frequency <= _MAX_CENTER_FREQUENCY_HZ:
            raise ValueError("center_frequency must be within the B210 70 MHz to 6 GHz range")

        rx_gain = _finite_number("rx_gain", self.rx_gain)
        if not 0.0 <= rx_gain <= _MAX_RX_GAIN_DB:
            raise ValueError("rx_gain must be between 0 and 76 dB")
        tx_gain = _finite_number("tx_gain", self.tx_gain)
        if not 0.0 <= tx_gain <= _MAX_TX_GAIN_DB:
            raise ValueError("tx_gain must be between 0 and 89.8 dB")

        bandwidth = _finite_number("bandwidth", self.bandwidth)
        if not _MIN_BANDWIDTH_HZ <= bandwidth <= _MAX_BANDWIDTH_HZ:
            raise ValueError("bandwidth must be between 200 kHz and 56 MHz")
        if self.rx_antenna not in {"RX2", "TX/RX"}:
            raise ValueError("rx_antenna must be RX2 or TX/RX")
        if self.tx_antenna != "TX/RX":
            raise ValueError("tx_antenna must be TX/RX for a B210")
        if type(self.channel) is not int or self.channel not in (0, 1):
            raise ValueError("channel must be the RF A (0) or RF B (1) front end")
        if self.clock_source != "internal":
            raise ValueError("clock_source must be internal in v1")


@dataclass(frozen=True, slots=True)
class UhdEndpointIdentity:
    """Operator-facing identity kept separate from UHD's product readback."""

    role: str
    name: str
    product: str
    serial: str

    def __post_init__(self) -> None:
        if self.role not in {"coordinator", "station"}:
            raise ValueError("endpoint role must be coordinator or station")
        for field_name in ("name", "product"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"endpoint {field_name} must be a non-empty string")
        if not isinstance(self.serial, str) or _SERIAL.fullmatch(self.serial) is None:
            raise ValueError("endpoint serial must be an exact alphanumeric serial")


@dataclass(frozen=True, slots=True)
class UhdRadioParameters:
    """One immutable requested or actual UHD channel configuration."""

    sample_rate_hz: float
    center_frequency_hz: float
    gain_db: float
    bandwidth_hz: float
    antenna: str
    clock_source: str

    def __post_init__(self) -> None:
        positive = ("sample_rate_hz", "center_frequency_hz", "bandwidth_hz")
        for name in positive:
            if _finite_number(name, getattr(self, name)) <= 0.0:
                raise ValueError(f"{name} must be positive")
        _finite_number("gain_db", self.gain_db)
        if not isinstance(self.antenna, str) or not self.antenna:
            raise ValueError("antenna must be a non-empty string")
        if not isinstance(self.clock_source, str) or not self.clock_source:
            raise ValueError("clock_source must be a non-empty string")


@dataclass(frozen=True, slots=True)
class UhdSensorReadback:
    """Optional UHD sensor values; ``None`` means the sensor was unavailable."""

    lo_locked: bool | None = None
    ref_locked: bool | None = None
    temperature_c: float | None = None
    rssi_dbm: float | None = None

    def __post_init__(self) -> None:
        for name in ("lo_locked", "ref_locked"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be a bool or None")
        for name in ("temperature_c", "rssi_dbm"):
            value = getattr(self, name)
            if value is not None:
                _finite_number(name, value)

    @property
    def available(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in ("lo_locked", "ref_locked", "temperature_c", "rssi_dbm")
            if getattr(self, name) is not None
        )


@dataclass(frozen=True, slots=True)
class UhdEndpointReadback:
    """Requested and actual settings read from one exact UHD endpoint."""

    identity: UhdEndpointIdentity
    uhd_product: str
    uhd_serial: str
    direction: str
    requested: UhdRadioParameters
    actual: UhdRadioParameters
    sensors: UhdSensorReadback = UhdSensorReadback()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, UhdEndpointIdentity):
            raise TypeError("identity must be a UhdEndpointIdentity")
        if not isinstance(self.uhd_product, str) or not self.uhd_product:
            raise ValueError("uhd_product must be a non-empty string")
        if self.uhd_serial != self.identity.serial:
            raise ValueError("UHD serial readback does not match the requested endpoint")
        if self.direction not in {"rx", "tx"}:
            raise ValueError("direction must be rx or tx")
        if not isinstance(self.requested, UhdRadioParameters) or not isinstance(
            self.actual, UhdRadioParameters
        ):
            raise TypeError("requested and actual must be UhdRadioParameters")
        if not isinstance(self.sensors, UhdSensorReadback):
            raise TypeError("sensors must be a UhdSensorReadback")


@dataclass(frozen=True, slots=True)
class UhdEndpoint:
    """One named physical endpoint and its requested radio settings."""

    identity: UhdEndpointIdentity
    settings: B210Settings

    def __post_init__(self) -> None:
        if not isinstance(self.identity, UhdEndpointIdentity):
            raise TypeError("identity must be a UhdEndpointIdentity")
        if not isinstance(self.settings, B210Settings):
            raise TypeError("settings must be B210Settings")


@dataclass(frozen=True, slots=True)
class UhdEndpointPair:
    """Coordinator/station settings for one exact two-device hardware run."""

    coordinator: UhdEndpoint
    station: UhdEndpoint

    def __post_init__(self) -> None:
        if not isinstance(self.coordinator, UhdEndpoint) or not isinstance(
            self.station, UhdEndpoint
        ):
            raise TypeError("hardware pair endpoints must be UhdEndpoint values")
        if self.coordinator.identity.role != "coordinator":
            raise ValueError("coordinator endpoint role must be coordinator")
        if self.station.identity.role != "station":
            raise ValueError("station endpoint role must be station")
        for endpoint in (self.coordinator, self.station):
            expected = f"serial={endpoint.identity.serial}"
            if endpoint.settings.device_args != expected:
                raise ValueError(
                    f"{endpoint.identity.role} endpoint requires exact serial selector {expected}"
                )
        if self.coordinator.identity.serial == self.station.identity.serial:
            raise ValueError("hardware endpoints require distinct serials")

    @property
    def serials(self) -> tuple[str, str]:
        return (self.coordinator.identity.serial, self.station.identity.serial)


def read_b210_endpoint(
    endpoint: UhdEndpoint,
    block: Any,
    *,
    direction: str,
) -> UhdEndpointReadback:
    """Read back one configured source or sink without assuming requested values."""

    if not isinstance(endpoint, UhdEndpoint):
        raise TypeError("endpoint must be a UhdEndpoint")
    if direction not in {"rx", "tx"}:
        raise ValueError("direction must be rx or tx")
    # Block-relative, as in _configure_block; the physical front end was
    # chosen through the stream args when the block was built.
    channel = _BLOCK_CHANNEL
    info = block.get_usrp_info(channel)
    if not isinstance(info, Mapping):
        raise TypeError("get_usrp_info() must return a mapping")
    product = _info_value(info, "mboard_name", "product", "mboard_id", "type")
    serial = _info_value(info, "mboard_serial", "serial")

    settings = endpoint.settings
    requested = UhdRadioParameters(
        sample_rate_hz=float(settings.sample_rate),
        center_frequency_hz=settings.center_frequency,
        gain_db=settings.tx_gain if direction == "tx" else settings.rx_gain,
        bandwidth_hz=settings.bandwidth,
        antenna=settings.tx_antenna if direction == "tx" else settings.rx_antenna,
        clock_source=settings.clock_source,
    )
    actual = UhdRadioParameters(
        sample_rate_hz=float(block.get_samp_rate()),
        center_frequency_hz=float(block.get_center_freq(channel)),
        gain_db=float(block.get_gain(channel)),
        bandwidth_hz=float(block.get_bandwidth(channel)),
        antenna=str(block.get_antenna(channel)),
        clock_source=str(block.get_clock_source(0)),
    )
    return UhdEndpointReadback(
        identity=endpoint.identity,
        uhd_product=product,
        uhd_serial=serial,
        direction=direction,
        requested=requested,
        actual=actual,
        sensors=_read_sensors(block, channel),
    )


def create_b210_source(
    settings: B210Settings,
    *,
    recv_frames: int | None = None,
    uhd_api: Any | None = None,
) -> Any:
    """Construct and configure one read-only GNU Radio UHD source block.

    The returned block is not connected to or started in a flowgraph.  Passing
    ``uhd_api`` is the supported system-boundary injection seam for tests.
    ``recv_frames`` sets UHD's ``num_recv_frames`` so a host that stalls for a
    moment (for example a GUI holding the GIL) does not overflow the small
    default USB receive buffer.
    """

    _require_settings(settings)
    if recv_frames is not None and (
        type(recv_frames) is not int or not 1 <= recv_frames <= 1024
    ):
        raise ValueError("recv_frames must be an integer in [1, 1024]")
    uhd = uhd_api if uhd_api is not None else _load_uhd()
    stream_args = _make_stream_args(uhd, settings.channel)
    device_args = settings.device_args
    if recv_frames is not None:
        device_args = ",".join(
            part for part in (device_args, f"num_recv_frames={recv_frames}") if part
        )
    source = uhd.usrp_source(device_args, stream_args)
    _configure_block(
        source,
        settings=settings,
        gain=settings.rx_gain,
        antenna=settings.rx_antenna,
    )
    return source


def create_b210_sink(
    settings: B210Settings,
    rf_enable: RFEnableToken | None,
    *,
    uhd_api: Any | None = None,
) -> Any:
    """Construct and configure one TX block after an explicit RF safety gate.

    The guard is evaluated before importing GNU Radio or calling an injected
    UHD API, so an absent/invalid capability cannot even construct a sink.
    The returned block is not connected to or started in a flowgraph.
    """

    return _create_b210_sink(
        settings,
        rf_enable,
        length_tag_name=TX_LENGTH_TAG,
        uhd_api=uhd_api,
    )


def create_b210_untagged_diagnostic_sink(
    settings: B210Settings,
    rf_enable: RFEnableToken | None,
    *,
    uhd_api: Any | None = None,
) -> Any:
    """Construct a gated untagged sink solely for bounded RF-path diagnosis.

    Production OFDM bursts use :func:`create_b210_sink`.  This control path
    distinguishes UHD length-tag handling from antenna/front-end failures and
    is not acceptance evidence.
    """

    return _create_b210_sink(
        settings,
        rf_enable,
        length_tag_name="",
        uhd_api=uhd_api,
    )


def _create_b210_sink(
    settings: B210Settings,
    rf_enable: RFEnableToken | None,
    *,
    length_tag_name: str,
    uhd_api: Any | None,
) -> Any:
    _require_rf_enable(rf_enable)
    _require_settings(settings)
    uhd = uhd_api if uhd_api is not None else _load_uhd()
    stream_args = _make_stream_args(uhd, settings.channel)
    sink = uhd.usrp_sink(settings.device_args, stream_args, length_tag_name)
    _configure_block(
        sink,
        settings=settings,
        gain=settings.tx_gain,
        antenna=settings.tx_antenna,
    )
    return sink


# gr-uhd addresses a block by its own channel index, 0 to N-1, where N is the
# length of the stream_args channel list -- not by the physical channel number.
# These blocks always carry exactly one channel, selected in the stream args, so
# every setter addresses index 0 whichever front end that channel is.  Passing
# the physical channel here raises "RX channel out of range" for RF B.
_BLOCK_CHANNEL = 0


def _configure_block(
    block: Any,
    *,
    settings: B210Settings,
    gain: float,
    antenna: str,
) -> None:
    block.set_clock_source(settings.clock_source, 0)
    block.set_samp_rate(settings.sample_rate)
    block.set_center_freq(settings.center_frequency, _BLOCK_CHANNEL)
    block.set_gain(gain, _BLOCK_CHANNEL)
    block.set_bandwidth(settings.bandwidth, _BLOCK_CHANNEL)
    block.set_antenna(antenna, _BLOCK_CHANNEL)


def _make_stream_args(uhd_api: Any, channel: int) -> Any:
    return uhd_api.stream_args(
        cpu_format="fc32",
        otw_format="sc16",
        channels=[channel],
    )


def _load_uhd() -> Any:
    try:
        from gnuradio import uhd
    except ImportError as error:
        raise UhdUnavailableError(
            "GNU Radio UHD bindings are required to construct B210 blocks"
        ) from error
    return uhd


def _require_rf_enable(token: RFEnableToken | None) -> None:
    if not isinstance(token, RFEnableToken) or not token._is_valid():
        raise RFTransmissionDisabledError(
            "B210 TX requires explicit runtime acknowledgement; a config boolean is not enough"
        )


def _require_settings(settings: B210Settings) -> None:
    if not isinstance(settings, B210Settings):
        raise TypeError("settings must be B210Settings")


def _finite_number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be a finite number")
    return converted


def _info_value(info: Mapping[object, object], *names: str) -> str:
    for name in names:
        value = info.get(name)
        if value is not None and str(value):
            return str(value)
    raise ValueError(f"UHD identity readback is missing {names[0]}")


def _read_sensors(block: Any, channel: int) -> UhdSensorReadback:
    temperature = _optional_sensor(
        block,
        "temp",
        channel=channel,
        boolean=False,
    )
    if temperature is None:
        temperature = _optional_sensor(block, "temp", mboard=0, boolean=False)
    return UhdSensorReadback(
        lo_locked=_optional_sensor(block, "lo_locked", channel=channel, boolean=True),
        ref_locked=_optional_sensor(block, "ref_locked", mboard=0, boolean=True),
        temperature_c=temperature,
        rssi_dbm=_optional_sensor(block, "rssi", channel=channel, boolean=False),
    )


def _optional_sensor(
    block: Any,
    name: str,
    *,
    channel: int | None = None,
    mboard: int | None = None,
    boolean: bool,
) -> bool | float | None:
    if channel is not None:
        names_method = getattr(block, "get_sensor_names", None)
        value_method = getattr(block, "get_sensor", None)
        index = channel
    else:
        names_method = getattr(block, "get_mboard_sensor_names", None)
        value_method = getattr(block, "get_mboard_sensor", None)
        index = mboard
    if not callable(names_method) or not callable(value_method):
        return None
    try:
        names = tuple(names_method(index))
        if name not in names:
            return None
        sensor = value_method(name, index)
        return bool(sensor.to_bool()) if boolean else float(sensor.to_real())
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
