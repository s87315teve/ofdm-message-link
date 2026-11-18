"""Sample transports: localhost UDP for development, UHD for real OTA.

Both halves of the demo talk to one of these and to nothing else, so the same
GUI, the same PHY and the same measurements run over either.  ``udp`` needs no
radio and no RF authorisation and is the only transport this repository can
exercise unattended; ``uhd`` is the real link and is gated exactly as every
other transmit path in this project is.

An ADALM-Pluto is a third transport with the same two interfaces; it lives in
:mod:`.pluto` because it is driven through libiio rather than UHD.

An OFDM burst leaves the encoder with a peak-to-average ratio of about
9.5-10 dB (wire v5, scrambled; the legacy bursts had about 13-14 dB) and a
peak well above 1.0.  A UHD sink clips anything outside the unit circle,
so every sink here scales each burst by its own peak to a configured
amplitude.  Scaling by peak rather than by power is what keeps the clipping
guarantee independent of the payload.
"""

from __future__ import annotations

import math
import queue
import socket
import struct
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from ofdm_link.phy._arrays import all_finite

from .devices import uhd_access_lock

_UDP_HEADER = struct.Struct(">IHH")
_SAMPLES_PER_DATAGRAM = 1024
_COMPLEX_BYTES = 8
# Bounds how long a stopping TX graph can wait on an idle source thread.
_TX_IDLE_WAIT_S = 0.05
# UHD's B200 default of 16 USB receive frames holds only a few milliseconds of
# samples, less than one GUI repaint that keeps the GIL from the RX sink block.
# 256 frames is about 0.1 s at 5 MS/s; the receive defaults below scale with
# the sample rate so every rate keeps that much time in each buffer.
DEFAULT_RX_RECV_FRAMES = 256
_RX_REFERENCE_RATE = 5_000_000
# The Python sink block needs the GIL once per work() call.  While the decode
# owner is busy that wait can reach the 5 ms switch interval; a cap of 8192
# samples (1.6 ms at 5 MS/s) then fell behind OTA at 330 burst/s, 65536
# (13 ms) does not.  This caps one call; how many samples a call usually gets
# is the ingest multiple N (rx_ingest_multiple), or without it whatever UHD
# has delivered (~5.9k samples, ~843 calls/s at 5 MS/s on a B210).
_RX_CHUNK_SAMPLES = 65536
_MAX_RX_RECV_FRAMES = 1024
# A full receive queue makes the sink block return 0, and GNU Radio calls it
# again at once while the input is still there: without a wait that loop spins
# on the GIL and starves the receiver that would drain the queue (B210 node_app:
# 1.9 million back-pressure returns and ~20 % of samples lost in 190 s).  The
# blocking put releases the GIL while it waits.
_RX_QUEUE_WAIT_S = 0.005
# rx_time tags kept for device time: one at start and one per overflow or retune.
ANCHOR_HISTORY = 64
# Chunks the UHD receive queue holds when GNU Radio is not batching (N = 1):
# the depth every release before ingest batching used.
_RX_QUEUE_DEPTH_UNBATCHED = 16
# With batching the queue is bounded in samples instead: at most this much of
# the stream (one chunk is always accepted into an empty queue).
_RX_QUEUE_BUDGET_S = 0.1
# The one switch for ingest batching on the PC defaults.  True: the default
# ReceiveFeed (8192 samples, 2 ms) batches like any other feed.  False: the
# default feed gives N = 1, so a PC run without acceleration settings keeps
# the pre-batching sink behaviour; a configured feed (Jetson) still batches.
RX_BATCH_DEFAULT_FEED = True


@dataclass(frozen=True, slots=True)
class ReceiveFeed:
    """How the receive worker batches transport chunks into decoder feeds.

    A chunk shorter than ``min_samples`` is merged with the chunks queued
    behind it, waiting at most ``max_wait_s`` for them (rx_app.ReceiveWorker).
    Each feed has a fixed cost, so larger feeds save CPU on a slow host
    (Jetson: 65536 samples, 15 ms); the samples and their order never change.
    The defaults are the PC setting every release has used.

    A UHD source also uses it to batch earlier, in GNU Radio itself (see
    :func:`rx_ingest_multiple`), so the Python sink block runs once per
    batch instead of once per few thousand samples.
    """

    min_samples: int = 8192
    max_wait_s: float = 0.002


def _rx_rate_scale(sample_rate: float) -> int:
    return max(1, math.ceil(float(sample_rate) / _RX_REFERENCE_RATE))


def rx_ingest_multiple(feed: ReceiveFeed, sample_rate: float, chunk_samples: int) -> int:
    """How many samples GNU Radio groups before the Python RX sink sees them (N).

    The sink block then receives only whole multiples of N (``set_output_multiple``),
    so its fixed per-call cost (the GIL, the gateway, a copy and a queue put)
    is paid once per N samples instead of once per UHD packet group (~843
    calls/s at 5 MS/s).  N is the feed's ``min_samples``, but never more than
    ``max_wait_s`` of samples (GNU Radio waits for a whole N, so N is also the
    longest a sample waits before the decoder can see it) nor than the sink's
    chunk cap.  1 means no batching: a feed of 0 samples, a wait shorter than
    two samples, or the default feed while :data:`RX_BATCH_DEFAULT_FEED` is off.
    """

    if not RX_BATCH_DEFAULT_FEED and feed == ReceiveFeed():
        return 1
    within_wait = int(feed.max_wait_s * float(sample_rate))
    if feed.min_samples <= 0 or within_wait < 2:
        return 1
    return max(1, min(int(feed.min_samples), within_wait, int(chunk_samples)))


