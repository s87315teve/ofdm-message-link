from __future__ import annotations

import pytest

from ofdm_link.phy.ampdu import ampdu_body_length
from ofdm_message_link.aggregation import AggregationLimits, plan_aggregate

SAMPLE_RATE = 5_000_000


def _samples(body_bytes: int) -> int:
    # Stand-in for the PHY: fixed overhead plus a linear payload term.
    return 560 + 80 * ((body_bytes + 4) // 4)


def _plan(lengths: list[int], **kwargs: object) -> int:
    limits = AggregationLimits(**kwargs)  # type: ignore[arg-type]
    return plan_aggregate(lengths, _samples, SAMPLE_RATE, limits)


def test_empty_queue_plans_nothing() -> None:
    assert _plan([]) == 0


def test_max_subframes_caps_count() -> None:
    assert _plan([20] * 40, max_subframes=16, max_burst_airtime_s=1.0) == 16
    assert _plan([20] * 40, max_subframes=5) == 5
    assert _plan([20] * 3, max_subframes=16) == 3


def test_max_psdu_bytes_caps_total() -> None:
    lengths = [100] * 20
    take = _plan(lengths, max_psdu_bytes=1000, max_burst_airtime_s=1.0)
    assert ampdu_body_length(lengths[:take]) + 4 <= 1000
    assert ampdu_body_length(lengths[: take + 1]) + 4 > 1000
    assert take == 9


def test_airtime_cap() -> None:
    lengths = [100] * 16
    limit = 0.001
    take = _plan(lengths, max_burst_airtime_s=limit, max_psdu_bytes=1 << 20)
    assert _samples(ampdu_body_length(lengths[:take])) / SAMPLE_RATE <= limit
    assert _samples(ampdu_body_length(lengths[: take + 1])) / SAMPLE_RATE > limit
    assert 1 < take < 16


def test_oversize_single_datagram_is_still_sent() -> None:
    assert _plan([9000], max_psdu_bytes=8192) == 1
    assert _plan([9000, 20], max_psdu_bytes=8192) == 1
    assert _plan([5000], max_burst_airtime_s=0.0001) == 1


def test_at_least_one_when_nonempty() -> None:
    assert _plan([20, 20], max_subframes=1) == 1
    assert _plan([20, 20], max_psdu_bytes=1, max_burst_airtime_s=1e-9) == 1


def test_disabled_sends_one_per_burst() -> None:
    assert _plan([20] * 5, enabled=False) == 1


def test_prefix_is_fifo_not_best_fit() -> None:
    # A large datagram in the middle stops the prefix; later small ones are not pulled forward.
    lengths = [50, 50, 5000, 50, 50]
    assert _plan(lengths, max_psdu_bytes=500) == 2


def test_limits_validation_and_defaults() -> None:
    defaults = AggregationLimits()
    assert (defaults.max_subframes, defaults.max_psdu_bytes) == (16, 8192)
    assert defaults.max_burst_airtime_s == 0.010
    with pytest.raises(ValueError):
        AggregationLimits(max_subframes=0)
    with pytest.raises(ValueError):
        AggregationLimits(max_burst_airtime_s=0.0)
    with pytest.raises(ValueError):
        AggregationLimits(max_wait_s=-1.0)
    with pytest.raises(ValueError):
        plan_aggregate([20], _samples, 0, defaults)
