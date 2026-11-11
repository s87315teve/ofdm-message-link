"""Per-burst receive events: the measurement output of ``node_app --events``.

Off by default.  When it is on, every preamble the receiver acquires becomes
one ``burst`` record that pairs what the receiver measured with what became
of the burst:

* where it was: the stream offset of its preamble (``off``, samples since the
  receive stream started), the device time of that sample (``dev_t``, from
  UHD ``rx_time``; None on transports without one), and the host clocks
  (``mono`` = ``time.monotonic()``, ``wall`` = ``time.time()``) when the
  receive thread handed the chunk holding the preamble to the decoder;
* what the preamble said: SNR (dB, the same value as the ``link_snr``
  events), complex noise variance per sample and mean ``|H|^2`` on the active
  carriers (both linear, received full scale 1.0);
* whether it clipped: the largest ``|I|`` or ``|Q|`` over the burst's samples
  (``peak``, full scale 1.0; over the fixed header when the header failed) and
  ``clip`` = ``peak >= CLIPPING_LEVEL`` (1900/2048, the level rx_app uses);
* how it decoded: the header (``hdr``), the payload (``pl``) and, for a
  decoded payload, what it carried (``kind``, ``mcs``, ``seq``, ``sf_ok``,
  ``sf_bad``, ``lost``, ``fcs``).

The field order of a record is :data:`BURST_FIELDS`; the JSON lines written by
:class:`EventLogWriter` are documented in ``docs/07-reference.md`` section 7.11
and versioned by :data:`EVENTS_SCHEMA_VERSION`.

Two halves: :class:`BurstEventRecorder` runs in the engine child, next to the
receiver (the decoder and :class:`~.link.MessageReceiver` call it), and
:class:`EventLogWriter` runs in the parent and writes the file.  Records cross
the process boundary in batches of plain tuples, numbered so that a batch
the event queue dropped is recorded as a ``gap`` line, never silently lost.
"""

from __future__ import annotations

import json
import threading
import time
from bisect import bisect_right
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Sequence
from typing import Any, NamedTuple

#: Version of the JSON-lines format; a consumer checks it in the ``start`` line.
EVENTS_SCHEMA_VERSION = 1
EVENTS_SCHEMA = "ofdm-message-link/events"

#: Same threshold as ``rx_app.CLIPPING_LEVEL`` (a 12-bit ADC's 1900 of 2048).
CLIPPING_LEVEL = 1900.0 / 2048.0

#: Default lock timeout: no header decoded from the peer for this much stream
#: time is a lost lock.  An idle peer still sends a control burst about every
#: 0.5 s, so 1 s does not call an idle link unlocked.
DEFAULT_LOCK_TIMEOUT_S = 1.0

# Records waiting for their outcome; more than this means the outcomes stopped
# coming (should not happen) and the oldest is written as it stands.
_MAX_OPEN = 2048
# Stream-offset tolerance when a decoded burst names its start: the decoder
# may report it a few samples off the acquired preamble start.
_MATCH_TOLERANCE = 400

#: ``hdr``: ``ok`` (CRC-checked header), ``fail`` (header decode failed),
#: ``invalid`` (acquired, no usable header for another reason).
HEADER_OUTCOMES = ("ok", "fail", "invalid")
#: ``pl``: ``ok``, ``fcs_fail`` (v5 PSDU whose FCS failed; its CRC-valid
#: subframes were still delivered), ``fail`` (no payload), ``abandoned``
#: (overload protection gave up on it), ``dropped`` (overload protection never
#: decoded it), None (header not ok, so no payload attempt).
PAYLOAD_OUTCOMES = ("ok", "fcs_fail", "fail", "abandoned", "dropped", None)
#: ``kind`` of a decoded payload: ``data`` (at least one of our DATA frames),
#: ``control`` (control frames only), ``own`` (this node's own transmitter,
#: dropped), ``foreign`` (CRC-valid frames that are not ours), ``other``.
KINDS = ("data", "control", "own", "foreign", "other", None)


class BurstEvent(NamedTuple):
    """One acquired preamble and everything known about its burst."""

    off: int
    dev_t: float | None
    mono: float
    wall: float
    snr_db: float | None
    noise_var: float | None
    signal_power: float | None
    peak: float | None
    clip: bool | None
    hdr: str
    pl: str | None
    kind: str | None
    mcs: int | None
    seq: int | None
    sf_ok: int | None
    sf_bad: int | None
    lost: int | None
    fcs: bool | None