def rx_queue_limits(
    sample_rate: float, ingest_multiple: int, fixed_depth: int | None = None
) -> tuple[int | None, int | None]:
    """(most chunks, most samples) the UHD receive queue holds; None is no limit.

    Without batching the queue keeps the 16 chunks it always had.  With
    batching it is bounded in samples, to :data:`_RX_QUEUE_BUDGET_S` of the
    stream: a chunk is refused (back-pressure) when the samples already queued
    plus its own would pass that, unless the queue is empty.  A bound in chunks
    would not do, because after back-pressure the sink takes up to its chunk
    cap per call, not N.  ``fixed_depth`` (the ``queue_depth`` argument) also
    caps the number of chunks.
    """

    if ingest_multiple <= 1:
        return (_RX_QUEUE_DEPTH_UNBATCHED if fixed_depth is None else fixed_depth), None
    return fixed_depth, math.ceil(_RX_QUEUE_BUDGET_S * float(sample_rate))


class _RxChunkQueue:
    """FIFO of (chunk, tags) bounded in chunks and in queued samples."""

    def __init__(self, max_chunks: int | None, max_samples: int | None) -> None:
        self.max_chunks = max_chunks
        self.max_samples = max_samples
        self._items: deque[tuple[Any, int]] = deque()
        self._samples = 0
        self._ready = threading.Condition()

    @property
    def queued_samples(self) -> int:
        return self._samples

    def _fits(self, count: int) -> bool:
        if self.max_chunks is not None and len(self._items) >= self.max_chunks:
            return False
        return (
            self.max_samples is None
            or not self._items
            or self._samples + count <= self.max_samples
        )

    def put(self, item: Any, count: int, timeout: float) -> bool:
        """Queue ``item`` of ``count`` samples, waiting up to ``timeout`` for room."""

        with self._ready:
            if not self._ready.wait_for(lambda: self._fits(count), timeout):
                return False
            self._items.append((item, count))
            self._samples += count
            self._ready.notify_all()
            return True

    def get(self, timeout: float) -> Any | None:
        with self._ready:
            if not self._ready.wait_for(lambda: bool(self._items), max(0.0, timeout)):
                return None
            item, count = self._items.popleft()
            self._samples -= count
            self._ready.notify_all()
            return item


DEFAULT_TX_PORT = 52101
DEFAULT_PEAK_AMPLITUDE = 0.7


class TransportError(RuntimeError):
    """Raised when a transport cannot be opened or used as configured."""


class SampleSink(Protocol):
    """Somewhere to put one burst's samples."""

    def start(self) -> None: ...

    def send(self, samples: NDArray[np.complex64]) -> bool: ...

    def stop(self) -> None: ...

    def snapshot(self) -> dict[str, object]: ...


class SampleSource(Protocol):
    """Somewhere to get a continuous stream of samples from."""

    def start(self) -> None: ...

    def recv(self, timeout: float) -> NDArray[np.complex64] | None: ...

    def stop(self) -> None: ...

    def snapshot(self) -> dict[str, object]: ...


def scale_to_peak(
    samples: NDArray[np.complex64],
    peak_amplitude: float,
) -> NDArray[np.complex64]:
    """Scale one burst so its largest magnitude is ``peak_amplitude``."""

    values = np.asarray(samples, dtype=np.complex64)
    peak = float(np.max(np.abs(values))) if values.size else 0.0
    if peak <= 0.0:
        return values
    return (values * np.complex64(peak_amplitude / peak)).astype(np.complex64, copy=False)


# --------------------------------------------------------------------------
# UDP loopback
# --------------------------------------------------------------------------


