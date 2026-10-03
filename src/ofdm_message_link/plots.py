"""Small self-contained PyQt5 plot widgets for the demo windows.

These paint with ``QPainter`` and NumPy only, so the windows need no plotting
library beyond the PyQt5 that GNU Radio already installs.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray
from PyQt5 import QtCore, QtGui, QtWidgets

from ofdm_link.phy import MCS
from ofdm_link.phy.codec import map_symbols

_BACKGROUND = QtGui.QColor(18, 20, 24)
_GRID = QtGui.QColor(48, 54, 62)
_TRACE = QtGui.QColor(94, 214, 168)
_POINT = QtGui.QColor(120, 190, 255)
_POINT_FADED = QtGui.QColor(120, 190, 255, 70)
_POINT_STALE = QtGui.QColor(145, 150, 158, 110)
_REFERENCE = QtGui.QColor(238, 188, 90, 180)
_LABEL = QtGui.QColor(150, 160, 172)
_SECONDARY = QtGui.QColor(238, 188, 90)
_ALERT = QtGui.QColor(200, 50, 50, 90)


@dataclass(frozen=True, slots=True)
class BufferedSymbols:
    """One bounded symbol block and optional caller-owned context."""

    values: NDArray[np.complex64]
    context: Any = None


@dataclass(frozen=True, slots=True)
class PlotSnapshot:
    """The plot data accumulated since the previous UI refresh."""

    samples: NDArray[np.complex64] | None
    symbols: tuple[BufferedSymbols, ...]


class PlotBuffers:
    """Thread-safe, bounded handoff from sample workers to the Qt thread."""

    def __init__(
        self,
        *,
        max_sample_count: int = 65_536,
        max_symbol_batches: int = 4,
        max_symbols_per_batch: int = 1_500,
    ) -> None:
        for name, value in (
            ("max_sample_count", max_sample_count),
            ("max_symbol_batches", max_symbol_batches),
            ("max_symbols_per_batch", max_symbols_per_batch),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._max_sample_count = max_sample_count
        self._max_symbols_per_batch = max_symbols_per_batch
        self._lock = threading.Lock()
        self._latest_samples: NDArray[np.complex64] | None = None
        self._symbols: deque[BufferedSymbols] = deque(maxlen=max_symbol_batches)

    def set_samples(self, samples: NDArray[np.complex64]) -> None:
        """Replace the pending spectrum block without invoking any Qt API."""

        values = np.asarray(samples, dtype=np.complex64).ravel()
        if values.size < 16:
            return
        bounded = values[-self._max_sample_count :].copy()
        with self._lock:
            self._latest_samples = bounded

    def add_symbols(
        self,
        symbols: NDArray[np.complex64],
        *,
        context: Any = None,
    ) -> None:
        """Append one bounded block, dropping the oldest block on overflow."""

        values = np.asarray(symbols, dtype=np.complex64).ravel()
        if values.size == 0:
            return
        bounded = values[: self._max_symbols_per_batch].copy()
        with self._lock:
            self._symbols.append(BufferedSymbols(bounded, context))

    def snapshot(self) -> PlotSnapshot:
        """Atomically take pending data for one timer-driven UI refresh."""

        with self._lock:
            snapshot = PlotSnapshot(self._latest_samples, tuple(self._symbols))
            self._latest_samples = None
            self._symbols.clear()
        return snapshot


def _strongest_window(
    values: NDArray[np.complex64],
    window_length: int,
) -> NDArray[np.complex64]:
    """Return the highest-energy window in the block.

    A chunk straddling a burst and the idle gap either side of it would
    otherwise be plotted from whichever end the slice happened to land on,
    showing the noise floor while a signal was plainly present.
    """

    if values.size <= window_length:
        return values
    energy = np.abs(values) ** 2
    cumulative = np.concatenate(([0.0], np.cumsum(energy)))
    totals = cumulative[window_length:] - cumulative[:-window_length]
    return values[int(np.argmax(totals)) :][:window_length]


class _PlotBase(QtWidgets.QWidget):
    def __init__(self, title: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self.setMinimumHeight(150)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Expanding,
        )

    def _paint_frame(self, painter: QtGui.QPainter) -> QtCore.QRect:
        painter.fillRect(self.rect(), _BACKGROUND)
        area = self.rect().adjusted(8, 22, -8, -8)
        painter.setPen(_GRID)
        painter.drawRect(area)
        for step in range(1, 4):
            y = area.top() + area.height() * step // 4
            painter.drawLine(area.left(), y, area.right(), y)
            x = area.left() + area.width() * step // 4
            painter.drawLine(x, area.top(), x, area.bottom())
        painter.setPen(_LABEL)
        painter.drawText(10, 16, self._title)
        return area

    def _paint_empty(
        self,
        painter: QtGui.QPainter,
        area: QtCore.QRect,
        text: str = "waiting for samples",
    ) -> None:
        painter.setPen(_LABEL)
        painter.drawText(area, QtCore.Qt.AlignCenter, text)


class SpectrumPlot(_PlotBase):
    """Magnitude spectrum of the most recent sample block, in dB."""

    def __init__(
        self,
        title: str = "Spectrum",
        *,
        fft_size: int = 512,
        floor_db: float = -90.0,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        self._fft_size = fft_size
        self._floor_db = floor_db
        self._spectrum: NDArray[np.float64] | None = None
        self._sample_rate: float | None = None

    def set_samples(
        self,
        samples: NDArray[np.complex64],
        *,
        sample_rate: float | None = None,
    ) -> None:
        values = np.asarray(samples, dtype=np.complex64).ravel()
        if values.size < 16:
            return
        window_length = min(self._fft_size, values.size)
        block = _strongest_window(values, window_length)
        window = np.hanning(window_length)
        spectrum = np.fft.fftshift(np.fft.fft(block * window, self._fft_size))
        magnitude = 20.0 * np.log10(np.abs(spectrum) / self._fft_size + 1e-12)
        self._spectrum = magnitude
        self._sample_rate = sample_rate

    def refresh(self) -> None:
        """Schedule one repaint from the UI timer."""

        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802 (Qt override)
        del event
        painter = QtGui.QPainter(self)
        area = self._paint_frame(painter)
        spectrum = self._spectrum
        if spectrum is None or spectrum.size == 0:
            self._paint_empty(painter, area, "waiting for channel samples")
            return

        top = float(np.max(spectrum))
        ceiling = top + 6.0
        floor = max(self._floor_db, ceiling - 90.0)
        span = max(ceiling - floor, 1e-6)
        clipped = np.clip(spectrum, floor, ceiling)
        xs = np.linspace(area.left(), area.right(), clipped.size)
        ys = area.bottom() - (clipped - floor) / span * area.height()

        path = QtGui.QPainterPath()
        path.moveTo(float(xs[0]), float(ys[0]))
        for x, y in zip(xs[1:], ys[1:], strict=True):
            path.lineTo(float(x), float(y))
        painter.setPen(QtGui.QPen(_TRACE, 1.4))
        painter.drawPath(path)

        painter.setPen(_LABEL)
        painter.drawText(area.left() + 4, area.top() + 14, f"peak {top:6.1f} dB")
        if self._sample_rate:
            span = f"{self._sample_rate / 1e6:.2f} MS/s span"
            width = painter.fontMetrics().width(span)
            painter.drawText(area.right() - width - 6, area.bottom() - 6, span)


class ConstellationPlot(_PlotBase):
    """Post-equalization payload symbols, with a short fading history."""

    def __init__(
        self,
        title: str = "Constellation",
        *,
        history: int = 4,
        max_points: int = 1500,
        stale_after_s: float = 3.0,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        if not np.isfinite(stale_after_s) or stale_after_s <= 0.0:
            raise ValueError("stale_after_s must be finite and positive")
        self._history: list[NDArray[np.complex128]] = []
        self._history_depth = history
        self._max_points = max_points
        self._stale_after_s = float(stale_after_s)
        self._last_symbols_at: float | None = None
        self._modulation: MCS | None = None
        self._reference: NDArray[np.complex64] | None = None
        self._display_time = time.monotonic()

    @property
    def reference_point_count(self) -> int:
        return 0 if self._reference is None else int(self._reference.size)

    def add_symbols(
        self,
        symbols: NDArray[np.complex64],
        *,
        modulation: MCS,
        received_at: float | None = None,
    ) -> None:
        if not isinstance(modulation, MCS):
            raise TypeError("modulation must be an MCS")
        values = np.asarray(symbols, dtype=np.complex128).ravel()
        if values.size == 0:
            return
        power = float(np.mean(np.abs(values) ** 2))
        if np.isfinite(power) and power > 0.0:
            values = values / np.sqrt(power)
        if values.size > self._max_points:
            values = values[: self._max_points]
        if modulation is not self._modulation:
            self._history.clear()
            self._modulation = modulation
            self._reference = _reference_constellation(modulation)
        self._history.append(values)
        del self._history[: -self._history_depth]
        self._last_symbols_at = time.monotonic() if received_at is None else received_at

    def refresh(self, *, now: float | None = None) -> None:
        """Schedule one repaint from the UI timer."""

        self._display_time = time.monotonic() if now is None else now
        self.update()

    def clear(self) -> None:
        self._history.clear()
        self._last_symbols_at = None
        self._modulation = None
        self._reference = None
        self.update()

    def status_text(self, *, now: float | None = None) -> str:
        """Describe whether the displayed decoded-burst data is current."""

        if self._last_symbols_at is None:
            return "no decoded burst yet"
        current = time.monotonic() if now is None else now
        age = max(0.0, current - self._last_symbols_at)
        if age > self._stale_after_s:
            return f"stale, last burst {age:.1f} s ago"
        name = self._modulation.name.replace("QAM16", "16QAM")
        return f"live decoded burst ({name})"

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802 (Qt override)
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        area = self._paint_frame(painter)
        if not self._history:
            self._paint_empty(painter, area, "no decoded burst yet")
            return

        side = min(area.width(), area.height())
        centre_x = area.center().x()
        centre_y = area.center().y()
        scale = side / 2.0 / 1.8  # unit-power symbols sit near +-1

        painter.setPen(_GRID)
        painter.drawLine(area.left(), centre_y, area.right(), centre_y)
        painter.drawLine(centre_x, area.top(), centre_x, area.bottom())

        if self._reference is not None:
            painter.setPen(QtGui.QPen(_REFERENCE, 1.2))
            for value in self._reference:
                point = QtCore.QPointF(
                    float(centre_x + np.real(value) * scale),
                    float(centre_y - np.imag(value) * scale),
                )
                painter.drawEllipse(point, 3.0, 3.0)

        stale = (
            self._last_symbols_at is not None
            and self._display_time - self._last_symbols_at > self._stale_after_s
        )
        for index, block in enumerate(self._history):
            newest = index == len(self._history) - 1
            color = _POINT_STALE if stale else (_POINT if newest else _POINT_FADED)
            painter.setPen(QtGui.QPen(color, 2.0 if newest else 1.4))
            xs = centre_x + np.real(block) * scale
            ys = centre_y - np.imag(block) * scale
            inside = (
                (xs >= area.left())
                & (xs <= area.right())
                & (ys >= area.top())
                & (ys <= area.bottom())
            )
            points = [
                QtCore.QPointF(float(x), float(y))
                for x, y in zip(xs[inside], ys[inside], strict=True)
            ]
            if points:
                painter.drawPoints(QtGui.QPolygonF(points))

        painter.setPen(_LABEL)
        painter.drawText(
            area.left() + 4,
            area.top() + 14,
            f"{sum(block.size for block in self._history)} symbols",
        )
        status = self.status_text(now=self._display_time)
        width = painter.fontMetrics().width(status)
        painter.drawText(area.right() - width - 6, area.top() + 14, status)


class TrendPlot(_PlotBase):
    """Two refresh-rate series over the last minute, each on its own y axis.

    Points arrive once per stats refresh (about 120 a minute), so a Python
    loop over them is cheap; no sample ever reaches this widget.  Intervals
    whose point is flagged ``alert`` are shaded red behind the traces.
    """

    def __init__(
        self,
        title: str,
        *,
        primary_label: str,
        primary_scale: float = 1.0,
        primary_unit: str = "",
        secondary_label: str,
        secondary_unit: str = "",
        secondary_range: tuple[float, float] | None = None,
        span_s: float = 60.0,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        self.setMinimumHeight(110)
        self._primary_label = primary_label
        self._primary_scale = primary_scale
        self._primary_unit = primary_unit
        self._secondary_label = secondary_label
        self._secondary_unit = secondary_unit
        self._secondary_range = secondary_range
        self._span_s = span_s
        self._points: tuple[Any, ...] = ()

    @property
    def point_count(self) -> int:
        return len(self._points)

    def set_points(self, points) -> None:
        """Replace the series with ``TrendPoint``-like items and repaint."""

        self._points = tuple(points)
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802 (Qt override)
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        area = self._paint_frame(painter)
        points = self._points
        if len(points) < 2:
            self._paint_empty(painter, area, "collecting the last 60 s")
            return
        newest = points[-1].t

        def x_of(t: float) -> float:
            return area.right() - (newest - t) / self._span_s * area.width()

        for previous, point in zip(points, points[1:], strict=False):
            if point.alert:
                left = max(float(area.left()), x_of(previous.t))
                painter.fillRect(
                    QtCore.QRectF(left, area.top(), max(1.0, x_of(point.t) - left), area.height()),
                    _ALERT,
                )

        primary = [point.primary / self._primary_scale for point in points]
        top = max(max(primary) * 1.15, 1e-3)
        self._draw_series(
            painter, area, [(x_of(p.t), v) for p, v in zip(points, primary, strict=True)],
            0.0, top, _TRACE,
        )
        secondary = [(x_of(p.t), p.secondary) for p in points if p.secondary is not None]
        if self._secondary_range is not None:
            low, high = self._secondary_range
        elif secondary:
            values = [value for _, value in secondary]
            low, high = min(values) - 1.0, max(values) + 1.0
        else:
            low, high = 0.0, 1.0
        self._draw_series(painter, area, secondary, low, high, _SECONDARY)

        painter.setPen(_TRACE)
        painter.drawText(
            area.left() + 4, area.top() + 14,
            f"{self._primary_label} {primary[-1]:.3f} {self._primary_unit} (axis 0-{top:.3g})",
        )
        painter.setPen(_SECONDARY)
        last = points[-1].secondary
        text = (
            f"{self._secondary_label} "
            + ("\u2014" if last is None else f"{last:.1f} {self._secondary_unit}")
            + f" (axis {low:.0f}-{high:.0f})"
        )
        width = painter.fontMetrics().width(text)
        painter.drawText(area.right() - width - 6, area.top() + 14, text)

    @staticmethod
    def _draw_series(painter, area, xy, low: float, high: float, colour) -> None:
        if len(xy) < 2:
            return
        span = max(high - low, 1e-9)
        path = QtGui.QPainterPath()
        for index, (x, value) in enumerate(xy):
            clipped = min(max(value, low), high)
            y = area.bottom() - (clipped - low) / span * area.height()
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QtGui.QPen(colour, 1.6))
        painter.drawPath(path)


def _reference_constellation(modulation: MCS) -> NDArray[np.complex64]:
    bits_per_symbol = modulation.bits_per_symbol
    patterns = np.array(
        [
            [
                (index >> (bits_per_symbol - 1 - bit)) & 1
                for bit in range(bits_per_symbol)
            ]
            for index in range(1 << bits_per_symbol)
        ],
        dtype=np.uint8,
    ).ravel()
    return map_symbols(patterns, modulation)
