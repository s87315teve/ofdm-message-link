"""What the two demo windows show at a glance, decided without Qt.

The indicator light, the sliding-window statistics and the 60-second trend
are described in docs/02-gui.md.  Everything here is computed from counter
values the windows already refresh twice a second, so nothing is added to
the sample or decode path, and the rules can be unit tested headless.

Times are ``time.monotonic()`` seconds supplied by the caller.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

from .link import RateMeter

# Effective SNR each MCS needed for loss-free video in the over-the-air
# measurements summarised in docs/08-measurements.md.  An MCS missing
# here has no measured threshold, and its light is judged on loss alone.
SNR_THRESHOLD_DB: Mapping[int, float] = {0: 8.0, 4: 11.0}

LOSS_WINDOW_S = 10.0
SNR_WINDOW_S = 2.0
GOODPUT_WINDOW_S = 2.0
TREND_SPAN_S = 60.0
DOWN_AFTER_S = 2.0
IDLE_AFTER_S = 10.0
LOSSY_RATIO = 0.01
MARGIN_DB = 1.0
TX_FAULT_WINDOW_S = 10.0
TX_BUSY_AIRTIME = 0.90
TX_RISING_REFRESHES = 3
COLLAPSE_BELOW_PX = 600

# UHD async events that mean a transmitted burst did not go out as scheduled.
TX_FAULT_KEYS = (
    "tx_time_error",
    "tx_underflow",
    "tx_underflow_in_packet",
    "tx_sequence_error",
    "tx_sequence_error_in_burst",
)

_DASH = "—"


@dataclass(frozen=True, slots=True)
class Light:
    """One indicator: a state word, its colour and a one-line reason."""

    state: str
    colour: str  # grey, red, yellow or green
    reason: str


@dataclass(frozen=True, slots=True)
class TrendPoint:
    """One refresh on the 60-second chart; ``alert`` shades its interval."""

    t: float
    primary: float
    secondary: float | None
    alert: bool = False


def tabs_visible(height_px: int, *, choosing_radio: bool = False) -> bool:
    """Whether a window this tall still has room for the tab area.

    While the operator still has to choose a radio the tabs stay, however
    short the window: the device panel is on the Hardware tab, and nothing
    runs until it has been used.
    """

    return choosing_radio or height_px >= COLLAPSE_BELOW_PX


def live_view_visible(height_px: int, *, choosing_radio: bool = False) -> bool:
    """Whether the cards and 60-second chart keep their place in a window this tall.

    A short window that is still waiting for a radio gives their room to the
    device panel instead: before a radio runs they have nothing to show.
    """

    return not choosing_radio or height_px >= COLLAPSE_BELOW_PX


def suggested_mcs(snr_db: float | None, mcs_index: int | None) -> int | None:
    """A more robust measured MCS for this SNR, or None.

    Only offered below the current MCS's own threshold.  Among the measured
    MCS with a lower threshold, the highest one the SNR still meets is chosen;
    when it meets none of them, the most robust one is still worth trying.
    """

    if snr_db is None or mcs_index is None:
        return None
    current = SNR_THRESHOLD_DB.get(mcs_index)
    if current is None or snr_db >= current:
        return None
    robust = sorted(
        (threshold, index)
        for index, threshold in SNR_THRESHOLD_DB.items()
        if threshold < current
    )
    if not robust:
        return None
    enough = [index for threshold, index in robust if threshold <= snr_db]
    return enough[-1] if enough else robust[0][1]


def rx_light(
    *,
    now: float,
    last_burst_at: float | None,
    loss_10s: float | None,
    snr_2s: float | None,
    mcs_index: int | None,
) -> Light:
    """The receive light; the first rule that holds wins."""

    if last_burst_at is None:
        return Light("WAITING", "grey", "Waiting for the first burst")
    age = now - last_burst_at
    if age > IDLE_AFTER_S:
        return Light(
            "IDLE",
            "grey",
            f"No bursts for {age:.0f} s {_DASH} transmitter stopped or link lost",
        )
    if age > DOWN_AFTER_S:
        return Light("DOWN", "red", f"No bursts for {age:.0f} s")

    threshold = None if mcs_index is None else SNR_THRESHOLD_DB.get(mcs_index)
    unmeasured = (
        f"; no SNR threshold measured for MCS {mcs_index}"
        if threshold is None and mcs_index is not None
        else ""
    )
    loss = loss_10s or 0.0
    if loss >= LOSSY_RATIO:
        light = Light("LOSSY", "red", f"Loss {loss * 100:.1f} % in the last 10 s{unmeasured}")
    elif loss > 0.0:
        light = Light("LOSS", "yellow", f"Loss {loss * 100:.1f} % in the last 10 s{unmeasured}")
    elif threshold is not None and snr_2s is not None and snr_2s < threshold + MARGIN_DB:
        light = Light(
            "MARGINAL",
            "yellow",
            f"SNR {snr_2s:.1f} dB is within {MARGIN_DB:g} dB of {threshold:g} dB "
            f"needed for MCS {mcs_index}",
        )
    elif threshold is not None and snr_2s is not None:
        return Light(
            "GOOD",
            "green",
            f"No loss, SNR {snr_2s - threshold:.1f} dB above MCS {mcs_index} threshold",
        )
    else:
        return Light("GOOD", "green", f"No loss{unmeasured}")

    better = suggested_mcs(snr_2s, mcs_index)
    if better is None:
        return light
    return Light(light.state, light.colour, f"{light.reason} {_DASH} try MCS {better}")


def tx_light(*, radio_on: bool, faults_10s: int, queue_rising: bool, airtime: float,
             fault_names: str = "") -> Light:
    """The transmit light: grey, then red, then yellow, else green."""

    if not radio_on:
        return Light("OFF", "grey", "Radio not started")
    if faults_10s > 0:
        detail = fault_names or f"{faults_10s} UHD transmit fault(s)"
        return Light("FAULT", "red", f"{detail} in the last 10 s")
    if queue_rising:
        return Light(
            "BUSY",
            "yellow",
            f"Send queue grew {TX_RISING_REFRESHES} refreshes in a row {_DASH} "
            "offered rate exceeds the air rate",
        )
    if airtime > TX_BUSY_AIRTIME:
        return Light("BUSY", "yellow", f"Airtime {airtime * 100:.0f} % {_DASH} near capacity")
    return Light("OK", "green", "Transmitting normally")


class WindowedCounters:
    """Increase of each counter over the most recent ``window_s``.

    Like :class:`RateMeter`, one point at or before the window start is kept
    so the span stays full.
    """

    def __init__(self, window_s: float) -> None:
        self._window_s = window_s
        self._points: deque[tuple[float, dict[str, int]]] = deque()

    def update(self, now: float, values: Mapping[str, int]) -> None:
        self._points.append((now, dict(values)))
        while len(self._points) > 2 and self._points[1][0] <= now - self._window_s:
            self._points.popleft()

    def delta(self, name: str) -> int:
        if len(self._points) < 2:
            return 0
        first, last = self._points[0][1], self._points[-1][1]
        return int(last.get(name, 0)) - int(first.get(name, 0))

    def deltas(self) -> dict[str, int]:
        if not self._points:
            return {}
        return {name: self.delta(name) for name in self._points[-1][1]}

    def clear(self) -> None:
        self._points.clear()


class SampleWindow:
    """Mean of the values observed in the most recent ``window_s``."""

    def __init__(self, window_s: float) -> None:
        self._window_s = window_s
        self._samples: deque[tuple[float, float]] = deque()

    def add(self, now: float, value: float) -> None:
        self._samples.append((now, float(value)))

    def mean(self, now: float) -> float | None:
        while self._samples and self._samples[0][0] < now - self._window_s:
            self._samples.popleft()
        if not self._samples:
            return None
        return sum(value for _, value in self._samples) / len(self._samples)

    def clear(self) -> None:
        self._samples.clear()


class Trend:
    """The last ``span_s`` seconds of refresh points for the chart."""

    def __init__(self, span_s: float = TREND_SPAN_S) -> None:
        self.span_s = span_s
        self._points: deque[TrendPoint] = deque()

    def append(self, point: TrendPoint) -> None:
        self._points.append(point)
        while self._points and self._points[0].t < point.t - self.span_s:
            self._points.popleft()

    def points(self) -> tuple[TrendPoint, ...]:
        return tuple(self._points)

    def clear(self) -> None:
        self._points.clear()


class Baseline:
    """Counters shown relative to the values captured at the last reset."""

    def __init__(self) -> None:
        self._base: dict[str, int] = {}

    def rebase(self, values: Mapping[str, int]) -> None:
        self._base = dict(values)

    def since(self, values: Mapping[str, int]) -> dict[str, int]:
        return {name: int(value) - int(self._base.get(name, 0)) for name, value in values.items()}


@dataclass(frozen=True, slots=True)
class RxView:
    goodput_bps: float
    loss_10s: float | None
    snr_2s: float | None
    light: Light
    recent: dict[str, int]
    totals: dict[str, int]
    trend: tuple[TrendPoint, ...]


class RxDashboard:
    """Receive-side cards, light and trend from cumulative counters.

    ``counters`` must include ``bursts_decoded``, ``missing_bursts`` and
    ``message_bytes_delivered``; any other integer counters are carried into
    the 10-second and since-reset columns.
    """

    def __init__(self) -> None:
        self._goodput = RateMeter(GOODPUT_WINDOW_S)
        self._window = WindowedCounters(LOSS_WINDOW_S)
        self._snr = SampleWindow(SNR_WINDOW_S)
        self._trend = Trend()
        self._baseline = Baseline()
        self._last_burst_at: float | None = None
        self._previous: Mapping[str, int] | None = None

    def observe_burst(self, now: float, snr_db: float | None) -> None:
        """Record one decoded burst's time and effective SNR."""

        self._last_burst_at = now if self._last_burst_at is None else max(
            self._last_burst_at, now
        )
        if snr_db is not None:
            self._snr.add(now, snr_db)

    def refresh(self, now: float, counters: Mapping[str, int], *, mcs_index: int | None) -> RxView:
        previous = self._previous
        self._previous = dict(counters)
        decoded = int(counters["bursts_decoded"])
        missing = int(counters["missing_bursts"])
        # Before the first refresh every counter was zero.
        before = previous if previous is not None else {}
        if decoded > int(before.get("bursts_decoded", 0)):
            self._last_burst_at = now if self._last_burst_at is None else max(
                self._last_burst_at, now
            )
        lost_now = missing > int(before.get("missing_bursts", 0))

        totals = self._baseline.since(counters)
        self._goodput.update(now, totals["message_bytes_delivered"])
        self._window.update(now, counters)
        recent = self._window.deltas()
        expected = recent.get("bursts_decoded", 0) + recent.get("missing_bursts", 0)
        loss = None if expected <= 0 else recent.get("missing_bursts", 0) / expected
        snr = self._snr.mean(now)
        goodput = self._goodput.bits_per_second
        self._trend.append(TrendPoint(now, goodput, snr, lost_now))
        light = rx_light(
            now=now,
            last_burst_at=self._last_burst_at,
            loss_10s=loss,
            snr_2s=snr,
            mcs_index=mcs_index,
        )
        return RxView(goodput, loss, snr, light, recent, totals, self._trend.points())

    def reset(self, now: float, counters: Mapping[str, int]) -> None:
        """Make the current counter values the new zero.

        The radio, the data path and any cumulative log are untouched.  The
        time of the last burst is kept, so the light does not fall back to
        WAITING while bursts are still arriving.
        """

        del now
        self._baseline.rebase(counters)
        self._goodput = RateMeter(GOODPUT_WINDOW_S)
        self._window.clear()
        self._snr.clear()
        self._trend.clear()
        self._previous = dict(counters)


