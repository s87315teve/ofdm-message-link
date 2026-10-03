"""Rules behind the message-link dashboard, tested without Qt.

The indicator light, the sliding windows and the 60-second trend live in a
plain Python module so every rule in docs/02-gui.md can be checked
here, including its boundaries, without a display.
"""

from __future__ import annotations

import pytest

from ofdm_message_link import dashboard
from ofdm_message_link.dashboard import (
    RxDashboard,
    TxDashboard,
    rx_light,
    tabs_visible,
    tx_light,
)


def _light(**overrides):
    values = {
        "now": 100.0,
        "last_burst_at": 99.5,
        "loss_10s": 0.0,
        "snr_2s": 15.0,
        "mcs_index": 4,
    }
    values.update(overrides)
    return rx_light(**values)


# -- threshold table -------------------------------------------------------


def test_threshold_table_holds_the_measured_mcs_thresholds():
    assert dashboard.SNR_THRESHOLD_DB == {0: 8.0, 4: 11.0}


# -- receive light, in rule order -----------------------------------------


def test_grey_waiting_until_the_first_burst():
    light = _light(last_burst_at=None, loss_10s=None, snr_2s=None, mcs_index=None)
    assert (light.state, light.colour) == ("WAITING", "grey")
    assert light.reason == "Waiting for the first burst"


def test_grey_idle_after_more_than_ten_seconds_without_a_burst():
    light = _light(last_burst_at=86.0, snr_2s=None)
    assert (light.state, light.colour) == ("IDLE", "grey")
    assert light.reason == "No bursts for 14 s — transmitter stopped or link lost"


def test_exactly_ten_seconds_is_still_down_not_idle():
    light = _light(last_burst_at=90.0, snr_2s=None)
    assert (light.state, light.colour) == ("DOWN", "red")
    assert light.reason == "No bursts for 10 s"


def test_red_down_after_more_than_two_seconds_without_a_burst():
    light = _light(last_burst_at=97.0, snr_2s=None)
    assert (light.state, light.colour) == ("DOWN", "red")
    assert light.reason == "No bursts for 3 s"


def test_exactly_two_seconds_is_not_yet_down():
    assert _light(last_burst_at=98.0).state == "GOOD"


def test_red_lossy_at_exactly_one_percent_loss():
    light = _light(loss_10s=0.01)
    assert (light.state, light.colour) == ("LOSSY", "red")
    assert light.reason == "Loss 1.0 % in the last 10 s"


def test_red_lossy_reason_quotes_the_loss():
    assert _light(loss_10s=0.042).reason == "Loss 4.2 % in the last 10 s"


def test_yellow_loss_just_below_one_percent():
    light = _light(loss_10s=0.003)
    assert (light.state, light.colour) == ("LOSS", "yellow")
    assert light.reason == "Loss 0.3 % in the last 10 s"


def test_yellow_marginal_within_one_db_of_the_threshold():
    light = _light(snr_2s=11.4)
    assert (light.state, light.colour) == ("MARGINAL", "yellow")
    assert light.reason == "SNR 11.4 dB is within 1 dB of 11 dB needed for MCS 4"


def test_exactly_threshold_plus_one_db_is_good():
    light = _light(snr_2s=12.0)
    assert (light.state, light.colour) == ("GOOD", "green")
    assert light.reason == "No loss, SNR 1.0 dB above MCS 4 threshold"


def test_green_good_reports_the_margin():
    assert _light(snr_2s=13.3).reason == "No loss, SNR 2.3 dB above MCS 4 threshold"


def test_mcs_without_a_measured_threshold_is_judged_on_loss_only():
    light = _light(mcs_index=7, snr_2s=3.0)
    assert (light.state, light.colour) == ("GOOD", "green")
    assert "no SNR threshold measured for MCS 7" in light.reason
    lossy = _light(mcs_index=7, snr_2s=3.0, loss_10s=0.05)
    assert lossy.state == "LOSSY"
    assert "no SNR threshold measured for MCS 7" in lossy.reason


# -- suggestion -------------------------------------------------------------


def test_lossy_below_the_mcs4_threshold_suggests_mcs0():
    light = _light(loss_10s=0.05, snr_2s=9.5)
    assert light.reason == "Loss 5.0 % in the last 10 s — try MCS 0"


def test_marginal_below_the_threshold_suggests_mcs0():
    light = _light(snr_2s=10.6)
    assert light.state == "MARGINAL"
    assert light.reason.endswith("— try MCS 0")


