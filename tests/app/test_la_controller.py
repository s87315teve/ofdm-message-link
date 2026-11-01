import io
import json
from dataclasses import dataclass

import numpy as np
import pytest
from la_fakes import LADDER, context, report

from ofdm_link.phy.mcs_table import mcs_entry
from ofdm_message_link.la.api import BurstReport, Decision, ReporterContext, RxHealth
from ofdm_message_link.la.controller import (
    FLAG_CONTROL_ONLY,
    FLAG_TX_QUEUE_NONEMPTY,
    ControlContext,
    ControlVerdict,
    DecodedControl,
    LinkController,
)
from ofdm_message_link.la.olla import OllaLinkAdaptation
from ofdm_message_link.la.reporter import DefaultReporter

HEALTH = RxHealth(0, 0, 0, clipping_seen=False, decode_backlog=False)
CONTEXT = ControlContext(t_s=0.0, mcs_index=8, link_snr_db=15.0, control_sequence=0)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeLa:
    def __init__(self):
        self.mcs = 12
        self.reason = "init"
        self.feedback = []
        self.sent = []

    def on_sent(self, burst):
        self.sent.append(burst)

    def on_feedback(self, report):
        self.feedback.append(report)

    def decide(self, now_s):
        return Decision(self.mcs, self.reason, snr_eff_db=15.0, offset_db=0.5)

    def snapshot(self):
        return {"snr_f_db": 14.5, "offset_db": 0.5}


class FakeReporter:
    reporter_id = 7

    def __init__(self):
        self.is_due = False
        self.due_at = None
        self.calls = []
        self.built = 0

    def on_preambles(self, now_s, snr_db, *, control_only):
        self.calls.append(("preambles", now_s, tuple(snr_db), control_only))

    def on_burst(self, report):
        self.calls.append(("burst", report))

    def on_rx_health(self, now_s, health):
        self.calls.append(("health", now_s, health))

    def due(self, now_s):
        return self.is_due

    def next_due_s(self, now_s):
        return self.due_at

    def build(self, now_s):
        self.built += 1
        return report(self.built, None, ok=10 * self.built)


class FakeCodec:
    """Encodes to Python reprs so tests can read what was sent."""

    def __init__(self):
        self.inbox = {}

    def encode_link_header(self, *, src_node, dst_node, flags):
        return json.dumps(["H", src_node, dst_node, flags]).encode()

    def encode_feedback(self, report, *, src_node, dst_node, flags):
        key = len(self.inbox)
        self.inbox[key] = report
        return json.dumps(["F", src_node, dst_node, flags, key]).encode()

    def decode(self, payload):
        try:
            item = json.loads(payload)
        except ValueError:
            return None
        feedback = self.inbox[item[4]] if item[0] == "F" else None
        return DecodedControl(item[1], item[2], item[3], feedback)


def _controller(**kw):
    clock = Clock()
    la = FakeLa()
    reporter = FakeReporter()
    codec = FakeCodec()
    log = io.StringIO()
    events = []
    values = dict(
        node_id=1, peer_id=2, la=la, reporter=reporter, ladder=LADDER,
        control_entry=mcs_entry(8), clock=clock, la_log=log, sample_rate=5e6,
        codec=codec, on_event=lambda kind, record: events.append((kind, record)),
    )
    values.update(kw)
    return LinkController(**values), clock, la, reporter, codec, log, events


def _log_lines(log):
    return [json.loads(line) for line in log.getvalue().splitlines()]


def _peer_feedback(codec, seq, **kw):
    return codec.encode_feedback(report(seq, None, **kw), src_node=2, dst_node=1, flags=0)


# -- ControlHandler ---------------------------------------------------------


def test_own_burst_is_dropped_and_counted():
    ctl, _, la, _, codec, _, _ = _controller()
    own = codec.encode_link_header(src_node=1, dst_node=2, flags=0)
    assert ctl.on_control(own, CONTEXT) is ControlVerdict.DROP_BURST
    own_feedback = codec.encode_feedback(report(1, None), src_node=1, dst_node=2, flags=0)
    assert ctl.on_control(own_feedback, CONTEXT) is ControlVerdict.DROP_BURST
    assert ctl.snapshot()["own_bursts_heard"] == 2 and la.feedback == []


