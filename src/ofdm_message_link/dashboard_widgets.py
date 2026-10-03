"""Qt pieces shared by the two dashboard windows.

The rules live in :mod:`.dashboard`; this module only draws them.  Every
string handed to Qt here is either a number or text this program wrote, never
received payload bytes (those go through ``qt_runtime.preview_payload``).
"""

from __future__ import annotations

from PyQt5 import QtCore, QtGui, QtWidgets

from . import dashboard

_LIGHT_COLOURS = {
    "grey": ("#5c636e", "#e6e9ee"),
    "red": ("#b3261e", "#ffffff"),
    "yellow": ("#c99a06", "#1b1b1b"),
    "green": ("#1e8e3e", "#ffffff"),
}


class Card(QtWidgets.QFrame):
    """A titled value, large enough to read from across the room."""

    def __init__(self, title: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        # Wide enough for "-10.0 dB" or "100.00 %" so a changing value never
        # clips before the layout catches up.
        self.setMinimumWidth(130)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(0)
        heading = QtWidgets.QLabel(title)
        heading.setStyleSheet("color:#8a94a3; font-size:9pt;")
        layout.addWidget(heading)
        self._value = QtWidgets.QLabel("—")
        self._value.setStyleSheet("font-size:17pt; font-weight:bold;")
        layout.addWidget(self._value)
        self._detail = QtWidgets.QLabel("")
        self._detail.setStyleSheet("color:#8a94a3; font-size:8pt;")
        layout.addWidget(self._detail)
        self.body = layout

    def set_value(self, value: str, detail: str = "") -> None:
        self._value.setText(value)
        self._detail.setText(detail)

    def replace_value(self, widget: QtWidgets.QWidget) -> None:
        """Show a control (the TX MCS selector) where the value would be."""

        self._value.setVisible(False)
        self.body.insertWidget(1, widget)

    def value_text(self) -> str:
        return self._value.text()


class LightCard(QtWidgets.QFrame):
    """The indicator light: a coloured state word and its reason."""

    def __init__(self, title: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)
        top = QtWidgets.QHBoxLayout()
        heading = QtWidgets.QLabel(title)
        heading.setStyleSheet("color:#8a94a3; font-size:9pt;")
        top.addWidget(heading)
        self._state = QtWidgets.QLabel()
        top.addWidget(self._state)
        self._badge = QtWidgets.QLabel()
        self._badge.setVisible(False)
        top.addWidget(self._badge)
        top.addStretch(1)
        layout.addLayout(top)
        self._reason = QtWidgets.QLabel()
        self._reason.setWordWrap(True)
        self._reason.setMinimumWidth(1)
        layout.addWidget(self._reason)
        self._light: dashboard.Light | None = None
        self.set_light(dashboard.Light("WAITING", "grey", "Starting"))

    @property
    def light(self) -> dashboard.Light | None:
        return self._light

    def set_light(self, light: dashboard.Light) -> None:
        if light == self._light:
            return
        self._light = light
        background, foreground = _LIGHT_COLOURS[light.colour]
        self._state.setText(light.state)
        self._state.setStyleSheet(
            f"background:{background}; color:{foreground}; font-size:13pt; "
            "font-weight:bold; padding:1px 8px; border-radius:4px;"
        )
        self._reason.setText(light.reason)

    def set_badge(self, text: str, colour: str) -> None:
        """A second, louder marker next to the light (TX: RF ON / SIMULATED)."""

        background, foreground = _LIGHT_COLOURS[colour]
        self._badge.setText(text)
        self._badge.setStyleSheet(
            f"background:{background}; color:{foreground}; font-size:13pt; "
            "font-weight:bold; padding:1px 8px; border-radius:4px;"
        )
        self._badge.setVisible(True)


def one_line_banner(text: str) -> QtWidgets.QLabel:
    """The evidence boundary on a single line; the full text is the tooltip."""

    label = QtWidgets.QLabel(text)
    label.setToolTip(text)
    label.setWordWrap(False)
    # Ignored width lets the window shrink below the text's natural width;
    # the line is clipped instead of forcing a minimum window size.
    label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
    label.setMinimumWidth(1)
    label.setStyleSheet(
        "background:#5a2d2d; color:#ffd9d9; padding:2px 6px; border-radius:3px; "
        "font-weight:bold; font-size:8pt;"
    )
    return label


def monospace_view(minimum_height: int = 60) -> QtWidgets.QPlainTextEdit:
    view = QtWidgets.QPlainTextEdit()
    view.setReadOnly(True)
    view.setFont(QtGui.QFont("Monospace", 9))
    view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
    view.setMinimumHeight(minimum_height)
    return view


def reset_button(parent: QtWidgets.QWidget | None = None) -> QtWidgets.QPushButton:
    button = QtWidgets.QPushButton("Reset stats", parent)
    button.setToolTip(
        "Zero the counters, windows and chart shown here. The radio keeps "
        "running and the --stats-log counters stay cumulative."
    )
    button.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Preferred)
    return button


def tab_area() -> QtWidgets.QTabWidget:
    """The lower tab area, whose pages never set the window's minimum height.

    Left to Qt, the tallest page (the device panel) would make the window's
    minimum taller than :data:`dashboard.COLLAPSE_BELOW_PX`, and a short window
    could then never reach the height at which the tabs fold away.
    """

    tabs = QtWidgets.QTabWidget()
    tabs.setMinimumHeight(140)
    return tabs


def scroll_page(page: QtWidgets.QWidget) -> QtWidgets.QScrollArea:
    """Wrap a tall tab page so it scrolls instead of being squeezed."""

    area = QtWidgets.QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QtWidgets.QFrame.NoFrame)
    area.setWidget(page)
    return area


def choosing_radio(panel: QtWidgets.QWidget | None) -> bool:
    """Whether the operator still has to pick and start a radio on the panel."""

    return (
        panel is not None
        and not getattr(panel, "running", False)
        and not getattr(panel, "_auto_start", False)
    )


def apply_tab_collapse(
    window: QtWidgets.QWidget,
    tabs: QtWidgets.QTabWidget,
    *,
    choosing_radio: bool = False,
    live_view: tuple[QtWidgets.QWidget, ...] = (),
) -> None:
    """Hide the tab area in a short window and bring it back when tall.

    While a radio still has to be chosen, a short window keeps the tabs and
    folds ``live_view`` (the cards and chart) instead, so the device panel on
    the Hardware tab has room.
    """

    height = window.height()
    shown = dashboard.live_view_visible(height, choosing_radio=choosing_radio)
    for widget in live_view:
        if widget.isVisibleTo(window) != shown:
            widget.setVisible(shown)
    visible = dashboard.tabs_visible(height, choosing_radio=choosing_radio)
    if tabs.isVisibleTo(window) != visible:
        tabs.setVisible(visible)


def fixed_line(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color:#9fb3c8; padding:2px;")
    label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
    return label