class UdpSampleSink:
    """Send each burst as a short run of UDP datagrams.

    A burst larger than one datagram is split and tagged, and the receiver
    drops any burst whose fragments do not all arrive.  That is UDP behaving
    like UDP; it is not a model of any particular radio channel.

    ``retry_failed_send``: a send that raised may have sent some fragments,
    but never all of them, and a retry goes under a new burst id, so the
    receiver can never hand on the burst twice.  The fragments of the failed
    attempt stay unmatched and show in the source's ``bursts_incomplete``
    once evicted (one spurious count per failed attempt that got any out).
    """

    retry_failed_send = True

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_TX_PORT,
        peak_amplitude: float = DEFAULT_PEAK_AMPLITUDE,
    ) -> None:
        self._address = (host, int(port))
        self._peak_amplitude = float(peak_amplitude)
        self._socket: socket.socket | None = None
        self._burst_id = 0
        self._datagrams_sent = 0
        self._bursts_sent = 0

    @property
    def description(self) -> str:
        return f"udp {self._address[0]}:{self._address[1]}"

    def start(self) -> None:
        if self._socket is None:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)

    def send(self, samples: NDArray[np.complex64]) -> bool:
        if self._socket is None:
            raise TransportError("sink is not started")
        scaled = scale_to_peak(samples, self._peak_amplitude)
        raw = scaled.tobytes()
        per_datagram = _SAMPLES_PER_DATAGRAM * _COMPLEX_BYTES
        chunks = [raw[at : at + per_datagram] for at in range(0, len(raw), per_datagram)]
        if not chunks or len(chunks) > 0xFFFF:
            return False
        self._burst_id = (self._burst_id + 1) & 0xFFFFFFFF
        for index, chunk in enumerate(chunks):
            header = _UDP_HEADER.pack(self._burst_id, index, len(chunks))
            try:
                self._socket.sendto(header + chunk, self._address)
            except OSError as error:
                raise TransportError(f"UDP send failed: {error}") from error
            self._datagrams_sent += 1
        self._bursts_sent += 1
        return True

    def stop(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def snapshot(self) -> dict[str, object]:
        return {
            "transport": "udp",
            "destination": f"{self._address[0]}:{self._address[1]}",
            "peak_amplitude": self._peak_amplitude,
            "bursts_sent": self._bursts_sent,
            "datagrams_sent": self._datagrams_sent,
        }


class UdpSampleSource:
    """Reassemble UDP bursts and present them as a free-running stream.

    Between bursts this emits noise-only chunks, so the streaming decoder does
    the same continuous preamble search it has to do on a real receiver rather
    than being handed burst-aligned buffers.  With ``snr_db`` set, AWGN is
    added at that SNR relative to the measured mean power of the most recent
    burst; the idle chunks carry the same noise.  This is a development aid,
    not a channel model: there is no fading, no frequency offset and no
    sample-clock error beyond what the samples already carry.

    Every chunk it returns is finite (``finite_samples``): a reassembled
    burst with a NaN or infinite sample is dropped, and counted in
    ``bursts_non_finite``, before it reaches the queue.
    """

    # The receive worker may skip the decoder's finite check for this source.
    finite_samples = True

    def __init__(
        self,
        *,
        bind_host: str = "127.0.0.1",
        port: int = DEFAULT_TX_PORT,
        snr_db: float | None = None,
        idle_chunk_samples: int = 4096,
        queue_depth: int = 64,
        seed: int | None = None,
    ) -> None:
        if type(idle_chunk_samples) is not int or idle_chunk_samples < 256:
            raise TransportError("idle_chunk_samples must be an integer at least 256")
        if snr_db is not None:
            # The added noise must stay finite: see finite_samples.  Checked
            # as _with_noise computes it, for a full-scale reference power,
            # with ten standard deviations of room in complex64.
            snr_db = float(snr_db)
            try:
                factor = 10.0 ** (snr_db / 10.0) if math.isfinite(snr_db) else math.nan
            except OverflowError:
                factor = math.inf
            scale = math.sqrt(1.0 / factor / 2.0) if factor > 0.0 else math.inf
            if not math.isfinite(factor) or not 10.0 * scale < float(np.finfo(np.float32).max):
                raise ValueError(f"snr_db {snr_db:g} gives noise that is not finite")
        self._address = (bind_host, int(port))
        self._snr_db = None if snr_db is None else float(snr_db)
        self._idle_chunk_samples = idle_chunk_samples
        self._queue: queue.Queue[NDArray[np.complex64]] = queue.Queue(maxsize=queue_depth)
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._rng = np.random.default_rng(seed)
        self._lock = threading.Lock()
        self._pending: dict[int, dict[int, bytes]] = {}
        self._bursts_received = 0
        self._bursts_incomplete = 0
        self._bursts_non_finite = 0
        self._datagrams_received = 0
        self._dropped_overflow = 0
        self._reference_power = 1.0

    @property
    def description(self) -> str:
        snr = "no added noise" if self._snr_db is None else f"{self._snr_db:g} dB AWGN"
        return f"udp {self._address[0]}:{self._address[1]} ({snr})"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 21)
        try:
            self._socket.bind(self._address)
        except OSError as error:
            self._socket.close()
            self._socket = None
            raise TransportError(f"cannot bind {self._address}: {error}") from error
        self._socket.settimeout(0.05)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="udp-sample-source", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                packet, _ = self._socket.recvfrom(1 << 16)
            except TimeoutError:
                self._emit(self._idle_chunk())
                continue
            except OSError:
                return
            if len(packet) <= _UDP_HEADER.size:
                continue
            burst_id, index, count = _UDP_HEADER.unpack(packet[: _UDP_HEADER.size])
            self._datagrams_received += 1
            fragments = self._pending.setdefault(burst_id, {})
            fragments[index] = packet[_UDP_HEADER.size :]
            if len(fragments) < count:
                self._evict(burst_id)
                continue
            del self._pending[burst_id]
            raw = b"".join(fragments[position] for position in range(count))
            samples = np.frombuffer(raw, dtype=np.complex64)
            self._bursts_received += 1
            if not all_finite(samples):
                # Checked once here so the decoder need not check every chunk.
                self._bursts_non_finite += 1
                continue
            with self._lock:
                power = float(np.mean(np.abs(samples) ** 2))
                if np.isfinite(power) and power > 0.0:
                    self._reference_power = power
            self._emit(self._with_noise(samples))

    def _evict(self, keep: int) -> None:
        while len(self._pending) > 8:
            oldest = next(iter(self._pending))
            if oldest == keep:
                break
            del self._pending[oldest]
            self._bursts_incomplete += 1

    def _idle_chunk(self) -> NDArray[np.complex64]:
        return self._with_noise(np.zeros(self._idle_chunk_samples, dtype=np.complex64))

    def _with_noise(self, samples: NDArray[np.complex64]) -> NDArray[np.complex64]:
        if self._snr_db is None:
            return samples
        with self._lock:
            reference = self._reference_power
        variance = reference / (10.0 ** (self._snr_db / 10.0))
        noise = self._rng.normal(scale=np.sqrt(variance / 2.0), size=(samples.size, 2))
        return (samples + (noise[:, 0] + 1j * noise[:, 1])).astype(np.complex64, copy=False)

    def _emit(self, samples: NDArray[np.complex64]) -> None:
        try:
            self._queue.put_nowait(samples)
        except queue.Full:
            self._dropped_overflow += 1

    def recv(self, timeout: float) -> NDArray[np.complex64] | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._socket = None

    def snapshot(self) -> dict[str, object]:
        return {
            "transport": "udp",
            "listening": f"{self._address[0]}:{self._address[1]}",
            "snr_db": self._snr_db,
            "bursts_received": self._bursts_received,
            "bursts_incomplete": self._bursts_incomplete,
            "bursts_non_finite": self._bursts_non_finite,
            "datagrams_received": self._datagrams_received,
            "chunks_dropped_overflow": self._dropped_overflow,
        }


# --------------------------------------------------------------------------
# UHD / real radio
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UhdSinkLimits:
    queue_capacity: int = 16
    # Each burst is timed this far past the device clock so UHD holds the
    # whole burst before keying the transmitter; an untimed burst starts on
    # its first packet and underflows whenever the host falls behind.
    tx_lead_seconds: float = 0.05
    # OTA captures show the B210 front end about 5 dB low for the first
    # ~100 us after it keys up, settling by ~200 us: exactly where preamble,
    # training and header sit.  Zeros lead each keyed burst through that.
    tx_settle_guard_seconds: float = 250e-6
    # A timed burst starting exactly where the previous one ends reaches the
    # radio late ('L'): OTA, 1122 of 1470 queued-up video bursts with no gap,
    # none with 20 us.
    tx_burst_gap_seconds: float = 20e-6
    # Items in the TX source's output buffer; larger than any burst this demo
    # sends (a 996-byte MCS 0 frame is about 21,000 samples plus guards).
    source_buffer_samples: int = 1 << 16