def test_no_suggestion_while_snr_meets_the_current_threshold():
    assert "try MCS" not in _light(snr_2s=11.4).reason
    assert "try MCS" not in _light(loss_10s=0.05, snr_2s=14.0).reason


def test_no_suggestion_when_already_on_the_most_robust_measured_mcs():
    light = _light(mcs_index=0, loss_10s=0.05, snr_2s=6.0)
    assert "try MCS" not in light.reason


def test_no_suggestion_for_a_green_light():
    assert "try MCS" not in _light(mcs_index=0, snr_2s=10.0).reason


# -- receive dashboard: windows, reset, trend ------------------------------


def _counters(decoded, missing, delivered_bytes=0, **extra):
    return {
        "bursts_decoded": decoded,
        "missing_bursts": missing,
        "message_bytes_delivered": delivered_bytes,
        **extra,
    }


def test_loss_is_measured_over_the_last_ten_seconds_only():
    board = RxDashboard()
    board.refresh(0.0, _counters(0, 0), mcs_index=0)
    # 10 missing out of 100 expected in the first ten seconds.
    view = board.refresh(10.0, _counters(90, 10), mcs_index=0)
    assert view.loss_10s == pytest.approx(0.10)
    # A clean ten seconds follows: the old loss has left the window.
    board.refresh(15.0, _counters(140, 10), mcs_index=0)
    view = board.refresh(20.0, _counters(190, 10), mcs_index=0)
    assert view.loss_10s == pytest.approx(0.0)


def test_loss_is_unknown_when_no_burst_was_expected():
    board = RxDashboard()
    board.refresh(0.0, _counters(0, 0), mcs_index=None)
    view = board.refresh(0.5, _counters(0, 0), mcs_index=None)
    assert view.loss_10s is None


def test_snr_is_the_mean_of_the_last_two_seconds():
    board = RxDashboard()
    board.observe_burst(0.0, 20.0)
    board.observe_burst(1.0, 10.0)
    board.observe_burst(2.5, 12.0)
    view = board.refresh(2.5, _counters(3, 0), mcs_index=4)
    assert view.snr_2s == pytest.approx(11.0)
    view = board.refresh(10.0, _counters(3, 0), mcs_index=4)
    assert view.snr_2s is None


def test_a_rising_decode_counter_counts_as_a_burst_for_the_light():
    board = RxDashboard()
    board.refresh(0.0, _counters(0, 0), mcs_index=None)
    assert board.refresh(0.5, _counters(0, 0), mcs_index=None).light.state == "WAITING"
    view = board.refresh(1.0, _counters(5, 0), mcs_index=0)
    assert view.light.state == "GOOD"
    assert board.refresh(3.5, _counters(5, 0), mcs_index=0).light.state == "DOWN"
    assert board.refresh(11.5, _counters(5, 0), mcs_index=0).light.state == "IDLE"


def test_reset_makes_the_current_counters_the_new_zero():
    board = RxDashboard()
    board.refresh(0.0, _counters(0, 0), mcs_index=0)
    board.observe_burst(1.0, 12.0)
    board.refresh(1.0, _counters(100, 20, 5000), mcs_index=0)
    board.reset(1.0, _counters(100, 20, 5000))
    view = board.refresh(1.5, _counters(100, 20, 5000), mcs_index=0)
    assert view.totals["bursts_decoded"] == 0
    assert view.totals["missing_bursts"] == 0
    assert view.loss_10s is None
    assert view.snr_2s is None
    assert view.goodput_bps == 0.0
    assert len(view.trend) == 1
    view = board.refresh(2.0, _counters(110, 20, 6000), mcs_index=0)
    assert view.totals["bursts_decoded"] == 10
    assert view.totals["message_bytes_delivered"] == 1000
    assert view.loss_10s == pytest.approx(0.0)


def test_trend_keeps_sixty_seconds_and_marks_refreshes_with_loss():
    board = RxDashboard()
    missing = 0
    for step in range(0, 181):  # 90 s of 0.5 s refreshes
        now = step * 0.5
        if step == 170:
            missing += 3
        view = board.refresh(now, _counters(step * 10, missing), mcs_index=0)
    times = [point.t for point in view.trend]
    assert times[0] >= 90.0 - 60.0
    assert times[-1] == 90.0
    assert len(view.trend) == 121
    assert [point.t for point in view.trend if point.alert] == [85.0]