BURST_FIELDS = BurstEvent._fields


class _Open:
    """A record still waiting for an outcome (mutable, child side only)."""

    __slots__ = ("off", "mono", "wall", "snr_db", "noise_var", "signal_power", "peak",
                 "hdr", "pl", "link", "pending_payload")

    def __init__(self, off: int, mono: float, wall: float, snr_db, noise_var,
                 signal_power) -> None:
        self.off = off
        self.mono = mono
        self.wall = wall
        self.snr_db = snr_db
        self.noise_var = noise_var
        self.signal_power = signal_power
        self.peak: float | None = None
        self.hdr = "invalid"
        self.pl: str | None = None
        self.link: tuple | None = None
        self.pending_payload = False


def _payload_outcome(failure, has_burst: bool) -> str:
    if failure is None:
        return "ok"
    if getattr(failure, "abandoned", False):
        return "abandoned"
    if has_burst:
        return "fcs_fail"
    return "fail"


class BurstEventRecorder:
    """Child-side observer that pairs each preamble with its burst's outcome.

    The streaming decoder calls :meth:`acquired`, :meth:`header_ok`,
    :meth:`header_failed`, :meth:`peak`, :meth:`segment` and :meth:`payload`
    in stream order; :class:`~.link.MessageReceiver` calls :meth:`on_feed`,
    :meth:`link` and :meth:`dropped`.  :meth:`drain` returns the finished
    records, the lock transitions and the new device-time anchors.  All calls
    come from the receive thread; nothing here blocks or raises into it.
    """

    def __init__(self, sample_rate: float, *, lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        if not sample_rate > 0.0:
            raise ValueError("sample_rate must be positive")
        if not lock_timeout_s > 0.0:
            raise ValueError("lock_timeout_s must be positive")
        self.sample_rate = float(sample_rate)
        self.lock_timeout_samples = int(round(lock_timeout_s * self.sample_rate))
        self._clock = clock
        self._wall = wall_clock
        self._open: OrderedDict[int, _Open] = OrderedDict()
        self._current: _Open | None = None
        self._segments: deque[int] = deque()
        self._done: list[_Open] = []
        self._feed_mono = clock()
        self._feed_wall = wall_clock()
        self._stream_end = 0
        # Lock: offset of the last header from the peer, and the state.
        self._last_peer_off: int | None = None
        self._last_peer_dev: float | None = None
        self._locked: bool | None = None
        self._lock_events: list[tuple] = []
        self.records = 0
        self.evicted = 0
        self.unmatched_links = 0

    # ---------------------------------------------------- receiver side

    def on_feed(self, stream_offset: int, sample_count: int) -> None:
        """A chunk of ``sample_count`` samples starting at ``stream_offset`` is being fed."""

        self._feed_mono = self._clock()
        self._feed_wall = self._wall()
        self._stream_end = int(stream_offset) + int(sample_count)

    def link(self, burst_start: int | None, kind: str, mcs: int | None, seq: int | None,
             sf_ok: int | None, sf_bad: int | None, lost: int | None,
             fcs: bool | None) -> None:
        """What a decoded burst carried (from :meth:`MessageReceiver._accept`)."""

        record = self._find(burst_start)
        if record is None:
            self.unmatched_links += 1
            return
        record.link = (kind, mcs, seq, sf_ok, sf_bad, lost, fcs)
        self._finish(record)

    def dropped(self, count: int) -> None:
        """Overload protection dropped the last ``count`` segments undecoded."""

        for _ in range(min(count, len(self._segments))):
            record = self._open.get(self._segments.pop())
            if record is not None:
                record.pl = "dropped"
                self._finish(record)

    # ---------------------------------------------------- decoder side

    def acquired(self, start: int, snr_db: float | None, noise_var: float | None,
                 signal_power: float | None) -> None:
        # A new preamble: the previous one can no longer be re-acquired, so a
        # failed header there is final.
        previous = self._current
        if previous is not None and previous.hdr != "ok" and not previous.pending_payload:
            self._finish(previous)
        record = _Open(int(start), self._feed_mono, self._feed_wall, snr_db, noise_var,
                       signal_power)
        self._open[record.off] = record
        self._current = record
        self.records += 1
        while len(self._open) > _MAX_OPEN:
            _, oldest = self._open.popitem(last=False)
            self.evicted += 1
            self._done.append(oldest)

    def header_ok(self) -> None:
        record = self._current
        if record is not None:
            record.hdr = "ok"
            record.pending_payload = True

    def header_failed(self, reason: str, peak: float | None) -> None:
        record = self._current
        if record is not None and record.hdr != "ok":
            record.hdr = reason
            if peak is not None:
                record.peak = peak if record.peak is None else max(record.peak, peak)

    def peak(self, value: float | None) -> None:
        record = self._current
        if record is not None and value is not None:
            record.peak = value

    def segment(self, stream_offset: int, peak: float | None) -> None:
        record = self._current
        if record is None:
            return
        record.peak = peak
        self._segments.append(record.off)

    def payload(self, failure, has_burst: bool) -> None:
        if self._segments:
            record = self._open.get(self._segments.popleft())
        else:
            record = self._current
        if record is None:
            return
        record.pl = _payload_outcome(failure, has_burst)
        record.pending_payload = False
        if not has_burst:
            self._finish(record)
        # With a burst, link() finishes it once the receiver has unpacked it.

    # ---------------------------------------------------- output

    def _find(self, burst_start: int | None) -> _Open | None:
        if burst_start is None:
            return None
        best = None
        for off in reversed(self._open):
            if off > burst_start + _MATCH_TOLERANCE:
                continue
            if off < burst_start - _MATCH_TOLERANCE:
                break
            if best is None or abs(off - burst_start) < abs(best - burst_start):
                best = off
        return None if best is None else self._open[best]

    def _finish(self, record: _Open) -> None:
        if self._open.pop(record.off, None) is None:
            return
        if self._current is record:
            self._current = None
        self._done.append(record)

    def _device_time(self, off: int, anchors: Sequence[tuple[int, float]],
                     offsets: list[int]) -> float | None:
        """Device time of stream offset ``off`` from the last anchor at or before it."""

        i = bisect_right(offsets, off) - 1
        if i < 0:
            return None
        a_off, a_sec = anchors[i]
        return a_sec + (off - a_off) / self.sample_rate

    def _offset_at(self, seconds: float, anchors: Sequence[tuple[int, float]]) -> int | None:
        """Stream offset of device time ``seconds``; in a gap, the first sample after it."""

        for i in range(len(anchors) - 1, -1, -1):
            a_off, a_sec = anchors[i]
            if a_sec <= seconds:
                off = a_off + int(round((seconds - a_sec) * self.sample_rate))
                if i + 1 < len(anchors):
                    off = min(off, anchors[i + 1][0])
                return off
        return None

    def _timed_out(self, off: int, dev_t: float | None) -> bool:
        """Whether more than the timeout passed between the last peer header and ``off``.

        Counted on the device clock when both ends have a device time, so the
        samples a receive overflow lost count too; on the stream offsets
        otherwise (no ``rx_time``: udp, Pluto), where nothing is ever lost.
        """

        if dev_t is not None and self._last_peer_dev is not None:
            elapsed = int(round((dev_t - self._last_peer_dev) * self.sample_rate))
        else:
            elapsed = off - self._last_peer_off
        return elapsed > self.lock_timeout_samples

    def _lose(self, anchors: Sequence[tuple[int, float]], offsets: list[int],
              mono: float, wall: float) -> None:
        when_dev = None
        when = None
        if self._last_peer_dev is not None and anchors:
            when_dev = self._last_peer_dev + self.lock_timeout_samples / self.sample_rate
            when = self._offset_at(when_dev, anchors)
        if when is None:
            when = self._last_peer_off + self.lock_timeout_samples
            when_dev = self._device_time(when, anchors, offsets) if anchors else None
        self._lock_events.append(("lost", when, mono, wall, when_dev))
        self._locked = False

    def _note_lock(self, record: _Open, dev_t: float | None,
                   anchors: Sequence[tuple[int, float]], offsets: list[int]) -> None:
        if record.hdr != "ok":
            return
        kind = None if record.link is None else record.link[0]
        if kind == "own":
            return
        newer = self._last_peer_off is None or record.off > self._last_peer_off
        # A header from the peer after a silence longer than the timeout that
        # no drain saw (a receive overflow can lose it inside one chunk).
        if self._locked is True and newer and self._timed_out(record.off, dev_t):
            self._lose(anchors, offsets, record.mono, record.wall)
        if self._locked is not True:
            self._lock_events.append(("acquired", record.off, record.mono, record.wall, dev_t))
            self._locked = True
        if newer:
            self._last_peer_off = record.off
            self._last_peer_dev = dev_t

    def drain(self, anchors: Sequence[tuple[int, float]] = ()) -> tuple[
            list[BurstEvent], list[tuple]]:
        """Finished records (stream order) and lock transitions since the last call.

        ``anchors`` are the receive stream's device-time anchors, ``(offset,
        seconds)`` sorted by offset (UHD ``rx_time`` tags); a record gets the
        device time of its offset from the last anchor at or before it.  A
        lock transition is ``(state, off, mono, wall, dev_t)``; the lock
        timeout runs on device time when there is one (see :meth:`_timed_out`).
        """

        done, self._done = self._done, []
        done.sort(key=lambda record: record.off)
        offsets = [anchor[0] for anchor in anchors]
        events = []
        for record in done:
            dev_t = self._device_time(record.off, anchors, offsets) if anchors else None
            self._note_lock(record, dev_t, anchors, offsets)
            peak = record.peak
            link = record.link or (None,) * 7
            events.append(BurstEvent(
                record.off, dev_t, record.mono, record.wall, record.snr_db, record.noise_var,
                record.signal_power, peak, None if peak is None else peak >= CLIPPING_LEVEL,
                record.hdr, record.pl, *link,
            ))
        if self._locked is True and self._last_peer_off is not None:
            end_dev = (self._device_time(self._stream_end, anchors, offsets)
                       if anchors else None)
            if self._timed_out(self._stream_end, end_dev):
                self._lose(anchors, offsets, self._feed_mono, self._feed_wall)
        locks, self._lock_events = self._lock_events, []
        return events, locks

    def flush(self) -> None:
        """Make every open record final as it stands (end of the run)."""

        for record in list(self._open.values()):
            self._finish(record)


def _round(value: Any, digits: int) -> Any:
    return round(value, digits) if isinstance(value, float) else value


class EventLogWriter:
    """Parent-side JSON-lines writer for ``node_app --events``.

    Chain :meth:`on_event` behind the node's event listener and call
    :meth:`status` with each stats line.  Thread-safe; every write is flushed.
    A failing write is counted (``write_errors``) and never raised into the
    caller, so a full disk cannot stop the radio.
    """

    def __init__(self, path: str, *, node_id: int, sample_rate: float,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        self.node_id = int(node_id)
        self.sample_rate = float(sample_rate)
        self._clock = clock
        self._wall = wall_clock
        self._lock = threading.Lock()
        self._file = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        self._next_batch: int | None = None
        self._radio_written = False
        self._readback_written = False
        self._last_radio: dict[str, Any] = {}
        self._last_counters: dict[str, float] = {}
        self.counts: dict[str, int] = {}
        self.write_errors = 0
        self.lost_batches = 0

    # ------------------------------------------------------------ writing

    def _write(self, kind: str, fields: dict[str, Any], *, mono: float | None = None,
               wall: float | None = None) -> None:
        line = {"k": kind, "node": self.node_id,
                "mono": round(self._clock() if mono is None else mono, 6),
                "wall": round(self._wall() if wall is None else wall, 6)}
        line.update(fields)
        try:
            text = json.dumps(line, separators=(",", ":"), default=str)
            with self._lock:
                if self._file.closed:
                    return
                self._file.write(text + "\n")
                self._file.flush()
            self.counts[kind] = self.counts.get(kind, 0) + 1
        except Exception:
            self.write_errors += 1

    def start(self, meta: dict[str, Any]) -> None:
        self._write("start", {"schema": EVENTS_SCHEMA, "version": EVENTS_SCHEMA_VERSION,
                              "sample_rate": self.sample_rate, "burst_fields": list(BURST_FIELDS),
                              **meta})

    def stop(self, extra: dict[str, Any] | None = None) -> None:
        self._write("stop", {"counts": dict(self.counts), "lost_batches": self.lost_batches,
                             "write_errors": self.write_errors, **(extra or {})})
        with self._lock:
            if not self._file.closed:
                self._file.close()

    # ------------------------------------------------------------ events

    def on_event(self, kind: str, value: Any) -> None:
        """One engine event (the listener of :class:`~.node_engine.ProcessNodeWorker`)."""

        try:
            if kind == "bursts":
                self._bursts(value)
            elif kind == "gain":
                self._write("gain", {"dir": value.get("dir"), "db": value.get("db"),
                                     "readback": value.get("readback")},
                            mono=value.get("t"))
            elif kind == "mark":
                self._write("mark", {"text": value.get("text")}, mono=value.get("t"))
            elif kind == "failed":
                self._write("failed", {"text": str(value)})
            elif kind == "stats" and not self._readback_written:
                self._readback(value)
        except Exception:
            self.write_errors += 1

    def _readback(self, stats: dict[str, Any]) -> None:
        """The device's own report of its settings (UHD readback), once, when it first appears."""

        readback = {}
        for direction in ("tx", "rx"):
            transport = (stats.get(direction) or {}).get("transport") or {}
            if isinstance(transport.get("readback"), dict):
                readback[direction] = transport["readback"]
        if readback:
            self._write("readback", readback)
            self._readback_written = True

    def _bursts(self, batch: dict[str, Any]) -> None:
        seq = int(batch["seq"])
        if self._next_batch is not None and seq != self._next_batch:
            missing = (seq - self._next_batch) % (1 << 31)
            self.lost_batches += missing
            self._write("gap", {"lost_batches": missing, "next_seq": seq})
        self._next_batch = seq + 1
        for off, dev_t in batch.get("anchors", ()):
            self._write("rx_time", {"off": off, "dev_t": dev_t})
        for off, dev_t, lost in batch.get("overflows", ()):
            self._write("overflow", {"source": "rx_time", "off": off, "dev_t": dev_t,
                                     "lost": lost})
        for row in batch.get("events", ()):
            fields = dict(zip(BURST_FIELDS, row, strict=True))
            mono, wall = fields.pop("mono"), fields.pop("wall")
            for key in ("snr_db", "peak"):
                fields[key] = _round(fields[key], 3)
            if fields["dev_t"] is not None:
                fields["dev_t"] = round(fields["dev_t"], 9)
            self._write("burst", fields, mono=mono, wall=wall)
        for state, off, mono, wall, *rest in batch.get("locks", ()):
            dev_t = rest[0] if rest else None
            self._write("lock", {"state": state, "off": off,
                                 "stream_t": round(off / self.sample_rate, 6),
                                 "dev_t": None if dev_t is None else round(dev_t, 9)},
                        mono=mono, wall=wall)

    def status(self, line: dict[str, Any] | None) -> None:
        """One node_app stats line: counters, and ``radio`` / ``freq`` / ``overflow`` changes."""

        if line is None:
            return
        tx, rx = line.get("tx") or {}, line.get("rx") or {}
        tx_transport = tx.get("transport") or {}
        rx_transport = rx.get("transport") or {}
        radio = {
            "tx_freq_hz": tx_transport.get("center_frequency_hz"),
            "rx_freq_hz": rx_transport.get("center_frequency_hz"),
            "tx_gain_db": tx_transport.get("tx_gain_db", tx_transport.get("gain_db")),
            "rx_gain_db": rx_transport.get("rx_gain_db"),
            "tx_bandwidth_hz": tx.get("bandwidth_hz"),
            "rx_bandwidth_hz": rx.get("bandwidth_hz"),
        }
        if not self._radio_written:
            self._write("radio", radio)
            self._radio_written = True
        else:
            changed = {key: value for key, value in radio.items()
                       if key.endswith("freq_hz") and value != self._last_radio.get(key)}
            if changed:
                self._write("freq", changed)
        self._last_radio = radio
        faults = rx_transport.get("uhd_faults") or {}
        counters = {
            "uhd_rx_overflow": faults.get("rx_overflow"),
            "chunks_dropped_overflow": rx_transport.get("chunks_dropped_overflow"),
            "rx_overflow_events": rx_transport.get("rx_overflow_events"),
        }
        for name, value in counters.items():
            if not isinstance(value, (int, float)):
                continue
            before = self._last_counters.get(name)
            if before is not None and value > before:
                self._write("overflow", {"source": "counter", "counter": name,
                                         "delta": value - before, "total": value})
            self._last_counters[name] = value
        self._write("status", {
            "elapsed_s": line.get("elapsed_s"),
            "tx": {key: tx.get(key) for key in ("bursts", "control_bursts", "subframes",
                                                "payload_bytes", "active_mcs")},
            "rx": {
                "decoder": rx.get("decoder"),
                "receiver": rx.get("receiver"),
                "rx_samples_lost": rx_transport.get("rx_samples_lost"),
                "rx_time_discontinuities": rx_transport.get("rx_time_discontinuities"),
                "uhd_faults": faults or None,
                "chunks_dropped_overflow": rx_transport.get("chunks_dropped_overflow"),
                "queue_full_backpressure_events": rx_transport.get(
                    "queue_full_backpressure_events"),
            },
            "la": {key: (line.get("la") or {}).get(key) for key in ("mode", "mcs_index")},
            "dropped_events": line.get("dropped_events"),
            "lost_batches": self.lost_batches,
        }, mono=line.get("monotonic_s"), wall=line.get("wall_time"))


class BurstEventTap:
    """The engine child's receive observer when ``--events`` is on.

    It forwards every call to the node's own observer (the link controller)
    first, then drains the recorder and puts one numbered ``bursts`` batch on
    the event queue.  ``source`` is a callable returning the live receive
    transport, whose ``time_anchors()`` (UHD only) gives device time.
    """

    def __init__(self, inner, recorder: BurstEventRecorder, put: Callable[[str, Any], None],
                 source: Callable[[], Any]) -> None:
        self._inner = inner
        self._recorder = recorder
        self._put = put
        self._source = source
        self._seq = 0
        self._anchors_seen = 0
        self._anchors: list[tuple[int, float]] = []
        # The receive thread emits after every feed; the final flush comes from
        # the engine's control thread at detach.
        self._lock = threading.Lock()
        self.errors = 0

    def on_receive(self, now: float, **kwargs) -> None:
        if self._inner is not None:
            self._inner.on_receive(now, **kwargs)
        self.emit()

    def emit(self, *, final: bool = False) -> None:
        with self._lock:
            self._emit(final)

    def _emit(self, final: bool) -> None:
        try:
            new_anchors, overflows = self._new_anchors()
            if final:
                self._recorder.flush()
            events, locks = self._recorder.drain(self._anchors)
            if not (events or locks or new_anchors or overflows):
                return
            self._put("bursts", {"seq": self._seq, "events": [tuple(e) for e in events],
                                 "locks": locks, "anchors": new_anchors,
                                 "overflows": overflows})
            self._seq += 1
        except Exception:  # never raised into the receive thread
            self.errors += 1

    def _new_anchors(self) -> tuple[list[tuple[int, float]], list[tuple]]:
        source = self._source()
        reader = getattr(source, "time_anchors", None)
        if reader is None:
            return [], []
        total, anchors = reader()
        fresh = min(total - self._anchors_seen, len(anchors))
        self._anchors_seen = total
        if fresh <= 0:
            return [], []
        new = list(anchors[-fresh:])
        for off, seconds, _lost in new:
            self._anchors.append((off, seconds))
        del self._anchors[:-64]
        self._anchors.sort()
        return ([(off, seconds) for off, seconds, _ in new],
                [(off, seconds, lost) for off, seconds, lost in new if lost > 0])


def read_events(path: str, kinds: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Lines of an events file (a torn last line is skipped), optionally only ``kinds``."""

    wanted = None if kinds is None else set(kinds)
    rows = []
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    for i, text in enumerate(lines):
        if not text.strip():
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                break
            raise
        if wanted is None or row.get("k") in wanted:
            rows.append(row)
    return rows


__all__ = [
    "BURST_FIELDS",
    "CLIPPING_LEVEL",
    "DEFAULT_LOCK_TIMEOUT_S",
    "EVENTS_SCHEMA",
    "EVENTS_SCHEMA_VERSION",
    "BurstEvent",
    "BurstEventRecorder",
    "BurstEventTap",
    "EventLogWriter",
    "read_events",
]
