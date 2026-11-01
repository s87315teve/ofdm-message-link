"""Default receive-side reporter (``--feedback-reporter default``).

What it measures: the SNR of every peer preamble in the window (mean,
10th percentile and last, in dB), cumulative counts of DATA subframes the
peer's bursts delivered and lost, the receiver's health counters (clipping
and decode backlog: set if any chunk in the window had them), and up to
``max_entries`` of the most recent DATA bursts as compact entries.

When a report is due:

* a new DATA burst arrived, or a PSDU failed its FCS, since the last report
  and ``period_ms`` passed, or
* ``keepalive_ms`` passed (also with no data, so the peer can tell a silent
  link from a dead one).

Preambles of control-only bursts update the SNR statistics but never make a
report due; otherwise two idle nodes would answer each other's feedback
bursts forever.  A failed PSDU does: when the peer's SNR falls below what its
MCS needs, its bursts arrive with a valid header and a failed FCS and none
of them yields a DATA burst, so without this the peer would hear of the drop
(the low preamble SNR) only at the next keepalive, 200 ms of lost bursts
later.  The failure counter is ``RxHealth.fcs_fail_total``; its first value
is only a baseline.  Each failure makes one report due, so two idle nodes
answer each other only for as long as their feedback bursts keep failing.

Every report carries a ``session_id`` drawn at random when the reporter is
built, so the peer's link adaptation recognises a restart of this node at
once instead of ignoring the restarted ``report_seq`` as old.
"""

from __future__ import annotations

import math
import secrets
from collections import deque
from collections.abc import Sequence

from .api import (
    DEFAULT_REPORTER_ID,
    FEEDBACK_VERSION,
    FLAG_CLIPPING_SEEN,
    FLAG_DECODE_BACKLOG,
    FLAG_LAST_SEQ_VALID,
    MAX_REPORT_ENTRIES,
    BurstReport,
    FeedbackReport,
    ReporterContext,
    RxHealth,
)

_U16 = 1 << 16
_U32 = 1 << 32


def percentile(values: Sequence[float], fraction: float) -> float:
    """Linear-interpolated percentile (numpy's default) of a non-empty sequence."""

    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


