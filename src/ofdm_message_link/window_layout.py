"""Where the OTA video demo puts its three windows.

Plain Python, no Qt: ``scripts/run_ota_video_demo.sh`` pipes ``xrandr`` into
this module and passes the result to the apps as ``--geometry`` and to ffplay
as ``-left -top -x -y``.

``WxH+X+Y`` follows the X11 convention both Qt (``resize`` + ``move``) and
ffplay honour under XWayland on GNOME: ``X+Y`` is the top-left corner of the
window *frame* (title bar included), ``WxH`` is the client area below it.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

_GEOMETRY = re.compile(r"(\d+)x(\d+)\+(\d+)\+(\d+)")


@dataclass(frozen=True, slots=True)
class Geometry:
    width: int
    height: int
    x: int
    y: int

    def __str__(self) -> str:
        return f"{self.width}x{self.height}+{self.x}+{self.y}"


def parse_geometry(text: str) -> Geometry:
    """Parse ``WxH+X+Y``; usable directly as an argparse ``type``."""

    match = _GEOMETRY.fullmatch(text)
    if match is None:
        raise argparse.ArgumentTypeError(f"expected WxH+X+Y, e.g. 1280x720+0+0, got {text!r}")
    geometry = Geometry(*(int(group) for group in match.groups()))
    if geometry.width == 0 or geometry.height == 0:
        raise argparse.ArgumentTypeError(f"width and height must be positive, got {text!r}")
    return geometry


# Title bar mutter draws above each XWayland client (measured on GNOME Shell 42:
# a client placed with its frame at y=0 lands at y=37).
TITLE_BAR_PX = 37

_MONITOR = re.compile(r"^(\S+) connected (primary )?(\d+)x(\d+)\+(\d+)\+(\d+)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class _Rect:
    x: int
    y: int
    width: int
    height: int

    def contains(self, other: _Rect) -> bool:
        return (
            self.x <= other.x
            and self.y <= other.y
            and other.x + other.width <= self.x + self.width
            and other.y + other.height <= self.y + self.height
        )


def _monitors(xrandr: str) -> list[_Rect]:
    """Connected, active outputs from ``xrandr --query``, primary first."""

    found = [
        (match.group(2) is None, _Rect(*(int(match.group(i)) for i in (5, 6, 3, 4))))
        for match in _MONITOR.finditer(xrandr)
    ]
    return [rect for _, rect in sorted(found, key=lambda item: item[0])]


def _usable(monitor: _Rect, workareas: str) -> _Rect:
    """The monitor minus docks and panels, from ``xprop -root _GTK_WORKAREAS_D0``."""

    _, _, values = workareas.partition("=")
    numbers = [int(value) for value in re.findall(r"\d+", values)]
    for index in range(0, len(numbers) - 3, 4):
        area = _Rect(*numbers[index : index + 4])
        if monitor.contains(area):
            return area
    return monitor


def _window(x: int, y: int, width: int, height: int) -> Geometry:
    """A frame occupying the given cell, as client size plus frame position."""

    return Geometry(width, height - TITLE_BAR_PX, x, y)


def _resolve(mode: str, monitor_count: int) -> str:
    if mode == "auto":
        return "dual" if monitor_count >= 2 else "single"
    return mode


def plan_layout(mode: str, xrandr: str, workareas: str = "") -> dict[str, Geometry] | None:
    """Geometries for ``tx``, ``rx`` and ``video``, or ``None`` to leave placement alone.

    ``dual``: TX fills the primary screen; the other screen has video on the
    top 2/3 and RX on the bottom 1/3.  ``single``: RX fills the left half of
    the primary; video top-right, TX bottom-right.  ``auto`` picks ``dual``
    for two or more screens and ``single`` for one.  ``None`` means ``none``
    was asked for, or the screens needed could not be detected.
    """

    monitors = [_usable(monitor, workareas) for monitor in _monitors(xrandr)]
    mode = _resolve(mode, len(monitors))
    if mode == "dual" and len(monitors) >= 2:
        first, second = monitors[0], monitors[1]
        video_height = second.height * 2 // 3
        return {
            "tx": _window(first.x, first.y, first.width, first.height),
            "video": _window(second.x, second.y, second.width, video_height),
            "rx": _window(
                second.x, second.y + video_height, second.width, second.height - video_height
            ),
        }
    if mode == "single" and monitors:
        screen = monitors[0]
        left = screen.width // 2
        top = screen.height // 2
        right_x = screen.x + left
        return {
            "rx": _window(screen.x, screen.y, left, screen.height),
            "video": _window(right_x, screen.y, screen.width - left, top),
            "tx": _window(right_x, screen.y + top, screen.width - left, screen.height - top),
        }
    return None


def main(argv: list[str] | None = None) -> int:
    """Read ``xrandr --query`` on stdin; print ``layout NAME`` then ``ROLE WxH+X+Y`` lines."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("layout", choices=("auto", "single", "dual", "none"))
    parser.add_argument(
        "--workareas", default="", help="output of xprop -root _GTK_WORKAREAS_D0"
    )
    args = parser.parse_args(argv)
    xrandr = sys.stdin.read()
    plan = plan_layout(args.layout, xrandr, args.workareas)
    if plan is None:
        print("layout none")
        return 0
    print(f"layout {_resolve(args.layout, len(_monitors(xrandr)))}")
    for role in ("tx", "rx", "video"):
        print(f"{role} {plan[role]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