@dataclass(frozen=True, slots=True)
class TxView:
    goodput_bps: float
    airtime: float
    light: Light
    totals: dict[str, int]
    trend: tuple[TrendPoint, ...]


class TxDashboard:
    """Transmit-side cards, light and trend (Goodput and Airtime)."""

    def __init__(self, sample_rate: float) -> None:
        self.sample_rate = float(sample_rate)
        self._goodput = RateMeter(GOODPUT_WINDOW_S)
        self._samples = WindowedCounters(GOODPUT_WINDOW_S)
        self._faults = WindowedCounters(TX_FAULT_WINDOW_S)
        self._queue: deque[int] = deque(maxlen=TX_RISING_REFRESHES + 1)
        self._trend = Trend()
        self._baseline = Baseline()

    def refresh(
        self,
        now: float,
        *,
        radio_on: bool,
        sent_bytes: int,
        samples: int,
        pending: int,
        faults: Mapping[str, object] | None,
    ) -> TxView:
        totals = self._baseline.since({"sent_bytes": sent_bytes, "samples": samples})
        self._goodput.update(now, totals["sent_bytes"])
        self._samples.update(now, {"samples": samples, "t_us": round(now * 1e6)})
        span = self._samples.delta("t_us") / 1e6
        airtime = 0.0 if span <= 0.0 else self._samples.delta("samples") / self.sample_rate / span
        self._faults.update(now, _tx_fault_counts(faults))
        fault_deltas = {name: count for name, count in self._faults.deltas().items() if count > 0}
        self._queue.append(int(pending))
        rising = len(self._queue) == self._queue.maxlen and all(
            later > earlier
            for earlier, later in zip(list(self._queue)[:-1], list(self._queue)[1:], strict=True)
        )
        goodput = self._goodput.bits_per_second
        self._trend.append(TrendPoint(now, goodput, airtime * 100.0, bool(fault_deltas)))
        light = tx_light(
            radio_on=radio_on,
            faults_10s=sum(fault_deltas.values()),
            queue_rising=rising,
            airtime=airtime,
            fault_names=", ".join(f"{name} +{count}" for name, count in fault_deltas.items()),
        )
        return TxView(goodput, airtime, light, totals, self._trend.points())

    def reset(
        self,
        now: float,
        *,
        sent_bytes: int,
        samples: int,
        faults: Mapping[str, object] | None,
    ) -> None:
        del now, faults
        self._baseline.rebase({"sent_bytes": sent_bytes, "samples": samples})
        self._goodput = RateMeter(GOODPUT_WINDOW_S)
        self._samples.clear()
        self._faults.clear()
        self._queue.clear()
        self._trend.clear()


def _tx_fault_counts(faults: Mapping[str, object] | None) -> dict[str, int]:
    if not faults:
        return {}
    counts = {}
    for name in TX_FAULT_KEYS:
        value = faults.get(name)
        if isinstance(value, int):
            counts[name] = value
    return counts


__all__ = [
    "COLLAPSE_BELOW_PX",
    "SNR_THRESHOLD_DB",
    "Light",
    "RxDashboard",
    "RxView",
    "TrendPoint",
    "TxDashboard",
    "TxView",
    "rx_light",
    "suggested_mcs",
    "tabs_visible",
    "live_view_visible",
    "tx_light",
]
