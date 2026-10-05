"""Transmit-only demo app: type a message, watch it leave as an OFDM burst.

Run this in one terminal and ``rx_app`` in another.  The two processes share
nothing but the sample transport, so the receiver really is decoding what came
off the wire rather than reading a variable the transmitter set.

A UDP ingress port is open the whole time, so any other program can hand this
transmitter bytes without going through the window::

    import socket
    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(
        b"Hello from USRP B210!", ("127.0.0.1", 52001)
    )

Typed messages and ingress datagrams take exactly the same path from there on.
"""

from __future__ import annotations

import argparse
import functools
import queue
import socket
import threading
import time
from dataclasses import dataclass, replace

from PyQt5 import QtCore, QtGui, QtWidgets

from ofdm_link.phy import CURRENT_PROTOCOL_VERSION, Frame, FrameKind
from ofdm_link.phy.mcs_table import (
    MCS_TABLE,
    McsTableEntry,
    describe_mcs_entry,
    mcs_entry,
)
from ofdm_link.runtime.airtime import burst_sample_count
from ofdm_link.runtime.factory import build_burst_config

from . import dashboard_widgets as widgets
from . import options, qt_runtime
from .dashboard import TxDashboard, TxView, tx_light
from .link import MessageTransmitter, RateMeter
from .plots import PlotBuffers, SpectrumPlot, TrendPlot
from .radio_panel import RadioPanel
from .transport import set_transport_gain

_SEND_QUEUE_DEPTH = 256


@dataclass(frozen=True, slots=True)
class SentRecord:
    """What one accepted message turned into on the wire."""

    source: str
    text: str
    byte_count: int
    burst_count: int
    sample_count: int
    first_sequence: int
    mcs_index: int
    mcs_label: str


class _Bridge(QtCore.QObject):
    """Qt signal carrier for the transmit worker thread."""

    sent = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)


class TransmitWorker:
    """Encode queued messages and push each burst into the sample transport.

    The thread outlives any one radio.  Messages typed before a radio is
    started stay queued and go out when one is, rather than being silently
    dropped or forcing the operator to retype them.
    """

    def __init__(
        self,
        transmitter: MessageTransmitter,
        bridge: _Bridge,
        plot_buffers: PlotBuffers | None = None,
    ) -> None:
        self._transmitter = transmitter
        self._bridge = bridge
        self._plot_buffers = plot_buffers if plot_buffers is not None else PlotBuffers()
        self._sink = None
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=_SEND_QUEUE_DEPTH)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def sink(self):
        with self._lock:
            return self._sink

    @property
    def active_mcs_entry(self) -> McsTableEntry:
        return self._transmitter.active_mcs_entry

    def select_mcs_entry(self, entry: McsTableEntry) -> None:
        self._transmitter.select_mcs_entry(entry)

    def set_gain(self, gain_db: float) -> float:
        """Change the running radio's transmit gain; returns the readback."""

        return set_transport_gain(self.sink, gain_db)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="tx-worker", daemon=True)
        self._thread.start()

    def attach(self, sink) -> None:
        """Start one transport and send through it until it is detached."""

        sink.start()
        with self._lock:
            self._sink = sink

    def attach_from(self, factory) -> str:
        sink = factory()
        self.attach(sink)
        return sink.description

    def detach(self) -> None:
        with self._lock:
            sink, self._sink = self._sink, None
        if sink is not None:
            sink.stop()

    def submit(self, source: str, payload: bytes) -> bool:
        try:
            self._queue.put_nowait((source, payload))
            return True
        except queue.Full:
            return False

    def _run(self) -> None:
        while not self._stop.is_set():
            sink = self.sink
            if sink is None:
                # Hold the queue rather than draining it into nothing.
                self._stop.wait(0.1)
                continue
            try:
                source, payload = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                bursts = self._transmitter.encode_message(payload)
                samples = 0
                for burst in bursts:
                    if not self._send_when_there_is_room(sink, burst.samples):
                        break
                    samples += burst.sample_count
                    self._plot_buffers.set_samples(burst.samples)
                if bursts:
                    self._bridge.sent.emit(
                        SentRecord(
                            source=source,
                            text=qt_runtime.preview_payload(payload),
                            byte_count=len(payload),
                            burst_count=len(bursts),
                            sample_count=samples,
                            first_sequence=bursts[0].sequence,
                            mcs_index=bursts[0].mcs_index,
                            mcs_label=describe_mcs_entry(
                                mcs_entry(bursts[0].mcs_index)
                            ),
                        )
                    )
            except Exception as error:  # surfaced in the window, never silent
                self._bridge.failed.emit(f"{type(error).__name__}: {error}")

    def _send_when_there_is_room(self, sink, samples) -> bool:
        """Block this worker until the transport accepts the burst.

        The radio's queue is short by design, and a batch of messages fills it
        far faster than the air can drain it.  Dropping the overflow would
        report loss the link never caused, so the worker waits instead: the
        transmit thread paces itself against the radio and the window stays
        responsive because the wait is not on the UI thread.
        """

        while not self._stop.is_set():
            if sink.send(samples):
                return True
            if self.sink is not sink:
                return False  # the radio was stopped underneath us
            self._stop.wait(0.005)
        return False

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self.detach()


