"""Fake link and report builders shared by the link adaptation tests."""

from __future__ import annotations

import heapq
import math
import random
from dataclasses import dataclass, field, replace

from ofdm_message_link.la.api import (
    BurstReport,
    FeedbackReport,
    LaContext,
    ReporterContext,
    RxHealth,
    SentBurst,
)
from ofdm_message_link.la.reporter import DefaultReporter
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


# -- burst-level duplex link, calibrated on the M3 hardware runs -------------------
#
# experiments/duplex_fdd/results/m3 (two PlutoSDRs, A->B at 4 Mb/s offered, TX gain
# -5 -> -20 -> -5 dB).  At -20 dB (preamble SNR 9.8 dB) the fixed-MCS runs lost 0 %
# of subframes at MCS 8, 4.1 % at 10, 25 % at 12 and 96 % at 15, and OLLA's own
# feedback showed 0.3-0.4 % at MCS 9.  A logistic per-burst failure (slope 2/dB)
# under a 0.5 dB slow fade reproduces those; 8, 13, 16 and 17 are placed from their
# neighbours.  Rates: fixed 8 carried 156 subframes/s and fixed 10 310/s (airtime
# bound, 234/s per bit/symbol); MCS 17 at -5 dB 540/s (offered load bound).  Bursts
# carried 3 DATA subframes on average and failed whole (31 FCS failures, 70 lost).
# The report SNR (window mean) scatters by 1.2 dB with a long low tail, and A's
# feedback lagged its bursts by about 0.1 s (B's decode pipeline): both modelled.
BURST_FAIL_CENTER_DB = {8: 5.0, 9: 6.73, 10: 8.0, 12: 9.14, 13: 10.6, 15: 11.61, 16: 16.1,
                        17: 17.1}
SUBFRAMES_PER_S_PER_SE = 234.0
SUBFRAMES_PER_S_CAP = 540.0
SUBFRAME_BYTES = 900


def burst_failure(snr_db: float, mcs_index: int) -> float:
    z = 2.0 * (snr_db - BURST_FAIL_CENTER_DB[mcs_index])
    return 0.0 if z > 50 else 1.0 / (1.0 + math.exp(z))


def subframes_per_s(mcs_index: int) -> float:
    entry = next(e for e in LADDER if e.index == mcs_index)
    return min(SUBFRAMES_PER_S_PER_SE * entry.spectral_efficiency, SUBFRAMES_PER_S_CAP)


