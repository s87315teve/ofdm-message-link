"""Pure planning of how many queued datagrams share one v5 burst.

The planner lives apart from ``link.py`` and from the PHY so it can be tested
with plain numbers: the burst length in samples is supplied as a callable
instead of being imported, which keeps this module independent of the PHY's
sample-count formula.

The airtime cap exists because very long bursts drift against the receiver's
timing tracker, lose mean power to per-burst peak normalization, and make
decode latency lumpy.  Bytes alone do not bound those, airtime does.  The
drift also has a hard bound in samples, whatever the sample rate:
:data:`~ofdm_link.phy.burst_v5.V5_MAX_TRACKED_BURST_SAMPLES`, the longest
burst the receiver's timing correction covers at 50 ppm (4 samples).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ofdm_link.phy.ampdu import ampdu_body_length
from ofdm_link.phy.burst_v5 import V5_MAX_TRACKED_BURST_SAMPLES

PSDU_FCS_BYTES = 4


@dataclass(frozen=True, slots=True)
class AggregationLimits:
    """Caps on one aggregate; ``enabled=False`` sends one MPDU per burst."""

    enabled: bool = True
    max_wait_s: float = 0.0
    max_subframes: int = 16
    max_psdu_bytes: int = 8192
    max_burst_airtime_s: float = 0.008

    def __post_init__(self) -> None:
        if self.max_wait_s < 0:
            raise ValueError("max_wait_s must be non-negative")
        if self.max_subframes < 1:
            raise ValueError("max_subframes must be at least 1")
        if self.max_psdu_bytes < 1:
            raise ValueError("max_psdu_bytes must be at least 1")
        if self.max_burst_airtime_s <= 0:
            raise ValueError("max_burst_airtime_s must be positive")


def plan_aggregate(
    lengths: Sequence[int],
    burst_samples: Callable[[int], int],
    sample_rate: float,
    limits: AggregationLimits,
) -> int:
    """Number of leading MPDUs (FIFO prefix) to send in the next burst.

    ``lengths`` are the queued MPDU lengths in bytes.  ``burst_samples(n)``
    returns the burst length in samples for a PSDU body of ``n`` bytes (FCS
    excluded).  The result is the longest prefix with at most
    ``max_subframes`` MPDUs, a PSDU (body plus 4-byte FCS) of at most
    ``max_psdu_bytes`` and a burst of at most ``max_burst_airtime_s`` and
    :data:`~ofdm_link.phy.burst_v5.V5_MAX_TRACKED_BURST_SAMPLES` samples.  It
    is at least 1 for a non-empty queue: a datagram that alone exceeds a cap
    is still sent, alone, because refusing it would stall the queue forever.
    Returns 0 only for an empty queue.
    """
    if not lengths:
        return 0
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if not limits.enabled:
        return 1
    take = 1
    for count in range(2, min(len(lengths), limits.max_subframes) + 1):
        body = ampdu_body_length(lengths[:count])
        if body + PSDU_FCS_BYTES > limits.max_psdu_bytes:
            break
        samples = burst_samples(body)
        if samples / sample_rate > limits.max_burst_airtime_s:
            break
        if samples > V5_MAX_TRACKED_BURST_SAMPLES:
            break
        take = count
    return take
