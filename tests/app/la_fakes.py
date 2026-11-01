"""Fake link and report builders shared by the link adaptation tests."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from ofdm_message_link.la.api import FeedbackReport, LaContext
from ofdm_message_link.la.thresholds import build_ladder, load_thresholds

TABLE = load_thresholds()
LADDER = build_ladder(TABLE)
K_PER_DB = 2.0
X50 = -math.log(99.0) / K_PER_DB  # p = 1 % at x = 0


def context(initial=8, thresholds=None, ladder=None) -> LaContext:
    table = TABLE if thresholds is None else thresholds
    return LaContext(
        ladder=build_ladder(table) if ladder is None else ladder,
        thresholds_db=table,
        initial_mcs_index=initial,
        sample_rate=5e6,
        node_id=1,
    )


def report(seq, t, *, snr=15.0, ok=0, lost=0, overflow=0, preambles=5, **extra):
    values = dict(
        version=1, reporter_id=0, report_seq=seq % 65536, window_s=0.02, age_s=0.0,
        preambles=preambles, snr_mean_db=snr, snr_p10_db=snr, snr_last_db=snr,
        data_ok_total=ok % 2**32, data_lost_total=lost % 2**32, last_data_seq=None,
        psdu_fcs_fail_total=0, header_fail_total=0, rx_overflow_total=overflow % 65536,
        last_mcs_index=None, received_t_s=t,
    )
    values.update(extra)
    return FeedbackReport(**values)


def loss_probability(snr_db: float, mcs_index: int, bias_db: float = 0.0) -> float:
    """Logistic loss: 1 % at the true threshold ``T_m - 1 + bias``."""

    x = snr_db - (TABLE[mcs_index] - 1.0 + bias_db)
    z = K_PER_DB * (x - X50)
    if z > 50:
        return 0.0
    return 1.0 / (1.0 + math.exp(z))


@dataclass
class FakeLink:
    """One feedback report per ``period_s``, ``subframes`` DATA subframes each."""

    la: object
    snr_db: float | callable
    seed: int = 1
    period_s: float = 0.02
    subframes: int = 40
    bias_db: float = 0.0
    snr_noise_db: float = 0.3
    t: float = 0.0
    seq: int = 0
    ok_total: int = 0
    lost_total: int = 0
    history: list = field(default_factory=list)  # (t, mcs, ok, lost, offset)

    def __post_init__(self):
        self.rng = random.Random(self.seed)

    def snr_at(self, t):
        return self.snr_db(t) if callable(self.snr_db) else self.snr_db

    def run(self, seconds: float):
        steps = round(seconds / self.period_s)
        for _ in range(steps):
            decision = self.la.decide(self.t)
            snr = self.snr_at(self.t)
            p = loss_probability(snr, decision.mcs_index, self.bias_db)
            lost = sum(self.rng.random() < p for _ in range(self.subframes))
            ok = self.subframes - lost
            self.ok_total += ok
            self.lost_total += lost
            self.t += self.period_s
            self.seq += 1
            measured = snr + self.rng.gauss(0.0, self.snr_noise_db)
            self.la.on_feedback(
                report(self.seq, self.t, snr=measured, ok=self.ok_total, lost=self.lost_total)
            )
            self.history.append((self.t, decision.mcs_index, ok, lost, self.la.offset_db))
        return self

    def window(self, start_s):
        rows = [row for row in self.history if row[0] > start_s]
        ok = sum(r[2] for r in rows)
        lost = sum(r[3] for r in rows)
        return rows, lost / max(ok + lost, 1)
