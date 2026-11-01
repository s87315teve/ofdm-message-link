"""Where link adaptation decisions meet the link layer.

:class:`LinkController` implements the three link-layer hooks of one node:

* ``BurstPolicy`` (TransmitWorker): the MCS of each data burst, the CONTROL
  subframe that leads every burst (a LINK_HEADER, or FEEDBACK when the
  reporter says one is due), and when an idle node must wake up to send a
  control-only burst;
* ``ControlHandler`` (MessageReceiver): decodes the peer's CONTROL subframe,
  drops the node's own bursts, and hands feedback to the algorithm;
* ``ReceiveObserver`` (ReceiveWorker): feeds preamble SNRs, DATA burst
  reports and receiver health to the reporter.

All calls are serialised under one lock, so algorithms and reporters need not
be thread-safe.  An exception from the algorithm, the reporter, the LA log
or ``on_event`` never escapes into the transmit or receive thread: it is
counted (``la_errors``, ``reporter_errors``, ``log_errors``,
``event_errors``; the last one in ``last_error``) and a safe default is used
(the current MCS, no report).  The wire codec is ``ofdm_message_link.control``; it is
reached through :class:`ControlCodec` so tests can replace it.
"""

from __future__ import annotations

import enum
import json
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, TextIO

from ofdm_link.phy.mcs_table import McsTableEntry, McsTableError, mcs_entry

from .api import (
    BurstReport,
    Decision,
    FeedbackReport,
    FeedbackReporter,
    FeedbackTracker,
    LinkAdaptation,
    RxHealth,
    SentBurst,
)
from .thresholds import rung_at_or_below

# -- WP-B types (link.py / tx_app.py, design section 3.5) -------------------
# Stand-ins with the same shape keep this module usable until the link-layer
# hooks are merged; once they exist the real classes are used.
try:
    from ..link import ControlContext, ControlVerdict
except ImportError:  # pragma: no cover - replaced by the link-layer merge

    @dataclass(frozen=True, slots=True)
    class ControlContext:  # type: ignore[no-redef]
        t_s: float
        mcs_index: int | None
        link_snr_db: float | None
        control_sequence: int

    class ControlVerdict(enum.Enum):  # type: ignore[no-redef]
        ACCEPT = 0
        DROP_BURST = 1


try:
    from ..tx_app import ControlPlan
except ImportError:  # pragma: no cover - replaced by the link-layer merge

    @dataclass(frozen=True, slots=True)
    class ControlPlan:  # type: ignore[no-redef]
        payloads: tuple[bytes, ...] = ()
        standalone: bool = False


# LCP header flags (design section 3.2).
FLAG_CONTROL_ONLY = 0x01
FLAG_LBT_MODE = 0x02
FLAG_TX_QUEUE_NONEMPTY = 0x04

STALE_AFTER_S = 0.5
"""Feedback older than this counts as stale in the statistics, unless the
algorithm has its own ``stale_after_s`` (then that one, so both agree)."""
ROBUST_FEEDBACK_S = 0.1
"""A report goes in a control-only burst at the control MCS if no burst at
MCS <= ``ROBUST_MAX_MCS`` carried feedback for this long."""
ROBUST_MAX_MCS = 9
IDLE_POLL_S = 0.02
"""Longest idle sleep of the transmit worker while a report may become due
(the reporter's ``period_s`` when it has one): a DATA burst received while
the worker sleeps makes a report due ``period_ms`` after the last one, and
nothing on the receive side can wake the worker earlier."""
PEER_LOSS_WINDOW_S = 10.0
FEEDBACK_EVENT_MIN_S = 0.1


@dataclass(frozen=True, slots=True)
class DecodedControl:
    """One CONTROL subframe as the controller needs it."""

    src_node: int
    dst_node: int
    flags: int
    feedback: FeedbackReport | None = None


class ControlCodec(Protocol):
    def encode_link_header(self, *, src_node: int, dst_node: int, flags: int) -> bytes: ...

    def encode_feedback(
        self, report: FeedbackReport, *, src_node: int, dst_node: int, flags: int
    ) -> bytes: ...

    def decode(self, payload: bytes) -> DecodedControl | None: ...