def test_peer_feedback_reaches_the_la_with_the_receive_time():
    ctl, clock, la, _, codec, _, _ = _controller()
    clock.t = 12.5
    assert ctl.on_control(_peer_feedback(codec, 3), CONTEXT) is ControlVerdict.ACCEPT
    assert [r.report_seq for r in la.feedback] == [3]
    assert la.feedback[0].received_t_s == 12.5
    assert ctl.snapshot()["feedback_received"] == 1


def test_link_header_other_destination_and_garbage_are_accepted():
    ctl, _, la, _, codec, _, _ = _controller()
    header = codec.encode_link_header(src_node=2, dst_node=1, flags=0)
    assert ctl.on_control(header, CONTEXT) is ControlVerdict.ACCEPT
    other = codec.encode_feedback(report(1, None), src_node=2, dst_node=9, flags=0)
    assert ctl.on_control(other, CONTEXT) is ControlVerdict.ACCEPT
    assert ctl.on_control(b"\xff\x00garbage", CONTEXT) is ControlVerdict.ACCEPT
    snap = ctl.snapshot()
    assert (snap["control_other_dst"], snap["control_undecodable"]) == (1, 1)
    assert la.feedback == []


def test_feedback_events_are_rate_limited_but_all_logged():
    ctl, clock, _, _, codec, log, events = _controller()
    for seq in range(1, 51):  # 1 s at 50 reports/s
        clock.t = seq * 0.02
        ctl.on_control(_peer_feedback(codec, seq), CONTEXT)
    feedback_events = [e for e in events if e[0] == "feedback"]
    assert 9 <= len(feedback_events) <= 11
    assert len([e for e in _log_lines(log) if e["event"] == "feedback"]) == 50
    assert feedback_events[0][1]["offset_db"] == 0.5


def test_peer_loss_and_feedback_age_in_the_snapshot():
    ctl, clock, _, _, codec, _, _ = _controller()
    clock.t = 1.0
    ctl.on_control(_peer_feedback(codec, 1, ok=100, lost=0), CONTEXT)
    clock.t = 1.02
    ctl.on_control(_peer_feedback(codec, 2, ok=190, lost=10), CONTEXT)
    clock.t = 1.1
    snap = ctl.snapshot()
    assert snap["peer_loss_10s"] == pytest.approx(0.1)
    assert snap["feedback_age_s"] == pytest.approx(0.08)
    assert snap["stale"] is False and snap["feedback_stale"] == 0
    clock.t = 1.7
    snap = ctl.snapshot()
    assert snap["stale"] is True and snap["feedback_stale"] == 1
    clock.t = 12.0
    assert ctl.snapshot()["peer_loss_10s"] is None


# -- BurstPolicy --------------------------------------------------------------


def test_decisions_pick_the_entry_and_changes_are_logged_once():
    ctl, clock, la, _, _, log, events = _controller()
    assert ctl.entry_for_next_burst(0.0) == mcs_entry(12)
    assert ctl.entry_for_next_burst(0.01) == mcs_entry(12)
    la.mcs, la.reason = 15, "up"
    assert ctl.entry_for_next_burst(0.02) == mcs_entry(15)
    changes = [e for e in _log_lines(log) if e["event"] == "la"]
    assert [(c["mcs_index"], c["previous"], c["reason"]) for c in changes] == [
        (12, None, "init"), (15, 12, "up")]
    assert [e[1]["mcs_index"] for e in events if e[0] == "la"] == [12, 15]
    assert ctl.snapshot()["decisions"] == 2


def test_invalid_decision_falls_back_to_a_slower_rung():
    ctl, _, la, _, _, log, _ = _controller()
    la.mcs = 11  # hidden entry
    assert ctl.entry_for_next_burst(0.0) == mcs_entry(10)
    la.mcs = 99
    assert ctl.entry_for_next_burst(0.1) == mcs_entry(8)
    la.mcs = 14  # transmit-visible, off the ladder: allowed
    assert ctl.entry_for_next_burst(0.2) == mcs_entry(14)
    assert ctl.snapshot()["invalid_decisions"] == 2
    assert [e["used"] for e in _log_lines(log) if e["event"] == "invalid"] == [10, 8]