class UhdSampleSink:
    """Transmit bursts through a persistent GNU Radio UHD flowgraph.

    The sink block is built by :func:`ofdm_link.radio.create_b210_sink`, so the
    project's RF capability gate applies unchanged: without a valid
    :class:`~ofdm_link.radio.RFEnableToken` no sink is constructed at all.

    Each burst is emitted as one length-tagged run, which is what makes UHD
    key the transmitter for exactly that burst and stop between them.  Each
    run also carries a ``tx_time`` a short lead past the device clock and a
    short gap past the end of the previous burst, so a burst is on the air only once UHD
    already holds its samples, and starts with a zero guard that covers the
    front end's key-up settling.
    """

    def __init__(
        self,
        settings: Any,
        rf_enable: Any,
        *,
        peak_amplitude: float = DEFAULT_PEAK_AMPLITUDE,
        limits: UhdSinkLimits | None = None,
    ) -> None:
        self._settings = settings
        self._rf_enable = rf_enable
        self._peak_amplitude = float(peak_amplitude)
        self._limits = limits or UhdSinkLimits()
        self._top: Any | None = None
        self._source_block: Any | None = None
        self._sink_block: Any | None = None
        self._async_block: Any | None = None
        self._fault_collector: Any | None = None
        self._bursts_sent = 0
        self._rejected_full = 0
        self._next_free_device_time = 0.0
        self._readback: dict[str, object] | None = None
        # Gain read back after an operator change; None until the first one.
        self._gain_db: float | None = None

    @property
    def description(self) -> str:
        args = getattr(self._settings, "device_args", "") or "first available device"
        return f"uhd {args} @ {getattr(self._settings, 'center_frequency', 0) / 1e6:.3f} MHz"

    def start(self) -> None:
        if self._top is not None:
            return
        import pmt
        from gnuradio import gr

        from ofdm_link.radio.uhd import create_b210_sink
        from ofdm_link.radio.uhd_faults import UhdFaultCollector

        # Every block stays referenced from Python for the lifetime of the
        # flowgraph; one reachable only from C++ can be collected under the
        # running scheduler, which segfaults the process.
        with uhd_access_lock():
            self._sink_block = create_b210_sink(self._settings, self._rf_enable)
        require_tuned(self._sink_block, self._settings.center_frequency)
        self._source_block = _build_tx_source_block(self._limits.queue_capacity)()
        self._fault_collector = UhdFaultCollector(max_events=64)
        self._async_block = _build_uhd_async_sink_block(
            "ofdm_message_link_tx_async",
            self._fault_collector,
        )()
        # One work() call can then hand UHD a whole burst.  With GNU Radio's
        # default few-thousand-sample buffer a burst took several calls, each
        # waiting for the GIL, and a late one underflowed mid-burst on air.
        self._source_block.set_min_output_buffer(self._limits.source_buffer_samples)
        self._top = gr.top_block("ofdm_message_link_tx")
        self._top.connect(self._source_block, self._sink_block)
        self._top.msg_connect(
            (self._sink_block, pmt.intern("async_msgs")),
            (self._async_block, pmt.intern("in")),
        )
        self._top.start()
        # Read once: a UHD control call holds the GIL for its USB round trip,
        # and these values do not change while the flowgraph runs.
        self._readback = _block_readback(self._sink_block)

    # A raising send never queued anything: every step that can raise (the
    # started check, scaling, the device time) runs before the enqueue, which
    # either takes the whole burst or returns False.
    retry_failed_send = True

    def send(self, samples: NDArray[np.complex64]) -> bool:
        if self._source_block is None:
            raise TransportError("sink is not started")
        scaled = scale_to_peak(samples, self._peak_amplitude)
        guard = int(round(self._limits.tx_settle_guard_seconds * float(self._settings.sample_rate)))
        if guard:
            scaled = np.concatenate((np.zeros(guard, dtype=np.complex64), scaled))
        start = max(
            _device_time_now(self._sink_block) + self._limits.tx_lead_seconds,
            self._next_free_device_time + self._limits.tx_burst_gap_seconds,
        )
        if not self._source_block.enqueue(scaled, start):
            self._rejected_full += 1
            return False
        self._next_free_device_time = start + scaled.size / float(self._settings.sample_rate)
        self._bursts_sent += 1
        return True

    def set_gain(self, gain_db: float) -> float:
        """Change the transmit gain while bursts keep flowing; returns the readback."""

        if self._sink_block is None:
            raise TransportError("sink is not started")
        self._gain_db = _apply_gain(self._sink_block, gain_db, self._readback)
        return self._gain_db

    def wait_until_drained(self, timeout: float) -> bool:
        """Wait until every queued sample has been consumed by the UHD sink."""

        if self._source_block is None or self._sink_block is None:
            raise TransportError("sink is not started")
        if not np.isfinite(timeout) or timeout <= 0.0:
            raise TransportError("timeout must be finite and positive")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            source = self._source_block.snapshot()
            consumed = _block_item_count(self._sink_block, "nitems_read")
            if source["depth"] == 0 and consumed >= source["samples_enqueued"]:
                return True
            time.sleep(0.002)
        return False

    def stop(self) -> None:
        if self._top is None:
            return
        if self._source_block is not None:
            self._source_block.shutdown()
        self._top.stop()
        self._top.wait()
        self._top = None
        self._source_block = None
        self._sink_block = None
        self._async_block = None
        self._fault_collector = None

    def snapshot(self) -> dict[str, object]:
        source = None if self._source_block is None else self._source_block.snapshot()
        consumed = (
            None
            if self._sink_block is None
            else _block_item_count(self._sink_block, "nitems_read")
        )
        faults = (
            None
            if self._fault_collector is None
            else self._fault_collector.snapshot().to_dict()
        )
        return {
            "transport": "uhd",
            "device_args": getattr(self._settings, "device_args", ""),
            "center_frequency_hz": getattr(self._settings, "center_frequency", None),
            "sample_rate_sps": getattr(self._settings, "sample_rate", None),
            "tx_gain_db": (
                self._gain_db
                if self._gain_db is not None
                else getattr(self._settings, "tx_gain", None)
            ),
            "peak_amplitude": self._peak_amplitude,
            "bursts_sent": self._bursts_sent,
            "bursts_rejected_queue_full": self._rejected_full,
            "tx_source": source,
            "samples_consumed_by_uhd_sink": consumed,
            "uhd_faults": faults,
            "readback": self._readback,
        }


