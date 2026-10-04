"""Receive-only demo app: watch messages arrive, or not arrive.

Run this in one terminal and ``tx_app`` in another.  Every delivered message
is also forwarded to a localhost UDP port, so any other program can consume
them without going through the window::

    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 52002))
    while True:
        data, _ = sock.recvfrom(65535)
        print(data.decode(errors="replace"))

One-way loss is reported honestly.  A gap in the PHY sequence number means a
burst did not arrive intact, and on a link with no return path there is no way
to tell a preamble that was never detected from a payload that failed its CRC.
Both are counted as missing and neither can be recovered.
"""

from __future__ import annotations

import argparse
import functools
import json
import socket
import threading
import time
from collections import Counter, deque
from dataclasses import asdict

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from ofdm_link.phy.mcs_table import describe_mcs_entry, mcs_entry_for_wire

from . import dashboard_widgets as widgets
from . import log_limits, options, qt_runtime
from .dashboard import RxDashboard, RxView, rx_light
from .link import MessageReceiver, RateMeter, ReceivedMessage
from .plots import ConstellationPlot, PlotBuffers, SpectrumPlot, TrendPlot
from .radio_panel import RadioPanel
from .transport import DEFAULT_RX_RECV_FRAMES, set_transport_gain

_LATENCY_HISTORY = 64


class _Bridge(QtCore.QObject):
    """Qt signal carrier for the receive worker thread."""

    message = QtCore.pyqtSignal(object)
    level = QtCore.pyqtSignal(float)
    failed = QtCore.pyqtSignal(str)


class ReceiveWorker:
    """Pull sample chunks from the transport and decode them continuously."""

    def __init__(
        self,
        receiver: MessageReceiver,
        bridge: _Bridge,
        plot_buffers: PlotBuffers | None = None,
        *,
        egress: EgressForwarder | None = None,
    ) -> None:
        self._receiver = receiver
        # Forwarded from this thread, not the window's, so a busy event loop
        # never delays an application datagram.
        self._egress = egress
        self._source = None
        self._lock = threading.Lock()
        self._bridge = bridge
        self._plot_buffers = plot_buffers if plot_buffers is not None else PlotBuffers()
        self._stop = threading.Event()
        self._attached = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_level = 0.0

    @property
    def stats(self):
        return self._receiver.stats

    @property
    def source(self):
        with self._lock:
            return self._source

    @property
    def egress_datagrams(self) -> int:
        return 0 if self._egress is None else self._egress.datagrams

    def decoder_snapshot(self):
        return self._receiver.decoder_snapshot()

    def set_gain(self, gain_db: float) -> float:
        """Change the running radio's receive gain; returns the readback."""

        return set_transport_gain(self.source, gain_db)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="rx-worker", daemon=True)
        self._thread.start()

    def attach(self, source) -> None:
        """Start one transport and decode from it until it is detached."""

        source.start()
        with self._lock:
            self._source = source
        # The radio is already streaming; a worker still asleep in its idle
        # poll would leave the first ~0.1 s of samples to fill every buffer.
        self._attached.set()

    def attach_from(self, factory) -> str:
        source = factory()
        self.attach(source)
        return source.description

    def detach(self) -> None:
        with self._lock:
            source, self._source = self._source, None
        if source is not None:
            source.stop()

    def _run(self) -> None:
        while not self._stop.is_set():
            source = self.source
            if source is None:
                self._attached.wait(0.1)
                self._attached.clear()
                continue
            chunk = source.recv(0.2)
            if chunk is None:
                continue
            now = time.monotonic()
            if now - self._last_level > 0.25:
                self._last_level = now
                power = float(np.mean(np.abs(chunk) ** 2))
                self._bridge.level.emit(
                    10.0 * np.log10(power) if power > 0.0 else -200.0
                )
            try:
                delivered = self._receiver.feed(chunk)
            except Exception as error:  # surfaced in the window, never silent
                self._bridge.failed.emit(f"{type(error).__name__}: {error}")
                continue
            self._plot_buffers.set_samples(chunk)
            observations = self._receiver.drain_observations()
            for observation in observations:
                self._plot_buffers.add_symbols(
                    observation.payload_symbols,
                    context=observation,
                )
            for message in delivered:
                if self._egress is not None:
                    self._egress.forward(message.message.payload)
                self._bridge.message.emit(message)

    def stop(self) -> None:
        self._stop.set()
        self._attached.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self.detach()
        if self._egress is not None:
            self._egress.close()
        close = getattr(self._receiver, "close", None)
        if callable(close):
            close()