def test_data_burst_carries_a_link_header_when_no_report_is_due():
    ctl, _, _, reporter, codec, _, _ = _controller()
    plan = ctl.control_plan(0.0, idle=False)
    assert len(plan.payloads) == 1 and not plan.standalone
    decoded = codec.decode(plan.payloads[0])
    assert (decoded.src_node, decoded.dst_node, decoded.feedback) == (1, 2, None)
    assert decoded.flags == FLAG_TX_QUEUE_NONEMPTY
    assert ctl.control_plan(0.0, idle=True).payloads == ()
    assert reporter.built == 0


def test_idle_report_goes_in_a_control_only_burst():
    ctl, _, _, reporter, codec, _, _ = _controller()
    reporter.is_due = True
    plan = ctl.control_plan(0.0, idle=True)
    decoded = codec.decode(plan.payloads[0])
    assert decoded.flags == FLAG_CONTROL_ONLY and decoded.feedback.report_seq == 1
    assert not plan.standalone
    assert ctl.control_entry() == mcs_entry(8)
    assert ctl.snapshot()["feedback_sent"] == 1


def test_robust_copy_at_the_control_mcs_every_100_ms_while_data_uses_a_high_mcs():
    ctl, _, la, reporter, codec, _, _ = _controller()
    la.mcs = 15
    reporter.is_due = True
    ctl.entry_for_next_burst(0.0)
    first = ctl.control_plan(0.0, idle=False)
    assert first.standalone
    assert codec.decode(first.payloads[0]).flags == FLAG_CONTROL_ONLY
    piggy = ctl.control_plan(0.02, idle=False)
    assert not piggy.standalone
    assert codec.decode(piggy.payloads[0]).flags == FLAG_TX_QUEUE_NONEMPTY
    assert ctl.control_plan(0.1, idle=False).standalone


def test_no_robust_copy_needed_when_data_goes_at_a_low_mcs():
    ctl, _, la, reporter, _, _, _ = _controller()
    la.mcs = 9
    reporter.is_due = True
    ctl.entry_for_next_burst(0.0)
    assert not any(ctl.control_plan(0.05 * i, idle=False).standalone for i in range(10))


def test_idle_wait_follows_the_reporter():
    ctl, _, _, reporter, _, _, _ = _controller()
    assert ctl.idle_wait_s(1.0) is None
    reporter.due_at = 1.25
    assert ctl.idle_wait_s(1.0) == pytest.approx(0.25)
    assert ctl.idle_wait_s(2.0) == 0.0


@dataclass
class _Burst:
    sequences: tuple
    datagrams: tuple
    mcs_index: int
    sample_count: int


def test_sent_bursts_reach_the_la():
    ctl, _, la, _, _, _, _ = _controller()
    ctl.set_tx_gain(-5.0)
    ctl.on_sent(_Burst((100, 101, 102), ("a", "b", "c"), 12, 50_000), 3.0)
    ctl.on_sent(_Burst((), (), 8, 2_400), 3.1)
    data, control = la.sent
    assert (data.first_sequence, data.subframes, data.mcs_index) == (100, 3, 12)
    assert data.airtime_s == pytest.approx(0.01) and data.tx_gain_db == -5.0
    assert not data.control_only
    assert control.control_only and control.first_sequence is None
    assert ctl.snapshot()["control_bursts"] == 1


# -- fixed mode ------------------------------------------------------------------


def test_fixed_mode_and_freezing():
    ctl, _, la, _, _, log, _ = _controller()
    ctl.entry_for_next_burst(0.0)
    ctl.set_fixed(mcs_entry(14))
    assert ctl.mode == "fixed"
    assert ctl.entry_for_next_burst(0.1) == mcs_entry(14)
    la.mcs = 16
    assert ctl.entry_for_next_burst(0.2) == mcs_entry(14)
    ctl.set_la_mode("auto")
    assert ctl.entry_for_next_burst(0.3) == mcs_entry(16)
    ctl.set_la_mode("fixed")
    la.mcs = 8
    assert ctl.entry_for_next_burst(0.4) == mcs_entry(16)
    assert ctl.snapshot()["reason"] == "fixed"
    with pytest.raises(ValueError):
        ctl.set_fixed(mcs_entry(4))
    with pytest.raises(ValueError):
        ctl.set_la_mode("manual")
    assert [e["mode"] for e in _log_lines(log) if e["event"] == "mode"] == [
        "fixed", "auto", "fixed"]


