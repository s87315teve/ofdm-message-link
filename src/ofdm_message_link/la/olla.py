"""Default link adaptation: LTE-style inner and outer loop (``--link-adaptation olla``).

Inner loop: the filtered peer SNR plus an offset picks the highest ladder
rung whose threshold it clears, with hysteresis (going up needs the new
rung's threshold + ``hysteresis_up_db`` for ``up_hold_reports`` reports in a
row and ``min_dwell_s`` since the last change; going down is immediate).

Outer loop (OLLA): the offset moves by ``+step_up_db`` per DATA subframe the
peer received and ``-step_down_db`` per subframe it lost.  The offset stops
drifting when ``step_up * ok = step_down * lost``, so the loss settles at
``step_up / (step_up + step_down)``, which the default
``step_up = step_down * t / (1 - t)`` makes the target ``t`` (1 %: there is
no HARQ to recover losses, unlike LTE's 10 %).  A per-report clamp bounds
the reaction to one report, because the subframes of one aggregate fail
together.

The ladder offers discrete rates, so the realised loss reaches the target
only where the SNR sits near a threshold.  Elsewhere the best rung loses
less than the target, the offset rises to ``offset_max_db`` and stays there.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping

from ofdm_link.phy.mcs_table import McsTableEntry

from .api import (
    Decision,
    FeedbackDelta,
    FeedbackReport,
    FeedbackTracker,
    LaContext,
    SentBurst,
)
from .thresholds import build_ladder, load_thresholds, rung_at_or_below


class OllaLinkAdaptation:
    """Threshold table + hysteresis (inner loop) and loss-driven offset (outer)."""

    def __init__(
        self,
        context: LaContext,
        *,
        target_loss: float = 0.01,
        step_down_db: float = 0.5,
        step_up_db: float | None = None,
        max_report_down_db: float = 2.0,
        max_report_up_db: float = 0.5,
        offset_min_db: float = -10.0,
        offset_max_db: float = 3.0,
        initial_offset_db: float = 0.0,
        snr_tau_s: float = 0.10,
        hysteresis_up_db: float = 1.0,
        hysteresis_down_db: float = 0.0,
        up_hold_reports: int = 2,
        min_dwell_s: float = 0.10,
        stale_after_s: float = 0.5,
        stale_step_s: float = 0.5,
        fallback_mcs: int = 8,
        skip_outer_on_overflow: bool = True,
        outer_loop: bool = True,
        thresholds: str | None = None,
    ) -> None:
        if not 0.0 < target_loss < 1.0:
            raise ValueError("target_loss must be in (0, 1)")
        if offset_min_db > offset_max_db:
            raise ValueError("offset_min_db must not exceed offset_max_db")
        if stale_step_s <= 0:
            raise ValueError("stale_step_s must be positive")
        if thresholds is not None:
            self._thresholds: Mapping[int, float] = load_thresholds(thresholds)
            self._ladder: tuple[McsTableEntry, ...] = build_ladder(self._thresholds)
        else:
            self._thresholds = context.thresholds_db
            self._ladder = tuple(context.ladder)
        if not self._ladder:
            raise ValueError("the MCS ladder is empty: no v5 entry has a threshold")
        self.target_loss = float(target_loss)
        self.step_down_db = float(step_down_db)
        self.step_up_db = (
            self.step_down_db * self.target_loss / (1.0 - self.target_loss)
            if step_up_db is None
            else float(step_up_db)
        )
        self.max_report_down_db = float(max_report_down_db)
        self.max_report_up_db = float(max_report_up_db)
        self.offset_min_db = float(offset_min_db)
        self.offset_max_db = float(offset_max_db)
        self.snr_tau_s = float(snr_tau_s)
        self.hysteresis_up_db = float(hysteresis_up_db)
        self.hysteresis_down_db = float(hysteresis_down_db)
        self.up_hold_reports = int(up_hold_reports)
        self.min_dwell_s = float(min_dwell_s)
        self.stale_after_s = float(stale_after_s)
        self.stale_step_s = float(stale_step_s)
        self.skip_outer_on_overflow = bool(skip_outer_on_overflow)
        self.outer_loop = bool(outer_loop)
        self._floor = self._position(rung_at_or_below(self._ladder, fallback_mcs).index)

        self._cur = self._position(
            rung_at_or_below(self._ladder, context.initial_mcs_index).index
        )
        self._offset = min(max(float(initial_offset_db), self.offset_min_db), self.offset_max_db)
        self._snr_f: float | None = None
        self._snr_t: float | None = None
        self._snr_eff: float | None = None
        self._up_cand: int | None = None
        self._up_count = 0
        # Duplicates, peer restarts (session id, feedback gap, counters going
        # backwards) and the counter differences.
        self._feedback = FeedbackTracker(rebaseline_after_s=self.stale_after_s)
        self._started_t: float | None = None
        self._last_feedback_t: float | None = None
        self._next_stale_t: float | None = None
        self._last_change_t: float | None = None
        self._reason = "init"
        self._now: float | None = None
        self._counts = {
            "reports": 0,
            "ignored_reports": 0,
            "peer_rebaselines": 0,
            "overflow_skips": 0,
            "changes": 0,
            "subframes_ok": 0,
            "subframes_lost": 0,
        }

    # -- helpers --------------------------------------------------------

    def _position(self, mcs_index: int) -> int:
        for position, entry in enumerate(self._ladder):
            if entry.index == mcs_index:
                return position
        raise ValueError(f"MCS {mcs_index} is not on the ladder")

    def _threshold(self, position: int) -> float:
        return float(self._thresholds[self._ladder[position].index])

    def _change(self, position: int, now_s: float) -> None:
        self._cur = position
        self._last_change_t = now_s
        self._counts["changes"] += 1
        self._up_count = 0
        self._up_cand = None

    @property
    def ladder(self) -> tuple[McsTableEntry, ...]:
        return self._ladder

    @property
    def offset_db(self) -> float:
        return self._offset

    @property
    def mcs_index(self) -> int:
        return self._ladder[self._cur].index

    # -- LinkAdaptation ---------------------------------------------------

    def on_sent(self, burst: SentBurst) -> None:
        pass

    def on_feedback(self, report: FeedbackReport) -> None:
        t = report.received_t_s
        if t is None:
            t = self._now if self._now is not None else time.monotonic()
        delta = self._feedback.update(report, t)
        if delta is None:
            self._counts["ignored_reports"] += 1
            return
        self._counts["reports"] += 1
        if delta.rebaselined not in (None, "first"):
            self._counts["peer_rebaselines"] += 1
        self._last_feedback_t = t
        self._next_stale_t = None

        if report.preambles > 0 and report.snr_mean_db is not None:
            if self._snr_f is None or self._snr_t is None or self.snr_tau_s <= 0:
                self._snr_f = float(report.snr_mean_db)
            else:
                weight = 1.0 - math.exp(-max(t - self._snr_t, 0.0) / self.snr_tau_s)
                self._snr_f += weight * (float(report.snr_mean_db) - self._snr_f)
            self._snr_t = t

        self._outer_update(delta)

        if self._snr_f is None:
            return
        self._snr_eff = self._snr_f + self._offset
        cand = None
        for position in range(len(self._ladder) - 1, self._cur, -1):
            if self._threshold(position) + self.hysteresis_up_db <= self._snr_eff:
                cand = position
                break
        self._up_cand = cand
        self._up_count = self._up_count + 1 if cand is not None else 0

    def _outer_update(self, delta: FeedbackDelta) -> None:
        if delta.rebaselined is not None:
            return  # first report, or the peer restarted: only a new baseline
        d_ok, d_lost = delta.ok, delta.lost
        self._counts["subframes_ok"] += d_ok
        self._counts["subframes_lost"] += d_lost
        if d_ok + d_lost == 0 or not self.outer_loop:
            return
        if self.skip_outer_on_overflow and delta.overflow_changed:
            self._counts["overflow_skips"] += 1
            return
        step = self.step_up_db * d_ok - self.step_down_db * d_lost
        step = min(max(step, -self.max_report_down_db), self.max_report_up_db)
        self._offset = min(max(self._offset + step, self.offset_min_db), self.offset_max_db)

    def decide(self, now_s: float) -> Decision:
        self._now = now_s
        reason = "hold"
        if self._started_t is None:
            self._started_t = now_s
            self._last_change_t = now_s
            reason = "init"
        reference = self._last_feedback_t if self._last_feedback_t is not None else self._started_t
        if now_s - reference > self.stale_after_s:
            # One rung down per stale_step_s without feedback, not below the floor.
            reason = "stale"
            if self._next_stale_t is None:
                self._next_stale_t = reference + self.stale_after_s
            while self._next_stale_t <= now_s:
                self._next_stale_t += self.stale_step_s
                if self._cur > self._floor:
                    self._change(self._cur - 1, now_s)
        elif self._snr_eff is not None and (
            self._snr_eff < self._threshold(self._cur) - self.hysteresis_down_db
        ):
            target = 0
            for position in range(self._cur - 1, -1, -1):
                if self._threshold(position) <= self._snr_eff:
                    target = position
                    break
            if target != self._cur:
                self._change(target, now_s)
                reason = "down"
        elif (
            self._up_cand is not None
            and self._up_count >= self.up_hold_reports
            and now_s - (self._last_change_t or 0.0) >= self.min_dwell_s
        ):
            self._change(self._up_cand, now_s)
            reason = "up"
        self._reason = reason
        return Decision(self.mcs_index, reason, self._snr_eff, self._offset)

    def snapshot(self) -> Mapping[str, object]:
        def rounded(value: float | None) -> float | None:
            return None if value is None else round(value, 3)

        return {
            "algorithm": "olla",
            "mcs_index": self.mcs_index,
            "reason": self._reason,
            "snr_f_db": rounded(self._snr_f),
            "offset_db": rounded(self._offset),
            "snr_eff_db": rounded(self._snr_eff),
            "up_count": self._up_count,
            "ladder": [entry.index for entry in self._ladder],
            **self._counts,
        }


__all__ = ["OllaLinkAdaptation"]
