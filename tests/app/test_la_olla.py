import json
import math
from collections import Counter

import pytest
from la_fakes import LADDER, FakeLink, context, loss_probability, report

from ofdm_message_link.la.api import LinkAdaptation
from ofdm_message_link.la.olla import OllaLinkAdaptation

RATE = {entry.index: entry.spectral_efficiency for entry in LADDER}


def _oracle_rate(snr_db, bias_db=0.0):
    """Rate of the fastest rung whose true loss is <= 1 % (the best fixed MCS)."""

    fits = [e for e in LADDER if loss_probability(snr_db, e.index, bias_db) <= 0.01]
    return fits[-1].spectral_efficiency if fits else 0.0


def _goodput(rows):
    return sum(RATE[mcs] * ok for _, mcs, ok, _, _ in rows) / (40 * len(rows))


def _feed(la, seq, t, **kw):
    la.on_feedback(report(seq, t, **kw))


def test_conforms_to_the_protocol_and_derives_step_up_from_the_target():
    la = OllaLinkAdaptation(context())
    assert isinstance(la, LinkAdaptation)
    assert la.step_up_db == pytest.approx(0.5 * 0.01 / 0.99)
    assert [e.index for e in la.ladder] == [8, 9, 10, 12, 13, 15, 16, 17]


# -- convergence on a fake link --------------------------------------------


@pytest.mark.parametrize("snr_db", [12.0, 16.0, 21.0])
def test_converges_to_the_best_fixed_mcs_at_low_loss(snr_db):
    link = FakeLink(OllaLinkAdaptation(context()), snr_db, seed=3).run(60)
    rows, loss = link.window(40)
    assert loss <= 0.02
    assert _goodput(rows) >= 0.95 * _oracle_rate(snr_db)


@pytest.mark.parametrize("snr_db", [10.0, 21.0])
def test_realised_loss_meets_the_target_where_a_rung_sits_at_its_threshold(snr_db):
    # At 10 and 21 dB the true 1 % point of MCS 10 and 17 is the SNR itself.
    for seed in (1, 2, 3):
        _, loss = FakeLink(OllaLinkAdaptation(context()), snr_db, seed=seed).run(60).window(40)
        assert 0.005 <= loss <= 0.02, (seed, loss)


def test_offset_settles_at_its_maximum_when_one_rung_clearly_fits():
    rows, _ = FakeLink(OllaLinkAdaptation(context()), 16.0, seed=3).run(60).window(40)
    assert {mcs for _, mcs, *_ in rows} == {15}
    assert sum(offset for *_, offset in rows) / len(rows) >= 3.0 - 0.5


@pytest.mark.parametrize("snr_db", [13.0, 18.0])
def test_offset_compensates_an_optimistic_table(snr_db):
    # The true thresholds are 3 dB worse than the table.
    for seed in (1, 2, 3):
        link = FakeLink(OllaLinkAdaptation(context()), snr_db, seed=seed, bias_db=3.0)
        rows, loss = link.run(60).window(40)
        assert 0.005 <= loss <= 0.02, (seed, loss)
        assert _goodput(rows) >= 0.9 * _oracle_rate(snr_db, 3.0)
        if snr_db == 18.0:  # the table says 15 fits (16.5 dB); it needs 18.5: offset < 0
            assert sum(offset for *_, offset in rows) / len(rows) < -1.0


def test_offset_compensates_a_pessimistic_table_up_to_its_maximum():
    # The true thresholds are 3 dB better: 17 fits at 18 dB, the table says 15.
    la = OllaLinkAdaptation(context(), offset_max_db=6.0)
    rows, loss = FakeLink(la, 18.0, seed=3, bias_db=-3.0).run(60).window(40)
    assert loss <= 0.02
    assert Counter(mcs for _, mcs, *_ in rows).most_common(1)[0][0] == 17
    assert la.offset_db > 2.0


def test_follows_an_snr_step_down_and_back_up():
    link = FakeLink(
        OllaLinkAdaptation(context()), lambda t: 21.0 if t < 20 or t >= 40 else 12.0, seed=4
    ).run(60)
    middle = [mcs for t, mcs, *_ in link.history if 25 < t < 40]
    end = [mcs for t, mcs, *_ in link.history if t > 50]
    assert max(middle) <= 12
    assert Counter(end).most_common(1)[0][0] == 17
    _, loss = link.window(0)
    assert loss <= 0.03


# -- inner loop: hysteresis, down, stale -------------------------------------


def _changes(la, snrs, *, t0=0.0, seq0=0, period=0.02):
    changes = 0
    t = t0
    previous = la.decide(t).mcs_index
    for i, snr in enumerate(snrs):
        t += period
        _feed(la, seq0 + i + 1, t, snr=snr)
        mcs = la.decide(t).mcs_index
        changes += mcs != previous
        previous = mcs
    return changes, t


