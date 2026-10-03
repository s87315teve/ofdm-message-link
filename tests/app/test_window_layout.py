"""Window placement for the OTA video demo, tested without a display.

``--geometry`` parsing and the xrandr-to-layout planning are plain Python so
the rules in docs/03-your-own-app.md ("視窗排列") can be checked here.
"""

from __future__ import annotations

import argparse

import pytest

from ofdm_message_link.window_layout import Geometry, parse_geometry


def test_geometry_parses_x11_style_size_and_position():
    assert parse_geometry("1850x1016+70+27") == Geometry(1850, 1016, 70, 27)


@pytest.mark.parametrize(
    "text",
    ["", "1850x1016", "1850x1016+70", "0x100+0+0", "100x0+0+0", "axb+1+2",
     "100x100-5+0", "100X100+0+0", " 100x100+0+0", "100x100+0+0 extra"],
)
def test_geometry_rejects_malformed_text(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_geometry(text)


# -- apps ------------------------------------------------------------------


@pytest.mark.parametrize("app", ["tx_app", "rx_app"])
def test_apps_take_an_optional_geometry(app):
    import importlib

    build_parser = importlib.import_module(f"ofdm_message_link.{app}").build_parser
    assert build_parser().parse_args([]).geometry is None
    args = build_parser().parse_args(["--geometry", "925x1016+70+27"])
    assert args.geometry == Geometry(925, 1016, 70, 27)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--geometry", "925x1016"])


@pytest.mark.gui
def test_place_window_sets_client_size_and_frame_position(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    qt_widgets = pytest.importorskip("PyQt5.QtWidgets")
    from ofdm_message_link import qt_runtime

    application = qt_runtime.create_application()
    window = qt_widgets.QWidget()
    qt_runtime.place_window(window, Geometry(640, 360, 100, 50))
    assert (window.width(), window.height()) == (640, 360)
    assert (window.frameGeometry().x(), window.frameGeometry().y()) == (100, 50)
    window.close()
    assert application is qt_widgets.QApplication.instance()


# -- layout planning ---------------------------------------------------------

# Trimmed ``xrandr --query`` from the demo PC (GNOME Wayland, two XWayland outputs).
XRANDR_DUAL = """\
Screen 0: minimum 16 x 16, current 3840 x 1080, maximum 32767 x 32767
XWAYLAND0 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 600mm x 340mm
   1920x1080     59.96*+
   1440x1080     59.99
XWAYLAND1 connected 1920x1080+1920+0 (normal left inverted right x axis y axis) 600mm x 340mm
   1920x1080     59.96*+
"""
XRANDR_SINGLE = """\
Screen 0: minimum 320 x 200, current 1920 x 1080, maximum 16384 x 16384
eDP-1 connected primary 1920x1080+0+0 (normal left inverted right x axis y axis) 344mm x 194mm
   1920x1080     60.02*+
HDMI-1 disconnected (normal left inverted right x axis y axis)
"""
# ``xprop -root _GTK_WORKAREAS_D0``: the secondary is listed first; the primary
# loses a 70 px dock on the left and a 27 px top bar.
WORKAREAS = "_GTK_WORKAREAS_D0(CARDINAL) = 1920, 0, 1920, 1080, 70, 27, 1850, 1053\n"


def _plan(mode, xrandr=XRANDR_DUAL, workareas=WORKAREAS):
    from ofdm_message_link.window_layout import plan_layout

    return plan_layout(mode, xrandr, workareas)


def test_dual_puts_tx_on_the_primary_and_video_over_rx_on_the_other():
    # Heights are client heights: a 37 px title bar sits above each one.
    assert _plan("dual") == {
        "tx": Geometry(1850, 1016, 70, 27),
        "video": Geometry(1920, 683, 1920, 0),
        "rx": Geometry(1920, 323, 1920, 720),
    }


def test_single_puts_rx_left_and_video_over_tx_right_on_the_primary():
    assert _plan("single") == {
        "rx": Geometry(925, 1016, 70, 27),
        "video": Geometry(925, 489, 995, 27),
        "tx": Geometry(925, 490, 995, 553),
    }


def test_auto_chooses_dual_for_two_screens_and_single_for_one():
    assert _plan("auto") == _plan("dual")
    assert _plan("auto", XRANDR_SINGLE, "") == _plan("single", XRANDR_SINGLE, "")


def test_without_work_areas_the_whole_monitor_is_used():
    assert _plan("single", XRANDR_SINGLE, "")["rx"] == Geometry(960, 1043, 0, 0)


def test_none_or_undetectable_screens_give_no_layout():
    assert _plan("none") is None
    assert _plan("auto", "", "") is None
    assert _plan("auto", "Can't open display :0\n", "") is None
    assert _plan("dual", XRANDR_SINGLE, "") is None


def test_command_line_prints_one_line_per_window_for_the_script(monkeypatch, capsys):
    import io

    from ofdm_message_link.window_layout import main

    monkeypatch.setattr("sys.stdin", io.StringIO(XRANDR_DUAL))
    assert main(["auto", "--workareas", WORKAREAS]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "layout dual",
        "tx 1850x1016+70+27",
        "rx 1920x323+1920+720",
        "video 1920x683+1920+0",
    ]

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["auto"]) == 0
    assert capsys.readouterr().out.splitlines() == ["layout none"]


@pytest.mark.gui
def test_a_window_placed_short_opens_short_with_its_tabs_collapsed(monkeypatch):
    # With tabs showing the TX window needs about 410 px; collapsed, about 265.
    # The script's short cells (e.g. 323 px under the video) must not be
    # clamped back up to the tabbed minimum when the window is first shown.
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5.QtWidgets")
    from ofdm_message_link import options, qt_runtime
    from ofdm_message_link.tx_app import TransmitWindow, build_parser

    application = qt_runtime.create_application()
    args = build_parser().parse_args(["--udp-port", "53311", "--ingress-port", "53312"])
    resolved = options.resolve(args)
    window = TransmitWindow(
        resolved,
        53312,
        build_sink=lambda selection: options.build_sink(args, resolved, selection),
    )
    try:
        qt_runtime.place_window(window, Geometry(1000, 300, 0, 0))
        window.show()
        application.processEvents()
        assert window.height() == 300
        assert not window._tabs.isVisible()
    finally:
        window.close()