class IngressListener:
    """Accept application bytes over UDP and feed them to the transmitter."""

    def __init__(self, host: str, port: int, submit) -> None:
        self._address = (host, int(port))
        self._submit = submit
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.datagrams = 0

    @property
    def address(self) -> str:
        return f"{self._address[0]}:{self._address[1]}"

    def start(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(self._address)
        self._socket.settimeout(0.2)
        self._thread = threading.Thread(target=self._run, name="tx-ingress", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                payload, _ = self._socket.recvfrom(1 << 16)
            except TimeoutError:
                continue
            except OSError:
                return
            self.datagrams += 1
            self._submit("udp", payload)

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


class TransmitWindow(QtWidgets.QWidget):
    """The transmit demo window."""

    def __init__(
        self,
        resolved: options.ResolvedOptions,
        ingress_port: int,
        *,
        build_sink,
        radio_panel: RadioPanel | None = None,
        ui_fps: int = options.DEFAULT_UI_FPS,
        log_lines: int = options.DEFAULT_LOG_LINES,
        process_engine: bool = False,
        ready_file: str | None = None,
    ) -> None:
        super().__init__()
        self._log_lines = log_lines
        self._resolved = resolved
        self._build_sink = build_sink
        self._panel = radio_panel
        self._ready_file = ready_file
        self._bridge = _Bridge()
        self._plot_buffers = PlotBuffers(max_symbol_batches=1)
        if process_engine:
            from .engine import ProcessTransmitWorker

            # The child owns the UDP ingress too; build_sink must be picklable.
            self._worker = ProcessTransmitWorker(
                resolved.profile,
                resolved.mcs_entry,
                self._bridge,
                self._plot_buffers,
                ingress_port=ingress_port,
                ui_fps=ui_fps,
            )
            self._ingress = None
        else:
            self._worker = TransmitWorker(
                MessageTransmitter(resolved.profile, mcs_entry=resolved.mcs_entry),
                self._bridge,
                self._plot_buffers,
            )
            self._ingress = IngressListener("127.0.0.1", ingress_port, self._worker.submit)
        self._ingress_address = f"127.0.0.1:{ingress_port}"
        self._started = time.monotonic()
        self._goodput_now = RateMeter()
        self._messages = 0
        self._bursts = 0
        self._messages_base = 0
        self._bursts_base = 0
        self._bytes = 0
        self._samples = 0
        self._sample_rate = float(resolved.profile.sample_rate)
        self._selection = None
        self._dashboard = TxDashboard(self._sample_rate)
        self._last_view: TxView | None = None

        self.setWindowTitle("OFDM message link - transmitter")
        self.resize(1040, 780)
        self._build_ui()

        self._bridge.sent.connect(self._on_sent)
        self._bridge.failed.connect(self._on_failed)

        self._worker.start()
        if self._ingress is not None:
            self._ingress.start()
        self._status.setText(f"UDP ingress open on {self._ingress_address}")

        if self._panel is None:
            # UDP needs no device choice, so the transport comes up with the
            # window and there is nothing for the operator to select.
            description = self._worker.attach_from(functools.partial(self._build_sink, None))
            options.mark_ready(self._ready_file, description)
        else:
            self._panel.startRequested.connect(self._start_radio)
            self._panel.stopRequested.connect(self._stop_radio)
            self._panel.gainChangeRequested.connect(self._change_gain)

        self._plot_timer = QtCore.QTimer(self)
        self._plot_timer.timeout.connect(self._refresh_plot)
        self._plot_timer.start(max(1, round(1000 / ui_fps)))
        self._stats_timer = QtCore.QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start(500)

    # -- radio lifecycle --------------------------------------------------

    def _start_radio(self, selection) -> None:
        if self._panel is None:
            return
        self._panel.set_running(True, f"starting {selection.describe()}\u2026")
        QtWidgets.QApplication.processEvents()
        try:
            description = self._worker.attach_from(
                functools.partial(self._build_sink, selection)
            )
        except Exception as error:
            self._panel.set_running(False, f"{type(error).__name__}: {error}")
            self._status.setText(f"radio did not start: {error}")
            self._refresh_tab_collapse()
            return
        self._sample_rate = float(selection.sample_rate)
        self._dashboard.sample_rate = self._sample_rate
        self._selection = selection
        self._panel.set_running(True, f"transmitting on {description}")
        options.mark_ready(self._ready_file, selection.describe())
        self._refresh_tab_collapse()
        self._status.setText(
            f"RADIO ON - {selection.describe()}. "
            f"{self._worker.pending} queued message(s) will go out now."
        )

    def _minimum_gain_warning(self) -> str | None:
        """Warn when the transmitter is on but as good as off.

        Transmit gain defaults to the bottom of the device's range so that
        starting a radio never keys an amplifier at an arbitrary power.  The
        cost of that default is that a first run looks broken rather than
        quiet, so say which it is.
        """

        if self._selection is None or self._panel is None:
            return None
        minimum, maximum = self._panel.gain_range_db()
        if self._selection.gain_db > minimum + 3.0:
            return None
        return (
            f"TX gain is {self._selection.gain_db:g} dB, the minimum for this device. "
            f"Bursts are going out but the receiver will almost certainly hear nothing. "
            f"Raise Gain on the Hardware tab (range {minimum:g}-{maximum:g} dB); "
            "it takes effect immediately."
        )

    def _change_gain(self, gain_db: float) -> None:
        """Apply a gain the operator changed while the radio runs."""

        try:
            self._worker.set_gain(gain_db)
        except Exception as error:
            self._panel.show_status(f"gain change failed: {error}", error=True)
            return
        if self._selection is not None:
            self._selection = replace(self._selection, gain_db=float(gain_db))
        self._panel.show_status(f"TX gain set to {gain_db:g} dB while transmitting")

    def _stop_radio(self) -> None:
        self._selection = None
        options.clear_ready(self._ready_file)
        self._worker.detach()
        if self._panel is not None:
            self._panel.set_running(False, "radio stopped; no RF is being emitted")
        self._status.setText("radio stopped")
        self._refresh_tab_collapse()

    # -- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        """Top: evidence line, cards, MCS selector, light and trend.  Below: tabs."""

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(4)
        layout.addWidget(widgets.one_line_banner(options.evidence_banner(self._resolved)))

        # In a widget, not a bare layout, so a short window can fold it.
        self._cards = QtWidgets.QWidget()
        cards = QtWidgets.QHBoxLayout(self._cards)
        cards.setContentsMargins(0, 0, 0, 0)
        self._goodput_card = widgets.Card("Goodput (2 s)")
        self._airtime_card = widgets.Card("Airtime (2 s)")
        self._queue_card = widgets.Card("Queue")
        for card in (self._goodput_card, self._airtime_card, self._queue_card):
            cards.addWidget(card)
        mcs_card = widgets.Card("MCS for next message")
        self._mcs_combo = QtWidgets.QComboBox()
        for entry in MCS_TABLE:
            self._mcs_combo.addItem(describe_mcs_entry(entry), entry.index)
        selected = self._mcs_combo.findData(self._resolved.mcs_entry.index)
        self._mcs_combo.setCurrentIndex(selected)
        self._mcs_combo.currentIndexChanged.connect(self._on_mcs_changed)
        mcs_card.replace_value(self._mcs_combo)
        cards.addWidget(mcs_card)
        self._light_card = widgets.LightCard("TX")
        self._light_card.set_light(
            tx_light(radio_on=False, faults_10s=0, queue_rising=False, airtime=0.0)
        )
        if self._resolved.uses_radio:
            self._light_card.set_badge("RF OFF", "grey")
        else:
            self._light_card.set_badge("SIMULATED", "grey")
        cards.addWidget(self._light_card, stretch=1)
        self._reset_button = widgets.reset_button()
        self._reset_button.clicked.connect(self._reset_stats)
        cards.addWidget(self._reset_button)
        layout.addWidget(self._cards)

        # Only UHD reports late or underflowed bursts; a Pluto and the
        # simulated channel have no such events to name.
        fault = "UHD transmit fault" if self._resolved.transport == "uhd" else "transmit fault"
        self._trend = TrendPlot(
            f"Last 60 s (red: {fault})",
            primary_label="Goodput",
            primary_scale=1e6,
            primary_unit="Mbit/s",
            secondary_label="Airtime",
            secondary_unit="%",
            secondary_range=(0.0, 100.0),
        )
        layout.addWidget(self._trend, stretch=1)

        self._tabs = widgets.tab_area()
        layout.addWidget(self._tabs, stretch=3)

        send = QtWidgets.QWidget()
        left = QtWidgets.QVBoxLayout(send)
        self._mcs_detail = QtWidgets.QLabel()
        self._mcs_detail.setWordWrap(True)
        left.addWidget(self._mcs_detail)
        self._mcs_warning = QtWidgets.QLabel()
        self._mcs_warning.setWordWrap(True)
        self._mcs_warning.setStyleSheet("color:#b35c00; font-weight:bold;")
        left.addWidget(self._mcs_warning)
        self._show_mcs_details(self._resolved.mcs_entry)

        entry = QtWidgets.QHBoxLayout()
        self._input = QtWidgets.QLineEdit()
        self._input.setPlaceholderText("Type a message and press Enter to transmit it")
        self._input.returnPressed.connect(self._send_typed)
        send_button = QtWidgets.QPushButton("Send")
        send_button.clicked.connect(self._send_typed)
        entry.addWidget(self._input, stretch=1)
        entry.addWidget(send_button)
        left.addLayout(entry)

        repeat = QtWidgets.QHBoxLayout()
        repeat.addWidget(QtWidgets.QLabel("Loss test:"))
        self._repeat_count = QtWidgets.QSpinBox()
        self._repeat_count.setRange(1, 5000)
        self._repeat_count.setValue(100)
        repeat.addWidget(self._repeat_count)
        repeat.addWidget(QtWidgets.QLabel("numbered messages"))
        burst_button = QtWidgets.QPushButton("Send batch")
        burst_button.clicked.connect(self._send_batch)
        repeat.addWidget(burst_button)
        repeat.addStretch(1)
        left.addLayout(repeat)
        left.addStretch(1)
        self._tabs.addTab(send, "Send")

        self._spectrum = SpectrumPlot("Last transmitted burst")
        self._tabs.addTab(self._spectrum, "Signal")

        hardware = QtWidgets.QWidget()
        hardware_layout = QtWidgets.QVBoxLayout(hardware)
        hardware_layout.addWidget(_profile_label(self._resolved))
        if self._panel is not None:
            hardware_layout.addWidget(self._panel)
        self._hardware_text = widgets.monospace_view()
        hardware_layout.addWidget(self._hardware_text, stretch=1)
        self._hardware_text.setMinimumHeight(200)
        hardware_page = widgets.scroll_page(hardware)
        self._tabs.addTab(hardware_page, "Hardware")

        self._log = widgets.monospace_view()
        self._log.setMaximumBlockCount(self._log_lines)
        self._tabs.addTab(self._log, "Log")
        if self._panel is not None and not bool(getattr(self._panel, "_auto_start", False)):
            # Nothing is sent until a device is chosen, and that is on this tab.
            self._tabs.setCurrentWidget(hardware_page)

        self._status = QtWidgets.QLabel()
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._refresh_tab_collapse()

    def _refresh_tab_collapse(self) -> None:
        widgets.apply_tab_collapse(
            self,
            self._tabs,
            choosing_radio=widgets.choosing_radio(self._panel),
            live_view=(self._cards, self._trend),
        )

    # -- actions ---------------------------------------------------------

    def _on_mcs_changed(self, combo_index: int) -> None:
        del combo_index
        entry = mcs_entry(int(self._mcs_combo.currentData()))
        try:
            self._worker.select_mcs_entry(entry)
        except Exception as error:
            self._status.setText(f"MCS change failed: {error}")
            return
        self._show_mcs_details(entry)
        self._status.setText(
            f"{describe_mcs_entry(entry)} will apply to the next encoded message"
        )

    def _show_mcs_details(self, entry: McsTableEntry) -> None:
        airtime_ms = _airtime_ms(self._resolved, entry)
        self._mcs_detail.setText(
            f"Spectral efficiency {entry.spectral_efficiency:.2f} bit/symbol; "
            f"996-byte PHY payload airtime {airtime_ms:.2f} ms at "
            f"{self._resolved.profile.sample_rate / 1e6:g} MS/s."
        )
        self._mcs_warning.setText(
            "WARNING: Uncoded uses CRC error detection only and has no error "
            "correction. It needs substantially higher SNR."
            if entry.fec_scheme == "uncoded"
            else ""
        )
        self._mcs_warning.setVisible(entry.fec_scheme == "uncoded")

    def _send_typed(self) -> None:
        text = self._input.text()
        if not text:
            return
        if not self._worker.submit("typed", text.encode("utf-8")):
            self._status.setText("send queue is full; the transport is not keeping up")
            return
        self._input.clear()
        if self._worker.sink is None:
            self._status.setText(
                f"queued ({self._worker.pending}); nothing is transmitted until you start the radio"
            )

    def _send_batch(self) -> None:
        total = int(self._repeat_count.value())
        accepted = 0
        for index in range(1, total + 1):
            payload = f"seq-test {index}/{total}".encode()
            if not self._worker.submit("batch", payload):
                break
            accepted += 1
        self._status.setText(
            f"queued {accepted} of {total} batch messages"
            + ("" if accepted == total else "; queue filled up")
        )

    # -- slots -----------------------------------------------------------

    def _on_sent(self, record: SentRecord) -> None:
        self._messages += 1
        self._bursts += record.burst_count
        self._bytes += record.byte_count
        self._samples += record.sample_count
        stamp = time.strftime("%H:%M:%S")
        qt_runtime.append_line_following_tail(
            self._log,

            f"{stamp}  seq {record.first_sequence:5d}  "
            f"{record.byte_count:6d} B  {record.burst_count} burst(s)  "
            f"{record.mcs_label}  [{record.source}]  {record.text}"
        )

    def _refresh_plot(self) -> None:
        snapshot = self._plot_buffers.snapshot()
        if snapshot.samples is not None:
            self._spectrum.set_samples(
                snapshot.samples,
                sample_rate=self._resolved.profile.sample_rate,
            )
        self._spectrum.refresh()

    def _on_failed(self, message: str) -> None:
        self._status.setText(f"transmit error: {message}")

    def _refresh_stats(self) -> None:
        now = time.monotonic()
        elapsed = max(now - self._started, 1e-9)
        self._goodput_now.update(now, self._bytes)
        sink = self._worker.sink
        snapshot = None if sink is None else sink.snapshot()
        faults = None if snapshot is None else snapshot.get("uhd_faults")
        pending = self._worker.pending
        view = self._dashboard.refresh(
            now,
            radio_on=sink is not None,
            sent_bytes=self._bytes,
            samples=self._samples,
            pending=pending,
            faults=faults if isinstance(faults, dict) else None,
        )
        self._last_view = view
        self._goodput_card.set_value(f"{view.goodput_bps / 1e6:.3f}", "Mbit/s sent")
        self._airtime_card.set_value(f"{view.airtime * 100:.1f} %", "transmit duty")
        self._queue_card.set_value(f"{pending}", "messages queued to send")
        self._light_card.set_light(view.light)
        if self._resolved.uses_radio:
            self._light_card.set_badge(
                *(("RF ON", "red") if sink is not None else ("RF OFF", "grey"))
            )
        self._trend.set_points(view.trend)

        if self._tabs.isVisible():
            qt_runtime.set_text_preserving_scroll(
                self._hardware_text,
                follow_tail=False,
                text=self._hardware_rows(view, snapshot, elapsed),
            )

        warning = self._minimum_gain_warning()
        if warning is not None and self._messages > 0:
            self._status.setStyleSheet("color:#b35c00; font-weight:bold;")
            self._status.setText(warning)
        elif warning is None:
            self._status.setStyleSheet("")

    def _hardware_rows(self, view: TxView, snapshot, elapsed: float) -> str:
        airtime = view.totals["samples"] / self._sample_rate
        ingress = (
            self._worker.ingress_datagrams if self._ingress is None else self._ingress.datagrams
        )
        rows = [
            f"radio              {'ON' if snapshot is not None else 'off':>10}",
            f"messages sent      {self._messages - self._messages_base:10d}   (since reset)",
            f"bursts sent        {self._bursts - self._bursts_base:10d}",
            f"application bytes  {view.totals['sent_bytes']:10d}",
            f"waveform airtime   {airtime:10.3f} s"
            f"  ({airtime / elapsed * 100:.1f}% duty since reset)",
            f"offered goodput    {view.totals['sent_bytes'] * 8 / elapsed:10.0f} bit/s"
            "   (average since reset)",
            f"udp ingress        {ingress:10d} datagrams",
            f"active MCS         {describe_mcs_entry(self._worker.active_mcs_entry)}",
            "",
        ]
        if snapshot is None:
            rows.append("transport: no radio started")
            return "\n".join(rows)
        faults = snapshot.get("uhd_faults")
        if isinstance(faults, dict):
            rows.append("UHD faults:")
            rows += [f"  {key:<28} {value}" for key, value in faults.items() if key != "events"]
            rows.append("")
        rows.append("transport:")
        rows += [
            f"  {key:<28} {value}" for key, value in snapshot.items() if key != "uhd_faults"
        ]
        return "\n".join(rows)

    def _reset_stats(self) -> None:
        """Zero what this window shows; the radio keeps transmitting."""

        now = time.monotonic()
        sink = self._worker.sink
        snapshot = None if sink is None else sink.snapshot()
        faults = None if snapshot is None else snapshot.get("uhd_faults")
        self._dashboard.reset(
            now,
            sent_bytes=self._bytes,
            samples=self._samples,
            faults=faults if isinstance(faults, dict) else None,
        )
        self._messages_base = self._messages
        self._bursts_base = self._bursts
        self._started = now
        self._goodput_now = RateMeter()
        self._trend.set_points(())
        self._status.setText("stats reset; the radio kept running")

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 (Qt override)
        self._plot_timer.stop()
        self._stats_timer.stop()
        if self._ingress is not None:
            self._ingress.stop()
        self._worker.stop()
        options.clear_ready(self._ready_file)
        super().closeEvent(event)


def _profile_label(resolved: options.ResolvedOptions) -> QtWidgets.QLabel:
    described = resolved.profile.describe()
    text = (
        f"startup {describe_mcs_entry(resolved.mcs_entry)} | "
        f"frame payload {described['frame_payload_bytes']} B "
        f"({described['max_message_payload_bytes']} B usable) | "
        f"sample rate {resolved.profile.sample_rate / 1e6:g} MS/s must match; "
        "the receiver learns MCS/FEC from each burst header"
    )
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color:#9fb3c8; padding:2px;")
    return label


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ofdm-message-tx",
        description="One-way OFDM message transmitter with a Qt demo window.",
    )
    options.add_common_arguments(parser)
    app = parser.add_argument_group("transmitter")
    app.add_argument(
        "--ingress-port",
        type=int,
        default=options.DEFAULT_INGRESS_PORT,
        help=(
            "localhost UDP port that accepts application bytes to transmit "
            f"(default: {options.DEFAULT_INGRESS_PORT})"
        ),
    )
    app.add_argument(
        "--peak-amplitude",
        type=float,
        default=0.7,
        help="scale each burst to this peak magnitude before transmission (default: 0.7)",
    )
    app.add_argument(
        "--engine",
        choices=("process", "thread"),
        default="process",
        help=(
            "process runs the radio, encoder and UDP ingress in a child process so "
            "the GUI cannot stall them; thread keeps them in this process "
            "(default: process)"
        ),
    )
    rf = parser.add_argument_group("RF safety gate")
    rf.add_argument(
        "--enable-rf",
        action="store_true",
        help="required for --transport uhd or pluto; this process will key a transmitter",
    )
    rf.add_argument(
        "--acknowledgement",
        default=None,
        help=f"must be exactly {options.RF_ACKNOWLEDGEMENT!r}",
    )
    return parser


def _airtime_ms(resolved: options.ResolvedOptions, entry: McsTableEntry) -> float:
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        entry.modulation,
        0,
        bytes(996),
    )
    samples = burst_sample_count(
        frame,
        build_burst_config(resolved.config),
        wire_version=entry.wire_version,
    )
    return samples / resolved.profile.sample_rate * 1000.0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Plots use NumPy too; keep its BLAS pool from spinning idle cores.
    from ofdm_link.runtime.cpu_budget import apply_cpu_budget

    apply_cpu_budget(1)
    resolved = options.resolve(args)
    if resolved.uses_radio:
        # Taken before any window exists, so a process that was not authorised
        # to transmit cannot become one by clicking Start.
        options.require_rf_capability(args)

    # The QApplication must stay referenced: collecting it before the first
    # QWidget is constructed aborts the process.
    application = qt_runtime.create_application()
    panel = (
        RadioPanel(
            direction="tx",
            center_frequency_hz=resolved.config.phy.center_frequency,
            sample_rate=resolved.config.phy.sample_rate,
            backend=resolved.transport,
            preferred_serial=args.serial,
            preferred_antenna=args.antenna or resolved.config.radio.tx_antenna,
            preferred_channel=args.channel,
            preferred_gain_db=args.gain,
            auto_start=args.auto_start,
        )
        if resolved.uses_radio
        else None
    )
    window = TransmitWindow(
        resolved,
        args.ingress_port,
        build_sink=functools.partial(options.build_sink, args, resolved),
        radio_panel=panel,
        ui_fps=args.ui_fps,
        log_lines=args.log_lines,
        process_engine=args.engine == "process",
        ready_file=args.ready_file,
    )
    return qt_runtime.run_window(window, application, args.geometry)


if __name__ == "__main__":
    raise SystemExit(main())