class UhdSampleSource:
    """Receive a continuous sample stream from a GNU Radio UHD flowgraph.

    Receiving transmits nothing, so this needs no RF authorisation.  UHD
    converts the radio's 16-bit integer samples to complex64, so every
    sample is finite (``finite_samples``).

    With a :class:`ReceiveFeed` set (:meth:`set_receive_feed`, before
    :meth:`start`), GNU Radio hands the Python sink block only whole multiples
    of N samples (:func:`rx_ingest_multiple`), and the queue between the sink
    and :meth:`recv` holds at most 100 ms of samples (:func:`rx_queue_limits`).
    The sink counts the samples each ``rx_time`` tag says were lost as soon as
    it has queued the chunk holding the tag (``rx_samples_lost``,
    ``rx_time_discontinuities``); the tag becomes a device-time anchor
    (:meth:`time_anchors`) only when :meth:`recv` returns that chunk, so an
    anchor is never published ahead of the samples it describes.
    """

    # The receive worker may skip the decoder's finite check for this source.
    finite_samples = True

    def __init__(
        self,
        settings: Any,
        *,
        chunk_samples: int | None = None,
        queue_depth: int | None = None,
        recv_frames: int | None = None,
    ) -> None:
        """``None`` scales chunk size and UHD receive frames with the sample
        rate, keeping the buffering time of the 5 MS/s defaults; an explicit
        ``recv_frames=0`` leaves UHD's own default in place.  ``queue_depth``
        ``None`` sizes the chunk queue at each start (:func:`rx_queue_limits`);
        a number also caps it at that many chunks."""

        scale = _rx_rate_scale(getattr(settings, "sample_rate", _RX_REFERENCE_RATE))
        if chunk_samples is None:
            chunk_samples = _RX_CHUNK_SAMPLES * scale
        if recv_frames is None:
            recv_frames = min(_MAX_RX_RECV_FRAMES, DEFAULT_RX_RECV_FRAMES * scale)
        if type(chunk_samples) is not int or chunk_samples < 256:
            raise TransportError("chunk_samples must be an integer at least 256")
        if queue_depth is not None and (type(queue_depth) is not int or queue_depth < 1):
            raise TransportError("queue_depth must be a positive integer or None")
        self._settings = settings
        self._recv_frames = recv_frames or None
        self._chunk_samples = chunk_samples
        self._fixed_queue_depth = queue_depth
        # Each item is one chunk and the rx_time tags inside it, as (stream
        # offset, device seconds, samples lost before it); recv publishes them.
        self._queue = _RxChunkQueue(*rx_queue_limits(0.0, 1, queue_depth))
        self._feed: ReceiveFeed | None = None
        self._ingest_multiple = 1
        self._top: Any | None = None
        self._source_block: Any | None = None
        self._sink_block: Any | None = None
        self._async_block: Any | None = None
        self._fault_collector: Any | None = None
        self._queue_full_backpressure = 0
        self._chunks_received = 0
        self._samples_received = 0
        self._gaps: RxTimeGapCounter | None = None
        # The queue recv reads and the counter its chunks' tags go to, swapped
        # together at each start.
        self._stream: tuple[_RxChunkQueue, RxTimeGapCounter | None] = (self._queue, None)
        self._readback: dict[str, object] | None = None
        # Gain read back after an operator change; None until the first one.
        self._gain_db: float | None = None

    @property
    def description(self) -> str:
        args = getattr(self._settings, "device_args", "") or "first available device"
        return f"uhd {args} @ {getattr(self._settings, 'center_frequency', 0) / 1e6:.3f} MHz"

    def start(self) -> None:
        if self._top is not None:
            return
        import pmt
        from gnuradio import gr

        from ofdm_link.radio.uhd import create_b210_source
        from ofdm_link.radio.uhd_faults import UhdFaultCollector

        # Every block stays referenced from Python for the lifetime of the
        # flowgraph; one reachable only from C++ can be collected under the
        # running scheduler, which segfaults the process.
        with uhd_access_lock():
            self._source_block = create_b210_source(
                self._settings, recv_frames=self._recv_frames
            )
        require_tuned(self._source_block, self._settings.center_frequency)
        # Batching, the queue bound and the gap counter all use the rate the
        # device actually runs at, decided again at every start.  A fresh
        # queue: chunks left from the previous run would hand recv samples
        # (and tags) from before this start.
        rate = _actual_sample_rate(self._source_block, self._settings.sample_rate)
        self._gaps = RxTimeGapCounter(rate)
        self._ingest_multiple = (
            1
            if self._feed is None
            else rx_ingest_multiple(self._feed, rate, self._chunk_samples)
        )
        # Room for the sink to take a whole chunk per work() call, and for
        # GNU Radio to fill the next multiple of N while it does.
        self._source_block.set_min_output_buffer(
            2 * self._chunk_samples + (self._ingest_multiple if self._ingest_multiple > 1 else 0)
        )
        self._queue = _RxChunkQueue(
            *rx_queue_limits(rate, self._ingest_multiple, self._fixed_queue_depth)
        )
        self._stream = (self._queue, self._gaps)
        self._sink_block = _build_rx_sink_block(
            self._chunk_samples, self._enqueue, ingest_multiple=self._ingest_multiple
        )()
        if self._ingest_multiple > 1:
            # GNU Radio then calls work() only with whole multiples of N
            # samples, tags and offsets intact; a sink stopped while it waits
            # for the rest of a multiple returns at once.
            self._sink_block.set_output_multiple(self._ingest_multiple)
        self._fault_collector = UhdFaultCollector(max_events=64)
        self._async_block = _build_uhd_async_sink_block(
            "ofdm_message_link_rx_async",
            self._fault_collector,
        )()
        self._top = gr.top_block("ofdm_message_link_rx")
        self._top.connect(self._source_block, self._sink_block)
        self._top.msg_connect(
            (self._source_block, pmt.intern("async_msgs")),
            (self._async_block, pmt.intern("in")),
        )
        self._top.start()
        # Read once; see UhdSampleSink.start.
        self._readback = _block_readback(self._source_block)

    def set_gain(self, gain_db: float) -> float:
        """Change the receive gain without stopping the stream; returns the readback."""

        if self._source_block is None:
            raise TransportError("source is not started")
        self._gain_db = _apply_gain(self._source_block, gain_db, self._readback)
        return self._gain_db

    def set_receive_feed(self, feed: ReceiveFeed | None) -> None:
        """Batch the stream in GNU Radio for this feed (see :func:`rx_ingest_multiple`).

        Takes effect at the next :meth:`start`; ``None`` turns batching off.
        """

        if self._top is not None:
            raise TransportError("set the receive feed before the source is started")
        self._feed = feed

    @property
    def ingest_multiple(self) -> int:
        """N of the running stream: every chunk :meth:`recv` returns is a multiple of it."""

        return self._ingest_multiple

    def _enqueue(
        self,
        samples: NDArray[np.complex64],
        tags: tuple[tuple[int, float], ...] = (),
    ) -> bool:
        # Called from the sink block's thread only.  The losses are measured
        # here but counted only once the chunk is queued: a refused chunk is
        # offered again with the same tags, and must not count them twice.
        gaps = self._stream[1]
        measured = gaps.measure(tags) if tags and gaps is not None else ()
        if not self._queue.put((samples, measured), int(samples.size), _RX_QUEUE_WAIT_S):
            # The GNU Radio sink returns zero, so this is backpressure rather
            # than a dropped sample chunk. UHD may still overflow upstream;
            # that distinct event is collected from its async message port.
            self._queue_full_backpressure += 1
            return False
        if measured:
            gaps.count(measured)
        self._chunks_received += 1
        self._samples_received += int(samples.size)
        return True

    def recv(self, timeout: float) -> NDArray[np.complex64] | None:
        # One read of the pair: a chunk from a previous run's queue can only
        # ever publish its tags in that run's counter.
        chunks, gaps = self._stream
        item = chunks.get(timeout)
        if item is None:
            return None
        samples, tags = item
        # The chunk's rx_time tags become anchors only as it is handed on, so
        # time_anchors (--events device time) never runs ahead of the samples.
        if tags and gaps is not None:
            gaps.publish(tags)
        return samples

    def stop(self) -> None:
        if self._top is None:
            return
        self._top.stop()
        self._top.wait()
        self._top = None
        self._source_block = None
        self._sink_block = None
        self._async_block = None
        self._fault_collector = None

    def time_anchors(self) -> tuple[int, tuple[tuple[int, float, int], ...]]:
        """Device-time anchors of the receive stream from its UHD ``rx_time`` tags.

        Returns (tags seen since start, the most recent ones as (stream offset,
        device seconds, samples lost just before)).  Stream offsets count the
        samples handed to :meth:`recv` since :meth:`start`; a tag appears here
        when :meth:`recv` returns the chunk that holds it.  The device clock
        is the B210's own; nothing here sets it to host or GNSS time.
        """

        if self._gaps is None:
            return 0, ()
        return self._gaps.anchors()

    def snapshot(self) -> dict[str, object]:
        faults = (
            None
            if self._fault_collector is None
            else self._fault_collector.snapshot().to_dict()
        )
        return {
            "transport": "uhd",
            "device_args": getattr(self._settings, "device_args", ""),
            "center_frequency_hz": getattr(self._settings, "center_frequency", None),
            "sample_rate_sps": getattr(self._settings, "sample_rate", None),
            "rx_gain_db": (
                self._gain_db
                if self._gain_db is not None
                else getattr(self._settings, "rx_gain", None)
            ),
            "uhd_num_recv_frames": self._recv_frames,
            "chunk_samples": self._chunk_samples,
            "ingest_multiple": self._ingest_multiple,
            "queue_depth_chunks": self._queue.max_chunks,
            "queue_budget_samples": self._queue.max_samples,
            "queued_samples": self._queue.queued_samples,
            "chunks_received": self._chunks_received,
            "samples_received": self._samples_received,
            "chunks_dropped_overflow": 0,
            "queue_full_backpressure_events": self._queue_full_backpressure,
            "samples_consumed_by_chunk_sink": (
                None
                if self._sink_block is None
                else _block_item_count(self._sink_block, "nitems_read")
            ),
            "uhd_faults": faults,
            "rx_time_discontinuities": None if self._gaps is None else self._gaps.discontinuities,
            "rx_samples_lost": None if self._gaps is None else self._gaps.samples_lost,
            "readback": self._readback,
        }


