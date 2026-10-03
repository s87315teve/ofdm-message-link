"""Bounded normalized UHD async fault observations."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from threading import Lock
from typing import Any


@dataclass(frozen=True, slots=True)
class UhdFaultEvent:
    code: str
    count: int
    channel: int | None
    time_spec: tuple[int, float] | None
    observed_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class UhdFaultSnapshot:
    rx_overflow: int = 0
    tx_burst_ack: int = 0
    tx_underflow: int = 0
    tx_underflow_in_packet: int = 0
    tx_sequence_error: int = 0
    tx_sequence_error_in_burst: int = 0
    tx_time_error: int = 0
    unknown: int = 0
    dropped_events: int = 0
    events: tuple[UhdFaultEvent, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "rx_overflow": self.rx_overflow,
            "tx_burst_ack": self.tx_burst_ack,
            "tx_underflow": self.tx_underflow,
            "tx_underflow_in_packet": self.tx_underflow_in_packet,
            "tx_sequence_error": self.tx_sequence_error,
            "tx_sequence_error_in_burst": self.tx_sequence_error_in_burst,
            "tx_time_error": self.tx_time_error,
            "unknown": self.unknown,
            # This counts old diagnostic records evicted from the bounded
            # history, not UHD/USB errors or dropped samples.
            "event_history_evictions": self.dropped_events,
            "events": [
                {
                    "code": event.code,
                    "count": event.count,
                    "channel": event.channel,
                    "time_spec": event.time_spec,
                    "observed_monotonic_ns": event.observed_monotonic_ns,
                }
                for event in self.events
            ],
        }


class UhdFaultCollector:
    """Thread-safe counters plus a bounded drop-oldest event history."""

    def __init__(self, *, max_events: int = 64) -> None:
        if type(max_events) is not int or max_events <= 0:
            raise ValueError("max_events must be a positive integer")
        self._max_events = max_events
        self._events: deque[UhdFaultEvent] = deque()
        self._counts = {name: 0 for name in _COUNTER_BY_CODE.values()}
        self._counts["unknown"] = 0
        self._dropped_events = 0
        self._lock = Lock()

    def record(
        self,
        code: str,
        *,
        count: int = 1,
        channel: int | None = None,
        time_spec: tuple[int, float] | None = None,
    ) -> None:
        if not isinstance(code, str) or not code:
            raise ValueError("fault code must be a non-empty string")
        if type(count) is not int or count <= 0:
            raise ValueError("fault count must be a positive integer")
        if channel is not None and (type(channel) is not int or channel < 0):
            raise ValueError("fault channel must be a non-negative integer or None")
        event = UhdFaultEvent(
            code=code,
            count=count,
            channel=channel,
            time_spec=time_spec,
            observed_monotonic_ns=time.monotonic_ns(),
        )
        counter = _COUNTER_BY_CODE.get(code, "unknown")
        with self._lock:
            self._counts[counter] += count
            if len(self._events) == self._max_events:
                self._events.popleft()
                self._dropped_events += 1
            self._events.append(event)

    def snapshot(self) -> UhdFaultSnapshot:
        with self._lock:
            return UhdFaultSnapshot(
                **self._counts,
                dropped_events=self._dropped_events,
                events=tuple(self._events),
            )


_COUNTER_BY_CODE = {
    "overflow": "rx_overflow",
    "burst_ack": "tx_burst_ack",
    "underflow": "tx_underflow",
    "underflow_in_packet": "tx_underflow_in_packet",
    "seq_error": "tx_sequence_error",
    "seq_error_in_burst": "tx_sequence_error_in_burst",
    "time_error": "tx_time_error",
}


def record_uhd_async_message(
    message: object,
    *,
    pmt: Any,
    collector: UhdFaultCollector,
) -> None:
    """Normalize GNU Radio 3.10.12 source/sink ``async_msgs`` dictionaries."""

    try:
        if not pmt.is_pair(message):
            raise ValueError("async message is not a PMT pair")
        metadata = pmt.to_python(pmt.cdr(message))
        if not isinstance(metadata, Mapping):
            raise ValueError("async message metadata is not a mapping")
        normalized = {_text(key): value for key, value in metadata.items()}
        channel = _optional_nonnegative_int(normalized.get("channel"))
        time_spec = _optional_time_spec(normalized.get("time_spec"))
        recorded = False

        # usrp_source publishes RX overflow as ``{"overflows": n}``, aggregated
        # and rate-limited to one message per ``uhd.logging_interval_ms``.
        overflow = normalized.get("overflows")
        if overflow is not None:
            count = _positive_int(overflow)
            collector.record(
                "overflow",
                count=count,
                channel=channel,
                time_spec=time_spec,
            )
            recorded = True

        if "event_code" in normalized:
            for code in _event_codes(normalized["event_code"]):
                collector.record(
                    _text(code),
                    channel=channel,
                    time_spec=time_spec,
                )
                recorded = True

        if not recorded:
            collector.record("unrecognized_async_message")
    except (TypeError, ValueError, OverflowError):
        collector.record("invalid_async_message")


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("async count must be a positive integer")
    converted = int(value)  # type: ignore[arg-type]
    if converted <= 0:
        raise ValueError("async count must be a positive integer")
    return converted


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("async channel must be a non-negative integer")
    converted = int(value)  # type: ignore[arg-type]
    if converted < 0:
        raise ValueError("async channel must be a non-negative integer")
    return converted


def _optional_time_spec(value: object) -> tuple[int, float] | None:
    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("async time_spec must be a pair")
    if len(value) != 2:
        raise ValueError("async time_spec must be a pair")
    return int(value[0]), float(value[1])  # type: ignore[arg-type]


def _event_codes(value: object) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)):
        return (value,)
    if isinstance(value, tuple) and len(value) == 2:
        values: list[object] = []
        cursor: object = value
        while isinstance(cursor, tuple) and len(cursor) == 2:
            head, cursor = cursor
            values.append(head)
        if cursor is not None:
            values.append(cursor)
        return tuple(values)
    if isinstance(value, Sequence):
        return tuple(value)
    return (value,)


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)
