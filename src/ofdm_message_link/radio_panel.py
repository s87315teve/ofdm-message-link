"""Device, front-end and radio-parameter selection widget.

The lists are populated from what UHD reports, not from a table in this file:
``Refresh`` enumerates attached devices and selecting one probes it for its
real channels, antenna ports and gain ranges.  Both run off the UI thread
because enumerating takes about half a second and opening a B2xx or an
N-series device takes a couple more, and because either can fail -- most often because the other app
already holds that device.

The panel never starts a radio by itself.  It emits a selection when the
operator presses Start, and the window decides what to do with it.  Gain is
the one setting that stays live while the radio runs: changing it emits
``gainChangeRequested`` and the window applies it to the running radio.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from PyQt5 import QtCore, QtWidgets

from . import devices
from .devices import DeviceCapabilities, DeviceError, DiscoveredDevice, RadioSelection


@dataclass(frozen=True, slots=True)
class _Discovery:
    found: tuple[DiscoveredDevice, ...]
    error: str | None


@dataclass(frozen=True, slots=True)
class _Probe:
    serial: str
    capabilities: DeviceCapabilities | None
    error: str | None


class _Worker(QtCore.QObject):
    discovered = QtCore.pyqtSignal(object)
    probed = QtCore.pyqtSignal(object)


class RadioPanel(QtWidgets.QGroupBox):
    """Choose a device and front end, then start or stop the radio."""

    startRequested = QtCore.pyqtSignal(object)
    stopRequested = QtCore.pyqtSignal()
    gainChangeRequested = QtCore.pyqtSignal(float)

    def __init__(
        self,
        *,
        direction: str,
        center_frequency_hz: float,
        sample_rate: int,
        backend: str = "uhd",
        preferred_serial: str | None = None,
        preferred_antenna: str | None = None,
        preferred_channel: int | None = None,
        preferred_gain_db: float | None = None,
        auto_start: bool = False,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        if direction not in {"rx", "tx"}:
            raise ValueError("direction must be rx or tx")
        super().__init__(f"Radio ({direction.upper()})", parent)
        self._direction = direction
        self._backend = backend
        self._preferred_serial = preferred_serial
        self._preferred_antenna = preferred_antenna
        self._preferred_channel = preferred_channel
        self._preferred_gain_db = preferred_gain_db
        self._auto_start = auto_start
        self._devices: tuple[DiscoveredDevice, ...] = ()
        self._capabilities: DeviceCapabilities | None = None
        self._busy = False
        self._running = False

        self._signals = _Worker()
        self._signals.discovered.connect(self._on_discovered)
        self._signals.probed.connect(self._on_probed)

        self._build_ui(center_frequency_hz, sample_rate)
        self.refresh()

    # -- construction ----------------------------------------------------

    def _build_ui(self, center_frequency_hz: float, sample_rate: int) -> None:
        layout = QtWidgets.QGridLayout(self)

        layout.addWidget(QtWidgets.QLabel("Device"), 0, 0)
        self._device = QtWidgets.QComboBox()
        self._device.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self._device.currentIndexChanged.connect(self._on_device_changed)
        layout.addWidget(self._device, 0, 1, 1, 3)
        self._refresh_button = QtWidgets.QPushButton("Refresh")
        self._refresh_button.clicked.connect(self.refresh)
        layout.addWidget(self._refresh_button, 0, 4)

        layout.addWidget(QtWidgets.QLabel("Front end"), 1, 0)
        self._channel = QtWidgets.QComboBox()
        self._channel.currentIndexChanged.connect(self._on_channel_changed)
        layout.addWidget(self._channel, 1, 1)
        layout.addWidget(QtWidgets.QLabel("Antenna"), 1, 2)
        self._antenna = QtWidgets.QComboBox()
        layout.addWidget(self._antenna, 1, 3)

        layout.addWidget(QtWidgets.QLabel("Gain (dB)"), 2, 0)
        self._gain = QtWidgets.QDoubleSpinBox()
        self._gain.setDecimals(2)
        self._gain.setRange(0.0, 0.0)
        self._gain.setSingleStep(1.0)
        # Typing "25" must not apply 2 dB on the way; arrows, Enter and
        # leaving the field still apply at once.
        self._gain.setKeyboardTracking(False)
        self._gain.setToolTip("Adjustable while the radio runs; applied immediately.")
        self._gain.valueChanged.connect(self._on_gain_changed)
        layout.addWidget(self._gain, 2, 1)
        layout.addWidget(QtWidgets.QLabel("Center (MHz)"), 2, 2)
        self._frequency = QtWidgets.QDoubleSpinBox()
        self._frequency.setDecimals(3)
        self._frequency.setRange(1.0, 8000.0)
        self._frequency.setSingleStep(1.0)
        self._frequency.setValue(center_frequency_hz / 1e6)
        layout.addWidget(self._frequency, 2, 3)

        layout.addWidget(QtWidgets.QLabel("Sample rate"), 3, 0)
        self._sample_rate = QtWidgets.QComboBox()
        for rate in sorted(devices.SUPPORTED_SAMPLE_RATES):
            self._sample_rate.addItem(f"{rate / 1e6:g} MS/s", rate)
        index = self._sample_rate.findData(sample_rate)
        self._sample_rate.setCurrentIndex(max(index, 0))
        layout.addWidget(self._sample_rate, 3, 1)

        self._start_button = QtWidgets.QPushButton("Start radio")
        self._start_button.clicked.connect(self._toggle)
        layout.addWidget(self._start_button, 3, 3, 1, 2)

        self._status = QtWidgets.QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status, 4, 0, 1, 5)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)

    # -- discovery and probing -------------------------------------------

    def refresh(self) -> None:
        """Re-enumerate attached devices without opening any of them."""

        if self._busy or self._running:
            return
        self._set_busy(True, "looking for attached devices…")

        def run() -> None:
            try:
                found = devices.discover(backend=self._backend)
                self._signals.discovered.emit(_Discovery(found, None))
            except DeviceError as error:
                self._signals.discovered.emit(_Discovery((), str(error)))

        threading.Thread(target=run, name="radio-discover", daemon=True).start()

    def _on_discovered(self, result: _Discovery) -> None:
        self._set_busy(False, "")
        self._devices = result.found
        self._device.blockSignals(True)
        self._device.clear()
        for device in result.found:
            self._device.addItem(device.describe(), device.serial)
        self._device.blockSignals(False)

        if result.error is not None:
            self._fail(result.error)
            return
        if not result.found:
            self._fail(
                "no ADALM-Pluto found. Check the USB connection, then press Refresh."
                if self._backend == "pluto"
                else "no USRP found. Check the USB connection, or for an N200/N210 that "
                "the Ethernet port has an address on its subnet, then press Refresh."
            )
            self._start_button.setEnabled(False)
            return

        wanted = self._device.findData(self._preferred_serial)
        self._device.setCurrentIndex(wanted if wanted >= 0 else 0)
        self._on_device_changed()

    def _on_device_changed(self) -> None:
        device = self.selected_device()
        if device is None or self._busy or self._running:
            return
        self._channel.clear()
        self._antenna.clear()
        self._capabilities = None
        self._set_busy(True, f"opening {device.serial} to read its front ends…")

        def run() -> None:
            try:
                self._signals.probed.emit(_Probe(device.serial, devices.probe(device), None))
            except DeviceError as error:
                self._signals.probed.emit(_Probe(device.serial, None, str(error)))

        threading.Thread(target=run, name="radio-probe", daemon=True).start()

    def _on_probed(self, result: _Probe) -> None:
        self._set_busy(False, "")
        current = self.selected_device()
        if current is None or current.serial != result.serial:
            return  # the operator moved on while the probe was running
        if result.capabilities is None:
            self._capabilities = None
            self._start_button.setEnabled(False)
            self._fail(
                f"{result.error}\n"
                "A device already streaming in another window cannot be probed."
            )
            return

        self._capabilities = result.capabilities
        usable = [
            channel
            for channel in result.capabilities.channels
            if channel.antennas(self._direction)
        ]
        self._channel.blockSignals(True)
        self._channel.clear()
        for channel in usable:
            self._channel.addItem(f"{channel.label} (ch {channel.index})", channel.index)
        self._channel.blockSignals(False)
        wanted_channel = self._channel.findData(self._preferred_channel)
        self._channel.setCurrentIndex(wanted_channel if wanted_channel >= 0 else 0)
        self._on_channel_changed()

        extra = "" if current.is_supported else "  This device family is untested here."
        model = result.capabilities.mboard_name or current.product or current.driver
        low, high = result.capabilities.freq_range_hz(self._direction)
        self._note(
            f"{model} serial {current.serial}, subdev {result.capabilities.subdev_spec}, "
            f"{len(usable)} {self._direction.upper()} front end(s), "
            f"tunes {low / 1e6:.0f}-{high / 1e6:.0f} MHz.{extra}"
        )
        self._start_button.setEnabled(True)

        if self._auto_start:
            # Once only: a later Refresh must not silently key a transmitter.
            self._auto_start = False
            self._toggle()

    def _on_channel_changed(self) -> None:
        capabilities = self._capabilities
        if capabilities is None or self._channel.currentIndex() < 0:
            return
        channel = capabilities.channel(self._channel.currentData())
        antennas = channel.antennas(self._direction)
        self._antenna.clear()
        for antenna in antennas:
            self._antenna.addItem(devices.antenna_label(capabilities.device.driver, antenna), antenna)
        wanted = self._antenna.findData(self._preferred_antenna)
        self._antenna.setCurrentIndex(wanted if wanted >= 0 else 0)

        low, high = channel.gain_range_db(self._direction)
        self._gain.setRange(low, high)
        if self._preferred_gain_db is not None:
            self._gain.setValue(min(max(self._preferred_gain_db, low), high))
            return
        # Transmit starts at the bottom of the range: raising power is an
        # operator decision, not a default that happens on first start.
        self._gain.setValue(low if self._direction == "tx" else min(high, low + (high - low) / 2))

    def _on_gain_changed(self, value: float) -> None:
        if self._running:
            self.gainChangeRequested.emit(float(value))

    # -- selection --------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    def selected_device(self) -> DiscoveredDevice | None:
        serial = self._device.currentData()
        for device in self._devices:
            if device.serial == serial:
                return device
        return None

    def gain_range_db(self) -> tuple[float, float]:
        """The gain limits the probed device reported for this direction."""

        return (float(self._gain.minimum()), float(self._gain.maximum()))

    def selection(self) -> RadioSelection | None:
        """The operator's current choice, or None when it is incomplete."""

        device = self.selected_device()
        if device is None or self._channel.currentIndex() < 0 or not self._antenna.currentData():
            return None
        return RadioSelection(
            driver=device.driver,
            address=device.address,
            serial=device.serial,
            channel=int(self._channel.currentData()),
            antenna=str(self._antenna.currentData()),
            gain_db=float(self._gain.value()),
            center_frequency_hz=float(self._frequency.value()) * 1e6,
            sample_rate=int(self._sample_rate.currentData()),
        )

    # -- start / stop ------------------------------------------------------

    def _toggle(self) -> None:
        if self._running:
            self.stopRequested.emit()
            return
        selection = self.selection()
        capabilities = self._capabilities
        if selection is None or capabilities is None:
            self._fail("pick a device and front end first")
            return
        problems = devices.validate(selection, capabilities, direction=self._direction)
        if problems:
            self._fail("; ".join(problems))
            return
        self.startRequested.emit(selection)

    def set_running(self, running: bool, detail: str = "") -> None:
        """Reflect the radio's real state, which the window owns."""

        self._running = running
        self._start_button.setText("Stop radio" if running else "Start radio")
        self._start_button.setEnabled(True)
        # Gain stays editable: it is applied to the running radio.
        for widget in (
            self._device,
            self._refresh_button,
            self._channel,
            self._antenna,
            self._frequency,
            self._sample_rate,
        ):
            widget.setEnabled(not running)
        if detail:
            self._note(detail) if running else self._fail(detail)

    def show_status(self, message: str, *, error: bool = False) -> None:
        """Report something the window did with this panel's radio."""

        self._fail(message) if error else self._note(message)

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        self._start_button.setEnabled(not busy and self._capabilities is not None)
        self._device.setEnabled(not busy)
        self._refresh_button.setEnabled(not busy)
        if message:
            self._note(message)

    def _note(self, message: str) -> None:
        self._status.setStyleSheet("color:#9fb3c8;")
        self._status.setText(message)

    def _fail(self, message: str) -> None:
        self._status.setStyleSheet("color:#ff9d9d;")
        self._status.setText(message)
