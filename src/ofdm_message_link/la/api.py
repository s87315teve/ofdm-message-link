"""Link adaptation (LA) interfaces: what an algorithm sees and what it returns.

A node picks the MCS of every data burst it sends from what the peer reports
back.  Two small objects do the work, and both are replaceable without
touching the program:

* a :class:`LinkAdaptation` on the transmit side turns feedback into the next
  MCS (``--link-adaptation SPEC`` and ``--link-adaptation-arg key=value``);
* a :class:`FeedbackReporter` on the receive side decides what is measured and
  how often it is reported (``--feedback-reporter SPEC`` and
  ``--feedback-reporter-arg key=value``).

``SPEC`` is a built-in name (``fixed``, ``olla``, ``arf``; reporter
``default``), an importable ``package.module:Class`` or a file
``path/to/file.py:Class``.  Values in ``key=value`` are Python literals
(``5``, ``0.02``, ``True``, ``"text"``); anything else is passed as a string.

Data flow
---------
The peer's reporter receives one :class:`BurstReport` per DATA burst, the SNR
of every preamble and an :class:`RxHealth` per receive chunk.  When it says
:meth:`FeedbackReporter.due`, the node calls :meth:`FeedbackReporter.build`
and sends the :class:`FeedbackReport` back, piggybacked on its next burst or
in a small control-only burst.  The wire format carries a version field; the
codec is ``ofdm_message_link.control``.  On the transmit side the decoded
report goes to :meth:`LinkAdaptation.on_feedback`, each sent burst to
:meth:`LinkAdaptation.on_sent`, and :meth:`LinkAdaptation.decide` is asked
once per data burst for a :class:`Decision`.

Counters in a :class:`FeedbackReport` (``data_ok_total``, ``data_lost_total``,
...) are cumulative and wrap at 2**32, so a lost report loses no information:
difference two reports with :func:`counter_delta`.  ``report_seq`` wraps at
2**16.  When the peer restarts, both start again from 0, so "ignore a report
whose ``report_seq`` is not newer" alone would ignore the restarted peer for
up to 32767 reports.  :class:`FeedbackTracker` does the bookkeeping right
(duplicates, restarts, gaps) and returns the counter increases; use it.

Writing your own algorithm
--------------------------
A class with the four :class:`LinkAdaptation` methods and a constructor
``Class(context, **kwargs)`` is enough; no base class is needed::

    # my_la.py
    from ofdm_message_link.la.api import Decision, FeedbackTracker, LaContext

    class Threshold:
        '''Use the highest rung whose threshold the reported SNR clears.'''

        def __init__(self, context: LaContext, margin_db: float = 0.0):
            self.ladder = context.ladder                  # ascending PHY rate
            self.thresholds = context.thresholds_db       # MCS index -> dB
            self.margin_db = float(margin_db)
            self.mcs = context.initial_mcs_index
            self.feedback = FeedbackTracker()             # duplicates, peer restarts

        def on_sent(self, burst):                         # SentBurst, may be ignored
            pass

        def on_feedback(self, report):
            if self.feedback.update(report) is None:      # an old or repeated report
                return
            if report.snr_mean_db is None:
                return
            fits = [e.index for e in self.ladder
                    if self.thresholds[e.index] + self.margin_db <= report.snr_mean_db]
            self.mcs = fits[-1] if fits else self.ladder[0].index

        def decide(self, now_s):
            return Decision(self.mcs, reason="threshold")

        def snapshot(self):
            return {"mcs_index": self.mcs}

and run ``node_app --link-adaptation my_la.py:Threshold --link-adaptation-arg
margin_db=1.5``.  Rules:

* ``decide`` is called before every data burst: keep it cheap, and return an
  index from ``context.ladder`` (any transmit-visible v5 index is accepted;
  anything else is replaced by the nearest slower rung and logged).
* ``snapshot`` returns JSON-able values only; the GUI and ``--la-log`` show it.
* Calls never overlap (the controller holds one lock), so no locking is needed.
  Times are ``time.monotonic()`` seconds.
* Optional: a method ``on_mode(mode)`` is called with ``"fixed"`` when the
  operator fixes the MCS and ``"auto"`` when the algorithm gets it back.
  Feedback keeps flowing in fixed mode, but it describes the operator's
  MCS; the built-ins freeze their learning (OLLA's offset, ARF's runs).
* ``ofdm_message_link.la.examples.ArfLinkAdaptation`` is a complete small
  example; ``ofdm_message_link.la.olla.OllaLinkAdaptation`` is the default.

A custom reporter implements :class:`FeedbackReporter` the same way, is built
as ``Class(context: ReporterContext, **kwargs)``, and should set a nonzero
``reporter_id`` (0 is the default reporter) so the peer can tell its reports
apart.  Extra measurements travel in ``FeedbackReport.extras`` as TLVs with
type 0x80-0xFF.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ofdm_link.phy.mcs_table import McsTableEntry

FEEDBACK_VERSION = 1
"""Version of the FEEDBACK body this package produces and understands."""

DEFAULT_REPORTER_ID = 0
"""``reporter_id`` of :class:`ofdm_message_link.la.reporter.DefaultReporter`."""

MAX_REPORT_ENTRIES = 16
"""The wire format carries at most this many :class:`BurstReport` entries."""

# FeedbackReport.flags bits (fb_flags on the wire).
FLAG_LAST_SEQ_VALID = 0x01
FLAG_CLIPPING_SEEN = 0x02
FLAG_DECODE_BACKLOG = 0x04


@dataclass(frozen=True, slots=True)
class BurstReport:
    """One received DATA burst (RX side); also the compact wire entry.

    ``subframes_lost`` counts DATA sequence numbers that went missing up to
    and including this burst (missed bursts and bad subframes alike).
    ``snr_db`` is the preamble (LTF-pair) in-band SNR.
    """

    t_s: float
    first_sequence: int | None
    mcs_index: int | None
    snr_db: float | None
    subframes_ok: int
    subframes_lost: int
    fcs_ok: bool | None = None


@dataclass(frozen=True, slots=True)
class FeedbackReport:
    """What the peer's reporter measured, as decoded from the wire.

    ``*_total`` counters are cumulative (mod 2**32 for the data counters,
    2**16 for the others).  ``window_s`` is the measurement window the SNR
    statistics cover, ``age_s`` the time from the window end to encoding.
    ``received_t_s`` is set locally by the node that received the report.
    ``session_id`` (a TLV on the wire) is drawn at random each time the
    peer's reporter starts, so a peer restart is recognised at once even
    though ``report_seq`` and the counters start again from 0; None if the
    peer's reporter does not send one.
    """

    version: int
    reporter_id: int
    report_seq: int
    window_s: float
    age_s: float
    preambles: int
    snr_mean_db: float | None
    snr_p10_db: float | None
    snr_last_db: float | None
    data_ok_total: int
    data_lost_total: int
    last_data_seq: int | None
    psdu_fcs_fail_total: int
    header_fail_total: int
    rx_overflow_total: int
    last_mcs_index: int | None
    flags: int = 0
    bursts: tuple[BurstReport, ...] = ()
    extras: tuple[tuple[int, bytes], ...] = ()
    received_t_s: float | None = None
    session_id: int | None = None


@dataclass(frozen=True, slots=True)
class SentBurst:
    """Local record of one burst this node transmitted."""

    t_s: float
    first_sequence: int | None
    subframes: int
    mcs_index: int
    airtime_s: float
    control_only: bool
    tx_gain_db: float | None


@dataclass(frozen=True, slots=True)
class Decision:
    """The MCS for the next data burst and why it was chosen.

    ``reason`` is free text; the built-ins use ``"init"``, ``"up"``,
    ``"down"``, ``"stale"``, ``"hold"`` and ``"fixed"``.
    """

    mcs_index: int
    reason: str = ""
    snr_eff_db: float | None = None
    offset_db: float | None = None


@dataclass(frozen=True, slots=True)
class LaContext:
    """What a :class:`LinkAdaptation` is built with.

    ``ladder`` holds the allowed v5 entries in ascending PHY rate, with
    dominated entries already removed; ``thresholds_db`` maps their indices
    (at least) to the preamble SNR each needs for < 1 % loss.
    """

    ladder: tuple[McsTableEntry, ...]
    thresholds_db: Mapping[int, float]
    initial_mcs_index: int
    sample_rate: float
    node_id: int


@dataclass(frozen=True, slots=True)
class ReporterContext:
    """What a :class:`FeedbackReporter` is built with."""

    node_id: int
    peer_node_id: int
    max_entries: int = MAX_REPORT_ENTRIES


@dataclass(frozen=True, slots=True)
class RxHealth:
    """Receiver health counters, cumulative since the receiver started."""

    overflow_total: int
    header_fail_total: int
    fcs_fail_total: int
    clipping_seen: bool
    decode_backlog: bool


@runtime_checkable
class LinkAdaptation(Protocol):
    """Transmit-side algorithm: feedback in, MCS for the next burst out."""

    def on_sent(self, burst: SentBurst) -> None: ...

    def on_feedback(self, report: FeedbackReport) -> None: ...

    def decide(self, now_s: float) -> Decision:
        """Cheap; called once per data burst."""
        ...

    def snapshot(self) -> Mapping[str, object]:
        """JSON-able state for the GUI and the LA log."""
        ...


@runtime_checkable
class FeedbackReporter(Protocol):
    """Receive-side measurement: what to report and when."""

    reporter_id: int

    def on_preambles(
        self, now_s: float, snr_db: Sequence[float], *, control_only: bool
    ) -> None: ...

    def on_burst(self, report: BurstReport) -> None: ...

    def on_rx_health(self, now_s: float, health: RxHealth) -> None: ...

    def due(self, now_s: float) -> bool: ...

    def next_due_s(self, now_s: float) -> float | None:
        """Monotonic time the next report becomes due, or None if never."""
        ...

    def build(self, now_s: float) -> FeedbackReport:
        """Return the report and close the measurement window."""
        ...


def is_newer_seq(candidate: int, reference: int, *, bits: int = 16) -> bool:
    """True if ``candidate`` is newer than ``reference`` in serial arithmetic.

    Values within half the window ahead (mod 2**bits) are newer; equal values
    and those behind are not.
    """

    modulus = 1 << bits
    delta = (candidate - reference) % modulus
    return 0 < delta < modulus // 2


def counter_delta(current: int, previous: int, *, bits: int = 32) -> int:
    """Increase of a cumulative counter that wraps at 2**bits."""

    return (current - previous) % (1 << bits)


SEQ_REORDER_WINDOW = 64
"""A report at most this many ``report_seq`` values behind the last accepted
one is a late or repeated copy; further behind, with counters that went
backwards, it is a restarted peer."""

_COUNTER_BACKWARDS = 1 << 31  # a larger u32 "increase" is a decrease


@dataclass(frozen=True, slots=True)
class FeedbackDelta:
    """What one accepted report adds to the previous one.

    ``rebaselined`` is None for an ordinary report.  Otherwise the report only
    set a new baseline for the cumulative counters (``ok`` and ``lost`` are 0)
    and the value says why: ``"first"`` report, the peer's ``"session"``
    changed (it restarted), a ``"gap"`` longer than ``rebaseline_after_s``
    since the last accepted report, or the counters went ``"backwards"``.
    """

    ok: int = 0
    lost: int = 0
    overflow_changed: bool = False
    rebaselined: str | None = None


class FeedbackTracker:
    """Accept or ignore each feedback report and difference its counters.

    A report is ignored (``update`` returns None) only when it is a late or
    repeated copy of one already accepted: its ``report_seq`` is not newer and
    lies within :data:`SEQ_REORDER_WINDOW` behind, from the same session.  A
    restarted peer is recognised, and its first report becomes the new
    baseline, by any of:

    * a different ``session_id`` (the default reporter sends one);
    * a report arriving more than ``rebaseline_after_s`` after the last
      accepted one (a restart takes longer than that, and counters that piled
      up while no feedback arrived say nothing about the current MCS);
    * ``data_ok_total`` or ``data_lost_total`` going backwards.

    ``rebaseline_after_s=None`` disables the gap rule.  Times come from
    ``report.received_t_s``, else from ``now_s``.
    """

    def __init__(self, *, rebaseline_after_s: float | None = 0.5) -> None:
        self.rebaseline_after_s = rebaseline_after_s
        self.last_accepted_t: float | None = None
        self.ignored = 0
        self.rebaselines = 0
        self._seq: int | None = None
        self._session: int | None = None
        self._ok = 0
        self._lost = 0
        self._overflow = 0

    @property
    def started(self) -> bool:
        """True once a report has been accepted."""

        return self._seq is not None

    def update(self, report: FeedbackReport, now_s: float | None = None) -> FeedbackDelta | None:
        t = report.received_t_s if report.received_t_s is not None else now_s
        d_ok = counter_delta(report.data_ok_total, self._ok)
        d_lost = counter_delta(report.data_lost_total, self._lost)
        backwards = d_ok >= _COUNTER_BACKWARDS or d_lost >= _COUNTER_BACKWARDS
        reason: str | None = None
        if self._seq is None:
            reason = "first"
        elif report.session_id != self._session:
            reason = "session"
        elif (
            self.rebaseline_after_s is not None
            and t is not None
            and self.last_accepted_t is not None
            and t - self.last_accepted_t > self.rebaseline_after_s
        ):
            reason = "gap"
        elif not is_newer_seq(report.report_seq, self._seq):
            behind = (self._seq - report.report_seq) % (1 << 16)
            if not (backwards and behind > SEQ_REORDER_WINDOW):
                self.ignored += 1
                return None
            reason = "backwards"
        elif backwards:
            reason = "backwards"
        overflow_changed = (
            reason is None
            and counter_delta(report.rx_overflow_total, self._overflow, bits=16) != 0
        )
        self._seq = report.report_seq
        self._session = report.session_id
        self._ok, self._lost = report.data_ok_total, report.data_lost_total
        self._overflow = report.rx_overflow_total
        if t is not None:
            self.last_accepted_t = t
        if reason is not None:
            if reason != "first":
                self.rebaselines += 1
            return FeedbackDelta(rebaselined=reason)
        return FeedbackDelta(ok=d_ok, lost=d_lost, overflow_changed=overflow_changed)


__all__ = [
    "DEFAULT_REPORTER_ID",
    "FEEDBACK_VERSION",
    "FLAG_CLIPPING_SEEN",
    "FLAG_DECODE_BACKLOG",
    "FLAG_LAST_SEQ_VALID",
    "MAX_REPORT_ENTRIES",
    "BurstReport",
    "Decision",
    "SEQ_REORDER_WINDOW",
    "FeedbackDelta",
    "FeedbackReport",
    "FeedbackReporter",
    "FeedbackTracker",
    "LaContext",
    "LinkAdaptation",
    "ReporterContext",
    "RxHealth",
    "SentBurst",
    "counter_delta",
    "is_newer_seq",
]
