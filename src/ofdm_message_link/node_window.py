"""The node_app window: one node's transmit, receive and link adaptation at a glance.

Top: the demo banner, a profile line, three card rows (TX, RX, LA) and a
60-second strip of the TX MCS and the filtered peer SNR the LA works from.
Tabs: Live (RX spectrum, constellation, RX goodput), Send, Link adaptation,
Log, Hardware.  The radio, codec and LA all run in the engine child
(:mod:`.node_engine`); this window only shows and steers them.
"""

from __future__ import annotations

import json
import time
from collections import namedtuple
from dataclasses import replace

from PyQt5 import QtCore, QtGui, QtWidgets

from ofdm_link.phy.mcs_table import describe_mcs_entry, mcs_entry

from . import dashboard_widgets as widgets
from . import log_limits, node_options, options, qt_runtime
from .dashboard import SNR_THRESHOLD_DB
from .node_app import (
    BANNER,
    STATS_INTERVAL_S,
    ControlServer,
    NodeMonitor,
    control_handler,
    pluto_factory,
    udp_factory,
)
from .node_engine import NodeSpec, ProcessNodeWorker
from .plots import ConstellationPlot, PlotBuffers, SpectrumPlot, TrendPlot
from .radio_panel import RadioPanel
from .tx_app import mcs_menu_label, tx_menu_entries

_LaPoint = namedtuple("_LaPoint", "t primary secondary alert")
_FEEDBACK_GREEN_S = 0.2
_FEEDBACK_YELLOW_S = 0.5


class _Bridge(QtCore.QObject):
    """Carries the engine pump thread's events onto the GUI thread."""

    event = QtCore.pyqtSignal(str, object)


def profile_line(setup: node_options.NodeSetup, la_spec: str) -> str:
    return (
        f"Node {setup.node_id} · TX {setup.tx_frequency / 1e6:.3f} MHz · "
        f"RX {setup.rx_frequency / 1e6:.3f} MHz · "
        f"{setup.resolved.profile.sample_rate / 1e6:g} MS/s · LA {la_spec} · "
        f"ingress :{setup.ingress_port} egress :{setup.egress[1]}"
    )


def feedback_colour(age_s: float | None) -> str:
    if age_s is None or age_s >= _FEEDBACK_YELLOW_S:
        return "red"
    return "green" if age_s < _FEEDBACK_GREEN_S else "yellow"


def la_points(history) -> tuple[_LaPoint, ...]:
    """LA history (t, mcs, snr_f, stale) as TrendPlot points: MCS primary, SNR secondary."""

    return tuple(
        _LaPoint(t, float(mcs if mcs is not None else 0), snr, bool(stale))
        for t, mcs, snr, stale in history
    )