@dataclass
class BurstLink:
    """Sender A runs ``la`` (or ``fixed_mcs``); receiver B runs the real DefaultReporter.

    Event driven: A decides the MCS of every burst, the burst goes on air
    ``tx_delay_s`` later and fails whole with :func:`burst_failure` of its true
    SNR; B sees it ``rx_latency_s`` after that, in ``chunk_s`` receive chunks,
    and counts the lost subframes only when a later burst gets through (the
    sequence gap, as ``link.py`` does).  A failed burst still shows its
    preamble SNR and counts in ``RxHealth.fcs_fail_total``.  B looks for a due
    report every ``b_tx_s`` (its own bursts carry the feedback) and A has it
    ``fb_delay_s`` later.  ``hide_fcs_failures`` keeps the failure counter at
    0, as a reporter that ignores it would see.
    """

    la: object
    snr_db: object  # callable: time (s) -> mean preamble SNR (dB)
    seed: int = 1
    fixed_mcs: int | None = None
    subframes_per_burst: int = 3
    tx_delay_s: float = 0.010
    rx_latency_s: float = 0.080
    chunk_s: float = 0.013
    b_tx_s: float = 0.008
    fb_delay_s: float = 0.008
    snr_noise_db: float = 1.5  # per preamble
    snr_outlier_prob: float = 0.002
    snr_outlier_db: float = 8.0
    fade_db: float = 0.5
    fade_tau_s: float = 0.3
    hide_fcs_failures: bool = False
    history: list = field(default_factory=list)  # (t_air, mcs, subframes, ok)

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self.reporter = DefaultReporter(ReporterContext(node_id=2, peer_node_id=1),
                                        session_id=7)
        self._fade = 0.0
        self._fade_t = 0.0
        self._next_seq = 0
        self._expected = None
        self._fcs_fail = 0
        self._decoded = []
        self._events = []
        self._order = 0

    def _at(self, t, handler, *args):
        self._order += 1
        heapq.heappush(self._events, (t, self._order, handler, args))

    def run(self, seconds: float):
        self._at(0.0, self._send)
        self._at(self.chunk_s, self._receive_chunk)
        self._at(0.0, self._peer_transmit)
        while self._events and self._events[0][0] <= seconds:
            t, _, handler, args = heapq.heappop(self._events)
            handler(t, *args)
        return self

    def _send(self, t):
        mcs = self.la.decide(t).mcs_index if self.fixed_mcs is None else self.fixed_mcs
        k = self.subframes_per_burst
        first, self._next_seq = self._next_seq, (self._next_seq + k) % 65536
        airtime = k / subframes_per_s(mcs)
        if self.fixed_mcs is None:
            self.la.on_sent(SentBurst(t_s=t, first_sequence=first, subframes=k, mcs_index=mcs,
                                      airtime_s=airtime, control_only=False, tx_gain_db=None))
        self._at(t + self.tx_delay_s, self._on_air, mcs, k, first)
        self._at(t + airtime, self._send)

    def _on_air(self, t, mcs, k, first):
        a = math.exp(-max(t - self._fade_t, 0.0) / self.fade_tau_s)
        self._fade = a * self._fade + math.sqrt(1.0 - a * a) * self.rng.gauss(0.0, self.fade_db)
        self._fade_t = t
        snr = self.snr_db(t) + self._fade
        ok = self.rng.random() >= burst_failure(snr, mcs)
        self.history.append((t, mcs, k, ok))
        measured = snr + self.rng.gauss(0.0, self.snr_noise_db)
        if self.rng.random() < self.snr_outlier_prob:
            measured -= self.snr_outlier_db
        self._at(t + self.rx_latency_s, self._on_decoded, (mcs, k, first, ok, measured))

    def _on_decoded(self, _t, burst):
        self._decoded.append(burst)

    def _receive_chunk(self, t):
        snrs, bursts = [], []
        for mcs, k, first, ok, measured in self._decoded:
            snrs.append(measured)
            if not ok:
                self._fcs_fail += 1
                continue
            lost = 0 if self._expected is None else (first - self._expected) % 65536
            self._expected = (first + k) % 65536
            bursts.append(BurstReport(t_s=t, first_sequence=first, mcs_index=mcs,
                                      snr_db=measured, subframes_ok=k, subframes_lost=lost,
                                      fcs_ok=True))
        self._decoded.clear()
        if snrs:
            self.reporter.on_preambles(t, snrs, control_only=not bursts)
        for burst in bursts:
            self.reporter.on_burst(burst)
        fcs_fail_total = 0 if self.hide_fcs_failures else self._fcs_fail
        self.reporter.on_rx_health(t, RxHealth(overflow_total=0, header_fail_total=0,
                                               fcs_fail_total=fcs_fail_total,
                                               clipping_seen=False, decode_backlog=False))
        self._at(t + self.chunk_s, self._receive_chunk)

    def _peer_transmit(self, t):
        if self.reporter.due(t):
            self._at(t + self.fb_delay_s, self._feedback, self.reporter.build(t))
        self._at(t + self.b_tx_s, self._peer_transmit)

    def _feedback(self, t, report):
        if self.fixed_mcs is None:
            self.la.on_feedback(replace(report, received_t_s=t))

    def window(self, t0: float, t1: float) -> tuple[float, int, float]:
        """Subframe loss, subframes lost and goodput (Mb/s) of the bursts aired in [t0, t1)."""

        rows = [row for row in self.history if t0 <= row[0] < t1]
        sent = sum(row[2] for row in rows)
        lost = sum(row[2] for row in rows if not row[3])
        goodput = (sent - lost) * SUBFRAME_BYTES * 8 / (t1 - t0) / 1e6
        return lost / max(sent, 1), lost, goodput