def test_no_flapping_when_the_snr_alternates_below_the_up_hysteresis():
    # 15 needs 16.5 + 1.0 dB to go up; every second report qualifies, never two in a row.
    la = OllaLinkAdaptation(context(initial=13), outer_loop=False, snr_tau_s=0.0)
    changes, _ = _changes(la, [17.3, 17.9] * 100)
    assert changes == 0 and la.mcs_index == 13


@pytest.mark.parametrize("initial", [13, 15])
def test_at_most_one_change_when_the_snr_hovers_on_a_threshold(initial):
    la = OllaLinkAdaptation(context(initial=initial), outer_loop=False)
    changes, _ = _changes(la, [16.0, 17.0] * 200)
    assert changes <= 1


def test_one_up_step_after_two_qualifying_reports():
    la = OllaLinkAdaptation(context(initial=13), outer_loop=False, snr_tau_s=0.0)
    changes, t = _changes(la, [17.0] * 10)
    assert changes == 0
    _feed(la, 11, t + 0.02, snr=19.0)
    assert la.decide(t + 0.02).mcs_index == 13
    _feed(la, 12, t + 0.04, snr=19.0)
    decision = la.decide(t + 0.04)
    assert (decision.mcs_index, decision.reason) == (15, "up")
    changes, _ = _changes(la, [19.0] * 50, t0=t + 0.04, seq0=12)
    assert changes == 0


def test_up_step_waits_for_the_minimum_dwell():
    la = OllaLinkAdaptation(context(initial=13), outer_loop=False, snr_tau_s=0.0)
    assert la.decide(0.0).reason == "init"
    _feed(la, 1, 0.02, snr=19.0)
    _feed(la, 2, 0.04, snr=19.0)
    assert la.decide(0.04).mcs_index == 13
    assert la.decide(0.099).mcs_index == 13
    assert la.decide(0.10).mcs_index == 15


def test_down_step_is_immediate_and_may_skip_rungs():
    la = OllaLinkAdaptation(context(initial=16), outer_loop=False, snr_tau_s=0.0)
    la.decide(0.0)
    _feed(la, 1, 0.02, snr=22.5)
    assert la.decide(0.02).mcs_index == 16
    _feed(la, 2, 0.03, snr=14.2)
    decision = la.decide(0.03)
    assert (decision.mcs_index, decision.reason) == (12, "down")
    assert decision.snr_eff_db == pytest.approx(14.2)


def test_below_the_lowest_threshold_selects_the_lowest_rung():
    la = OllaLinkAdaptation(context(initial=12), outer_loop=False, snr_tau_s=0.0)
    la.decide(0.0)
    _feed(la, 1, 0.02, snr=3.0)
    assert la.decide(0.02).mcs_index == 8


def test_stale_feedback_steps_down_one_rung_per_half_second():
    la = OllaLinkAdaptation(context(initial=17), outer_loop=False, snr_tau_s=0.0)
    la.decide(0.0)
    _feed(la, 1, 0.02, snr=30.0)
    assert la.decide(0.02).mcs_index == 17
    assert la.decide(0.52).mcs_index == 17
    decisions = [la.decide(0.02 + 0.01 * i) for i in range(51, 161)]
    assert decisions[-1].mcs_index == 13  # 17 -> 16 -> 15 -> 13 after 1.6 s
    assert decisions[-1].reason == "stale"
    assert la.snapshot()["changes"] == 3


def test_stale_never_steps_below_the_fallback():
    la = OllaLinkAdaptation(context(initial=10))
    assert la.decide(0.0).mcs_index == 10
    assert la.decide(10.0).mcs_index == 8
    la = OllaLinkAdaptation(context(initial=13), fallback_mcs=10)
    la.decide(0.0)
    assert la.decide(10.0).mcs_index == 10


def test_fresh_feedback_ends_the_stale_state():
    la = OllaLinkAdaptation(context(initial=12), outer_loop=False, snr_tau_s=0.0)
    la.decide(0.0)
    assert la.decide(0.6).reason == "stale"
    _feed(la, 1, 0.61, snr=15.0)
    assert la.decide(0.61).reason == "hold"


# -- outer loop: sequence, counters, clamps ----------------------------------


def test_first_report_only_sets_the_baseline():
    la = OllaLinkAdaptation(context())
    _feed(la, 1, 0.02, ok=1000, lost=900)
    assert la.offset_db == 0.0
    _feed(la, 2, 0.04, ok=1040, lost=900)
    assert la.offset_db == pytest.approx(40 * la.step_up_db)