class _ControlModuleCodec:
    """Adapter onto ``ofdm_message_link.control`` (WP-B's LCP codec)."""

    def __init__(self) -> None:
        from .. import control

        self._c = control

    def _header(self, pdu_type: int, src_node: int, dst_node: int, flags: int):
        return self._c.LcpHeader(
            pdu_type=pdu_type, src_node=src_node, dst_node=dst_node, flags=flags
        )

    def encode_link_header(self, *, src_node: int, dst_node: int, flags: int) -> bytes:
        header = self._header(self._c.PDU_LINK_HEADER, src_node, dst_node, flags)
        return self._c.encode_lcp(header, b"")

    def encode_feedback(
        self, report: FeedbackReport, *, src_node: int, dst_node: int, flags: int
    ) -> bytes:
        header = self._header(self._c.PDU_FEEDBACK, src_node, dst_node, flags)
        return self._c.encode_lcp(header, self._c.encode_feedback(report))

    def decode(self, payload: bytes) -> DecodedControl | None:
        lcp = self._c.decode_lcp(payload)
        if lcp is None:
            return None
        header = lcp.header
        feedback = None
        if header.pdu_type == self._c.PDU_FEEDBACK:
            try:
                feedback = self._c.decode_feedback(lcp.body)
            except Exception:
                feedback = None
            if feedback is None:
                return None
        return DecodedControl(header.src_node, header.dst_node, header.flags, feedback)


def _allowed_entry(index: int) -> McsTableEntry | None:
    try:
        entry = mcs_entry(index)
    except McsTableError:
        return None
    return entry if entry.family == "v5" and entry.tx_visible else None


