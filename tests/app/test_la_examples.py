from la_fakes import context, report

from ofdm_message_link.la import loader
from ofdm_message_link.la.api import LinkAdaptation
from ofdm_message_link.la.examples import ArfLinkAdaptation


class Feeder:
    def __init__(self, la):
        self.la = la
        self.seq = 0
        self.ok = 0
        self.lost = 0
        self.t = 0.0
        self.feed(0, 0)  # baseline

    def feed(self, ok, lost):
        self.seq += 1
        self.t += 0.02
        self.ok += ok
        self.lost += lost
        self.la.on_feedback(report(self.seq, self.t, ok=self.ok, lost=self.lost))
        return self.la.decide(self.t)


def test_steps_up_after_up_after_lossless_reports():
    la = ArfLinkAdaptation(context(initial=12), up_after=3)
    assert la.decide(0.0).reason == "init"
    feeder = Feeder(la)
    assert [feeder.feed(40, 0).mcs_index for _ in range(3)] == [12, 12, 13]
    assert [feeder.feed(40, 0).mcs_index for _ in range(3)] == [13, 13, 15]  # 14 is not a rung


def test_steps_down_after_down_after_lossy_reports():
    la = ArfLinkAdaptation(context(initial=12), down_after=2)
    feeder = Feeder(la)
    assert feeder.feed(39, 1).mcs_index == 12
    decision = feeder.feed(39, 1)
    assert (decision.mcs_index, decision.reason) == (10, "down")
    assert feeder.feed(39, 1).mcs_index == 10  # the run restarts after a step
    assert feeder.feed(39, 1).mcs_index == 9


def test_a_lossless_report_resets_the_lossy_run():
    la = ArfLinkAdaptation(context(initial=12), down_after=2)
    feeder = Feeder(la)
    for _ in range(5):
        feeder.feed(39, 1)
        assert feeder.feed(40, 0).mcs_index == 12


def test_failed_probe_falls_back_at_once():
    la = ArfLinkAdaptation(context(initial=12), up_after=2, down_after=3)
    feeder = Feeder(la)
    feeder.feed(40, 0)
    assert feeder.feed(40, 0).mcs_index == 13
    assert feeder.feed(30, 10).mcs_index == 12
    la = ArfLinkAdaptation(context(initial=12), up_after=2, down_after=3, probe_fail_down=False)
    feeder = Feeder(la)
    feeder.feed(40, 0)
    feeder.feed(40, 0)
    assert feeder.feed(30, 10).mcs_index == 13


def test_idle_reports_change_nothing_and_ends_are_clamped():
    la = ArfLinkAdaptation(context(initial=17), up_after=1)
    feeder = Feeder(la)
    for _ in range(5):
        assert feeder.feed(0, 0).mcs_index == 17
        assert feeder.feed(40, 0).mcs_index == 17
    la = ArfLinkAdaptation(context(initial=8), down_after=1)
    feeder = Feeder(la)
    assert feeder.feed(0, 40).mcs_index == 8


def test_loads_through_the_loader_both_ways():
    by_name = loader.build_link_adaptation("arf", context(), ["up_after=5"])
    by_path = loader.build_link_adaptation(
        "ofdm_message_link.la.examples:ArfLinkAdaptation", context(), ["down_after=4"]
    )
    assert isinstance(by_name, LinkAdaptation) and by_name.up_after == 5
    assert by_path.down_after == 4
    assert by_name.snapshot()["algorithm"] == "arf"


def test_arf_follows_a_restarted_peer():
    la = ArfLinkAdaptation(context(initial=13), up_after=10, down_after=2)
    for seq in range(1, 5001):
        la.on_feedback(report(seq, seq * 0.02, ok=seq * 40))
    top = la.decide(100.0).mcs_index
    t = 100.0
    for seq in range(0, 20):  # restart: seq and counters from 0, heavy loss
        t += 0.02
        la.on_feedback(report(seq, t, ok=seq * 40, lost=seq * 10))
    assert la.decide(t).mcs_index < top
    assert la.feedback.rebaselines == 1