class RxTimeGapCounter:
    """Count receive-stream discontinuities from UHD ``rx_time`` tags.

    ``usrp_source`` re-tags ``rx_time`` after every overflow, and also on start
    and on retune.  Only a tag whose time is ahead of where the samples since
    the previous tag put the clock is a gap, so a retune is not miscounted.
    Unlike the async overflow message, which UHD aggregates and rate-limits,
    this sees every overflow and how many samples it lost.
    """

    def __init__(self, sample_rate: float) -> None:
        if not math.isfinite(sample_rate) or sample_rate <= 0.0:
            raise ValueError("sample_rate must be finite and positive")
        self._rate = float(sample_rate)
        self._anchor: tuple[int, float] | None = None
        self.discontinuities = 0
        self.samples_lost = 0
        # Every tag as (stream offset, device seconds, samples lost before it),
        # the last ANCHOR_HISTORY of them, and how many there were in all: the
        # device time of any later sample is anchor seconds + offset / rate.
        self._anchors: deque[tuple[int, float, int]] = deque(maxlen=ANCHOR_HISTORY)
        self._anchors_total = 0
        self._anchor_lock = threading.Lock()

    def observe(self, offset: int, seconds: float) -> None:
        """Count one tag's loss and publish it as an anchor at once."""

        measured = self.measure(((offset, seconds),))
        self.count(measured)
        self.publish(measured)

    def measure(
        self, tags: Sequence[tuple[int, float]]
    ) -> tuple[tuple[int, float, int], ...]:
        """(offset, seconds, samples lost before it) of each tag, in order; changes nothing.

        Each tag is measured against the one before it (the last counted, then
        the earlier ones in ``tags``).  :meth:`count` makes the result count.
        """

        anchor = self._anchor
        measured = []
        for offset, seconds in tags:
            lost = 0
            if anchor is not None:
                anchor_offset, anchor_seconds = anchor
                expected = anchor_seconds + (offset - anchor_offset) / self._rate
                lost = max(0, int(round((seconds - expected) * self._rate)))
            anchor = (int(offset), float(seconds))
            measured.append((int(offset), float(seconds), lost))
        return tuple(measured)

    def count(self, measured: Sequence[tuple[int, float, int]]) -> None:
        """Add measured tags to ``discontinuities`` and ``samples_lost``."""

        for offset, seconds, lost in measured:
            if lost > 0:
                self.discontinuities += 1
                self.samples_lost += lost
            self._anchor = (offset, seconds)

    def publish(self, measured: Sequence[tuple[int, float, int]]) -> None:
        """Make counted tags visible as device-time anchors (:meth:`anchors`)."""

        with self._anchor_lock:
            for entry in measured:
                self._anchors.append(entry)
                self._anchors_total += 1

    def anchors(self) -> tuple[int, tuple[tuple[int, float, int], ...]]:
        """(tags seen in all, the most recent ones as (offset, seconds, lost))."""

        with self._anchor_lock:
            return self._anchors_total, tuple(self._anchors)


