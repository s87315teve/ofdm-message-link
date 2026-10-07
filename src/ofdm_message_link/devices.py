"""Discover attached radios and probe what their front ends can actually do.

USRPs are handled here through UHD; an ADALM-Pluto is not a UHD device, so
:func:`discover` and :func:`probe` hand it to :mod:`.pluto`.

Two levels, deliberately separated:

* :func:`discover` asks UHD what is attached.  It does not open anything, so it
  is cheap and safe to call while another process is streaming.
* :func:`probe` opens one device and reads its real channel count, antenna
  ports, gain ranges and tuning range.  Opening a B2xx loads its FPGA image and
  takes seconds, an N-series device answers over Ethernet, and either fails
  while another process holds the device, so this is always called off the UI
  thread and its failure is a normal outcome.

Only the *front-panel labels* are tabulated per device family, because nothing
in the UHD API exposes the silk-screen name of a port.  Everything else is read
from the device, so adding a family means adding one :class:`DeviceFamily`
entry -- and an unlisted device still works, with generic channel labels.

Nothing here transmits.  Probing configures nothing and opens no TX stream.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ofdm_link.radio.uhd import SUPPORTED_SAMPLE_RATES

DEFAULT_IMAGES_DIR = "/tmp/ofdm_uhd_4_10_images"
UHD_ACCESS_LOCK = os.path.join(tempfile.gettempdir(), "ofdm_message_link_uhd.lock")


class DeviceError(RuntimeError):
    """Raised when devices cannot be listed or a device cannot be probed."""


@dataclass(frozen=True, slots=True)
class GainControl:
    """What one direction's gain knob is on a device family, and how it reads.

    Every gain this example shows, takes on the command line or logs is in
    *shown* dB, and shown dB always rise with level: more output when
    transmitting, more sensitivity when receiving.  A USRP's gain and a
    Pluto's attenuator (-89.75 to 0 dB) already count that way, so both are
    shown as the device reports them and match the vendor's own tools.

    ``device_sign`` is for a device whose API counts the other way, such as an
    attenuation of 0 to 90 dB where larger is quieter: -1 shows it negated.
    ``name`` is what the hardware calls the knob; it is the only text a new
    family has to supply.
    """

    name: str
    detail: str = ""
    device_sign: int = 1

    def __post_init__(self) -> None:
        if self.device_sign not in (1, -1):
            raise ValueError("device_sign must be 1 or -1")

    def to_device(self, shown_db: float) -> float:
        return self.device_sign * float(shown_db)

    def from_device(self, device_db: float) -> float:
        return self.device_sign * float(device_db)

    def shown_range(self, device_range: tuple[float, float]) -> tuple[float, float]:
        low, high = sorted(self.from_device(limit) for limit in device_range)
        return (low, high)

    def label(self, shown_range: tuple[float, float], *, direction: str) -> str:
        """One short line for the Radio panel, beside the gain field."""

        low, high = shown_range
        if direction == "tx":
            return f"{self.name} \u00b7 max output at {high:g} dB"
        return f"{self.name} \u00b7 {low:g} to {high:g} dB"


AMPLIFIER_GAIN = GainControl(
    name="Amplifier gain",
    detail="Transmit gain: a higher value is more output power.",
)
RECEIVE_GAIN = GainControl(
    name="Receive gain",
    detail="Receive gain: a higher value amplifies the received signal more.",
)
# What an unlisted family gets: its numbers as reported, and no claim about
# what the knob is.
GENERIC_GAIN = GainControl(name="Gain")


@dataclass(frozen=True, slots=True)
class DeviceFamily:
    """Front-panel naming and gain controls for one driver type.

    ``channel_labels`` is indexed by UHD channel number.  For the B2xx family
    the device reports its subdevice specification as ``A:A A:B``, so channel 0
    is the port marked RF A and channel 1 is RF B.  The AD9361 front-end names
    from ``get_rx_subdev_name()`` run in the opposite order and must not be used
    for labelling; see docs/04-ota-hardware.md.
    """

    driver: str
    description: str
    channel_labels: tuple[str, ...]
    # Front-panel connector for a UHD antenna name, where the panel names it
    # differently.  An N200/N210 brings the daughterboard's TX/RX and RX2 out
    # on bulkhead cables to the ports marked RF1 and RF2.
    antenna_labels: tuple[tuple[str, str], ...] = ()
    tx_gain: GainControl = AMPLIFIER_GAIN
    rx_gain: GainControl = RECEIVE_GAIN


DEVICE_FAMILIES: dict[str, DeviceFamily] = {
    "b200": DeviceFamily(
        driver="b200",
        description="Ettus B200/B210 and NI USRP-29xx",
        channel_labels=("RF A", "RF B"),
    ),
    # One daughterboard slot, so one channel.  The daughterboard (WBX, SBX,
    # CBX, UBX...) sets the tuning and gain ranges, which the probe reads; a
    # CBX, for example, stops at 1.2 GHz.  See docs/04-ota-hardware.md.
    "usrp2": DeviceFamily(
        driver="usrp2",
        description="Ettus USRP N200/N210",
        channel_labels=("Slot A",),
        antenna_labels=(("TX/RX", "RF1"), ("RX2", "RF2")),
    ),
    # Not a UHD device: discovered and probed through libiio by pluto.py.  One
    # channel, with separate SMA connectors marked RX and TX on the case.
    "pluto": DeviceFamily(
        driver="pluto",
        description="Analog Devices ADALM-Pluto",
        channel_labels=("RF",),
        # The AD936x has no transmit gain stage to set, only an attenuator.
        # GNU Radio's PlutoSDR Sink takes the same setting as a positive
        # "Attenuation" where larger is quieter; here it is the device's own
        # hardwaregain, so larger is louder like every other family.
        tx_gain=GainControl(
            name="Attenuator",
            detail=(
                "A Pluto's transmit gain is an attenuator: 0 dB is full output and "
                "-89.75 dB is practically off. A higher value is more output power."
            ),
        ),
    ),
}

# Ports this example can stream through; B210Settings rejects anything else.
# A daughterboard's CAL port is an internal TX-to-RX loopback, not a connector.
_STREAM_ANTENNAS = ("TX/RX", "RX2")


def family_for(driver: str) -> DeviceFamily | None:
    """Return the known family for one UHD driver type, if it is listed."""

    return DEVICE_FAMILIES.get(driver)


def gain_control(driver: str, direction: str) -> GainControl:
    """The gain control of one direction of one device family."""

    if direction not in {"rx", "tx"}:
        raise DeviceError("direction must be rx or tx")
    family = family_for(driver)
    if family is None:
        return GENERIC_GAIN
    return family.tx_gain if direction == "tx" else family.rx_gain


def gain_label(driver: str, direction: str, shown_range: tuple[float, float]) -> str:
    """The Radio panel's one-line description of the selected device's gain."""

    label = gain_control(driver, direction).label(shown_range, direction=direction)
    return label if family_for(driver) is not None else f"{label}  [untested family]"


def antenna_label(driver: str, antenna: str) -> str:
    """UHD antenna name with its front-panel connector when those differ."""

    family = family_for(driver)
    panel = dict(family.antenna_labels).get(antenna) if family is not None else None
    return f"{antenna} ({panel})" if panel else antenna


def channel_label(driver: str, index: int) -> str:
    """Front-panel label for one channel, falling back to a generic name."""

    family = family_for(driver)
    if family is not None and 0 <= index < len(family.channel_labels):
        return family.channel_labels[index]
    return f"Channel {index}"


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """One attached device as UHD enumerates it, without opening it."""

    serial: str
    name: str
    product: str
    driver: str
    # Network devices report their IP address; USB devices leave this empty.
    address: str = ""

    @property
    def device_args(self) -> str:
        return f"serial={self.serial}"

    @property
    def is_supported(self) -> bool:
        """Whether this example has been exercised on this device family."""

        return self.driver in DEVICE_FAMILIES

    def describe(self) -> str:
        family = family_for(self.driver)
        label = self.name or self.product or (family.description if family else self.driver)
        details = [part for part in (self.product, f"serial {self.serial}", self.address) if part]
        suffix = "" if self.is_supported else "  [untested family]"
        return f"{label} ({', '.join(details)}){suffix}"


@dataclass(frozen=True, slots=True)
class ChannelCapability:
    """What one physical front end reports it can do.

    Gain ranges are in shown dB (see GainControl), like every gain the
    operator sees or chooses.
    """

    index: int
    label: str
    rx_antennas: tuple[str, ...]
    tx_antennas: tuple[str, ...]
    rx_gain_range_db: tuple[float, float]
    tx_gain_range_db: tuple[float, float]

    def antennas(self, direction: str) -> tuple[str, ...]:
        if direction == "rx":
            return self.rx_antennas
        if direction == "tx":
            return self.tx_antennas
        raise DeviceError("direction must be rx or tx")

    def gain_range_db(self, direction: str) -> tuple[float, float]:
        if direction == "rx":
            return self.rx_gain_range_db
        if direction == "tx":
            return self.tx_gain_range_db
        raise DeviceError("direction must be rx or tx")


@dataclass(frozen=True, slots=True)
class DeviceCapabilities:
    """Live probe result for one device."""

    device: DiscoveredDevice
    channels: tuple[ChannelCapability, ...]
    rx_freq_range_hz: tuple[float, float]
    tx_freq_range_hz: tuple[float, float]
    subdev_spec: str
    # Motherboard as the device names itself once open, e.g. "B210" or
    # "N210r4"; discovery alone does not report it for network devices.
    mboard_name: str = ""
    # Fastest rate the device's host link carries without losing samples,
    # where that is below the wire contract's own limit; None when it is not.
    max_sample_rate: int | None = None

    @property
    def sample_rates(self) -> tuple[int, ...]:
        """Sample rates the wire contract allows and this device can carry, sorted."""

        return tuple(
            rate
            for rate in sorted(SUPPORTED_SAMPLE_RATES)
            if self.max_sample_rate is None or rate <= self.max_sample_rate
        )

    def freq_range_hz(self, direction: str) -> tuple[float, float]:
        if direction == "rx":
            return self.rx_freq_range_hz
        if direction == "tx":
            return self.tx_freq_range_hz
        raise DeviceError("direction must be rx or tx")

    def channel(self, index: int) -> ChannelCapability:
        for candidate in self.channels:
            if candidate.index == index:
                return candidate
        raise DeviceError(f"device has no channel {index}")

    def supports(self, *, channel: int, antenna: str, direction: str) -> bool:
        try:
            return antenna in self.channel(channel).antennas(direction)
        except DeviceError:
            return False


@dataclass(frozen=True, slots=True)
class RadioSelection:
    """One operator choice of device, front end and radio parameters."""

    serial: str
    channel: int
    antenna: str
    # Shown dB, as the Radio panel displays it; device_gain_db() converts.
    gain_db: float
    center_frequency_hz: float
    sample_rate: int
    # Driver type of the selected device: front-panel labels and gain control.
    driver: str = "b200"
    # How to reach a device UHD does not address by serial: a Pluto's IIO URI.
    address: str = ""

    @property
    def device_args(self) -> str:
        return f"serial={self.serial}"

    def device_gain_db(self, direction: str) -> float:
        """The selected gain as this device's own API takes it."""

        return gain_control(self.driver, direction).to_device(self.gain_db)

    def describe(self) -> str:
        return (
            f"serial {self.serial} {channel_label(self.driver, self.channel)} "
            f"{antenna_label(self.driver, self.antenna)} "
            f"@ {self.center_frequency_hz / 1e6:.3f} MHz, "
            f"{self.sample_rate / 1e6:g} MS/s, gain {self.gain_db:g} dB"
        )


