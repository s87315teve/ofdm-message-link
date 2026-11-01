import pytest

from ofdm_link.phy.mcs_table import mcs_entry
from ofdm_message_link import dashboard
from ofdm_message_link.la.thresholds import (
    DEFAULT_THRESHOLDS_PATH,
    ThresholdsError,
    build_ladder,
    load_thresholds,
    parse_thresholds,
    rung_at_or_below,
)

MEASURED_V5 = {
    8: 7.5, 9: 9.0, 10: 11.0, 12: 14.0, 13: 15.5, 14: 17.5,
    15: 16.5, 16: 21.0, 17: 22.0,
}


def _indices(ladder):
    return [entry.index for entry in ladder]


def test_default_file_holds_the_measured_table():
    table = load_thresholds()
    assert {k: v for k, v in table.items() if k >= 8} == MEASURED_V5
    assert table[0] == 9.0 and table[4] == 15.0


def test_dashboard_reads_the_same_file():
    assert dict(dashboard.SNR_THRESHOLD_DB) == load_thresholds(DEFAULT_THRESHOLDS_PATH)


def test_ladder_drops_dominated_mcs_14():
    # 14 (16QAM 3/4) needs 17.5 dB, more than 15 (64QAM 1/2, same rate) at 16.5.
    assert _indices(build_ladder(load_thresholds())) == [8, 9, 10, 12, 13, 15, 16, 17]


def test_ladder_is_ascending_in_rate_and_threshold():
    table = load_thresholds()
    ladder = build_ladder(table)
    rates = [entry.spectral_efficiency for entry in ladder]
    thresholds = [table[entry.index] for entry in ladder]
    assert rates == sorted(rates) and len(set(rates)) == len(rates)
    assert thresholds == sorted(thresholds)


def test_missing_entries_never_join_the_ladder():
    # Without 64QAM values the ladder ends at 14; hidden 11 never joins.
    table = {8: 9.0, 9: 11.5, 10: 13.5, 11: 13.5, 12: 17.5, 13: 20.5, 14: 22.5}
    assert _indices(build_ladder(table)) == [8, 9, 10, 12, 13, 14]


def test_entry_with_threshold_not_below_a_faster_one_is_dominated():
    table = {8: 7.0, 9: 12.0, 10: 11.0, 12: 14.0}
    assert _indices(build_ladder(table)) == [8, 10, 12]


def test_equal_thresholds_keep_the_faster_entry():
    table = {8: 7.0, 9: 9.0, 10: 9.0}
    assert _indices(build_ladder(table)) == [8, 10]


def test_hidden_entry_joins_only_when_asked():
    table = {10: 11.0, 11: 10.0, 12: 14.0}
    assert _indices(build_ladder(table)) == [10, 12]
    entries = [mcs_entry(i) for i in (10, 11, 12)]
    assert _indices(build_ladder(table, entries)) == [11, 12]  # 10 equals 11's rate


def test_rung_at_or_below_maps_off_ladder_indices():
    ladder = build_ladder(load_thresholds())
    assert rung_at_or_below(ladder, 12).index == 12
    assert rung_at_or_below(ladder, 14).index == 15  # same rate as 15
    assert rung_at_or_below(ladder, 11).index == 10
    assert rung_at_or_below(ladder, 0).index == 8
    assert rung_at_or_below(ladder, 99).index == 8


@pytest.mark.parametrize(
    "text",
    [
        "snr_threshold_db: {8: high}\n",
        "snr_threshold_db: {99: 7.0}\n",
        "snr_threshold_db: {'8': 7.0}\n",
        "snr_threshold_db: {8: .nan}\n",
        "other: {}\n",
        "snr_threshold_db: [1, 2]\n",
        "snr_threshold_db: {8: [\n",
    ],
)
def test_malformed_files_are_refused(tmp_path, text):
    path = tmp_path / "t.yaml"
    path.write_text(text)
    with pytest.raises(ThresholdsError):
        load_thresholds(path)


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(ThresholdsError, match="cannot read"):
        load_thresholds(tmp_path / "none.yaml")


def test_parse_sorts_by_index():
    assert list(parse_thresholds({12: 14, 8: 7.5})) == [8, 12]