class EgressForwarder:
    """Forward every delivered message to a localhost UDP consumer."""

    def __init__(self, host: str, port: int) -> None:
        self._address = (host, int(port))
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.datagrams = 0

    @property
    def address(self) -> str:
        return f"{self._address[0]}:{self._address[1]}"

    def forward(self, payload: bytes) -> None:
        try:
            self._socket.sendto(payload, self._address)
            self.datagrams += 1
        except OSError:
            # Nothing is listening yet; that is the normal case and is not an
            # error in the radio path.
            pass

    def close(self) -> None:
        self._socket.close()


class ReceiveWindow(QtWidgets.QWidget):
    """The receive demo window."""

    def __init__(
        self,
        resolved: options.ResolvedOptions,
        egress_address: tuple[str, int],
        *,
        build_source,
        radio_panel: RadioPanel | None = None,
        ui_fps: int = options.DEFAULT_UI_FPS,
        log_lines: int = options.DEFAULT_LOG_LINES,
        stats_log=None,
        process_engine: bool = False,
        decode_workers: int = 1,
        ready_file: str | None = None,
    ) -> None:
        super().__init__()
        self._log_lines = log_lines
        self._stats_log = stats_log
        self._resolved = resolved
        self._build_source = build_source
        self._panel = radio_panel
        self._ready_file = ready_file
        self._bridge = _Bridge()
        self._plot_buffers = PlotBuffers()
        self._egress_address = f"{egress_address[0]}:{egress_address[1]}"
        if process_engine:
            from .engine import ProcessReceiveWorker

            # build_source must be picklable here: it runs in the child.
            self._worker = ProcessReceiveWorker(
                resolved.profile,
                self._bridge,
                self._plot_buffers,
                egress_address=egress_address,
                ui_fps=ui_fps,
                decode_workers=decode_workers,
            )
        else:
            self._worker = ReceiveWorker(
                MessageReceiver(resolved.profile, decode_workers=decode_workers),
                self._bridge,
                self._plot_buffers,
                egress=EgressForwarder(*egress_address),
            )
        self._sample_rate = float(resolved.profile.sample_rate)
        self._started = time.monotonic()
        # elapsed_s in --stats-log counts from here and ignores Reset stats.
        self._opened = self._started
        self._goodput_now = RateMeter()
        self._latencies: deque[float] = deque(maxlen=_LATENCY_HISTORY)
        self._evms: deque[float] = deque(maxlen=_LATENCY_HISTORY)
        self._snrs: deque[float] = deque(maxlen=_LATENCY_HISTORY)
        self._same_clock_domain: bool | None = None
        self._bursts_seen = 0
        self._mcs_counts: Counter[int] = Counter()
        self._last_mcs = None
        self._last_burst_at: float | None = None
        self._level_dbfs: float | None = None
        self._level_floor_dbfs: float | None = None
        self._level_peak_dbfs: float | None = None
        self._dashboard = RxDashboard()
        self._last_view: RxView | None = None

        self.setWindowTitle("OFDM message link - receiver")
        self.resize(1120, 840)
        self._build_ui()

        self._bridge.message.connect(self._on_message)
        self._bridge.level.connect(self._on_level)
        self._bridge.failed.connect(self._on_failed)

        self._worker.start()
        self._status.setText(f"forwarding delivered messages to {self._egress_address}")

        if self._panel is None:
            description = self._worker.attach_from(functools.partial(self._build_source, None))
            options.mark_ready(self._ready_file, description)
        else:
            self._panel.startRequested.connect(self._start_radio)
            self._panel.stopRequested.connect(self._stop_radio)
            self._panel.gainChangeRequested.connect(self._change_gain)

        self._plot_timer = QtCore.QTimer(self)
        self._plot_timer.timeout.connect(self._refresh_plots)
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
                functools.partial(self._build_source, selection)
            )
        except Exception as error:
            self._panel.set_running(False, f"{type(error).__name__}: {error}")
            self._status.setText(f"radio did not start: {error}")
            self._refresh_tab_collapse()
            return
        self._sample_rate = float(selection.sample_rate)
        self._panel.set_running(True, f"receiving on {description}")
        self._status.setText(f"RADIO ON - {selection.describe()}")
        options.mark_ready(self._ready_file, selection.describe())
        self._refresh_tab_collapse()

    def _change_gain(self, gain_db: float) -> None:
        """Apply a gain the operator changed while the radio runs."""

        try:
            self._worker.set_gain(gain_db)
        except Exception as error:
            self._panel.show_status(f"gain change failed: {error}", error=True)
            return
        self._panel.show_status(f"RX gain set to {gain_db:g} dB while receiving")

    def _stop_radio(self) -> None:
        options.clear_ready(self._ready_file)
        self._worker.detach()
        if self._panel is not None:
            self._panel.set_running(False, "radio stopped")
        self._status.setText("radio stopped")
        self._refresh_tab_collapse()

    def _build_ui(self) -> None:
        """Top: evidence line, cards, light and 60 s trend.  Below: tabs."""

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(4)
        layout.addWidget(widgets.one_line_banner(options.evidence_banner(self._resolved)))

        # In a widget, not a bare layout, so a short window can fold it.
        self._cards = QtWidgets.QWidget()
        cards = QtWidgets.QHBoxLayout(self._cards)
        cards.setContentsMargins(0, 0, 0, 0)
        self._goodput_card = widgets.Card("Goodput (2 s)")
        self._loss_card = widgets.Card("Loss (10 s)")
        self._snr_card = widgets.Card("SNR (2 s)")
        self._mcs_card = widgets.Card("MCS")
        for card in (self._goodput_card, self._loss_card, self._snr_card, self._mcs_card):
            cards.addWidget(card)
        self._light_card = widgets.LightCard("Link")
        self._light_card.set_light(
            rx_light(now=0.0, last_burst_at=None, loss_10s=None, snr_2s=None, mcs_index=None)
        )
        cards.addWidget(self._light_card, stretch=1)
        self._reset_button = widgets.reset_button()
        self._reset_button.clicked.connect(self._reset_stats)
        cards.addWidget(self._reset_button)
        layout.addWidget(self._cards)

        self._trend = TrendPlot(
            "Last 60 s (red: bursts missing)",
            primary_label="Goodput",
            primary_scale=1e6,
            primary_unit="Mbit/s",
            secondary_label="SNR",
            secondary_unit="dB",
        )
        layout.addWidget(self._trend, stretch=1)

        self._tabs = widgets.tab_area()
        layout.addWidget(self._tabs, stretch=3)

        signal = QtWidgets.QWidget()
        signal_layout = QtWidgets.QVBoxLayout(signal)
        plots = QtWidgets.QHBoxLayout()
        self._constellation = ConstellationPlot("Post-equalization payload symbols (live)")
        plots.addWidget(self._constellation, stretch=1)
        self._spectrum = SpectrumPlot("Received spectrum")
        plots.addWidget(self._spectrum, stretch=1)
        signal_layout.addLayout(plots, stretch=1)
        self._level_text = QtWidgets.QLabel()
        self._level_text.setFont(QtGui.QFont("Monospace", 9))
        signal_layout.addWidget(self._level_text)
        self._tabs.addTab(signal, "Signal")

        self._decode_text = widgets.monospace_view()
        self._tabs.addTab(self._decode_text, "Decode")

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
        if self._panel is not None and not self._panel_auto_starts():
            # Nothing runs until a device is chosen, and that is on this tab.
            self._tabs.setCurrentWidget(hardware_page)

        self._status = QtWidgets.QLabel()
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

    def _panel_auto_starts(self) -> bool:
        return bool(getattr(self._panel, "_auto_start", False))

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

    def _record_burst(self, observation) -> None:
        """Record the quality and actual header-selected MCS of one burst."""

        self._bursts_seen += 1
        self._last_burst_at = time.monotonic()
        self._dashboard.observe_burst(self._last_burst_at, observation.effective_snr_db)
        entry = mcs_entry_for_wire(observation.modulation, observation.wire_version)
        self._mcs_counts[entry.index] += 1
        self._last_mcs = entry
        if observation.evm is not None:
            self._evms.append(observation.evm)
        if observation.effective_snr_db is not None:
            self._snrs.append(observation.effective_snr_db)

    def _on_message(self, received: ReceivedMessage) -> None:
        if received.latency_s is not None:
            self._latencies.append(received.latency_s)
        self._same_clock_domain = received.same_clock_domain

        stamp = time.strftime("%H:%M:%S")
        latency = (
            f"{received.latency_s * 1000:7.1f} ms"
            if received.latency_s is not None
            else "      n/a"
        )
        entry = mcs_entry_for_wire(received.modulation, received.wire_version)
        qt_runtime.append_line_following_tail(
            self._log,

            f"{stamp}  seq {received.sequence:5d}  "
            f"MCS {entry.index} wire v{received.wire_version}  "
            f"{len(received.message.payload):6d} B  "
            f"{received.message.fragment_count} frag  "
            f"lat {latency}  {qt_runtime.preview_payload(received.message.payload)}"
        )

    def _on_level(self, dbfs: float) -> None:
        """Track the received level, and the quietest and loudest seen.

        The spread between them is what tells an operator whether this antenna
        port hears the transmitter at all: a port with nothing connected sits
        at its noise floor and never moves.  Finding that out by reading a
        number beats finding it out by wondering why nothing decodes.
        """

        self._level_dbfs = dbfs
        self._level_floor_dbfs = (
            dbfs if self._level_floor_dbfs is None else min(self._level_floor_dbfs, dbfs)
        )
        self._level_peak_dbfs = (
            dbfs if self._level_peak_dbfs is None else max(self._level_peak_dbfs, dbfs)
        )

    def _refresh_plots(self) -> None:
        snapshot = self._plot_buffers.snapshot()
        if snapshot.samples is not None:
            self._spectrum.set_samples(snapshot.samples, sample_rate=self._sample_rate)
        for batch in snapshot.symbols:
            self._constellation.add_symbols(
                batch.values,
                modulation=batch.context.modulation,
            )
            self._record_burst(batch.context)
        self._spectrum.refresh()
        self._constellation.refresh()

    def _on_failed(self, message: str) -> None:
        self._status.setText(f"receive error: {message}")

    def _counters(self, stats) -> dict[str, int]:
        """Every cumulative counter the dashboard windows over."""

        counters = {key: int(value) for key, value in asdict(stats).items()}
        decoder = self._worker.decoder_snapshot()
        if decoder is not None:
            counters.update({key: int(value) for key, value in asdict(decoder).items()})
        return counters

    def _refresh_stats(self) -> None:
        stats = self._worker.stats
        source = self._worker.source
        now = time.monotonic()
        self._goodput_now.update(now, stats.message_bytes_delivered)
        view = self._dashboard.refresh(
            now,
            self._counters(stats),
            mcs_index=None if self._last_mcs is None else self._last_mcs.index,
        )
        self._last_view = view
        self._show_cards(view)
        self._trend.set_points(view.trend)
        snapshot = None if source is None else source.snapshot()
        if self._tabs.isVisible():
            self._level_text.setText(self._level_rows())
            qt_runtime.set_text_preserving_scroll(
                self._decode_text,
                follow_tail=False,
                text=self._decode_rows(stats, view, now),
            )
            qt_runtime.set_text_preserving_scroll(
                self._hardware_text,
                follow_tail=False,
                text=self._hardware_rows(source, snapshot),
            )
        if self._stats_log is not None:
            self._write_stats_log(stats, snapshot, view)

    def _show_cards(self, view: RxView) -> None:
        self._goodput_card.set_value(
            f"{view.goodput_bps / 1e6:.3f}", "Mbit/s delivered to UDP"
        )
        if view.loss_10s is None:
            self._loss_card.set_value("\u2014", "no bursts expected")
        else:
            missing = view.recent.get("missing_bursts", 0)
            self._loss_card.set_value(f"{view.loss_10s * 100:.2f} %", f"{missing} bursts missing")
        if view.snr_2s is None:
            self._snr_card.set_value("\u2014", "no burst in 2 s")
        else:
            self._snr_card.set_value(f"{view.snr_2s:.1f} dB", "effective SNR")
        if self._last_mcs is None:
            self._mcs_card.set_value("\u2014", "from burst header")
        else:
            self._mcs_card.set_value(
                f"MCS {self._last_mcs.index}",
                describe_mcs_entry(self._last_mcs).split(" \u2014 ", 1)[-1],
            )
        self._light_card.set_light(view.light)

    def _reset_stats(self) -> None:
        """Zero what this window shows; the radio and the log are untouched."""

        stats = self._worker.stats
        now = time.monotonic()
        self._dashboard.reset(now, self._counters(stats))
        self._goodput_now = RateMeter()
        self._started = now
        self._latencies.clear()
        self._evms.clear()
        self._snrs.clear()
        self._mcs_counts.clear()
        self._level_floor_dbfs = self._level_dbfs
        self._level_peak_dbfs = self._level_dbfs
        self._trend.set_points(())
        self._status.setText("stats reset; the radio kept running")
        if self._stats_log is not None:
            self._stats_log.write(
                json.dumps(
                    {
                        "event": "reset",
                        "monotonic_s": now,
                        "receiver": asdict(stats),
                    }
                )
                + "\n"
            )
            self._stats_log.flush()

    def _decode_rows(self, stats, view: RxView, now: float) -> str:
        recent, totals = view.recent, view.totals
        elapsed = max(now - self._started, 1e-9)

        def row(label: str, key: str, note: str = "") -> str:
            return f"{label:<22}{recent.get(key, 0):>12d}{totals.get(key, 0):>14d}   {note}"

        rows = [
            f"{'':<22}{'last 10 s':>12}{'since reset':>14}",
            "decode stages:",
            row("  detected", "detected_burst_candidates"),
            row("  header OK", "header_decode_success"),
            row("  header failed", "header_decode_failure"),
            row("  payload attempts", "payload_decode_attempts"),
            row("  CRC failures", "crc_failures"),
            row("  valid", "valid_decoded_bursts"),
            "",
            row("bursts decoded", "bursts_decoded"),
            row("bursts missing", "missing_bursts",
                "undetected preamble or failed CRC; indistinguishable one-way"),
            row("sequence gaps", "sequence_gaps"),
            row("foreign bursts", "foreign_bursts", "valid CRC, not this demo's format"),
            row("incomplete messages", "incomplete_messages_dropped", "dropped, never repaired"),
            row("messages delivered", "messages_delivered"),
            row("application bytes", "message_bytes_delivered"),
            "",
            f"burst loss ratio     {stats.burst_loss_ratio * 100:10.2f} %"
            "   (since start, cumulative)",
            f"delivered goodput    {totals.get('message_bytes_delivered', 0) * 8 / elapsed:10.0f}"
            " bit/s   (average since reset)",
            f"mean EVM             {_mean(self._evms, '{:10.4f}')}",
            f"mean effective SNR   {_mean(self._snrs, '{:10.1f}')} dB   (last 64 bursts)",
            f"mean one-way latency {self._latency_text()}",
            f"last burst           {self._last_burst_text()}",
            f"last burst MCS       {self._last_mcs_text()}",
            *self._mcs_count_rows(),
        ]
        return "\n".join(rows)

    def _level_rows(self) -> str:
        return (
            f"rx level now {_dbfs(self._level_dbfs).strip()}   "
            f"quietest {_dbfs(self._level_floor_dbfs).strip()}   "
            f"loudest {_dbfs(self._level_peak_dbfs).strip()}   "
            f"spread {self._level_spread_text().strip()}"
        )

    def _hardware_rows(self, source, snapshot) -> str:
        if source is None or snapshot is None:
            return "radio        off (no radio started)"
        faults = snapshot.get("uhd_faults")
        rows = ["radio        ON", ""]
        if isinstance(faults, dict):
            rows.append("UHD faults:")
            rows += [
                f"  {key:<28} {value}" for key, value in faults.items() if key != "events"
            ]
        else:
            rows.append("UHD faults:  n/a for this transport")
        highlighted = (
            "rx_samples_lost",
            "rx_time_discontinuities",
            "queue_full_backpressure_events",
            "chunks_dropped_overflow",
            "engine_max_lag_ms",
            "engine_lag_spikes_over_20ms",
            "engine_dropped_display_events",
            "decode_pool",
        )
        rows += ["", "host path:"]
        rows += [f"  {key:<28} {snapshot[key]}" for key in highlighted if key in snapshot]
        rows.append(f"  {'udp egress datagrams':<28} {self._worker.egress_datagrams}")
        rows += ["", "transport:"]
        rows += [
            f"  {key:<28} {value}"
            for key, value in snapshot.items()
            if key not in highlighted and key != "uhd_faults"
        ]
        return "\n".join(rows)

    def _write_stats_log(self, stats, snapshot, view: RxView) -> None:
        """Append one JSON line so a scripted run can be measured headlessly."""

        transport = None
        if snapshot is not None:
            transport = {key: value for key, value in snapshot.items() if key != "readback"}
            faults = transport.get("uhd_faults")
            if isinstance(faults, dict):
                transport["uhd_faults"] = {k: v for k, v in faults.items() if k != "events"}
        decoder = self._worker.decoder_snapshot()
        record = {
            "monotonic_s": time.monotonic(),
            "elapsed_s": time.monotonic() - self._opened,
            "receiver": asdict(stats),
            "goodput_now_bps": self._goodput_now.bits_per_second,
            "decoder": None if decoder is None else asdict(decoder),
            "mean_effective_snr_db": (
                sum(self._snrs) / len(self._snrs) if self._snrs else None
            ),
            "egress_datagrams": self._worker.egress_datagrams,
            "transport": transport,
            # Dashboard view, added for the GUI; the fields above keep their
            # cumulative meaning even after Reset stats.
            "link_state": view.light.state,
            "link_reason": view.light.reason,
            "loss_10s": view.loss_10s,
            "snr_2s": view.snr_2s,
            "mcs_index": None if self._last_mcs is None else self._last_mcs.index,
        }
        self._stats_log.write(json.dumps(record, default=str) + "\n")
        self._stats_log.flush()

    def _last_burst_text(self) -> str:
        if self._last_burst_at is None:
            return "       none decoded yet"
        return f"{time.monotonic() - self._last_burst_at:10.1f} s ago"

    def _last_mcs_text(self) -> str:
        return "       none decoded yet" if self._last_mcs is None else describe_mcs_entry(
            self._last_mcs
        )

    def _mcs_count_rows(self) -> list[str]:
        if not self._mcs_counts:
            return ["decoded by MCS       none yet"]
        rows = ["decoded by MCS:"]
        for index, count in sorted(self._mcs_counts.items()):
            rows.append(f"  MCS {index:<2d} bursts {count:10d}")
        return rows

    def _level_spread_text(self) -> str:
        """How far the level moves, which is how you find the live antenna port."""

        if self._level_floor_dbfs is None or self._level_peak_dbfs is None:
            return "       n/a"
        spread = self._level_peak_dbfs - self._level_floor_dbfs
        verdict = "signal present" if spread >= 6.0 else "nothing heard yet on this port"
        return f"{spread:10.1f} dB  ({verdict})"

    def _latency_text(self) -> str:
        if self._same_clock_domain is False:
            return "       n/a   (transmitter is on another host; no shared clock)"
        if not self._latencies:
            return "       n/a"
        mean = sum(self._latencies) / len(self._latencies)
        return f"{mean * 1000:10.1f} ms  (same host, CLOCK_MONOTONIC)"

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 (Qt override)
        self._plot_timer.stop()
        self._stats_timer.stop()
        self._worker.stop()
        options.clear_ready(self._ready_file)
        if self._stats_log is not None:
            self._stats_log.close()
        super().closeEvent(event)