def _actual_sample_rate(block: Any, requested: float) -> float:
    """The rate the device reports it runs at, else the requested one."""

    getter = getattr(block, "get_samp_rate", None)
    if callable(getter):
        try:
            rate = float(getter())
        except Exception:  # a readback that fails must not stop the radio
            rate = math.nan
        if math.isfinite(rate) and rate > 0.0:
            return rate
    return float(requested)


def _device_time_now(sink_block: Any) -> float:
    value = sink_block.get_time_now()
    seconds = value.get_real_secs() if hasattr(value, "get_real_secs") else value
    seconds = float(seconds)
    if not math.isfinite(seconds):
        raise TransportError("UHD sink returned an invalid device time")
    return seconds


def _build_tx_source_block(capacity: int) -> type:
    """Build the queue-fed, length-tagged TX source block.

    Defined lazily so importing this module never imports GNU Radio, which
    keeps the UDP transport usable where GNU Radio is not installed.
    """

    import threading as _threading
    from collections import deque

    import pmt
    from gnuradio import gr

    from ofdm_link.radio.uhd import TX_LENGTH_TAG, TX_TIME_TAG

    class TxBurstSource(gr.sync_block):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__(
                name="ofdm_message_link_tx_source",
                in_sig=None,
                out_sig=[np.complex64],
            )
            self._pending: deque[tuple[NDArray[np.complex64], float | None]] = deque()
            self._current: NDArray[np.complex64] | None = None
            self._current_time: float | None = None
            self._offset = 0
            self._bursts_enqueued = 0
            self._bursts_emitted = 0
            self._samples_enqueued = 0
            self._samples_emitted = 0
            self._stopping = False
            self._lock = _threading.Lock()
            self._ready = _threading.Condition(self._lock)

        def enqueue(
            self,
            samples: NDArray[np.complex64],
            device_time: float | None = None,
        ) -> bool:
            with self._lock:
                if len(self._pending) + (self._current is not None) >= capacity:
                    return False
                self._pending.append((samples, device_time))
                self._bursts_enqueued += 1
                self._samples_enqueued += int(samples.size)
                self._ready.notify()
                return True

        def shutdown(self) -> None:
            """Release a ``work`` call waiting for a burst so the graph can stop."""

            with self._lock:
                self._stopping = True
                self._ready.notify_all()

        def snapshot(self) -> dict[str, int]:
            with self._lock:
                return {
                    "capacity": capacity,
                    "depth": len(self._pending) + (self._current is not None),
                    "bursts_enqueued": self._bursts_enqueued,
                    "bursts_emitted": self._bursts_emitted,
                    "samples_enqueued": self._samples_enqueued,
                    "samples_emitted": self._samples_emitted,
                }

        def work(self, input_items: object, output_items: list[np.ndarray]) -> int:
            del input_items
            with self._lock:
                if self._current is None:
                    # Returning 0 from a source parks its scheduler thread for
                    # about 250 ms, so queued bursts would leave in clumps
                    # after long silences.  Wait here for the next burst.
                    while not self._pending and not self._stopping:
                        self._ready.wait(_TX_IDLE_WAIT_S)
                    if not self._pending:
                        return 0
                    self._current, self._current_time = self._pending.popleft()
                    self._offset = 0
                current = self._current
                output = output_items[0]
                count = min(len(output), current.size - self._offset)
                if self._offset == 0:
                    offset = int(self.nitems_written(0))
                    self.add_item_tag(
                        0,
                        offset,
                        pmt.intern(TX_LENGTH_TAG),
                        pmt.from_long(int(current.size)),
                    )
                    if self._current_time is not None:
                        seconds = math.floor(self._current_time)
                        self.add_item_tag(
                            0,
                            offset,
                            pmt.intern(TX_TIME_TAG),
                            pmt.make_tuple(
                                pmt.from_uint64(int(seconds)),
                                pmt.from_double(self._current_time - seconds),
                            ),
                        )
                output[:count] = current[self._offset : self._offset + count]
                self._offset += count
                self._samples_emitted += count
                if self._offset == current.size:
                    self._current = None
                    self._offset = 0
                    self._bursts_emitted += 1
                return count

    return TxBurstSource