class DefaultReporter:
    """Window SNR statistics, cumulative DATA counters and recent burst entries."""

    reporter_id = DEFAULT_REPORTER_ID

    def __init__(
        self,
        context: ReporterContext,
        period_ms: float = 20.0,
        keepalive_ms: float = 200.0,
        max_entries: int = 8,
        session_id: int | None = None,
    ) -> None:
        if period_ms <= 0 or keepalive_ms <= 0:
            raise ValueError("period_ms and keepalive_ms must be positive")
        if int(max_entries) < 0:
            raise ValueError("max_entries must not be negative")
        self.period_s = float(period_ms) / 1000.0
        self.keepalive_s = float(keepalive_ms) / 1000.0
        self.max_entries = min(int(max_entries), context.max_entries, MAX_REPORT_ENTRIES)
        self._entries: deque[BurstReport] = deque(maxlen=max(self.max_entries, 1))
        self._snrs: list[float] = []
        self._preambles = 0
        self._snr_last: float | None = None
        self._window_start: float | None = None
        self._last_report_t: float | None = None
        self._first_seen_t: float | None = None
        self._new_data = False
        self.session_id = (
            secrets.randbits(32) if session_id is None else int(session_id) & 0xFFFFFFFF
        )
        self._seq = 0
        self._data_ok = 0
        self._data_lost = 0
        self._last_data_seq: int | None = None
        self._last_mcs: int | None = None
        self._health: RxHealth | None = None
        self._fcs_fail_total: int | None = None  # baseline for "a PSDU failed"
        # Per-chunk flags, ORed over the report window.
        self._clipping_seen = False
        self._decode_backlog = False

    def _seen(self, now_s: float) -> None:
        if self._first_seen_t is None:
            self._first_seen_t = now_s
        if self._window_start is None:
            self._window_start = now_s

    def on_preambles(
        self, now_s: float, snr_db: Sequence[float], *, control_only: bool
    ) -> None:
        self._seen(now_s)
        values = [float(v) for v in snr_db if v is not None and math.isfinite(v)]
        self._preambles += len(snr_db)
        if values:
            self._snrs.extend(values)
            self._snr_last = values[-1]

    def on_burst(self, report: BurstReport) -> None:
        self._seen(report.t_s)
        self._data_ok += max(report.subframes_ok, 0)
        self._data_lost += max(report.subframes_lost, 0)
        if report.first_sequence is not None:
            self._last_data_seq = (report.first_sequence + max(report.subframes_ok, 1) - 1) % _U16
        if report.mcs_index is not None:
            self._last_mcs = report.mcs_index
        self._entries.append(report)
        self._new_data = True

    def on_rx_health(self, now_s: float, health: RxHealth) -> None:
        self._seen(now_s)
        self._health = health
        fcs_fail_total = int(health.fcs_fail_total)
        if self._fcs_fail_total is not None and fcs_fail_total != self._fcs_fail_total:
            self._new_data = True  # a burst we could not decode: report it like data
        self._fcs_fail_total = fcs_fail_total
        self._clipping_seen = self._clipping_seen or bool(health.clipping_seen)
        self._decode_backlog = self._decode_backlog or bool(health.decode_backlog)

    def next_due_s(self, now_s: float) -> float | None:
        reference = self._last_report_t
        if reference is None:
            if self._new_data:
                return now_s
            reference = self._first_seen_t if self._first_seen_t is not None else now_s
            return reference + self.keepalive_s
        if self._new_data:
            return reference + self.period_s
        return reference + self.keepalive_s

    def due(self, now_s: float) -> bool:
        if self._first_seen_t is None:
            self._first_seen_t = now_s
        due_at = self.next_due_s(now_s)
        return due_at is not None and now_s >= due_at - 1e-9

    def build(self, now_s: float) -> FeedbackReport:
        start = self._window_start if self._window_start is not None else now_s
        snrs = self._snrs
        health = self._health
        flags = 0
        if self._last_data_seq is not None:
            flags |= FLAG_LAST_SEQ_VALID
        if self._clipping_seen:
            flags |= FLAG_CLIPPING_SEEN
        if self._decode_backlog:
            flags |= FLAG_DECODE_BACKLOG
        entries = tuple(self._entries)[-self.max_entries :] if self.max_entries else ()
        report = FeedbackReport(
            version=FEEDBACK_VERSION,
            reporter_id=self.reporter_id,
            report_seq=self._seq,
            window_s=max(now_s - start, 0.0),
            age_s=0.0,
            preambles=self._preambles,
            snr_mean_db=sum(snrs) / len(snrs) if snrs else None,
            snr_p10_db=percentile(snrs, 0.10) if snrs else None,
            snr_last_db=self._snr_last if snrs else None,
            data_ok_total=self._data_ok % _U32,
            data_lost_total=self._data_lost % _U32,
            last_data_seq=self._last_data_seq,
            psdu_fcs_fail_total=(health.fcs_fail_total % _U16) if health else 0,
            header_fail_total=(health.header_fail_total % _U16) if health else 0,
            rx_overflow_total=(health.overflow_total % _U16) if health else 0,
            last_mcs_index=self._last_mcs,
            flags=flags,
            bursts=entries,
            session_id=self.session_id,
        )
        self._seq = (self._seq + 1) % _U16
        self._last_report_t = now_s
        self._window_start = now_s
        self._snrs = []
        self._preambles = 0
        self._snr_last = None
        self._entries.clear()
        self._new_data = False
        self._clipping_seen = False
        self._decode_backlog = False
        return report


__all__ = ["DefaultReporter", "percentile"]