def test_mark_goes_to_the_log():
    ctl, clock, *_, log, _ = _controller()
    clock.t = 4.0
    ctl.mark("gain step")
    assert _log_lines(log)[-1] == {"event": "mark", "t": 4.0, "text": "gain step"}


# -- ReceiveObserver ----------------------------------------------------------------


def test_receive_observations_reach_the_reporter():
    ctl, _, _, reporter, _, _, _ = _controller()
    data = BurstReport(1.0, 5, 12, 15.0, 10, 0)
    ctl.on_receive(1.0, snrs=(15.0, 14.0), bursts=(data,), health=HEALTH)
    ctl.on_receive(1.1, snrs=(13.0,), bursts=(), health=HEALTH)
    ctl.on_receive(1.2, snrs=(), bursts=(), health=HEALTH)
    assert reporter.calls == [
        ("preambles", 1.0, (15.0, 14.0), False), ("burst", data), ("health", 1.0, HEALTH),
        ("preambles", 1.1, (13.0,), True), ("health", 1.1, HEALTH),
        ("health", 1.2, HEALTH),
    ]


def test_two_controllers_exchange_feedback_through_the_real_reporter():
    codec = FakeCodec()
    clock = Clock()
    ctl_a, _, la_a, *_ = _controller(codec=codec, clock=clock)
    rep_b = DefaultReporter(ReporterContext(node_id=2, peer_node_id=1))
    ctl_b, _, la_b, *_ = _controller(node_id=2, peer_id=1, reporter=rep_b, codec=codec,
                                      clock=clock)
    ctl_b.on_receive(0.0, snrs=(16.0,), bursts=(BurstReport(0.0, 0, 12, 16.0, 40, 0),),
                     health=HEALTH)
    plan = ctl_b.control_plan(0.001, idle=True)
    assert ctl_a.on_control(plan.payloads[0], CONTEXT) is ControlVerdict.ACCEPT
    assert la_a.feedback[0].data_ok_total == 40
    assert la_a.feedback[0].snr_mean_db == 16.0
    assert ctl_b.on_control(plan.payloads[0], CONTEXT) is ControlVerdict.DROP_BURST
    assert la_b.feedback == []


def test_default_codec_round_trip_when_the_link_layer_is_present():
    pytest.importorskip("ofdm_message_link.control")
    clock = Clock()
    ctl_a, _, la_a, *_ = _controller(codec=None, clock=clock)
    rep_b = DefaultReporter(ReporterContext(node_id=2, peer_node_id=1))
    ctl_b, *_ = _controller(node_id=2, peer_id=1, reporter=rep_b, codec=None, clock=clock)
    header = ctl_a.control_plan(0.0, idle=False).payloads[0]
    assert ctl_a.on_control(header, CONTEXT) is ControlVerdict.DROP_BURST
    assert ctl_b.on_control(header, CONTEXT) is ControlVerdict.ACCEPT
    ctl_b.on_receive(0.0, snrs=(16.0,), bursts=(BurstReport(0.0, 0, 12, 16.0, 40, 1),),
                     health=HEALTH)
    feedback = ctl_b.control_plan(0.001, idle=True).payloads[0]
    assert ctl_a.on_control(feedback, CONTEXT) is ControlVerdict.ACCEPT
    received = la_a.feedback[0]
    assert (received.data_ok_total, received.data_lost_total) == (40, 1)
    assert received.snr_mean_db == pytest.approx(16.0, abs=0.01)
    assert np.isclose(received.received_t_s, 0.0)


# -- peer restart and stale agreement (review finding 1) ------------------------


def test_an_ignored_report_does_not_refresh_the_feedback_age():
    ctl, clock, _, _, codec, _, _ = _controller()
    clock.t = 1.0
    ctl.on_control(_peer_feedback(codec, 10, ok=400), CONTEXT)
    clock.t = 1.4
    ctl.on_control(_peer_feedback(codec, 9, ok=360), CONTEXT)  # a late copy
    clock.t = 1.6
    snap = ctl.snapshot()
    assert snap["feedback_ignored"] == 1
    assert snap["feedback_age_s"] == pytest.approx(0.6) and snap["stale"] is True


