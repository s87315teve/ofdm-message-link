"""ADALM-Pluto radios for the message link, through libiio.

A Pluto is not a UHD device, so discovery, probing and the two sample
transports live here.  Everything above them is unchanged: the Radio panel gets
the same :class:`~.devices.DiscoveredDevice` and
:class:`~.devices.DeviceCapabilities` it gets for a USRP, and the apps get a
:class:`~.transport.SampleSink` or :class:`~.transport.SampleSource`.

libiio is driven directly rather than through gr-iio's blocks.  gr-iio's sink
has one fixed buffer size, so every burst would be padded to it and would stay
on the air for that long whatever its MCS; here each push is exactly one burst
long.  The library is the ``libiio-c`` the GNU Radio environment
already installs, so this adds no dependency.  Samples cross in whole buffers;
nothing here touches them one at a time.

How a Pluto differs from a USRP here:

* **No device clock and no timed transmit.**  A burst goes on the air when its
  buffer reaches the FPGA.  There is nothing to be late against, so there are
  no late or underflow counters; a transmit stream that is idle between bursts
  is the normal state, not a fault.
* **Receive loss is a flag, not a count.**  The FPGA latches an overflow bit
  when the host falls behind.  It is polled a few times a second, so
  ``rx_overflow_events`` is a lower bound on separate overflows and says
  nothing about how many samples each one lost.
* **Transmit "gain" is the AD936x attenuator**, -89.75 to 0 dB.  0 dB is full
  output, a few dBm.
* **USB 2.0 carries the samples**, which sustains 5 MS/s and no more.

See docs/04-ota-hardware.md for the measurements behind this.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import glob
import ipaddress
import os
import queue
import socket
import struct
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from ofdm_link.radio.uhd import (
    SUPPORTED_SAMPLE_RATES,
    RFEnableToken,
    RFTransmissionDisabledError,
)

from .devices import (
    ChannelCapability,
    DeviceCapabilities,
    DeviceError,
    DiscoveredDevice,
    RadioSelection,
    channel_label,
    gain_control,
)
from .transport import DEFAULT_PEAK_AMPLITUDE, TransportError, scale_to_peak

DRIVER = "pluto"
RX_ANTENNA = "RX"
TX_ANTENNA = "TX"

_USB_VENDOR = "0456"
_USB_PRODUCT = "b673"
_USB_SYSFS = "/sys/bus/usb/devices"

_PHY = "ad9361-phy"
_RX_DEVICE = "cf-ad9361-lpc"
_TX_DEVICE = "cf-ad9361-dds-core-lpc"
_RX_LO = "altvoltage0"
_TX_LO = "altvoltage1"
_DDS_TONES = ("altvoltage0", "altvoltage1", "altvoltage2", "altvoltage3")
# Channel 0's I and Q; a 2R2T board's second channel is switched off.
_STREAM_CHANNELS = ("voltage0", "voltage1")
_UNUSED_CHANNELS = ("voltage2", "voltage3")

# ADI's DMA status register, the one libiio's own iio_adi_xflow_check reads.
_STATUS_REGISTER = 0x80000088
_STATUS_OVERFLOW = 0x4
_OVERFLOW_POLL_S = 0.25

# The ADC is 12 bits, sign-extended into 16; the DAC takes 16 and keeps the
# top 12.  These are the scales gr-iio's fc32 blocks use.
_RX_FULL_SCALE = 2048.0
_TX_FULL_SCALE = 32767.0

# USB 2.0 carries 5 MS/s with nothing lost and no more: asked for 10 MS/s, a
# Pluto delivered 5.5.
MAX_SAMPLE_RATE = 5_000_000
# About 13 ms at 5 MS/s, the chunk the UHD source hands the decoder too.
_RX_CHUNK_SAMPLES = 65536
# Room for any burst a 996-byte frame makes (about 21,000 samples at MCS 0);
# a longer burst grows the transmit buffer.
_TX_BUFFER_SAMPLES = 1 << 15
# Longest wait for bursts already handed to the device to finish playing.
_TX_DRAIN_LIMIT_S = 1.0
_TUNE_TOLERANCE_HZ = 1_000.0
_TIMEOUT_MS = 3_000

USB_ACCESS_HINT = (
    "this user cannot open the Pluto's USB device node. Install ADI's udev rule "
    "(53-adi-plutosdr-usb.rules, see the example README) and replug the Pluto."
)


# --------------------------------------------------------------------------
# libiio
# --------------------------------------------------------------------------

_SSIZE = ctypes.c_ssize_t
_HANDLE = ctypes.c_void_p
_TEXT = ctypes.c_char_p
_SIGNATURES: dict[str, tuple[Any, list[Any]]] = {
    "iio_create_context_from_uri": (_HANDLE, [_TEXT]),
    "iio_context_destroy": (None, [_HANDLE]),
    "iio_context_set_timeout": (ctypes.c_int, [_HANDLE, ctypes.c_uint]),
    "iio_context_get_attr_value": (_TEXT, [_HANDLE, _TEXT]),
    "iio_context_find_device": (_HANDLE, [_HANDLE, _TEXT]),
    "iio_device_find_channel": (_HANDLE, [_HANDLE, _TEXT, ctypes.c_bool]),
    "iio_channel_enable": (None, [_HANDLE]),
    "iio_channel_disable": (None, [_HANDLE]),
    "iio_channel_attr_read": (_SSIZE, [_HANDLE, _TEXT, ctypes.c_char_p, ctypes.c_size_t]),
    "iio_channel_attr_write": (_SSIZE, [_HANDLE, _TEXT, _TEXT]),
    "iio_device_reg_read": (
        ctypes.c_int,
        [_HANDLE, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)],
    ),
    "iio_device_reg_write": (ctypes.c_int, [_HANDLE, ctypes.c_uint32, ctypes.c_uint32]),
    "iio_device_create_buffer": (_HANDLE, [_HANDLE, ctypes.c_size_t, ctypes.c_bool]),
    "iio_buffer_destroy": (None, [_HANDLE]),
    "iio_buffer_refill": (_SSIZE, [_HANDLE]),
    "iio_buffer_push_partial": (_SSIZE, [_HANDLE, ctypes.c_size_t]),
    "iio_buffer_start": (_HANDLE, [_HANDLE]),
}
_library: ctypes.CDLL | None = None


def _libiio() -> ctypes.CDLL:
    """Load libiio once.  Lazy, so importing this module needs no libiio."""

    global _library
    if _library is None:
        bundled = os.path.join(sys.prefix, "lib", "libiio.so.0")
        name = bundled if os.path.exists(bundled) else ctypes.util.find_library("iio")
        if not name:
            raise OSError("libiio is not installed; the pluto transport needs libiio-c")
        library = ctypes.CDLL(name, use_errno=True)
        for function, (result, arguments) in _SIGNATURES.items():
            getattr(library, function).restype = result
            getattr(library, function).argtypes = arguments
        _library = library
    return _library


def _failure(code: int, what: str) -> OSError:
    number = abs(int(code))
    return OSError(number, f"{what}: {os.strerror(number)}")


class IioBuffer:
    """One streaming buffer on a Pluto's receive or transmit device."""

    def __init__(self, library: ctypes.CDLL, device: int, samples: int, *, output: bool) -> None:
        self._library = library
        for name in _STREAM_CHANNELS + _UNUSED_CHANNELS:
            channel = library.iio_device_find_channel(device, name.encode(), output)
            if channel and name in _STREAM_CHANNELS:
                library.iio_channel_enable(channel)
            elif channel:
                library.iio_channel_disable(channel)
        self._handle = library.iio_device_create_buffer(device, samples, False)
        if not self._handle:
            raise _failure(ctypes.get_errno(), "cannot create an IIO buffer")

    def refill(self) -> NDArray[np.int16]:
        """Block for the next buffer; the result is only valid until the next call."""

        size = self._library.iio_buffer_refill(self._handle)
        if size < 0:
            raise _failure(size, "cannot refill the IIO buffer")
        start = self._library.iio_buffer_start(self._handle)
        return np.ctypeslib.as_array((ctypes.c_int16 * (size // 2)).from_address(start))

    def push(self, interleaved: NDArray[np.int16]) -> None:
        """Send exactly these I/Q values; blocks while the device is still busy.

        Fewer values than the buffer holds is a partial push.  The iiod in
        Pluto firmware v0.30 answers one with EFBIG and then drops the buffer;
        the one in v0.39 sends it.
        """

        start = self._library.iio_buffer_start(self._handle)
        ctypes.memmove(start, interleaved.ctypes.data, interleaved.nbytes)
        sent = self._library.iio_buffer_push_partial(self._handle, interleaved.size // 2)
        if sent < 0:
            raise _failure(sent, "cannot push the IIO buffer")

    def close(self) -> None:
        if self._handle:
            self._library.iio_buffer_destroy(self._handle)
            self._handle = None


class IioContext:
    """The few libiio calls this example needs, on one open device.

    Failures raise :class:`OSError`; callers turn that into the error their
    own interface promises.
    """

    def __init__(self, uri: str) -> None:
        self._library = _libiio()
        self._handle = self._library.iio_create_context_from_uri(uri.encode())
        if not self._handle:
            raise _failure(ctypes.get_errno(), f"cannot open {uri}")
        self._library.iio_context_set_timeout(self._handle, _TIMEOUT_MS)

    def attr(self, name: str) -> str:
        value = self._library.iio_context_get_attr_value(self._handle, name.encode())
        return value.decode() if value else ""

    def _device(self, device: str) -> int:
        handle = self._library.iio_context_find_device(self._handle, device.encode())
        if not handle:
            raise OSError(f"the device has no IIO device {device!r}")
        return handle

    def _channel(self, device: str, channel: str, output: bool) -> int:
        handle = self._library.iio_device_find_channel(
            self._device(device), channel.encode(), output
        )
        if not handle:
            raise OSError(f"{device} has no channel {channel!r}")
        return handle

    def read(self, device: str, channel: str, attr: str, *, output: bool = False) -> str:
        text = ctypes.create_string_buffer(256)
        size = self._library.iio_channel_attr_read(
            self._channel(device, channel, output), attr.encode(), text, len(text)
        )
        if size < 0:
            raise _failure(size, f"cannot read {device}/{channel}/{attr}")
        return text.value.decode()

    def write(
        self, device: str, channel: str, attr: str, value: str, *, output: bool = False
    ) -> None:
        size = self._library.iio_channel_attr_write(
            self._channel(device, channel, output), attr.encode(), value.encode()
        )
        if size < 0:
            raise _failure(size, f"cannot set {device}/{channel}/{attr} to {value}")

    def take_overflow(self) -> bool:
        """Whether the receive path overflowed since the last call; clears the flag."""

        device = self._device(_RX_DEVICE)
        status = ctypes.c_uint32(0)
        result = self._library.iio_device_reg_read(device, _STATUS_REGISTER, ctypes.byref(status))
        if result < 0:
            raise _failure(result, "cannot read the receive status register")
        if not status.value & _STATUS_OVERFLOW:
            return False
        self._library.iio_device_reg_write(device, _STATUS_REGISTER, _STATUS_OVERFLOW)
        return True

    def open_buffer(self, samples: int, *, output: bool) -> IioBuffer:
        device = self._device(_TX_DEVICE if output else _RX_DEVICE)
        return IioBuffer(self._library, device, samples, output=output)

    def close(self) -> None:
        if self._handle:
            self._library.iio_context_destroy(self._handle)
            self._handle = None


ContextFactory = Callable[[str], Any]


def _number(text: str) -> float:
    """Parse an IIO attribute such as ``-10.000000 dB``."""

    return float(text.split()[0])


def _span(text: str) -> tuple[float, float]:
    """Parse an IIO ``[low step high]`` range into its two ends."""

    parts = text.strip("[] \n").split()
    return (float(parts[0]), float(parts[-1]))


# --------------------------------------------------------------------------
# discovery and probing
# --------------------------------------------------------------------------


def _sysfs(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _first_host(interface: str) -> str:
    """The first address of an interface's IPv4 subnet, where a Pluto puts itself."""

    request = struct.pack("256s", interface.encode()[:15])
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            address = fcntl.ioctl(probe, 0x8915, request)[20:24]  # SIOCGIFADDR
            netmask = fcntl.ioctl(probe, 0x891B, request)[20:24]  # SIOCGIFNETMASK
    except OSError:
        return ""
    host = ipaddress.ip_interface(
        f"{socket.inet_ntoa(address)}/{socket.inet_ntoa(netmask)}"
    )
    return str(host.network.network_address + 1)


def _uri(base: str, *, device_nodes: str) -> str:
    """How to reach one USB Pluto: its USB endpoint, else its network address."""

    bus, number = int(_sysfs(f"{base}/busnum")), int(_sysfs(f"{base}/devnum"))
    interfaces = sorted(glob.glob(f"{base}:*"))
    if os.access(f"{device_nodes}/{bus:03d}/{number:03d}", os.R_OK | os.W_OK):
        for interface in interfaces:
            if _sysfs(f"{interface}/interface") == "IIO":
                return f"usb:{bus}.{number}.{int(_sysfs(f'{interface}/bInterfaceNumber'), 16)}"
    # No USB access: the Pluto is also an Ethernet gadget.  Two Plutos left on
    # the factory address share it, which the serial check in probe() catches.
    for network in sorted(glob.glob(f"{base}:*/net/*")):
        address = _first_host(os.path.basename(network))
        if address:
            return f"ip:{address}"
    return ""


def discover(
    *, usb_sysfs: str = _USB_SYSFS, device_nodes: str = "/dev/bus/usb"
) -> tuple[DiscoveredDevice, ...]:
    """List the Plutos on USB without opening any of them."""

    devices: list[DiscoveredDevice] = []
    for base in sorted(glob.glob(f"{usb_sysfs}/*")):
        if _sysfs(f"{base}/idVendor") != _USB_VENDOR or _sysfs(f"{base}/idProduct") != _USB_PRODUCT:
            continue
        serial = _sysfs(f"{base}/serial")
        if not serial:
            continue
        devices.append(
            DiscoveredDevice(
                serial=serial,
                name="ADALM-Pluto",
                product="",
                driver=DRIVER,
                address=_uri(base, device_nodes=device_nodes),
            )
        )
    return tuple(sorted(devices, key=lambda device: device.serial))


def _open(uri: str, serial: str, open_context: ContextFactory) -> Any:
    """Open one Pluto and refuse a different one answering at the same address."""

    if not uri:
        raise OSError(USB_ACCESS_HINT)
    context = open_context(uri)
    answered = context.attr("hw_serial")
    if answered != serial:
        context.close()
        raise OSError(
            f"{uri} is Pluto {answered}, not {serial}. Two Plutos on the factory "
            f"network address answer as one; {USB_ACCESS_HINT}"
        )
    return context


def probe(
    device: DiscoveredDevice, *, open_context: ContextFactory = IioContext
) -> DeviceCapabilities:
    """Open one Pluto and read its real gain and tuning ranges."""

    try:
        context = _open(device.address, device.serial, open_context)
    except OSError as error:
        raise DeviceError(f"cannot probe {device.serial}: {error}") from error
    try:
        channel = ChannelCapability(
            index=0,
            label=channel_label(DRIVER, 0),
            rx_antennas=(RX_ANTENNA,),
            tx_antennas=(TX_ANTENNA,),
            rx_gain_range_db=gain_control(DRIVER, "rx").shown_range(
                _span(context.read(_PHY, "voltage0", "hardwaregain_available"))
            ),
            tx_gain_range_db=gain_control(DRIVER, "tx").shown_range(
                _span(context.read(_PHY, "voltage0", "hardwaregain_available", output=True))
            ),
        )
        return DeviceCapabilities(
            device=device,
            channels=(channel,),
            rx_freq_range_hz=_span(
                context.read(_PHY, _RX_LO, "frequency_available", output=True)
            ),
            tx_freq_range_hz=_span(
                context.read(_PHY, _TX_LO, "frequency_available", output=True)
            ),
            subdev_spec=context.attr("ad9361-phy,model"),
            mboard_name=f"{context.attr('hw_model')}, firmware {context.attr('fw_version')}",
            max_sample_rate=MAX_SAMPLE_RATE,
        )
    except (OSError, ValueError, IndexError) as error:
        raise DeviceError(f"cannot probe {device.serial}: {error}") from error
    finally:
        context.close()


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlutoSettings:
    """One direction of one Pluto, as the operator selected it."""

    uri: str
    serial: str
    sample_rate: int
    center_frequency: float
    gain_db: float

    def __post_init__(self) -> None:
        if self.sample_rate not in SUPPORTED_SAMPLE_RATES or self.sample_rate > MAX_SAMPLE_RATE:
            raise ValueError("a Pluto's sample rate must be 5 MS/s")


def settings_for(selection: RadioSelection, *, direction: str) -> PlutoSettings:
    return PlutoSettings(
        uri=selection.address,
        serial=selection.serial,
        sample_rate=int(selection.sample_rate),
        center_frequency=float(selection.center_frequency_hz),
        gain_db=selection.device_gain_db(direction),
    )


def _tune(context: Any, settings: PlutoSettings, *, output: bool) -> dict[str, object]:
    """Set rate, bandwidth and frequency for one direction and read them back.

    Analog bandwidth follows the sample rate, as it does for a USRP here.  The
    sample rate is one setting for both directions of an AD936x.
    """

    oscillator = _TX_LO if output else _RX_LO
    context.write(_PHY, "voltage0", "sampling_frequency", str(settings.sample_rate), output=output)
    context.write(_PHY, "voltage0", "rf_bandwidth", str(settings.sample_rate), output=output)
    context.write(
        _PHY, oscillator, "frequency", str(round(settings.center_frequency)), output=True
    )
    rate = _number(context.read(_PHY, "voltage0", "sampling_frequency", output=output))
    centre = _number(context.read(_PHY, oscillator, "frequency", output=True))
    if rate != settings.sample_rate:
        raise TransportError(
            f"the Pluto runs at {rate / 1e6:g} MS/s instead of the requested "
            f"{settings.sample_rate / 1e6:g} MS/s"
        )
    if abs(centre - settings.center_frequency) > _TUNE_TOLERANCE_HZ:
        raise TransportError(
            f"the Pluto tuned to {centre / 1e6:.3f} MHz instead of the requested "
            f"{settings.center_frequency / 1e6:.3f} MHz"
        )
    return {
        "serial": settings.serial,
        "hw_model": context.attr("hw_model"),
        "fw_version": context.attr("fw_version"),
        "sample_rate_sps": rate,
        "center_frequency_hz": centre,
        "bandwidth_hz": _number(context.read(_PHY, "voltage0", "rf_bandwidth", output=output)),
    }


def _set_gain(context: Any, gain_db: float, *, output: bool) -> float:
    """Set one direction's gain and return what the device reports."""

    if not np.isfinite(gain_db):
        raise TransportError("gain must be a finite number of dB")
    try:
        context.write(_PHY, "voltage0", "hardwaregain", f"{gain_db:.2f}", output=output)
        return _number(context.read(_PHY, "voltage0", "hardwaregain", output=output))
    except OSError as error:
        raise TransportError(f"the Pluto rejected {gain_db:g} dB: {error}") from error


# --------------------------------------------------------------------------
# sample transports
# --------------------------------------------------------------------------


class PlutoSampleSink:
    """Transmit each burst as one push of exactly its length.

    A burst is one transfer, so the host cannot open a gap inside it, and it
    is on the air for its own length and no longer.  One buffer large enough
    for any burst is pushed partially.  Firmware whose iiod refuses a partial
    push (v0.30) gets a buffer of exactly the burst's length instead, rebuilt
    when that length changes.  Rebuilding is the slow path: 2-15 ms there, but
    about 56 ms on v0.39, where a video stream's occasional short datagram
    cost a third of the transmit rate before partial pushes were used.

    Like every transmit path in this project, this needs an
    :class:`~ofdm_link.radio.RFEnableToken`; without one nothing is opened.
    Stopping returns the attenuator to its maximum, because a Pluto's transmit
    chain stays powered for as long as the board is.
    """

    def __init__(
        self,
        settings: PlutoSettings,
        rf_enable: Any,
        *,
        peak_amplitude: float = DEFAULT_PEAK_AMPLITUDE,
        open_context: ContextFactory = IioContext,
    ) -> None:
        self._settings = settings
        self._rf_enable = rf_enable
        self._peak_amplitude = float(peak_amplitude)
        self._open_context = open_context
        self._context: Any | None = None
        self._buffer: Any | None = None
        self._buffer_samples = 0
        # Until this device's iiod refuses one.
        self._partial_push = True
        # When the bursts already pushed will have left the antenna.
        self._busy_until = 0.0
        self._gain_range: tuple[float, float] = (0.0, 0.0)
        self._gain_db: float | None = None
        self._readback: dict[str, object] | None = None
        self._bursts_sent = 0
        self._samples_sent = 0
        # send() and stop() come from different threads.
        self._lock = threading.Lock()

    @property
    def description(self) -> str:
        return f"pluto {self._settings.uri} @ {self._settings.center_frequency / 1e6:.3f} MHz"

    def start(self) -> None:
        if self._context is not None:
            return
        if not isinstance(self._rf_enable, RFEnableToken):
            raise RFTransmissionDisabledError(
                "Pluto transmit requires an explicit RF acknowledgement"
            )
        try:
            context = _open(self._settings.uri, self._settings.serial, self._open_context)
        except OSError as error:
            raise TransportError(str(error)) from error
        try:
            self._gain_range = _span(
                context.read(_PHY, "voltage0", "hardwaregain_available", output=True)
            )
            # Quiet first: the attenuator at maximum and the FPGA's test tones
            # at zero, so nothing but the requested bursts is ever radiated.
            _set_gain(context, self._gain_range[0], output=True)
            for tone in _DDS_TONES:
                context.write(_TX_DEVICE, tone, "scale", "0", output=True)
            self._readback = _tune(context, self._settings, output=True)
            self._gain_db = _set_gain(context, self._settings.gain_db, output=True)
            self._readback["gain_db"] = self._gain_db
        except OSError as error:
            context.close()
            raise TransportError(f"cannot start {self.description}: {error}") from error
        except TransportError:
            context.close()
            raise
        self._context = context

    def send(self, samples: NDArray[np.complex64]) -> bool:
        scaled = scale_to_peak(samples, self._peak_amplitude)
        interleaved = np.clip(
            np.rint(scaled.view(np.float32) * np.float32(_TX_FULL_SCALE)),
            -_TX_FULL_SCALE,
            _TX_FULL_SCALE,
        ).astype(np.int16)
        with self._lock:
            if self._context is None:
                raise TransportError("sink is not started")
            try:
                self._push(interleaved, int(scaled.size))
            except OSError as error:
                raise TransportError(f"Pluto transmit failed: {error}") from error
            self._busy_until = (
                max(time.monotonic(), self._busy_until)
                + scaled.size / self._settings.sample_rate
            )
            self._bursts_sent += 1
            self._samples_sent += int(scaled.size)
        return True

    def _push(self, interleaved: NDArray[np.int16], samples: int) -> None:
        if self._partial_push:
            if samples > self._buffer_samples:
                self._resize(max(samples, _TX_BUFFER_SAMPLES))
            try:
                self._buffer.push(interleaved)
                return
            except OSError:
                if samples == self._buffer_samples:
                    raise
                # An older iiod: the partial push failed and took the buffer
                # with it.  Fall back to whole buffers, starting with this burst.
                self._partial_push = False
                self._buffer_samples = 0
        if samples != self._buffer_samples:
            self._resize(samples)
        self._buffer.push(interleaved)

    def _drain(self) -> None:
        """Let pushed bursts finish: destroying a buffer cuts off what it still holds."""

        time.sleep(min(max(self._busy_until - time.monotonic(), 0.0), _TX_DRAIN_LIMIT_S))

    def _resize(self, samples: int) -> None:
        if self._buffer is not None:
            self._drain()
            self._buffer.close()
            self._buffer = None
            self._buffer_samples = 0
        self._buffer = self._context.open_buffer(samples, output=True)
        self._buffer_samples = samples

    def set_gain(self, gain_db: float) -> float:
        """Change the transmit gain while bursts keep flowing; returns the readback."""

        if self._context is None:
            raise TransportError("sink is not started")
        self._gain_db = _set_gain(self._context, gain_db, output=True)
        if self._readback is not None:
            self._readback["gain_db"] = self._gain_db
        return self._gain_db

    def stop(self) -> None:
        with self._lock:
            context, self._context = self._context, None
            if context is None:
                return
            self._drain()
            try:
                _set_gain(context, self._gain_range[0], output=True)
            except TransportError:
                pass  # the device is gone; there is nothing left to quieten
            if self._buffer is not None:
                self._buffer.close()
                self._buffer = None
                self._buffer_samples = 0
            context.close()

    def snapshot(self) -> dict[str, object]:
        return {
            "transport": "pluto",
            "uri": self._settings.uri,
            "center_frequency_hz": self._settings.center_frequency,
            "sample_rate_sps": self._settings.sample_rate,
            "tx_gain_db": self._gain_db if self._gain_db is not None else self._settings.gain_db,
            "peak_amplitude": self._peak_amplitude,
            "bursts_sent": self._bursts_sent,
            "samples_sent": self._samples_sent,
            "partial_push": self._partial_push,
            "readback": self._readback,
        }


class PlutoSampleSource:
    """Receive a continuous sample stream from a Pluto.

    Receiving transmits nothing, so this needs no RF authorisation.
    """

    def __init__(
        self,
        settings: PlutoSettings,
        *,
        chunk_samples: int | None = None,
        queue_depth: int = 16,
        open_context: ContextFactory = IioContext,
    ) -> None:
        if chunk_samples is None:
            chunk_samples = _RX_CHUNK_SAMPLES
        if type(chunk_samples) is not int or chunk_samples < 256:
            raise TransportError("chunk_samples must be an integer at least 256")
        self._settings = settings
        self._chunk_samples = chunk_samples
        self._open_context = open_context
        self._queue: queue.Queue[NDArray[np.complex64]] = queue.Queue(maxsize=queue_depth)
        self._context: Any | None = None
        self._buffer: Any | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._gain_db: float | None = None
        self._readback: dict[str, object] | None = None
        self._chunks_received = 0
        self._samples_received = 0
        self._queue_full_backpressure = 0
        self._overflow_events = 0
        self._stream_error: str | None = None

    @property
    def description(self) -> str:
        return f"pluto {self._settings.uri} @ {self._settings.center_frequency / 1e6:.3f} MHz"

    def start(self) -> None:
        if self._context is not None:
            return
        try:
            context = _open(self._settings.uri, self._settings.serial, self._open_context)
        except OSError as error:
            raise TransportError(str(error)) from error
        try:
            self._readback = _tune(context, self._settings, output=False)
            # A fixed gain, as on a USRP: the AGC would re-settle on every burst.
            context.write(_PHY, "voltage0", "gain_control_mode", "manual")
            self._gain_db = _set_gain(context, self._settings.gain_db, output=False)
            self._readback["gain_db"] = self._gain_db
            self._buffer = context.open_buffer(self._chunk_samples, output=False)
            context.take_overflow()  # discard whatever an earlier user left latched
        except OSError as error:
            context.close()
            raise TransportError(f"cannot start {self.description}: {error}") from error
        except TransportError:
            context.close()
            raise
        self._context = context
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pluto-sample-source", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        scale = np.float32(1.0 / _RX_FULL_SCALE)
        next_poll = time.monotonic() + _OVERFLOW_POLL_S
        # stop() clears self._context before it joins this thread, and closes
        # the context only afterwards: hold it here so a poll that lands in
        # between still has it.
        context = self._context
        try:
            while not self._stop.is_set():
                raw = self._buffer.refill()
                samples = raw.astype(np.float32).view(np.complex64)
                samples *= scale
                samples.setflags(write=False)
                self._chunks_received += 1
                self._samples_received += int(samples.size)
                while not self._stop.is_set():
                    try:
                        self._queue.put(samples, timeout=0.05)
                        break
                    except queue.Full:
                        # Held, not dropped: the device overflows instead and
                        # its flag reports it.
                        self._queue_full_backpressure += 1
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + _OVERFLOW_POLL_S
                    if context.take_overflow():
                        self._overflow_events += 1
        except OSError as error:
            if not self._stop.is_set():
                self._stream_error = str(error)

    def set_gain(self, gain_db: float) -> float:
        """Change the receive gain without stopping the stream; returns the readback."""

        if self._context is None:
            raise TransportError("source is not started")
        self._gain_db = _set_gain(self._context, gain_db, output=False)
        if self._readback is not None:
            self._readback["gain_db"] = self._gain_db
        return self._gain_db

    def recv(self, timeout: float) -> NDArray[np.complex64] | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        context, self._context = self._context, None
        if context is None:
            return
        self._stop.set()
        if self._thread is not None:
            # No cancel: a refill returns within one chunk, or at the context
            # timeout if the stream has stalled, and cancelling makes libiio
            # print errors for the read it interrupted.
            self._thread.join(timeout=_TIMEOUT_MS / 1000.0 + 1.0)
            self._thread = None
        if self._buffer is not None:
            self._buffer.close()
            self._buffer = None
        context.close()

    def snapshot(self) -> dict[str, object]:
        return {
            "transport": "pluto",
            "uri": self._settings.uri,
            "center_frequency_hz": self._settings.center_frequency,
            "sample_rate_sps": self._settings.sample_rate,
            "rx_gain_db": self._gain_db if self._gain_db is not None else self._settings.gain_db,
            "chunk_samples": self._chunk_samples,
            "chunks_received": self._chunks_received,
            "samples_received": self._samples_received,
            "chunks_dropped_overflow": 0,
            "queue_full_backpressure_events": self._queue_full_backpressure,
            "rx_overflow_events": self._overflow_events,
            "stream_error": self._stream_error,
            "readback": self._readback,
        }