def _dbfs(value: float | None) -> str:
    return "       n/a" if value is None else f"{value:10.1f} dBFS"


def _mean(values, template: str) -> str:
    if not values:
        return "       n/a"
    return template.format(sum(values) / len(values))


def _profile_label(resolved: options.ResolvedOptions) -> QtWidgets.QLabel:
    described = resolved.profile.describe()
    text = (
        f"display hint only: {describe_mcs_entry(resolved.mcs_entry)} | "
        f"frame payload {described['frame_payload_bytes']} B | "
        "RX reads actual MCS/FEC/wire v1-v4 from each protected burst header; "
        f"only sample rate {resolved.profile.sample_rate / 1e6:g} MS/s must match"
    )
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color:#9fb3c8; padding:2px;")
    return label


def _decode_workers(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 64:
        raise argparse.ArgumentTypeError("must be between 1 and 64")
    return parsed


def _stats_log_max_mb(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1_000_000.0:
        raise argparse.ArgumentTypeError("must be 0 (no cap) or a positive size in MiB")
    return parsed


def _stats_log_backups(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return parsed


def open_stats_log(args: argparse.Namespace) -> log_limits.RotatingLog | None:
    """The ``--stats-log`` file, capped at ``--stats-log-max-mb`` by rotation.

    Every line carries cumulative counters, so the newest line still gives the
    totals since the window opened after older lines have been rotated away.
    """

    if args.stats_log is None:
        return None
    return log_limits.RotatingLog(
        args.stats_log,
        max_bytes=log_limits.max_bytes_from_mb(args.stats_log_max_mb),
        backups=args.stats_log_backups,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ofdm-message-rx",
        description="One-way OFDM message receiver with a Qt demo window.",
    )
    options.add_common_arguments(parser, receiver_display_only=True)
    app = parser.add_argument_group("receiver")
    app.add_argument(
        "--egress-host",
        default="127.0.0.1",
        help="forward delivered messages to this address (default: 127.0.0.1)",
    )
    app.add_argument(
        "--egress-port",
        type=int,
        default=options.DEFAULT_EGRESS_PORT,
        help=f"forward delivered messages to this port (default: {options.DEFAULT_EGRESS_PORT})",
    )
    app.add_argument(
        "--rx-recv-frames",
        type=options.rx_recv_frames,
        default=None,
        help=(
            "uhd transport only: UHD num_recv_frames, the USB receive buffer that "
            "absorbs host stalls; 0 keeps the UHD default of 16 (default: "
            f"{DEFAULT_RX_RECV_FRAMES} per 5 MS/s of sample rate, about 0.1 s)"
        ),
    )
    app.add_argument(
        "--engine",
        choices=("process", "thread"),
        default="process",
        help=(
            "process runs the radio, decoder and UDP egress in a child process so "
            "the GUI cannot stall them; thread keeps them in this process "
            "(default: process)"
        ),
    )
    app.add_argument(
        "--decode-workers",
        type=_decode_workers,
        default=4,
        help=(
            "payload decode worker processes (default: 4); acquisition and "
            "headers stay on one owner. 1 keeps the original inline decoder"
        ),
    )
    app.add_argument(
        "--stats-log",
        default=None,
        metavar="PATH",
        help="append one JSON line of receiver and transport counters per stats refresh",
    )
    app.add_argument(
        "--stats-log-max-mb",
        type=_stats_log_max_mb,
        default=log_limits.DEFAULT_MAX_MB,
        metavar="MB",
        help=(
            "rotate the stats log to PATH.1 when it would pass this many MiB, so a "
            "long run cannot fill the disk; 0 lets it grow without limit "
            f"(default: {log_limits.DEFAULT_MAX_MB:g})"
        ),
    )
    app.add_argument(
        "--stats-log-backups",
        type=_stats_log_backups,
        default=log_limits.DEFAULT_BACKUPS,
        metavar="N",
        help=f"rotated stats logs to keep (default: {log_limits.DEFAULT_BACKUPS})",
    )
    app.add_argument(
        "--snr-db",
        type=float,
        default=None,
        help=(
            "udp transport only: add AWGN at this SNR so the demo shows a "
            "realistic constellation without a radio"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Plots use NumPy too; keep its BLAS pool from spinning idle cores.
    from ofdm_link.runtime.cpu_budget import apply_cpu_budget

    apply_cpu_budget(1)
    resolved = options.resolve(args)
    if args.snr_db is not None and resolved.uses_radio:
        raise SystemExit("--snr-db applies to the udp transport only; the radio has a real channel")

    # The QApplication must stay referenced: collecting it before the first
    # QWidget is constructed aborts the process.
    application = qt_runtime.create_application()
    panel = (
        RadioPanel(
            direction="rx",
            center_frequency_hz=resolved.config.phy.center_frequency,
            sample_rate=resolved.config.phy.sample_rate,
            backend=resolved.transport,
            preferred_serial=args.serial,
            preferred_antenna=args.antenna or resolved.config.radio.rx_antenna,
            preferred_channel=args.channel,
            preferred_gain_db=args.gain,
            auto_start=args.auto_start,
        )
        if resolved.uses_radio
        else None
    )
    window = ReceiveWindow(
        resolved,
        (args.egress_host, args.egress_port),
        build_source=functools.partial(options.build_source, args, resolved),
        process_engine=args.engine == "process",
        decode_workers=args.decode_workers,
        radio_panel=panel,
        ui_fps=args.ui_fps,
        log_lines=args.log_lines,
        stats_log=open_stats_log(args),
        ready_file=args.ready_file,
    )
    return qt_runtime.run_window(window, application, args.geometry)


if __name__ == "__main__":
    raise SystemExit(main())