def test_ten_second_deltas_cover_every_counter():
    board = RxDashboard()
    board.refresh(0.0, _counters(0, 0, crc_failures=0), mcs_index=0)
    view = board.refresh(4.0, _counters(40, 2, crc_failures=2), mcs_index=0)
    assert view.recent["crc_failures"] == 2
    assert view.recent["bursts_decoded"] == 40


# -- transmit light and dashboard ------------------------------------------


def test_transmit_light_rules():
    assert tx_light(radio_on=False, faults_10s=3, queue_rising=True, airtime=1.0).colour == "grey"
    red = tx_light(radio_on=True, faults_10s=1, queue_rising=True, airtime=1.0)
    assert (red.state, red.colour) == ("FAULT", "red")
    rising = tx_light(radio_on=True, faults_10s=0, queue_rising=True, airtime=0.2)
    assert rising.colour == "yellow"
    busy = tx_light(radio_on=True, faults_10s=0, queue_rising=False, airtime=0.91)
    assert busy.colour == "yellow"
    edge = tx_light(radio_on=True, faults_10s=0, queue_rising=False, airtime=0.90)
    assert edge.colour == "green"


def test_transmit_dashboard_measures_airtime_and_a_rising_queue():
    board = TxDashboard(sample_rate=1_000_000.0)
    board.refresh(0.0, radio_on=True, sent_bytes=0, samples=0, pending=0, faults=None)
    board.refresh(0.5, radio_on=True, sent_bytes=0, samples=250_000, pending=1, faults=None)
    board.refresh(1.0, radio_on=True, sent_bytes=0, samples=500_000, pending=2, faults=None)
    view = board.refresh(
        1.5, radio_on=True, sent_bytes=1500, samples=750_000, pending=3, faults=None
    )
    assert view.airtime == pytest.approx(0.5)
    assert view.light.state == "BUSY"
    view = board.refresh(
        2.0, radio_on=True, sent_bytes=1500, samples=750_000, pending=3, faults=None
    )
    assert view.light.state == "OK"


def test_transmit_faults_turn_the_light_red_for_ten_seconds():
    board = TxDashboard(sample_rate=1_000_000.0)
    board.refresh(0.0, radio_on=True, sent_bytes=0, samples=0, pending=0,
                  faults={"tx_underflow": 0, "tx_burst_ack": 0})
    view = board.refresh(0.5, radio_on=True, sent_bytes=0, samples=0, pending=0,
                         faults={"tx_underflow": 1, "tx_burst_ack": 9})
    assert view.light.colour == "red"
    assert "tx_underflow" in view.light.reason
    view = board.refresh(11.0, radio_on=True, sent_bytes=0, samples=0, pending=0,
                         faults={"tx_underflow": 1, "tx_burst_ack": 20})
    assert view.light.colour == "green"


def test_transmit_reset_zeroes_totals_and_trend():
    board = TxDashboard(sample_rate=1_000_000.0)
    board.refresh(0.0, radio_on=True, sent_bytes=0, samples=0, pending=0, faults=None)
    board.refresh(1.0, radio_on=True, sent_bytes=900, samples=1000, pending=0, faults=None)
    board.reset(1.0, sent_bytes=900, samples=1000, faults=None)
    view = board.refresh(1.5, radio_on=True, sent_bytes=900, samples=1000, pending=0,
                         faults=None)
    assert view.totals == {"sent_bytes": 0, "samples": 0}
    assert view.goodput_bps == 0.0
    assert len(view.trend) == 1


# -- layout ------------------------------------------------------------------


def test_tabs_collapse_below_the_height_threshold():
    assert dashboard.COLLAPSE_BELOW_PX == 600
    assert tabs_visible(600)
    assert not tabs_visible(599)


def test_tabs_stay_while_the_operator_still_has_to_choose_a_radio():
    """The device panel is on a tab; a short window must not hide it before Start."""

    assert tabs_visible(300, choosing_radio=True)
    assert not tabs_visible(300, choosing_radio=False)
    # The still-empty cards and chart give up their room instead, and only
    # in a short window.
    assert not dashboard.live_view_visible(300, choosing_radio=True)
    assert dashboard.live_view_visible(300, choosing_radio=False)
    assert dashboard.live_view_visible(800, choosing_radio=True)


def test_the_rules_module_never_imports_qt():
    import subprocess
    import sys

    probe = (
        "import sys, ofdm_message_link.dashboard; "
        "print(any(name.startswith('PyQt') for name in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def test_bursts_decoded_before_the_first_refresh_count_as_seen():
    board = RxDashboard()
    view = board.refresh(5.0, _counters(12, 0), mcs_index=0)
    assert view.light.state == "GOOD"