@contextmanager
def uhd_access_lock(path: str | None = None) -> Iterator[None]:
    """Let one of this example's processes at a time discover or open a USRP.

    UHD reads an N200/N210's serial number and daughterboard IDs from EEPROM
    over the network while it discovers or opens the device.  Two processes
    doing that at once interleave the reads: two concurrent ``uhd.find("")``
    calls have been seen to report one serial as a truncated or unrelated
    value, so the TX window could not find the device it was told to
    preselect.  The TX and RX apps start together, so this is the normal case,
    not a race.  A B2xx on USB is unaffected but shares the lock for
    simplicity.  Hold it only around discovery and opening, never while
    streaming.
    """

    with open(path or UHD_ACCESS_LOCK, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def ensure_images_dir(path: str | None = None) -> str | None:
    """Point UHD at its FPGA/firmware images when the environment has not.

    A B2xx cannot be opened without them, and the failure message UHD prints is
    a wall of download instructions rather than anything about this example.
    """

    if os.environ.get("UHD_IMAGES_DIR"):
        return os.environ["UHD_IMAGES_DIR"]
    candidate = path or DEFAULT_IMAGES_DIR
    if os.path.isdir(candidate):
        os.environ["UHD_IMAGES_DIR"] = candidate
        return candidate
    return None


def _load_uhd() -> Any:
    try:
        import uhd
    except ImportError as error:  # pragma: no cover - environment dependent
        raise DeviceError(
            "the UHD Python bindings are not importable; device selection needs them"
        ) from error
    return uhd


def discover(
    *, uhd_api: Any | None = None, backend: str = "uhd"
) -> tuple[DiscoveredDevice, ...]:
    """List attached devices without opening any of them.

    ``backend`` is the sample transport: ``uhd`` lists USRPs, ``pluto`` lists
    ADALM-Plutos.
    """

    if backend == "pluto":
        from . import pluto

        return pluto.discover()
    ensure_images_dir()
    uhd = uhd_api if uhd_api is not None else _load_uhd()
    try:
        with uhd_access_lock():
            found = uhd.find("")
    except Exception as error:  # UHD raises a variety of runtime errors
        raise DeviceError(f"UHD device enumeration failed: {error}") from error

    devices: list[DiscoveredDevice] = []
    for entry in found:
        fields = _address_fields(entry)
        serial = fields.get("serial", "")
        if not serial:
            continue
        devices.append(
            DiscoveredDevice(
                serial=serial,
                name=fields.get("name", ""),
                product=fields.get("product", ""),
                driver=fields.get("type", ""),
                address=fields.get("addr", ""),
            )
        )
    return tuple(sorted(devices, key=lambda device: device.serial))


def _address_fields(entry: Any) -> dict[str, str]:
    """Parse one UHD device address into plain fields."""

    if isinstance(entry, dict):
        return {str(key): str(value) for key, value in entry.items()}
    text = entry.to_string() if hasattr(entry, "to_string") else str(entry)
    fields: dict[str, str] = {}
    for part in text.replace("\n", ",").split(","):
        key, separator, value = part.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def probe(device: DiscoveredDevice, *, uhd_api: Any | None = None) -> DeviceCapabilities:
    """Open one device and read what its front ends actually offer.

    This holds the device for the duration of the call, so it cannot run while
    the same device is streaming in another process.
    """

    if not isinstance(device, DiscoveredDevice):
        raise DeviceError("device must be a DiscoveredDevice")
    if device.driver == "pluto":
        from . import pluto

        return pluto.probe(device)
    ensure_images_dir()
    uhd = uhd_api if uhd_api is not None else _load_uhd()

    usrp = None
    try:
        with uhd_access_lock():
            usrp = uhd.usrp.MultiUSRP(device.device_args)
        rx_count = int(usrp.get_rx_num_channels())
        tx_count = int(usrp.get_tx_num_channels())
        channels = []
        for index in range(max(rx_count, tx_count)):
            channels.append(
                ChannelCapability(
                    index=index,
                    label=channel_label(device.driver, index),
                    rx_antennas=_antennas(usrp, "rx", index) if index < rx_count else (),
                    tx_antennas=_antennas(usrp, "tx", index) if index < tx_count else (),
                    rx_gain_range_db=gain_control(device.driver, "rx").shown_range(
                        _range(usrp.get_rx_gain_range, index) if index < rx_count else (0.0, 0.0)
                    ),
                    tx_gain_range_db=gain_control(device.driver, "tx").shown_range(
                        _range(usrp.get_tx_gain_range, index) if index < tx_count else (0.0, 0.0)
                    ),
                )
            )
        return DeviceCapabilities(
            device=device,
            channels=tuple(channels),
            rx_freq_range_hz=_range(usrp.get_rx_freq_range, 0) if rx_count else (0.0, 0.0),
            tx_freq_range_hz=_range(usrp.get_tx_freq_range, 0) if tx_count else (0.0, 0.0),
            subdev_spec=_subdev_spec(usrp),
            mboard_name=_mboard_name(usrp),
        )
    except DeviceError:
        raise
    except Exception as error:
        raise DeviceError(f"cannot probe {device.serial}: {error}") from error
    finally:
        del usrp


def _antennas(usrp: Any, direction: str, index: int) -> tuple[str, ...]:
    getter = usrp.get_rx_antennas if direction == "rx" else usrp.get_tx_antennas
    try:
        reported = tuple(str(value) for value in getter(index))
    except Exception:
        return ()
    return tuple(name for name in reported if name in _STREAM_ANTENNAS)


def _range(getter: Any, index: int) -> tuple[float, float]:
    span = getter(index)
    return (float(span.start()), float(span.stop()))


def _mboard_name(usrp: Any) -> str:
    try:
        return str(usrp.get_mboard_name(0))
    except Exception:
        return ""


def _subdev_spec(usrp: Any) -> str:
    try:
        return str(usrp.get_rx_subdev_spec(0).to_string())
    except Exception:
        return ""


def default_selection(
    capabilities: DeviceCapabilities,
    *,
    direction: str,
    center_frequency_hz: float,
    sample_rate: int,
    gain_db: float | None = None,
    preferred_antenna: str | None = None,
) -> RadioSelection:
    """Build a selection that the probed device actually supports.

    Transmit gain defaults to the bottom of the range.  An operator raising it
    deliberately is a different thing from a default that keys an amplifier at
    an arbitrary power the first time the radio starts.
    """

    if direction not in {"rx", "tx"}:
        raise DeviceError("direction must be rx or tx")
    usable = [
        channel for channel in capabilities.channels if channel.antennas(direction)
    ]
    if not usable:
        raise DeviceError(f"device {capabilities.device.serial} has no {direction} front end")
    channel = usable[0]
    antennas = channel.antennas(direction)
    antenna = preferred_antenna if preferred_antenna in antennas else antennas[0]
    low, high = channel.gain_range_db(direction)
    if gain_db is None:
        resolved_gain = low if direction == "tx" else min(high, low + (high - low) * 0.5)
    else:
        resolved_gain = min(max(gain_db, low), high)
    return RadioSelection(
        driver=capabilities.device.driver,
        address=capabilities.device.address,
        serial=capabilities.device.serial,
        channel=channel.index,
        antenna=antenna,
        gain_db=float(resolved_gain),
        center_frequency_hz=float(center_frequency_hz),
        sample_rate=int(sample_rate),
    )


def validate(
    selection: RadioSelection,
    capabilities: DeviceCapabilities,
    *,
    direction: str,
) -> Sequence[str]:
    """Return the reasons this selection cannot be used, empty when it can."""

    problems: list[str] = []
    if selection.serial != capabilities.device.serial:
        problems.append("selection names a different device than the probe")
        return problems
    try:
        channel = capabilities.channel(selection.channel)
    except DeviceError as error:
        problems.append(str(error))
        return problems

    antennas = channel.antennas(direction)
    if selection.antenna not in antennas:
        available = ", ".join(antennas) or "none"
        problems.append(
            f"{channel.label} has no {direction.upper()} port {selection.antenna!r} "
            f"(available: {available})"
        )
    low, high = channel.gain_range_db(direction)
    if not low <= selection.gain_db <= high:
        problems.append(f"gain {selection.gain_db:g} dB is outside {low:g}..{high:g} dB")
    span = capabilities.freq_range_hz(direction)
    if span != (0.0, 0.0) and not span[0] <= selection.center_frequency_hz <= span[1]:
        problems.append(
            f"{selection.center_frequency_hz / 1e6:.3f} MHz is outside "
            f"{span[0] / 1e6:.3f}..{span[1] / 1e6:.3f} MHz"
        )
    if selection.sample_rate not in capabilities.sample_rates:
        allowed = ", ".join(f"{rate / 1e6:g}" for rate in capabilities.sample_rates)
        problems.append(f"sample rate must be one of {allowed} MS/s on this device")
    return problems


def detect(backend: str = "auto") -> tuple[str, tuple[DiscoveredDevice, ...]]:
    """Choose the sample transport from what is attached, and list its devices.

    ``auto`` takes USRPs when there are any and ADALM-Plutos otherwise; with
    nothing attached it stays on ``uhd``.  A backend that cannot enumerate
    (no UHD bindings, say) counts as having no devices.
    """

    for candidate in ("uhd", "pluto") if backend == "auto" else (backend,):
        try:
            found = discover(backend=candidate)
        except DeviceError:
            found = ()
        if found:
            return candidate, found
    return ("uhd" if backend == "auto" else backend), ()


def main(argv: list[str] | None = None) -> int:
    """Print ``transport NAME`` then one ``serial S`` line per attached device."""

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--backend", choices=("auto", "uhd", "pluto"), default="auto")
    args = parser.parse_args(argv)
    backend, found = detect(args.backend)
    print(f"transport {backend}")
    for device in found:
        print(f"serial {device.serial}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