class NodeWindow(QtWidgets.QWidget):
    """One node's window."""

    def __init__(
        self,
        args,
        setup: node_options.NodeSetup,
        spec: NodeSpec,
        *,
        radio_panel: RadioPanel | None = None,
        worker_class=ProcessNodeWorker,
        stats_log=None,
        ready_file: str | None = None,
        control_port: int = 0,
    ) -> None:
        super().__init__()
        self._args = args
        self._setup = setup
        self._spec = spec
        self._panel = radio_panel
        self._stats_log = stats_log
        self._ready_file = ready_file
        self._sample_rate = float(setup.resolved.profile.sample_rate)
        self._monitor = NodeMonitor(
            setup.node_id, setup.mode if setup.resolved.uses_radio else "udp", self._sample_rate
        )
        self._schedule = node_options.GainSchedule(setup.gain_profile)
        self._ready_at: float | None = None
        self._tx_gain_db = float(args.tx_gain)
        self._bridge = _Bridge()
        self._bridge.event.connect(self._on_event)
        self._plot_buffers = PlotBuffers(max_symbol_batches=4)
        self._worker = worker_class(
            setup.resolved.profile,
            spec,
            lambda kind, value: self._bridge.event.emit(kind, value),
            ui_fps=args.ui_fps,
            decode_workers=args.decode_workers,
            aggregation=setup.resolved.aggregation,
        )
        self._control = None
        if control_port:
            try:
                self._control = ControlServer(
                    control_port,
                    control_handler(self._worker, self._monitor, log_mark=self._log_mark,
                                    request_stop=self.close),
                )
            except OSError as error:
                raise SystemExit(f"cannot open control port {control_port}: {error}") from error

        self.setWindowTitle(f"OFDM node {setup.node_id}")
        self.resize(1180, 860)
        self._build_ui()
        self._worker.start()

        if self._panel is None:
            self._attach(udp_factory(args, setup), "udp simulation")
        else:
            self._panel.startRequested.connect(self._start_radio)
            self._panel.stopRequested.connect(self._stop_radio)
            self._panel.gainChangeRequested.connect(self._change_rx_gain)

        self._stats_timer = QtCore.QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start(round(STATS_INTERVAL_S * 1000))
        self._plot_timer = QtCore.QTimer(self)
        self._plot_timer.timeout.connect(self._refresh_plots)
        self._plot_timer.start(max(1, round(1000 / args.ui_fps)))
        self._control_timer = QtCore.QTimer(self)
        self._control_timer.timeout.connect(self._poll_control)
        self._control_timer.start(50)

    # -- construction ---------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(4)
        layout.addWidget(widgets.one_line_banner(BANNER))
        self._profile_label = QtWidgets.QLabel(profile_line(self._setup, self._spec.la_spec))
        self._profile_label.setStyleSheet("color:#9fb3c8; padding:2px;")
        layout.addWidget(self._profile_label)

        self._cards = QtWidgets.QWidget()
        rows = QtWidgets.QGridLayout(self._cards)
        rows.setContentsMargins(0, 0, 0, 0)
        self._tx_light = widgets.LightCard("TX")
        self._tx_light.set_badge("RF OFF" if self._setup.resolved.uses_radio else "SIMULATED",
                                 "grey")
        self._queue_card = widgets.Card("Queue")
        self._airtime_card = widgets.Card("Airtime (2 s)")
        self._sent_card = widgets.Card("Sent goodput (2 s)")
        self._rx_light = widgets.LightCard("Link")
        self._snr_card = widgets.Card("SNR (2 s)")
        self._loss_card = widgets.Card("Loss (10 s)")
        self._rx_goodput_card = widgets.Card("Rx goodput (2 s)")
        self._mcs_card = widgets.Card("TX MCS")
        self._snr_eff_card = widgets.Card("SNR_eff / offset")
        self._peer_loss_card = widgets.Card("Peer-reported loss (10 s)")
        self._feedback_card = widgets.Card("Feedback age")
        for row, cards in enumerate((
            (self._tx_light, self._queue_card, self._airtime_card, self._sent_card),
            (self._rx_light, self._snr_card, self._loss_card, self._rx_goodput_card),
            (self._mcs_card, self._snr_eff_card, self._peer_loss_card, self._feedback_card),
        )):
            for column, card in enumerate(cards):
                rows.addWidget(card, row, column)
        layout.addWidget(self._cards)

        self._la_trend = TrendPlot(
            "Last 60 s: TX MCS (red: stale feedback)",
            primary_label="MCS",
            secondary_label="SNR_f",
            secondary_unit="dB",
            secondary_range=(0.0, 35.0),
        )
        layout.addWidget(self._la_trend, stretch=1)

        self._tabs = widgets.tab_area()
        layout.addWidget(self._tabs, stretch=3)

        live = QtWidgets.QWidget()
        live_layout = QtWidgets.QHBoxLayout(live)
        self._spectrum = SpectrumPlot("RX spectrum")
        self._constellation = ConstellationPlot("RX constellation")
        self._rx_trend = TrendPlot(
            "RX goodput, 60 s", primary_label="Goodput", primary_scale=1e6,
            primary_unit="Mbit/s", secondary_label="SNR", secondary_unit="dB",
            secondary_range=(0.0, 35.0),
        )
        for plot in (self._spectrum, self._constellation, self._rx_trend):
            live_layout.addWidget(plot, stretch=1)
        self._tabs.addTab(live, "Live")

        send = QtWidgets.QWidget()
        send_layout = QtWidgets.QVBoxLayout(send)
        entry = QtWidgets.QHBoxLayout()
        self._input = QtWidgets.QLineEdit()
        self._input.setPlaceholderText("Type a message and press Enter to send it to the peer")
        self._input.returnPressed.connect(self._send_typed)
        button = QtWidgets.QPushButton("Send")
        button.clicked.connect(self._send_typed)
        entry.addWidget(self._input, stretch=1)
        entry.addWidget(button)
        send_layout.addLayout(entry)
        batch = QtWidgets.QHBoxLayout()
        batch.addWidget(QtWidgets.QLabel("Loss test:"))
        self._batch_count = QtWidgets.QSpinBox()
        self._batch_count.setRange(1, 5000)
        self._batch_count.setValue(100)
        batch.addWidget(self._batch_count)
        batch_button = QtWidgets.QPushButton("Send batch")
        batch_button.clicked.connect(self._send_batch)
        batch.addWidget(batch_button)
        batch.addStretch(1)
        send_layout.addLayout(batch)
        send_layout.addStretch(1)
        self._tabs.addTab(send, "Send")

        la = QtWidgets.QWidget()
        la_layout = QtWidgets.QVBoxLayout(la)
        switch = QtWidgets.QHBoxLayout()
        self._auto_button = QtWidgets.QRadioButton("Auto (link adaptation)")
        self._fixed_button = QtWidgets.QRadioButton("Fixed MCS")
        self._auto_button.setChecked(True)
        self._fixed_button.toggled.connect(self._on_la_mode)
        self._mcs_combo = QtWidgets.QComboBox()
        initial = mcs_entry(self._spec.mcs_index)
        for item in tx_menu_entries(initial):
            self._mcs_combo.addItem(mcs_menu_label(item, self._sample_rate), item.index)
        self._mcs_combo.setCurrentIndex(self._mcs_combo.findData(initial.index))
        self._mcs_combo.setEnabled(False)
        self._mcs_combo.currentIndexChanged.connect(self._on_mcs_changed)
        for item in (self._auto_button, self._fixed_button, self._mcs_combo):
            switch.addWidget(item)
        switch.addStretch(1)
        la_layout.addLayout(switch)
        thresholds = ", ".join(
            f"MCS {index}: {value:g} dB" for index, value in sorted(SNR_THRESHOLD_DB.items())
            if index >= 8
        )
        info = QtWidgets.QLabel(
            f"Algorithm {self._spec.la_spec} {' '.join(self._spec.la_args)}; reporter "
            f"{self._spec.reporter_spec}. Thresholds (preamble SNR for < 1 % loss): {thresholds}"
        )
        info.setWordWrap(True)
        la_layout.addWidget(info)
        self._decisions = widgets.monospace_view()
        self._decisions.setMaximumBlockCount(20)
        la_layout.addWidget(self._decisions, stretch=1)
        self._tabs.addTab(la, "Link adaptation")

        self._log = widgets.monospace_view()
        self._log.setMaximumBlockCount(self._args.log_lines)
        self._tabs.addTab(self._log, "Log")

        hardware = QtWidgets.QWidget()
        hardware_layout = QtWidgets.QVBoxLayout(hardware)
        if self._panel is not None:
            hardware_layout.addWidget(self._panel)
        tx_row = QtWidgets.QHBoxLayout()
        tx_row.addWidget(QtWidgets.QLabel("TX gain (dB)"))
        self._tx_gain = QtWidgets.QDoubleSpinBox()
        self._tx_gain.setRange(node_options.MIN_TX_GAIN_DB, node_options.MAX_TX_GAIN_DB)
        self._tx_gain.setSingleStep(0.25)
        self._tx_gain.setValue(self._tx_gain_db)
        self._tx_gain.editingFinished.connect(self._change_tx_gain)
        tx_row.addWidget(self._tx_gain)
        tx_row.addWidget(
            QtWidgets.QLabel(f"TX frequency {self._setup.tx_frequency / 1e6:.3f} MHz (profile)")
        )
        tx_row.addStretch(1)
        hardware_layout.addLayout(tx_row)
        self._hardware_text = widgets.monospace_view()
        self._hardware_text.setMinimumHeight(200)
        hardware_layout.addWidget(self._hardware_text, stretch=1)
        self._hardware_page = widgets.scroll_page(hardware)
        self._tabs.addTab(self._hardware_page, "Hardware")
        if self._panel is not None and not bool(getattr(self._panel, "_auto_start", False)):
            self._tabs.setCurrentWidget(self._hardware_page)

        self._status = QtWidgets.QLabel()
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

    # -- radio lifecycle ----------------------------------------------------------

    def _attach(self, factory, what: str) -> bool:
        try:
            description = self._worker.attach_from(factory)
        except Exception as error:
            self._status.setText(f"radio did not start: {error}")
            if self._panel is not None:
                self._panel.set_running(False, f"{type(error).__name__}: {error}")
            return False
        self._ready_at = time.monotonic()
        self._monitor.ready_at = self._ready_at
        options.mark_ready(self._ready_file, description)
        if self._setup.resolved.uses_radio:
            self._tx_light.set_badge("RF ON", "red")
        self._status.setText(f"running: {description} ({what})")
        return True

    def _start_radio(self, selection) -> None:
        if self._panel is None:
            return
        self._panel.set_running(True, f"starting {selection.describe()}…")
        QtWidgets.QApplication.processEvents()
        rx_gain = self._panel.device_gain_db(selection.gain_db)
        tx_selection = replace(selection, center_frequency_hz=self._setup.tx_frequency,
                               gain_db=self._tx_gain_db, antenna="TX")
        factory = pluto_factory(
            self._args, self._setup, uri=selection.address, serial=selection.serial,
            tx_gain_db=tx_selection.gain_db, rx_gain_db=rx_gain,
        )
        if self._attach(factory, selection.describe()):
            self._panel.set_running(True, f"node running on {selection.serial}")

    def _stop_radio(self) -> None:
        options.clear_ready(self._ready_file)
        self._worker.detach()
        self._ready_at = None
        if self._panel is not None:
            self._panel.set_running(False, "radio stopped; no RF is being emitted")
        self._tx_light.set_badge("RF OFF", "grey")
        self._status.setText("radio stopped")

    def _change_rx_gain(self, gain_db: float) -> None:
        try:
            readback = self._worker.set_gain("rx", self._panel.device_gain_db(gain_db))
        except Exception as error:
            self._panel.show_status(f"gain change failed: {error}", error=True)
            return
        self._panel.show_status(f"RX gain {readback:g} dB")

    def _change_tx_gain(self) -> None:
        self._set_tx_gain(float(self._tx_gain.value()), "operator")

    def _set_tx_gain(self, gain_db: float, why: str) -> None:
        self._tx_gain_db = gain_db
        if self._worker.attached is None:
            return
        try:
            readback = self._worker.set_gain("tx", gain_db)
        except Exception as error:
            self._status.setText(f"TX gain change failed: {error}")
            return
        self._status.setText(f"TX gain {readback:g} dB ({why})")

    # -- actions ----------------------------------------------------------------

    def _send_typed(self) -> None:
        text = self._input.text()
        if text and self._worker.submit("typed", text.encode("utf-8")):
            self._input.clear()

    def _send_batch(self) -> None:
        total = int(self._batch_count.value())
        accepted = sum(
            bool(self._worker.submit("batch", f"seq-test {index}/{total}".encode()))
            for index in range(1, total + 1)
        )
        self._status.setText(f"queued {accepted} of {total} batch messages")

    def _on_la_mode(self, fixed: bool) -> None:
        self._mcs_combo.setEnabled(fixed)
        try:
            if fixed:
                self._worker.select_mcs_entry(mcs_entry(int(self._mcs_combo.currentData())))
            else:
                self._worker.select_mcs_entry(None)
        except Exception as error:
            self._status.setText(f"LA mode change failed: {error}")

    def _on_mcs_changed(self, index: int) -> None:
        del index
        if self._fixed_button.isChecked():
            entry = mcs_entry(int(self._mcs_combo.currentData()))
            self._worker.select_mcs_entry(entry)
            self._status.setText(f"fixed at {describe_mcs_entry(entry)}")

    def _log_mark(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        qt_runtime.append_line_following_tail(self._log, f"{stamp}  MARK {text}")
        if self._stats_log is not None:
            record = {"monotonic_s": time.monotonic(), "node_id": self._setup.node_id, "mark": text}
            self._stats_log.write(json.dumps(record) + "\n")
            self._stats_log.flush()

    # -- events and refresh -------------------------------------------------------

    def _on_event(self, kind: str, value) -> None:
        self._monitor.on_event(kind, value)
        if kind == "samples":
            self._plot_buffers.set_samples(value)
        elif kind == "symbols":
            symbols, context = value
            self._plot_buffers.add_symbols(symbols, context=context)
        elif kind == "message":
            qt_runtime.append_line_following_tail(
                self._log,
                f"{time.strftime('%H:%M:%S')}  RX seq {value.sequence:5d}  MCS "
                f"{value.mcs_index}  {len(value.message.payload):6d} B  "
                f"{qt_runtime.preview_payload(value.message.payload)}",
            )
        elif kind == "la":
            qt_runtime.append_line_following_tail(
                self._decisions,
                f"{time.strftime('%H:%M:%S')}  MCS {value.get('mcs_index')}  "
                f"{value.get('reason', '')}  snr_eff {value.get('snr_eff_db')}  "
                f"offset {value.get('offset_db')}",
            )
        elif kind == "failed":
            self._status.setText(f"engine: {value}")

    def _refresh_plots(self) -> None:
        snapshot = self._plot_buffers.snapshot()
        if snapshot.samples is not None:
            self._spectrum.set_samples(snapshot.samples, sample_rate=self._sample_rate)
        for batch in snapshot.symbols:
            modulation = getattr(batch.context, "modulation", None)
            if modulation is not None:
                self._constellation.add_symbols(batch.values, modulation=modulation)
        self._spectrum.refresh()
        self._constellation.refresh()

    def _poll_control(self) -> None:
        if self._control is not None:
            while self._control.poll(0.0):
                pass

    def _refresh_stats(self) -> None:
        now = time.monotonic()
        if self._ready_at is not None:
            for _, gain in self._schedule.due(now - self._ready_at):
                self._tx_gain.setValue(gain)
                self._set_tx_gain(gain, "gain profile")
        line = self._monitor.line(now)
        if line is None:
            return
        if self._stats_log is not None:
            self._stats_log.write(json.dumps(line, default=str) + "\n")
            self._stats_log.flush()
        tx, rx, la = line["tx"], line["rx"], line["la"]
        self._queue_card.set_value(f"{tx['pending']}", "messages queued")
        self._airtime_card.set_value(f"{tx['airtime_frac'] * 100:.1f} %", "transmit duty")
        self._sent_card.set_value(f"{tx['goodput_bps'] / 1e6:.3f}", "Mbit/s")
        snr = rx["snr_2s"]
        loss = rx["loss_10s"]
        self._snr_card.set_value("—" if snr is None else f"{snr:.1f} dB", "preamble SNR")
        self._loss_card.set_value("—" if loss is None else f"{loss * 100:.2f} %", "frames")
        self._rx_goodput_card.set_value(f"{rx['goodput_now_bps'] / 1e6:.3f}", "Mbit/s")
        mcs = la.get("mcs_index")
        self._mcs_card.set_value("—" if mcs is None else f"MCS {mcs}", str(la.get("reason") or ""))
        snr_eff, offset = la.get("snr_eff_db"), la.get("offset_db")
        self._snr_eff_card.set_value(
            "—" if snr_eff is None else f"{snr_eff:.1f} dB",
            "" if offset is None else f"offset {offset:+.2f} dB",
        )
        peer_loss = la.get("peer_loss_10s")
        self._peer_loss_card.set_value(
            "—" if peer_loss is None else f"{peer_loss * 100:.2f} %", "from feedback"
        )
        age = la.get("feedback_age_s")
        colour = feedback_colour(age)
        self._feedback_card.set_value(
            "—" if age is None else f"{age * 1e3:.0f} ms",
            {"green": "fresh", "yellow": "late", "red": "stale"}[colour],
        )
        self._la_trend.set_points(la_points(self._monitor.la_snapshot()))
        rx_view, tx_view = self._monitor.last_views
        if rx_view is not None:
            self._rx_light.set_light(rx_view.light)
            self._rx_trend.set_points(rx_view.trend)
        if tx_view is not None:
            self._tx_light.set_light(tx_view.light)
        if self._tabs.currentWidget() is self._hardware_page:
            qt_runtime.set_text_preserving_scroll(
                self._hardware_text, follow_tail=False, text=self._hardware_rows(line)
            )

    def _hardware_rows(self, line) -> str:
        return json.dumps(
            {key: line.get(key) for key in ("tx", "rx", "la", "feedback", "cpu", "device")},
            indent=1,
            default=str,
        )

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 (Qt override)
        for timer in (self._stats_timer, self._plot_timer, self._control_timer):
            timer.stop()
        options.clear_ready(self._ready_file)
        try:
            self._worker.detach()
        except Exception:
            pass
        self._worker.stop()
        if self._control is not None:
            self._control.close()
        if self._stats_log is not None:
            self._stats_log.close()
        super().closeEvent(event)


def run_gui(args, setup: node_options.NodeSetup, spec: NodeSpec) -> int:
    application = qt_runtime.create_application()
    panel = (
        RadioPanel(
            direction="rx",
            center_frequency_hz=setup.rx_frequency,
            sample_rate=setup.resolved.config.phy.sample_rate,
            backend=setup.resolved.transport,
            preferred_serial=args.serial,
            preferred_antenna=args.antenna,
            preferred_channel=args.channel,
            preferred_gain_db=args.rx_gain,
            auto_start=args.auto_start,
        )
        if setup.resolved.uses_radio
        else None
    )
    window = NodeWindow(
        args,
        setup,
        spec,
        radio_panel=panel,
        stats_log=log_limits.open_stats_log(args),
        ready_file=args.ready_file,
        control_port=setup.control_port,
    )
    return qt_runtime.run_window(window, application, args.geometry)


__all__ = ["NodeWindow", "feedback_colour", "la_points", "profile_line", "run_gui"]