def test_old_or_repeated_report_seq_is_ignored():
    la = OllaLinkAdaptation(context())
    _feed(la, 5, 0.02, ok=0, lost=0)
    _feed(la, 6, 0.04, ok=40, lost=0)
    offset = la.offset_db
    _feed(la, 3, 0.06, ok=40, lost=40)
    _feed(la, 6, 0.06, ok=40, lost=40)
    assert la.offset_db == offset
    assert la.snapshot()["ignored_reports"] == 2


def test_report_seq_wraps():
    la = OllaLinkAdaptation(context())
    _feed(la, 65535, 0.02, ok=0)
    _feed(la, 65536, 0.04, ok=40)  # report_seq 0
    assert la.snapshot()["reports"] == 2
    assert la.offset_db > 0


def test_cumulative_counters_wrap_at_two_to_the_32():
    la = OllaLinkAdaptation(context())
    _feed(la, 1, 0.02, ok=2**32 - 10, lost=2**32 - 1)
    _feed(la, 2, 0.04, ok=2**32 + 30, lost=2**32 + 1)  # +40 ok, +2 lost
    expected = 40 * la.step_up_db - 2 * la.step_down_db
    assert la.offset_db == pytest.approx(expected)


def test_peer_counter_restart_rebaselines():
    la = OllaLinkAdaptation(context())
    _feed(la, 1, 0.02, ok=100_000, lost=50)
    _feed(la, 2, 0.04, ok=10, lost=0)
    assert la.offset_db == 0.0


def test_overflow_change_skips_the_outer_update():
    la = OllaLinkAdaptation(context())
    _feed(la, 1, 0.02, ok=0, lost=0, overflow=3)
    _feed(la, 2, 0.04, ok=20, lost=20, overflow=4)
    assert la.offset_db == 0.0
    assert la.snapshot()["overflow_skips"] == 1
    _feed(la, 3, 0.06, ok=40, lost=40, overflow=4)
    assert la.offset_db == pytest.approx(-2.0)

    la = OllaLinkAdaptation(context(), skip_outer_on_overflow=False)
    _feed(la, 1, 0.02, ok=0, lost=0, overflow=3)
    _feed(la, 2, 0.04, ok=20, lost=20, overflow=4)
    assert la.offset_db == pytest.approx(-2.0)


def test_per_report_and_total_clamps():
    la = OllaLinkAdaptation(context())
    _feed(la, 1, 0.0)
    ok = lost = 0
    for seq in range(2, 12):
        lost += 40
        _feed(la, seq, seq * 0.02, ok=ok, lost=lost)
        if seq == 2:
            assert la.offset_db == pytest.approx(-2.0)
    assert la.offset_db == pytest.approx(-10.0)
    for seq in range(12, 200):
        ok += 4000
        _feed(la, seq, seq * 0.02, ok=ok, lost=lost)
        if seq == 12:
            assert la.offset_db == pytest.approx(-9.5)
    assert la.offset_db == pytest.approx(3.0)


def test_outer_loop_moves_the_effective_snr():
    la = OllaLinkAdaptation(context(initial=12), snr_tau_s=0.0)
    la.decide(0.0)
    _feed(la, 1, 0.02, snr=14.5, ok=0, lost=0)
    assert la.decide(0.02).mcs_index == 12
    _feed(la, 2, 0.04, snr=14.5, ok=0, lost=40)  # offset -2: snr_eff 12.5 < 14
    decision = la.decide(0.04)
    assert decision.mcs_index == 10 and decision.offset_db == pytest.approx(-2.0)


def test_snr_filter_and_reports_without_preambles():
    la = OllaLinkAdaptation(context())
    _feed(la, 1, 1.00, snr=10.0)
    _feed(la, 2, 1.10, snr=20.0)
    expected = 10.0 + (1 - math.exp(-1.0)) * 10.0
    assert la.snapshot()["snr_f_db"] == pytest.approx(expected, abs=1e-3)
    _feed(la, 3, 1.20, snr=40.0, preambles=0)
    assert la.snapshot()["snr_f_db"] == pytest.approx(expected, abs=1e-3)


def test_thresholds_override_and_snapshot_is_json(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text("snr_threshold_db: {8: 5.0, 12: 8.0}\n")
    la = OllaLinkAdaptation(context(initial=12), thresholds=str(path))
    assert [e.index for e in la.ladder] == [8, 12]
    la.decide(0.0)
    json.dumps(dict(la.snapshot()))


def test_bad_parameters_are_refused():
    with pytest.raises(ValueError):
        OllaLinkAdaptation(context(), target_loss=0.0)
    with pytest.raises(ValueError):
        OllaLinkAdaptation(context(), offset_min_db=4.0, offset_max_db=3.0)