def test_controller_uses_the_algorithms_stale_after():
    la = OllaLinkAdaptation(context(), stale_after_s=2.0)
    ctl, clock, _, _, codec, _, _ = _controller(la=la)
    clock.t = 1.0
    ctl.on_control(_peer_feedback(codec, 1), CONTEXT)
    clock.t = 2.5
    assert ctl.snapshot()["stale"] is False
    clock.t = 3.1
    assert ctl.snapshot()["stale"] is True


def test_peer_loss_follows_a_restarted_peer():
    ctl, clock, _, _, codec, _, _ = _controller()
    clock.t = 1.0
    ctl.on_control(_peer_feedback(codec, 5000, ok=200_000, session_id=1), CONTEXT)
    for seq in range(0, 5):
        clock.t += 0.02
        ctl.on_control(
            _peer_feedback(codec, seq, ok=30 * seq, lost=10 * seq, session_id=2), CONTEXT
        )
    snap = ctl.snapshot()
    assert snap["peer_rebaselines"] == 1 and snap["feedback_ignored"] == 0
    assert snap["peer_loss_10s"] == pytest.approx(0.25)


# -- plug-in failures stay inside the controller (review finding 4) -------------


class _BrokenLa(FakeLa):
    def on_feedback(self, report):
        raise ZeroDivisionError("on_feedback bug")

    def decide(self, now_s):
        raise RuntimeError("decide bug")

    def on_sent(self, burst):
        raise RuntimeError("on_sent bug")

    def snapshot(self):
        raise RuntimeError("snapshot bug")


class _BrokenReporter(FakeReporter):
    def on_preambles(self, now_s, snr_db, *, control_only):
        raise RuntimeError("reporter bug")

    def build(self, now_s):
        raise RuntimeError("build bug")


class _BrokenLog:
    def write(self, line):
        raise OSError("disk full")

    def flush(self):
        pass


def test_algorithm_reporter_log_and_event_failures_never_escape():
    def bad_event(kind, record):
        raise RuntimeError("gui gone")

    reporter = _BrokenReporter()
    ctl, clock, _, _, codec, _, _ = _controller(
        la=_BrokenLa(), reporter=reporter, la_log=_BrokenLog(), on_event=bad_event
    )
    clock.t = 1.0
    assert ctl.on_control(_peer_feedback(codec, 1), CONTEXT) is ControlVerdict.ACCEPT
    assert ctl.entry_for_next_burst(1.0) == mcs_entry(8)  # no MCS yet: the control entry
    ctl.on_sent(_Burst(sequences=(0,), datagrams=(b"x",), sample_count=100, mcs_index=8), 1.0)
    reporter.is_due = True
    plan = ctl.control_plan(1.0, idle=False)
    assert len(plan.payloads) == 1 and not plan.standalone  # a LINK_HEADER instead
    assert ctl.control_plan(1.0, idle=True).payloads == ()
    ctl.on_receive(1.0, snrs=(15.0,), bursts=(), health=HEALTH)
    snap = ctl.snapshot()
    assert snap["la_errors"] >= 4 and snap["reporter_errors"] == 3
    assert snap["log_errors"] >= 1 and snap["event_errors"] >= 1
    assert snap["last_error"]
    assert snap["algorithm"] == {}


def test_the_controller_is_stale_when_the_algorithm_finds_the_link_dead():
    from ofdm_message_link.la.api import SentBurst

    la = OllaLinkAdaptation(context(initial=13))
    ctl, clock, _, _, codec, _, _ = _controller(la=la)
    for step in range(0, 40):
        clock.t = step * 0.02
        la.on_sent(SentBurst(clock.t, None, 8, 13, 0.002, False, None))
        ctl.on_control(_peer_feedback(codec, step + 1, preambles=0, snr=None), CONTEXT)
        ctl.entry_for_next_burst(clock.t)
    snap = ctl.snapshot()
    assert snap["feedback_age_s"] < 0.05 and snap["reason"] == "stale"
    assert snap["stale"] is True