def _build_rx_sink_block(chunk_samples: int, enqueue: Any, *, ingest_multiple: int = 1) -> type:
    """Build the chunking RX sink block. Lazy for the same reason as above.

    Each work() call hands ``enqueue(samples, tags)`` one chunk of at most
    ``chunk_samples`` and the ``rx_time`` tags inside it, as (absolute stream
    offset, device seconds).  With ``ingest_multiple`` N > 1 (and the block's
    ``set_output_multiple(N)``, which UhdSampleSource.start sets), a chunk is
    the largest whole multiple of N available: N when the sink keeps up, more
    when it catches up.
    """

    import pmt
    from gnuradio import gr

    time_key = pmt.intern("rx_time")
    multiple = max(1, int(ingest_multiple))

    class RxChunkSink(gr.sync_block):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__(
                name="ofdm_message_link_rx_sink",
                in_sig=[np.complex64],
                out_sig=None,
            )

        def work(self, input_items: list[np.ndarray], output_items: object) -> int:
            del output_items
            incoming = input_items[0]
            count = min(len(incoming), chunk_samples)
            count -= count % multiple
            if count == 0:
                return 0
            values = np.array(incoming[:count], dtype=np.complex64, copy=True)
            values.setflags(write=False)
            tags = tuple(
                (
                    int(tag.offset),
                    pmt.to_uint64(pmt.tuple_ref(tag.value, 0))
                    + pmt.to_double(pmt.tuple_ref(tag.value, 1)),
                )
                for tag in self.get_tags_in_window(0, 0, count, time_key)
            )
            # Returning 0 leaves the samples for the next call and applies
            # back-pressure rather than silently dropping a burst mid-way.
            # The tags travel with the samples, so nothing is counted twice
            # when the same window is offered again.
            if not enqueue(values, tags):
                return 0
            return count

    return RxChunkSink


def _build_uhd_async_sink_block(name: str, collector: Any) -> type:
    """Build a bounded observer for GNU Radio UHD async fault messages."""

    import pmt
    from gnuradio import gr

    from ofdm_link.radio.uhd_faults import record_uhd_async_message

    port = pmt.intern("in")

    class AsyncMessageSink(gr.basic_block):  # type: ignore[misc]
        def __init__(self) -> None:
            super().__init__(name=name, in_sig=None, out_sig=None)
            self.message_port_register_in(port)
            self.set_msg_handler(port, self._handle)

        def _handle(self, message: object) -> None:
            record_uhd_async_message(message, pmt=pmt, collector=collector)

    return AsyncMessageSink


# UHD reaches the requested centre with LO plus DSP; a residue of a few mHz
# is rounding, anything near a kilohertz is a front end that could not get there.
_TUNE_TOLERANCE_HZ = 1_000.0


def require_tuned(block: Any, requested_hz: float) -> None:
    """Refuse to stream on a frequency the front end did not actually reach.

    UHD clamps an out-of-range request to the nearest end of the daughterboard's
    range and only says so in a log line: an N210 with a CBX (1.2-6 GHz) asked
    for 915 MHz sits at 1180 MHz, while a B210 peer really is at 915 MHz, and
    the link fails with nothing on screen explaining why.
    """

    actual = float(block.get_center_freq(0))
    if abs(actual - float(requested_hz)) > _TUNE_TOLERANCE_HZ:
        raise TransportError(
            f"the radio tuned to {actual / 1e6:.3f} MHz instead of the requested "
            f"{float(requested_hz) / 1e6:.3f} MHz; the frequency is outside this "
            "front end's range (for an N200/N210 the daughterboard sets the range)"
        )


def set_transport_gain(transport: Any, gain_db: float) -> float:
    """Change a running transport's gain, or say why it cannot be changed."""

    if transport is None:
        raise TransportError("the radio is not running")
    setter = getattr(transport, "set_gain", None)
    if not callable(setter):
        raise TransportError(f"{transport.description} has no adjustable gain")
    return float(setter(gain_db))


def _apply_gain(block: Any, gain_db: float, readback: dict[str, object] | None) -> float:
    """Set one running block's gain and return what the device reports.

    Gain is the one radio setting this example changes while streaming: it
    moves neither the sample clock nor the centre frequency, so the peer
    needs no matching change.  The device quantises the request (0.5 dB
    steps on a CBX), which is why the readback, not the request, is kept.
    """

    requested = float(gain_db)
    if not math.isfinite(requested):
        raise TransportError("gain must be a finite number of dB")
    block.set_gain(requested, 0)
    actual = float(block.get_gain(0))
    if readback is not None:
        readback["gain_db"] = actual
    return actual


def _block_item_count(block: Any, method_name: str) -> int:
    method = getattr(block, method_name, None)
    if not callable(method):
        return 0
    try:
        value = method(0)
    except (RuntimeError, TypeError, ValueError):
        return 0
    return int(value) if type(value) is int and value >= 0 else 0


def _block_readback(block: Any | None) -> dict[str, object] | None:
    if block is None:
        return None
    result: dict[str, object] = {}
    try:
        info = block.get_usrp_info(0)
        for name in ("serial", "product", "mboard_id", "name"):
            if name in info:
                result[name] = str(info[name])
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    getters = {
        "sample_rate_sps": ("get_samp_rate", ()),
        "center_frequency_hz": ("get_center_freq", (0,)),
        "gain_db": ("get_gain", (0,)),
        "bandwidth_hz": ("get_bandwidth", (0,)),
        "antenna": ("get_antenna", (0,)),
        "clock_source": ("get_clock_source", (0,)),
    }
    for name, (method_name, arguments) in getters.items():
        method = getattr(block, method_name, None)
        if not callable(method):
            continue
        try:
            value = method(*arguments)
        except (RuntimeError, TypeError, ValueError):
            continue
        result[name] = value if isinstance(value, (int, float, str, bool)) else str(value)
    return result
