import pytest

from ofdm_message_link.la.api import (
    FLAG_CLIPPING_SEEN,
    FLAG_DECODE_BACKLOG,
    FLAG_LAST_SEQ_VALID,
    BurstReport,
    FeedbackReporter,
    ReporterContext,
    RxHealth,
)
from ofdm_message_link.la.reporter import DefaultReporter, percentile

CONTEXT = ReporterContext(node_id=2, peer_node_id=1)
HEALTH = RxHealth(overflow_total=3, header_fail_total=4, fcs_fail_total=5,
                  clipping_seen=False, decode_backlog=False)


def burst(t, seq=0, ok=10, lost=0, mcs=12, snr=15.0):
    return BurstReport(t_s=t, first_sequence=seq, mcs_index=mcs, snr_db=snr,
                       subframes_ok=ok, subframes_lost=lost, fcs_ok=True)


def test_conforms_to_the_protocol():
    reporter = DefaultReporter(CONTEXT)
    assert isinstance(reporter, FeedbackReporter) and reporter.reporter_id == 0


def test_first_data_burst_makes_a_report_due_at_once():
    reporter = DefaultReporter(CONTEXT)
    assert not reporter.due(0.0)
    reporter.on_burst(burst(0.001))
    assert reporter.due(0.001)
    assert reporter.next_due_s(0.001) == pytest.approx(0.001)


def test_period_after_a_report_while_data_flows():
    reporter = DefaultReporter(CONTEXT)
    reporter.on_burst(burst(0.0))
    reporter.build(0.0)
    reporter.on_burst(burst(0.005))
    assert not reporter.due(0.019)
    assert reporter.next_due_s(0.005) == pytest.approx(0.020)
    assert reporter.due(0.020)


def test_keepalive_without_data():
    reporter = DefaultReporter(CONTEXT)
    reporter.on_rx_health(1.0, HEALTH)
    assert not reporter.due(1.199)
    assert reporter.next_due_s(1.1) == pytest.approx(1.2)
    assert reporter.due(1.2)
    reporter.build(1.2)
    assert not reporter.due(1.39)
    assert reporter.due(1.4)


def test_control_only_preambles_never_make_a_report_due():
    reporter = DefaultReporter(CONTEXT)
    reporter.on_burst(burst(0.0))
    reporter.build(0.0)
    for i in range(1, 19):
        reporter.on_preambles(0.01 * i, [14.0], control_only=True)
        assert not reporter.due(0.01 * i + 0.005)
    assert reporter.due(0.2)
    report = reporter.build(0.2)
    assert report.preambles == 18 and report.snr_mean_db == pytest.approx(14.0)


def test_window_statistics_and_reset():
    reporter = DefaultReporter(CONTEXT)
    reporter.on_preambles(0.0, [10.0, 12.0], control_only=False)
    reporter.on_preambles(0.01, [20.0, 11.0, float("nan")], control_only=False)
    reporter.on_burst(burst(0.01, seq=100, ok=8, lost=2, mcs=13))
    reporter.on_rx_health(0.015, HEALTH)
    report = reporter.build(0.02)
    assert report.version == 1 and report.report_seq == 0
    assert report.preambles == 5
    assert report.snr_mean_db == pytest.approx(13.25)
    assert report.snr_p10_db == pytest.approx(percentile([10.0, 11.0, 12.0, 20.0], 0.1))
    assert report.snr_last_db == 11.0
    assert report.window_s == pytest.approx(0.02)
    assert (report.data_ok_total, report.data_lost_total) == (8, 2)
    assert report.last_data_seq == 107 and report.last_mcs_index == 13
    assert (report.rx_overflow_total, report.header_fail_total,
            report.psdu_fcs_fail_total) == (3, 4, 5)
    assert report.flags == FLAG_LAST_SEQ_VALID
    assert len(report.bursts) == 1

    empty = reporter.build(0.25)
    assert empty.report_seq == 1 and empty.preambles == 0
    assert empty.snr_mean_db is None and empty.snr_p10_db is None
    assert empty.snr_last_db is None and empty.bursts == ()
    assert (empty.data_ok_total, empty.data_lost_total) == (8, 2)  # cumulative
    assert empty.window_s == pytest.approx(0.23)


def test_percentile_matches_numpy():
    np = pytest.importorskip("numpy")
    values = [3.0, 9.5, 1.0, 7.25, 4.0, 12.0, 6.0]
    assert percentile(values, 0.1) == pytest.approx(np.percentile(values, 10))
    assert percentile([5.0], 0.1) == 5.0


def test_entry_cap_keeps_the_newest_bursts():
    reporter = DefaultReporter(CONTEXT, max_entries=3)
    for i in range(10):
        reporter.on_burst(burst(0.001 * i, seq=i * 10))
    report = reporter.build(0.02)
    assert [b.first_sequence for b in report.bursts] == [70, 80, 90]
    assert DefaultReporter(CONTEXT, max_entries=40).max_entries == 16
    assert DefaultReporter(ReporterContext(1, 2, max_entries=4), max_entries=8).max_entries == 4
    none = DefaultReporter(CONTEXT, max_entries=0)
    none.on_burst(burst(0.0))
    assert none.build(0.01).bursts == ()


def test_health_flags_and_counter_wrap():
    reporter = DefaultReporter(CONTEXT)
    reporter.on_rx_health(0.0, RxHealth(70_000, 0, 0, clipping_seen=True, decode_backlog=True))
    report = reporter.build(0.01)
    assert report.flags == FLAG_CLIPPING_SEEN | FLAG_DECODE_BACKLOG
    assert report.rx_overflow_total == 70_000 - 65_536
    reporter._data_ok = 2**32 + 5
    assert reporter.build(0.02).data_ok_total == 5


def test_report_seq_wraps():
    reporter = DefaultReporter(CONTEXT)
    reporter._seq = 65535
    assert reporter.build(0.0).report_seq == 65535
    assert reporter.build(0.1).report_seq == 0


def test_bad_parameters():
    with pytest.raises(ValueError):
        DefaultReporter(CONTEXT, period_ms=0)
    with pytest.raises(ValueError):
        DefaultReporter(CONTEXT, max_entries=-1)


def test_every_report_carries_the_session_id_of_this_reporter():
    first = DefaultReporter(CONTEXT)
    second = DefaultReporter(CONTEXT)
    assert first.session_id != second.session_id  # 2**-32 chance of a false failure
    assert first.build(0.0).session_id == first.build(0.2).session_id == first.session_id
    assert DefaultReporter(CONTEXT, session_id=5).build(0.0).session_id == 5
