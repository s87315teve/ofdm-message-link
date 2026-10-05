"""Shared Qt entry point for the demo windows.

These apps are launched from a terminal, so Ctrl-C has to work.  Qt's event
loop does not return to the interpreter while it is idle, which leaves a
Python signal handler unable to run: the process aborts instead of closing.
A short idle timer gives the interpreter a chance to service the signal.
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager

from PyQt5 import QtCore, QtGui, QtWidgets

from .window_layout import Geometry


def place_window(window: QtWidgets.QWidget, geometry: Geometry) -> None:
    """Size the client area and put the frame's top-left corner at X+Y.

    ``move`` positions the frame, ``resize`` the client area: the same
    convention ffplay's ``-left -top -x -y`` follows under XWayland, so the
    demo script can tile all three windows with one set of numbers.
    """

    window.resize(geometry.width, geometry.height)
    # A hidden widget only queues its resize event.  Deliver it now so a
    # window that collapses its tabs when short does so before ``show``, let
    # the layout settle on the smaller minimum, and size it again: otherwise
    # the taller tabbed minimum wins and the window opens taller than its cell.
    QtWidgets.QApplication.sendEvent(window, QtGui.QResizeEvent(window.size(), window.size()))
    if window.layout() is not None:
        window.layout().activate()
    window.resize(geometry.width, geometry.height)
    window.move(geometry.x, geometry.y)


def run_window(
    window: QtWidgets.QWidget,
    application: QtWidgets.QApplication | None = None,
    geometry: Geometry | None = None,
) -> int:
    """Show one window and run the event loop until it closes or Ctrl-C.

    Without ``geometry`` the window keeps its own default size and the window
    manager places it.
    """

    if application is None:
        application = QtWidgets.QApplication.instance()
    if application is None:
        raise RuntimeError("a QApplication must exist before run_window()")

    def _interrupt(signum: int, frame: object) -> None:
        del signum, frame
        window.close()
        application.quit()

    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)

    # Parented to the window so it lives exactly as long as it is needed.
    wake = QtCore.QTimer(window)
    wake.timeout.connect(lambda: None)
    wake.start(200)

    if geometry is not None:
        place_window(window, geometry)
    window.show()
    return application.exec_()


@contextmanager
def private_font_cache() -> Iterator[None]:
    """Keep fontconfig away from the user's shared font cache while it loads.

    ``~/.cache/fontconfig`` is written by every program on the machine, each
    with its own fontconfig build.  One that cannot parse a font leaves an
    entry without a character set, and this environment's fontconfig then
    segfaults the first time Qt looks for a fallback font: typing one Chinese
    character closed both windows.  Fontconfig resolves its cache directories
    once, when it loads its configuration, so pointing ``XDG_CACHE_HOME`` at a
    directory of our own for that moment is enough; the fonts themselves are
    still the system's.  See docs/06-troubleshooting.md.
    """

    saved = os.environ.get("XDG_CACHE_HOME")
    base = saved or os.path.join(os.path.expanduser("~"), ".cache")
    os.environ["XDG_CACHE_HOME"] = os.path.join(base, "ofdm-message-link")
    try:
        yield
    finally:
        if saved is None:
            del os.environ["XDG_CACHE_HOME"]
        else:
            os.environ["XDG_CACHE_HOME"] = saved


def create_application() -> QtWidgets.QApplication:
    """Create the QApplication without letting Qt eat our own arguments."""

    existing = QtWidgets.QApplication.instance()
    if existing is not None:
        return existing
    with private_font_cache():
        application = QtWidgets.QApplication(sys.argv[:1])
        # Qt loads fontconfig lazily; make it happen inside the override.
        QtGui.QFontDatabase().families()
    return application


def set_text_preserving_scroll(
    view: QtWidgets.QPlainTextEdit,
    text: str,
    *,
    follow_tail: bool = True,
) -> None:
    """Replace a read-only view's text without yanking it back into place.

    These panels refresh twice a second.  ``setPlainText`` resets both
    scrollbars, so anything below the fold could not be read at all: scroll
    down, and the next tick puts you back at the top.
    """

    if view.toPlainText() == text:
        return
    vertical = view.verticalScrollBar()
    horizontal = view.horizontalScrollBar()
    at_bottom = vertical.value() >= vertical.maximum() - 2
    previous_vertical = vertical.value()
    previous_horizontal = horizontal.value()

    view.setPlainText(text)

    # Following the tail is what you want while the content grows; holding
    # position is what you want once you have scrolled up to read something.
    # A fixed-layout table (``follow_tail=False``) always holds position, so
    # its heading row is never scrolled away by the first refresh.
    vertical.setValue(
        vertical.maximum() if at_bottom and follow_tail else previous_vertical
    )
    horizontal.setValue(previous_horizontal)


def preview_payload(payload: bytes, limit: int = 120) -> str:
    """One printable line summarising application bytes for a message list.

    Binary payloads such as MPEG-TS video crash this Qt build's text layout:
    ``appendPlainText`` segfaults on C0 controls, DEL and U+2028, and on
    accumulated lines of random mixed-script characters.  Text that is valid
    UTF-8 is shown as text; anything else is shown as printable ASCII only.
    Every other character becomes ``·``.
    """

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = "".join(chr(b) if 0x20 <= b < 0x7F else "·" for b in payload)
    collapsed = " ".join(text.split())
    printable = "".join(ch if ch.isprintable() else "·" for ch in collapsed)
    return printable if len(printable) <= limit else printable[: limit - 1] + "…"


def append_line_following_tail(view: QtWidgets.QPlainTextEdit, line: str) -> None:
    """Append one line, scrolling only if the view was already at the tail."""

    vertical = view.verticalScrollBar()
    horizontal = view.horizontalScrollBar()
    at_bottom = vertical.value() >= vertical.maximum() - 2
    previous_vertical = vertical.value()
    previous_horizontal = horizontal.value()

    view.appendPlainText(line)

    if at_bottom:
        vertical.setValue(vertical.maximum())
    else:
        vertical.setValue(previous_vertical)
    horizontal.setValue(previous_horizontal)
