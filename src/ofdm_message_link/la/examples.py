"""Example custom link adaptation: Auto Rate Fallback (ARF).

A complete algorithm in under a hundred lines, written against
:mod:`ofdm_message_link.la.api` only.  Copy it as a template.  Load it with
``--link-adaptation arf`` or, as any custom class would be loaded::

    --link-adaptation ofdm_message_link.la.examples:ArfLinkAdaptation \\
        --link-adaptation-arg up_after=5

ARF (Kamerman and Monteban, 1997) ignores the SNR.  It steps up one rung
after ``up_after`` lossless reports in a row and down one rung after
``down_after`` lossy ones; with ``probe_fail_down`` it steps down at once when
the first report after an up step is lossy (the probe failed).  Reports with
no DATA subframes (an idle peer) change nothing.
"""

from __future__ import annotations

from collections.abc import Mapping

from .api import Decision, FeedbackReport, LaContext, SentBurst, counter_delta, is_newer_seq


class ArfLinkAdaptation:
    """Auto Rate Fallback over the context's ladder."""

    def __init__(
        self,
        context: LaContext,
        up_after: int = 10,
        down_after: int = 2,
        probe_fail_down: bool = True,
    ) -> None:
        if int(up_after) < 1 or int(down_after) < 1:
            raise ValueError("up_after and down_after must be at least 1")
        self.ladder = tuple(entry.index for entry in context.ladder)
        if not self.ladder:
            raise ValueError("the MCS ladder is empty")
        self.up_after = int(up_after)
        self.down_after = int(down_after)
        self.probe_fail_down = bool(probe_fail_down)
        slower = [i for i, index in enumerate(self.ladder) if index <= context.initial_mcs_index]
        self.position = slower[-1] if slower else 0
        self.good_run = 0
        self.bad_run = 0
        self.probing = False  # True until the first data report after an up step
        self.last_seq: int | None = None
        self.last_totals: tuple[int, int] | None = None
        self.pending_reason = "init"

    def on_sent(self, burst: SentBurst) -> None:
        pass

    def on_feedback(self, report: FeedbackReport) -> None:
        if self.last_seq is not None and not is_newer_seq(report.report_seq, self.last_seq):
            return
        self.last_seq = report.report_seq
        totals = (report.data_ok_total, report.data_lost_total)
        previous, self.last_totals = self.last_totals, totals
        if previous is None:
            return  # the first report only sets the baseline of the cumulative counters
        ok = counter_delta(totals[0], previous[0])
        lost = counter_delta(totals[1], previous[1])
        if ok + lost == 0:
            return
        if lost:
            self.good_run = 0
            self.bad_run += 1
            if self.bad_run >= self.down_after or (self.probing and self.probe_fail_down):
                self._step(-1, "down")
        else:
            self.bad_run = 0
            self.good_run += 1
            if self.good_run >= self.up_after:
                self._step(+1, "up")
                self.probing = True
                return
        self.probing = False

    def _step(self, direction: int, reason: str) -> None:
        position = min(max(self.position + direction, 0), len(self.ladder) - 1)
        if position != self.position:
            self.position = position
            self.pending_reason = reason
        self.good_run = 0
        self.bad_run = 0

    def decide(self, now_s: float) -> Decision:
        reason, self.pending_reason = self.pending_reason, "hold"
        return Decision(self.ladder[self.position], reason=reason)

    def snapshot(self) -> Mapping[str, object]:
        return {
            "algorithm": "arf",
            "mcs_index": self.ladder[self.position],
            "good_run": self.good_run,
            "bad_run": self.bad_run,
            "probing": self.probing,
        }


__all__ = ["ArfLinkAdaptation"]