class LinkController:
    """BurstPolicy, ControlHandler and ReceiveObserver of one node."""

    def __init__(
        self,
        *,
        node_id: int,
        peer_id: int,
        la: LinkAdaptation,
        reporter: FeedbackReporter,
        ladder: Sequence[McsTableEntry],
        control_entry: McsTableEntry,
        mode: str = "auto",
        clock: Callable[[], float] = time.monotonic,
        la_log: TextIO | None = None,
        sample_rate: float | None = None,
        codec: ControlCodec | None = None,
        on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
        own_seed_range: tuple[int, int] | None = None,
    ) -> None:
        if mode not in {"auto", "fixed"}:
            raise ValueError("mode must be 'auto' or 'fixed'")
        if not ladder:
            raise ValueError("the MCS ladder is empty")
        self.node_id = int(node_id)
        self.peer_id = int(peer_id)
        # The scrambler seeds this node's transmitter uses
        # (link.node_seed_range); a MessageReceiver built with this
        # controller as its handler drops bursts with those seeds.
        self.own_seed_range = None if own_seed_range is None else tuple(own_seed_range)
        self._la = la
        self._reporter = reporter
        self._ladder = tuple(ladder)
        self._control_entry = control_entry
        self._mode = mode
        self._clock = clock
        self._la_log = la_log
        self._sample_rate = sample_rate
        self._codec = codec if codec is not None else _ControlModuleCodec()
        self._on_event = on_event
        self._lock = threading.RLock()
        self._log_lock = threading.Lock()

        self._fixed_entry: McsTableEntry = control_entry
        self._entry: McsTableEntry | None = None
        self._decision: Decision | None = None
        self._tx_gain_db: float | None = None
        self._last_feedback_t: float | None = None
        self._last_robust_t: float | None = None
        self._stale = False
        self._la_stale = False  # the algorithm's last decision said "stale"
        self._peer_counts: deque[tuple[float, int, int]] = deque()
        stale_after = getattr(la, "stale_after_s", STALE_AFTER_S)
        self._stale_after_s = (
            float(stale_after) if isinstance(stale_after, int | float) else STALE_AFTER_S
        )
        # The same acceptance rules as the built-in algorithms: a report the
        # LA ignores as old does not refresh the feedback age either.
        self._feedback = FeedbackTracker(rebaseline_after_s=self._stale_after_s)
        self._last_feedback_event_t: float | None = None
        self._counts = {
            "decisions": 0,
            "feedback_sent": 0,
            "feedback_received": 0,
            "feedback_ignored": 0,
            "peer_rebaselines": 0,
            "feedback_stale": 0,
            "control_bursts": 0,
            "own_bursts_heard": 0,
            "control_undecodable": 0,
            "control_other_dst": 0,
            "invalid_decisions": 0,
            "la_errors": 0,
            "reporter_errors": 0,
            "log_errors": 0,
            "event_errors": 0,
        }
        self._last_error: str | None = None
        if self._mode == "fixed":
            self._tell_mode()

    # -- helpers ------------------------------------------------------------

    def _failed(self, counter: str, what: str, error: BaseException) -> None:
        self._counts[counter] += 1
        self._last_error = f"{what}: {type(error).__name__}: {error}"

    def _guard(self, counter: str, what: str, fallback, method, *args, **kwargs):
        """Call a plug-in method; on an exception count it and return ``fallback``."""

        try:
            return method(*args, **kwargs)
        except Exception as error:
            self._failed(counter, what, error)
            return fallback

    def _emit(self, events: list[tuple[str, dict[str, Any]]], notify: bool = True) -> None:
        """Write events to the LA log; pass "la" and (if ``notify``) "feedback" on."""

        for kind, record in events:
            if self._la_log is not None:
                try:
                    line = json.dumps({"event": kind, **record}, default=str) + "\n"
                    with self._log_lock:  # TX and RX threads both log; keep lines whole
                        self._la_log.write(line)
                        self._la_log.flush()
                except Exception as error:  # a full disk must not stop the link
                    self._failed("log_errors", "la_log", error)
            if self._on_event is not None and (
                kind == "la" or (kind == "feedback" and notify)
            ):
                try:
                    self._on_event(kind, record)
                except Exception as error:
                    self._failed("event_errors", "on_event", error)

    def _check_stale(self, now: float) -> None:
        stale = (
            self._last_feedback_t is None
            or now - self._last_feedback_t > self._stale_after_s
            or (self._mode == "auto" and self._la_stale)  # e.g. a dead forward link
        )
        if stale and not self._stale and self._last_feedback_t is not None:
            self._counts["feedback_stale"] += 1
        self._stale = stale

    def _entry_for(self, decision: Decision, events: list) -> McsTableEntry:
        entry = _allowed_entry(decision.mcs_index)
        if entry is None:
            entry = rung_at_or_below(self._ladder, decision.mcs_index)
            self._counts["invalid_decisions"] += 1
            events.append(
                ("invalid", {"t": self._clock(), "mcs_index": decision.mcs_index,
                             "used": entry.index})
            )
        return entry

    def _change_to(self, entry: McsTableEntry, now: float, decision: Decision,
                   events: list) -> None:
        if self._entry is not None and entry.index == self._entry.index:
            return
        previous = None if self._entry is None else self._entry.index
        self._entry = entry
        self._counts["decisions"] += 1
        events.append(
            ("la", {
                "t": now, "mode": self._mode, "mcs_index": entry.index, "previous": previous,
                "reason": decision.reason, "snr_eff_db": decision.snr_eff_db,
                "offset_db": decision.offset_db,
            })
        )

    # -- BurstPolicy ----------------------------------------------------------

    def entry_for_next_burst(self, now: float) -> McsTableEntry:
        events: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            if self._mode == "fixed":
                decision = Decision(self._fixed_entry.index, reason="fixed")
                entry = self._fixed_entry
            else:
                decision = self._guard("la_errors", "decide", None, self._la.decide, now)
                if isinstance(decision, Decision):
                    entry = self._entry_for(decision, events)
                else:  # the algorithm failed: keep the current MCS
                    if decision is not None:
                        self._failed("la_errors", "decide",
                                     TypeError(f"returned {type(decision).__name__}"))
                    entry = self._entry if self._entry is not None else self._control_entry
                    decision = Decision(entry.index, reason="error")
            self._decision = decision
            self._la_stale = self._mode == "auto" and decision.reason == "stale"
            self._check_stale(now)
            self._change_to(entry, now, decision, events)
        self._emit(events)
        return entry

    def control_plan(self, now: float, *, idle: bool) -> ControlPlan:
        events: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            due = self._guard("reporter_errors", "due", False, self._reporter.due, now)
            report = (
                self._guard("reporter_errors", "build", None, self._reporter.build, now)
                if due
                else None
            )
            if report is None:
                if idle:
                    return ControlPlan()
                payload = self._codec.encode_link_header(
                    src_node=self.node_id, dst_node=self.peer_id, flags=FLAG_TX_QUEUE_NONEMPTY
                )
                return ControlPlan(payloads=(payload,))
            data_entry = self._entry if self._entry is not None else self._control_entry
            robust_overdue = (
                self._last_robust_t is None or now - self._last_robust_t >= ROBUST_FEEDBACK_S
            )
            standalone = (
                not idle and data_entry.index > ROBUST_MAX_MCS and robust_overdue
            )
            control_only = idle or standalone
            flags = FLAG_CONTROL_ONLY if control_only else FLAG_TX_QUEUE_NONEMPTY
            try:
                payload = self._codec.encode_feedback(
                    report, src_node=self.node_id, dst_node=self.peer_id, flags=flags
                )
            except Exception as error:  # e.g. a custom reporter's TLVs do not fit
                self._failed("reporter_errors", "encode_feedback", error)
                return ControlPlan()
            carrier = self._control_entry if control_only else data_entry
            if carrier.index <= ROBUST_MAX_MCS:
                self._last_robust_t = now
            self._counts["feedback_sent"] += 1
            events.append(
                ("report", {"t": now, "seq": report.report_seq, "snr_mean_db": report.snr_mean_db,
                            "preambles": report.preambles, "ok": report.data_ok_total,
                            "lost": report.data_lost_total, "control_only": control_only})
            )
        self._emit(events)
        return ControlPlan(payloads=(payload,), standalone=standalone)

    def control_entry(self) -> McsTableEntry:
        return self._control_entry

    def idle_wait_s(self, now: float) -> float | None:
        with self._lock:
            due_at = self._guard("reporter_errors", "next_due_s", None,
                                 self._reporter.next_due_s, now)
            period = getattr(self._reporter, "period_s", IDLE_POLL_S)
        if due_at is None:
            return None
        if not isinstance(period, int | float) or not period > 0:
            period = IDLE_POLL_S
        # Wake at least once per reporter period: data that arrives meanwhile
        # can make a report due sooner than next_due_s said.
        return min(max(0.0, float(due_at) - now), float(period))

    def on_sent(self, burst: Any, now: float) -> None:
        sequences = tuple(getattr(burst, "sequences", ()) or ())
        datagrams = tuple(getattr(burst, "datagrams", ()) or ())
        samples = getattr(burst, "sample_count", None)
        airtime = (
            float(samples) / self._sample_rate
            if samples is not None and self._sample_rate
            else 0.0
        )
        with self._lock:
            record = SentBurst(
                t_s=now,
                first_sequence=sequences[0] if sequences else None,
                subframes=len(datagrams),
                mcs_index=int(burst.mcs_index),
                airtime_s=airtime,
                control_only=not datagrams,
                tx_gain_db=self._tx_gain_db,
            )
            if record.control_only:
                self._counts["control_bursts"] += 1
            self._guard("la_errors", "on_sent", None, self._la.on_sent, record)

    # -- ControlHandler ---------------------------------------------------------

    def on_control(self, payload: bytes, context: ControlContext) -> ControlVerdict:
        events: list[tuple[str, dict[str, Any]]] = []
        notify = False
        with self._lock:
            try:
                decoded = self._codec.decode(bytes(payload))
            except Exception:
                decoded = None
            if decoded is None:
                self._counts["control_undecodable"] += 1
                return ControlVerdict.ACCEPT
            if decoded.src_node == self.node_id:
                self._counts["own_bursts_heard"] += 1
                return ControlVerdict.DROP_BURST
            if decoded.dst_node not in (0, self.node_id):
                self._counts["control_other_dst"] += 1
                return ControlVerdict.ACCEPT
            if decoded.feedback is not None:
                now = self._clock()
                report = replace(decoded.feedback, received_t_s=now)
                self._counts["feedback_received"] += 1
                delta = self._feedback.update(report, now)
                if delta is None:
                    self._counts["feedback_ignored"] += 1
                else:
                    self._last_feedback_t = now
                    self._check_stale(now)
                    if delta.rebaselined is None:
                        self._peer_counts.append((now, delta.ok, delta.lost))
                    elif delta.rebaselined != "first":
                        self._counts["peer_rebaselines"] += 1
                    while (
                        self._peer_counts and now - self._peer_counts[0][0] > PEER_LOSS_WINDOW_S
                    ):
                        self._peer_counts.popleft()
                self._guard("la_errors", "on_feedback", None, self._la.on_feedback, report)
                notify = (
                    self._last_feedback_event_t is None
                    or now - self._last_feedback_event_t >= FEEDBACK_EVENT_MIN_S
                )
                if notify:
                    self._last_feedback_event_t = now
                if notify or self._la_log is not None:
                    events.append(("feedback", self._feedback_record(report, now)))
        self._emit(events, notify=notify)
        return ControlVerdict.ACCEPT

    def _la_snapshot(self) -> Mapping[str, Any]:
        snapshot = self._guard("la_errors", "snapshot", {}, self._la.snapshot)
        return snapshot if isinstance(snapshot, Mapping) else {}

    def _feedback_record(self, report: FeedbackReport, now: float) -> dict[str, Any]:
        snapshot = self._la_snapshot()
        return {
            "t": now, "seq": report.report_seq, "snr_mean_db": report.snr_mean_db,
            "ok": report.data_ok_total, "lost": report.data_lost_total,
            "overflow": report.rx_overflow_total,
            "snr_f_db": snapshot.get("snr_f_db"), "offset_db": snapshot.get("offset_db"),
        }

    # -- ReceiveObserver ----------------------------------------------------------

    def on_receive(
        self,
        now: float,
        *,
        snrs: tuple[float, ...],
        bursts: tuple[BurstReport, ...],
        health: RxHealth,
    ) -> None:
        with self._lock:
            reporter = self._reporter
            if snrs:
                # A chunk with preambles but no DATA burst held control-only bursts.
                self._guard("reporter_errors", "on_preambles", None, reporter.on_preambles,
                            now, snrs, control_only=not bursts)
            for burst in bursts:
                self._guard("reporter_errors", "on_burst", None, reporter.on_burst, burst)
            self._guard("reporter_errors", "on_rx_health", None, reporter.on_rx_health,
                        now, health)

    # -- operator controls ----------------------------------------------------------

    @property
    def mode(self) -> str:
        return self._mode

    def set_fixed(self, entry: McsTableEntry | None) -> None:
        """Fix the MCS at ``entry`` (fixed mode), or return to the LA with None."""

        events: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            now = self._clock()
            if entry is None:
                self._mode = "auto"
                events.append(("mode", {"t": now, "mode": "auto"}))
            else:
                if _allowed_entry(entry.index) is None:
                    raise ValueError(f"MCS {entry.index} is not a transmit-visible v5 entry")
                self._mode = "fixed"
                self._fixed_entry = entry
                events.append(("mode", {"t": now, "mode": "fixed", "mcs_index": entry.index}))
            self._tell_mode()
        self._emit(events)

    def _tell_mode(self) -> None:
        """Pass the mode to an algorithm that wants it (optional ``on_mode``).

        In fixed mode the feedback describes the operator's MCS, not the
        algorithm's; the built-in algorithms freeze their learning then.
        """

        on_mode = getattr(self._la, "on_mode", None)
        if callable(on_mode):
            self._guard("la_errors", "on_mode", None, on_mode, self._mode)

    def set_la_mode(self, mode: str) -> None:
        """``"auto"`` hands the MCS to the LA; ``"fixed"`` freezes the current MCS."""

        if mode not in {"auto", "fixed"}:
            raise ValueError("mode must be 'auto' or 'fixed'")
        with self._lock:
            if mode == "fixed":
                current = self._entry if self._entry is not None else self._fixed_entry
            else:
                current = None
        self.set_fixed(current)

    def set_tx_gain(self, gain_db: float | None) -> None:
        with self._lock:
            self._tx_gain_db = gain_db

    def mark(self, text: str) -> None:
        self._emit([("mark", {"t": self._clock(), "text": str(text)})])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            self._check_stale(now)
            la_state = dict(self._la_snapshot())
            ok = sum(row[1] for row in self._peer_counts if now - row[0] <= PEER_LOSS_WINDOW_S)
            lost = sum(row[2] for row in self._peer_counts if now - row[0] <= PEER_LOSS_WINDOW_S)
            decision = self._decision
            return {
                "mode": self._mode,
                "mcs_index": None if self._entry is None else self._entry.index,
                "reason": None if decision is None else decision.reason,
                "snr_f_db": la_state.get("snr_f_db"),
                "offset_db": la_state.get("offset_db"),
                "snr_eff_db": None if decision is None else decision.snr_eff_db,
                "feedback_age_s": (
                    None if self._last_feedback_t is None else now - self._last_feedback_t
                ),
                "stale": self._stale,
                "peer_loss_10s": lost / (ok + lost) if ok + lost else None,
                "algorithm": la_state,
                "last_error": self._last_error,
                **self._counts,
            }


__all__ = [
    "FLAG_CONTROL_ONLY",
    "FLAG_LBT_MODE",
    "FLAG_TX_QUEUE_NONEMPTY",
    "ControlCodec",
    "DecodedControl",
    "LinkController",
]
