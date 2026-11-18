"""One flat TX health record for stats logs: queues, radio faults and every drop.

The transmit path can lose a datagram in six places, and each has its own
counter here so a run can say where loss happened:

``ingress_dropped_queue_full``
    UDP ingress datagrams the transmit worker refused because its send queue
    was full (tx_app used to drop these silently).
``bursts_rejected_queue_full``
    Encoded bursts the radio sink refused because its burst queue was full.
``tx_underflow`` / ``tx_underflow_in_packet``
    UHD ran out of samples before or inside a burst.
``tx_late``
    UHD's ``tx_time_error``: a burst reached the radio after its start time.
``dropped_encode_failed``
    Datagrams dropped because encoding their burst raised (a bug; retrying
    the same input would fail forever).
``dropped_send_failed``
    Datagrams dropped because the transport's send raised: at once on a sink
    that cannot retry safely (a Pluto), after the retry bound on one that can
    (udp, UHD; see ``TransmitWorker``).

``send_failures`` counts every transport send that raised, whether the
transmit worker then kept the burst for a retry or dropped it.

Queue depths are instantaneous: ``queue_datagrams`` is what the transmit
worker holds (accepted, not yet on air), ``queue_bursts`` the bursts waiting
in the radio sink.  Radio fields are None where the transport has no such
counter (a Pluto has no late or underflow events, the udp transport no
radio at all), never 0 that could read as "measured, none".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def tx_health(
    snapshot: Mapping[str, Any] | None,
    *,
    queue_datagrams: int,
    ingress_dropped_queue_full: int,
    send_failures: int | None = None,
    dropped_send_failed: int | None = None,
    dropped_encode_failed: int | None = None,
) -> dict[str, int | None]:
    """The TX health record from a sink ``snapshot()`` and the worker's own counts."""

    snapshot = snapshot or {}
    source = snapshot.get("tx_source")
    faults = snapshot.get("uhd_faults")
    faults = faults if isinstance(faults, Mapping) else {}

    def fault(name: str) -> int | None:
        value = faults.get(name)
        return None if value is None else int(value)

    rejected = snapshot.get("bursts_rejected_queue_full")
    depth = source.get("depth") if isinstance(source, Mapping) else None
    return {
        "queue_datagrams": int(queue_datagrams),
        "queue_bursts": None if depth is None else int(depth),
        "ingress_dropped_queue_full": int(ingress_dropped_queue_full),
        "bursts_rejected_queue_full": None if rejected is None else int(rejected),
        "tx_underflow": fault("tx_underflow"),
        "tx_underflow_in_packet": fault("tx_underflow_in_packet"),
        "tx_late": fault("tx_time_error"),
        "send_failures": None if send_failures is None else int(send_failures),
        "dropped_send_failed": (
            None if dropped_send_failed is None else int(dropped_send_failed)
        ),
        "dropped_encode_failed": (
            None if dropped_encode_failed is None else int(dropped_encode_failed)
        ),
    }
