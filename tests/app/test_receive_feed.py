"""Receive feed batching (acceleration.rx_*, --rx-*) and finite-sample sources.

``acceleration.rx_min_feed_samples`` / ``rx_max_merge_wait_ms`` and the
``--rx-min-feed-samples`` / ``--rx-max-merge-wait-ms`` flags set how the
receive worker merges short transport chunks.  Left out, every app keeps
8192 samples and 2 ms.  A source with ``finite_samples = True`` (UHD, udp)
lets the worker skip the decoder's finite check; udp drops a non-finite
burst itself.
"""

from __future__ import annotations

import queue
import socket
import struct
import time
from pathlib import Path

import numpy as np
import pytest
import yaml
from stream_fixtures import MEASUREMENT, deliver, profile, v5_stream

from ofdm_link.config import ConfigError, load_config
from ofdm_link.phy import streaming as streaming_module
from ofdm_link.runtime import cpu_budget
from ofdm_message_link import engine, node_app, node_engine, node_options, options, rx_app
from ofdm_message_link.link import MessageReceiver
from ofdm_message_link.rx_app import ReceiveWorker
from ofdm_message_link.transport import ReceiveFeed, UdpSampleSource

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = str(ROOT / "configs" / "default.yaml")
ONE_WAY = str(ROOT / "configs" / "profiles" / "ota_2p45ghz.yaml")
DUPLEX = str(ROOT / "configs" / "profiles" / "duplex_fdd_2420_2460.yaml")
JETSON = ReceiveFeed(min_samples=65536, max_wait_s=0.015)


def _overlay(tmp_path: Path, **acceleration) -> str:
    path = tmp_path / "feed.yaml"
    path.write_text(yaml.safe_dump({"acceleration": acceleration}), encoding="utf-8")
    return str(path)


def _rx(*argv: str) -> ReceiveFeed:
    args = rx_app.build_parser().parse_args(["--config", DEFAULT, *argv])
    return options.resolve(args).receive_feed


def _node(*argv: str) -> ReceiveFeed:
    args = node_options.parse_args(["--node-id", "1", "--headless", "--config", DEFAULT, *argv])
    return node_options.resolve_node(args).resolved.receive_feed


@pytest.fixture
def recorded_budget(monkeypatch):
    """Keep this test process's thread pools as they are."""

    def record(cores: int) -> cpu_budget.CpuBudget:
        resolved = cpu_budget.resolve_cpu_cores(cores)
        return cpu_budget.CpuBudget(cores=resolved, numeric_threads=resolved, bounded_libraries=())

    monkeypatch.setattr(cpu_budget, "apply_cpu_budget", record)


class _Reached(Exception):
    pass


# --------------------------------------------------------------------------
# Configuration and flags
# --------------------------------------------------------------------------


def test_default_yaml_leaves_both_keys_to_the_built_in_defaults():
    acceleration = load_config(DEFAULT).acceleration
    assert acceleration.rx_min_feed_samples is None
    assert acceleration.rx_max_merge_wait_ms is None
    assert ReceiveFeed() == ReceiveFeed(8192, 0.002)
    assert (rx_app.MIN_FEED_SAMPLES, rx_app.MAX_MERGE_WAIT_S) == (8192, 0.002)


def test_every_receiving_app_keeps_8192_samples_and_2_ms():
    assert _rx() == _node() == ReceiveFeed()
    args = rx_app.build_parser().parse_args(["--config", DEFAULT])
    assert args.rx_min_feed_samples is None and args.rx_max_merge_wait_ms is None


def test_a_configuration_without_the_keys_or_with_null_gets_the_defaults(tmp_path):
    raw = yaml.safe_load(Path(DEFAULT).read_text(encoding="utf-8"))
    del raw["acceleration"]["rx_min_feed_samples"], raw["acceleration"]["rx_max_merge_wait_ms"]
    base = tmp_path / "base.yaml"
    base.write_text(yaml.safe_dump(raw), encoding="utf-8")
    acceleration = load_config(str(base)).acceleration
    assert (acceleration.rx_min_feed_samples, acceleration.rx_max_merge_wait_ms) == (None, None)
    args = rx_app.build_parser().parse_args(["--config", str(base)])
    assert options.resolve(args).receive_feed == ReceiveFeed()
    null = _overlay(tmp_path, rx_min_feed_samples=None, rx_max_merge_wait_ms=None)
    assert _rx("--overlay", ONE_WAY, "--overlay", null) == ReceiveFeed()


def test_the_configuration_overrides_the_defaults_and_the_flags_override_it(tmp_path):
    jetson = _overlay(tmp_path, rx_min_feed_samples=65536, rx_max_merge_wait_ms=15)
    assert _rx("--overlay", ONE_WAY, "--overlay", jetson) == JETSON
    assert _node("--overlay", DUPLEX, "--overlay", jetson) == JETSON
    flags = ("--rx-min-feed-samples", "4096", "--rx-max-merge-wait-ms", "2.5")
    expected = ReceiveFeed(4096, 0.0025)
    assert _rx("--overlay", ONE_WAY, "--overlay", jetson, *flags) == expected
    assert _node("--overlay", DUPLEX, "--overlay", jetson, *flags) == expected
    # Each value on its own: a flag for one, the configuration for the other.
    assert _rx("--overlay", ONE_WAY, "--overlay", jetson, "--rx-min-feed-samples", "0") == (
        ReceiveFeed(0, 0.015)
    )
    assert _node("--overlay", DUPLEX, "--rx-max-merge-wait-ms", "0") == ReceiveFeed(8192, 0.0)


def test_the_bounds_are_inclusive(tmp_path):
    edges = _overlay(tmp_path, rx_min_feed_samples=131072, rx_max_merge_wait_ms=20)
    assert _rx("--overlay", ONE_WAY, "--overlay", edges) == ReceiveFeed(131072, 0.02)
    zero = _overlay(tmp_path, rx_min_feed_samples=0, rx_max_merge_wait_ms=0)
    assert _rx("--overlay", ONE_WAY, "--overlay", zero) == ReceiveFeed(0, 0.0)
    assert _rx("--rx-max-merge-wait-ms", "20", "--rx-min-feed-samples", "131072") == (
        ReceiveFeed(131072, 0.02)
    )


@pytest.mark.parametrize(
    "acceleration",
    [
        {"rx_min_feed_samples": -1},
        {"rx_min_feed_samples": 131073},
        {"rx_min_feed_samples": 2.5},
        {"rx_min_feed_samples": True},
        {"rx_min_feed_samples": "8192"},
        {"rx_max_merge_wait_ms": -0.1},
        {"rx_max_merge_wait_ms": 20.5},
        {"rx_max_merge_wait_ms": float("nan")},
        {"rx_max_merge_wait_ms": True},
        {"rx_max_merge_wait_ms": "2"},
    ],
)
def test_invalid_configuration_values_are_rejected(tmp_path, acceleration):
    with pytest.raises(ConfigError, match="acceleration.rx_"):
        load_config(DEFAULT, [_overlay(tmp_path, **acceleration)])


@pytest.mark.parametrize(
    "argv",
    [
        ("--rx-min-feed-samples", "-1"),
        ("--rx-min-feed-samples", "131073"),
        ("--rx-min-feed-samples", "1.5"),
        ("--rx-max-merge-wait-ms", "20.01"),
        ("--rx-max-merge-wait-ms", "-1"),
        ("--rx-max-merge-wait-ms", "nan"),
    ],
)
def test_invalid_flags_are_rejected(argv):
    with pytest.raises(SystemExit):
        rx_app.build_parser().parse_args(list(argv))
    with pytest.raises(SystemExit):
        node_options.build_parser().parse_args(["--node-id", "1", *argv])


def test_tx_app_has_no_feed_flags_and_keeps_the_default():
    from ofdm_message_link import tx_app

    args = tx_app.build_parser().parse_args(["--config", DEFAULT])
    assert not hasattr(args, "rx_min_feed_samples")
    assert options.resolve(args).receive_feed == ReceiveFeed()


# --------------------------------------------------------------------------
# The resolved values reach every receive worker
# --------------------------------------------------------------------------


@pytest.fixture
def offscreen_qt(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5.QtWidgets")
    from ofdm_message_link import qt_runtime

    return qt_runtime.create_application()


def _resolved_rx(tmp_path, *argv):
    jetson = _overlay(tmp_path, rx_min_feed_samples=65536, rx_max_merge_wait_ms=15)
    args = rx_app.build_parser().parse_args(
        ["--config", DEFAULT, "--overlay", ONE_WAY, "--overlay", jetson, *argv]
    )
    return options.resolve(args)


@pytest.mark.gui
def test_rx_app_thread_engine_hands_the_values_to_its_worker(tmp_path, monkeypatch, offscreen_qt):
    seen = {}

    def worker(receiver, bridge, plots, **kwargs):
        seen.update(kwargs)
        raise _Reached

    monkeypatch.setattr(rx_app, "ReceiveWorker", worker)
    with pytest.raises(_Reached):
        rx_app.ReceiveWindow(_resolved_rx(tmp_path), ("127.0.0.1", 9), build_source=None)
    assert (seen["min_feed_samples"], seen["max_merge_wait_s"]) == (65536, 0.015)


@pytest.mark.gui
def test_rx_app_process_engine_hands_the_values_to_its_child(tmp_path, monkeypatch, offscreen_qt):
    arguments = []

    class Engine:
        def __init__(self, target, args, dispatch):
            assert target is engine._rx_main
            arguments.append(args)
            raise _Reached

    monkeypatch.setattr(engine, "_EngineProcess", Engine)
    with pytest.raises(_Reached):
        rx_app.ReceiveWindow(
            _resolved_rx(tmp_path, "--rx-max-merge-wait-ms", "7"),
            ("127.0.0.1", 9),
            build_source=None,
            process_engine=True,
        )
    assert arguments[0][4] == ReceiveFeed(65536, 0.007)

    # And the child's main builds its worker with them.
    seen = {}

    def worker(receiver, bridge, plots, **kwargs):
        seen.update(kwargs)
        raise _Reached

    monkeypatch.setattr(rx_app, "ReceiveWorker", worker)
    monkeypatch.setattr(engine, "_bound_numeric_threads", lambda cores: None)
    with pytest.raises(_Reached):
        engine._rx_main(*arguments[0], queue.Queue(), queue.Queue(), queue.Queue())
    assert (seen["min_feed_samples"], seen["max_merge_wait_s"]) == (65536, 0.007)


def test_node_app_hands_the_values_to_its_engine_and_worker(tmp_path, monkeypatch,
                                                            recorded_budget):
    started = {}

    def process_worker(profile, spec, listener, **kwargs):
        started.update(profile=profile, spec=spec, **kwargs)
        raise _Reached

    monkeypatch.setattr(node_app, "ProcessNodeWorker", process_worker)
    jetson = _overlay(tmp_path, rx_min_feed_samples=65536, rx_max_merge_wait_ms=15)
    with pytest.raises(_Reached):
        node_app.main(["--node-id", "1", "--headless", "--config", DEFAULT, "--overlay", DUPLEX,
                       "--overlay", jetson, "--rx-min-feed-samples", "32768"])
    spec = started["spec"]
    assert spec.receive_feed == ReceiveFeed(32768, 0.015)

    seen = {}

    def worker(receiver, bridge, plots, **kwargs):
        seen.update(kwargs)
        receiver.close()
        raise _Reached

    monkeypatch.setattr(rx_app, "ReceiveWorker", worker)
    monkeypatch.setattr(node_engine, "_bound_numeric_threads", lambda cores: None)
    with pytest.raises(_Reached):
        node_engine._node_main(started["profile"], spec, 15, 1, started["aggregation"], 1,
                               queue.Queue(), queue.Queue(), queue.Queue())
    assert (seen["min_feed_samples"], seen["max_merge_wait_s"]) == (32768, 0.015)


# --------------------------------------------------------------------------
# What a worker delivers does not depend on the batching
# --------------------------------------------------------------------------


class _Signal:
    def __init__(self):
        self.values = []

    def emit(self, value):
        self.values.append(value)


class _Bridge:
    def __init__(self):
        self.message = _Signal()
        self.level = _Signal()
        self.failed = _Signal()
        self.link_snr = _Signal()


class _QueueSource:
    description = "queued chunks"

    def __init__(self, chunks, *, finite=None):
        self._queue: queue.Queue = queue.Queue()
        for chunk in chunks:
            self._queue.put(chunk)
        if finite is not None:
            self.finite_samples = finite

    def start(self):
        pass

    def stop(self):
        pass

    def recv(self, timeout):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None


class _RecordingReceiver(MessageReceiver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[tuple[int, dict]] = []

    def feed(self, samples, **kwargs):
        self.calls.append((int(np.asarray(samples).size), dict(kwargs)))
        return super().feed(samples, **kwargs)


def _run(worker, source, receiver, total):
    worker.start()
    worker.attach(source)
    deadline = time.monotonic() + 30.0
    try:
        while sum(size for size, _ in receiver.calls) < total and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        worker.stop()


def test_the_jetson_batching_delivers_the_same_payloads_in_order():
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=31, bursts=12, mcs_indices=(8, 11, 14, 17))
    chunks = [stream[start : start + 2560] for start in range(0, stream.size, 2560)]
    receiver = _RecordingReceiver(link)
    bridge = _Bridge()
    worker = ReceiveWorker(receiver, bridge, min_feed_samples=JETSON.min_samples,
                           max_merge_wait_s=JETSON.max_wait_s)
    _run(worker, _QueueSource(chunks), receiver, stream.size)
    assert not bridge.failed.values
    sizes = [size for size, _ in receiver.calls]
    assert sum(sizes) == stream.size
    assert all(size >= JETSON.min_samples for size in sizes[:-1])
    delivered = [message.message.payload for message in bridge.message.values]
    assert delivered == deliver(link, stream) == payloads


@pytest.mark.parametrize(("finite", "trusted"), [(None, False), (False, False), (1, False),
                                                 ("yes", False), (True, True)])
def test_only_a_finite_samples_source_skips_the_check(finite, trusted):
    link = profile(MEASUREMENT)
    receiver = _RecordingReceiver(link)
    worker = ReceiveWorker(receiver, _Bridge(), min_feed_samples=0)
    chunk = np.zeros(4096, np.complex64)
    _run(worker, _QueueSource([chunk], finite=finite), receiver, chunk.size)
    assert receiver.calls == [(4096, {"assume_finite": True} if trusted else {})]


def test_the_real_sources_declare_finite_samples():
    from ofdm_message_link.pluto import PlutoSampleSource
    from ofdm_message_link.transport import UhdSampleSource

    assert UhdSampleSource.finite_samples is True
    assert UdpSampleSource.finite_samples is True
    assert getattr(PlutoSampleSource, "finite_samples", False) is False


def test_message_receiver_checks_each_piece_once_unless_told_it_is_finite(monkeypatch):
    calls = []
    real = streaming_module.all_finite

    def counted(values):
        calls.append(int(values.size))
        return real(values)

    monkeypatch.setattr(streaming_module, "all_finite", counted)
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=3, bursts=3)
    receiver = MessageReceiver(link)
    delivered = receiver.feed(stream)
    assert calls == [min(8192, stream.size - start) for start in range(0, stream.size, 8192)]
    calls.clear()
    trusted = MessageReceiver(link)
    assert [m.message.payload for m in trusted.feed(stream, assume_finite=True)] == [
        m.message.payload for m in delivered
    ] == payloads
    assert calls == []
    # complex128 is checked whatever the caller says.
    trusted.feed(stream[:1000].astype(np.complex128), assume_finite=True)
    assert calls == [1000]


def _send_burst(port: int, burst_id: int, samples: np.ndarray) -> None:
    raw = samples.astype(np.complex64).tobytes()
    step = 1024 * 8
    pieces = [raw[start : start + step] for start in range(0, len(raw), step)]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        for index, piece in enumerate(pieces):
            sender.sendto(struct.pack(">IHH", burst_id, index, len(pieces)) + piece,
                          ("127.0.0.1", port))


def _non_idle(source: UdpSampleSource, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = source.recv(0.05)
        if chunk is not None and np.any(chunk != 0):
            return chunk
    return None


def test_udp_drops_a_non_finite_burst_and_counts_it():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    source = UdpSampleSource(port=port, idle_chunk_samples=256)
    source.start()
    try:
        bad = np.full(3000, 0.5 + 0.5j, np.complex64)
        bad[1234] = np.nan
        _send_burst(port, 1, bad)
        assert _non_idle(source, 0.5) is None
        assert source.snapshot()["bursts_non_finite"] == 1
        good = np.full(3000, 0.25 - 0.5j, np.complex64)
        _send_burst(port, 2, good)
        np.testing.assert_array_equal(_non_idle(source, 2.0), good)
        snapshot = source.snapshot()
        assert snapshot["bursts_non_finite"] == 1 and snapshot["bursts_received"] == 2
    finally:
        source.stop()


@pytest.mark.parametrize("snr", [float("nan"), float("inf"), -float("inf"), -770.0, -800.0,
                                 -3090.0, -3300.0, 3100.0])
def test_udp_refuses_an_snr_whose_noise_is_not_finite(snr):
    # -800 dB made the noise inf in complex64, about -3090 dB nan, and below
    # about -3240 dB the variance factor underflows (ZeroDivisionError);
    # above about 3080 dB it overflows (OverflowError in the reader thread).
    with pytest.raises(ValueError, match="finite"):
        UdpSampleSource(port=9, snr_db=snr)


@pytest.mark.parametrize("snr", [-740.0, -60.0, 0.0, 25.0, 400.0, 3000.0])
def test_udp_noise_stays_finite_at_every_accepted_snr(snr):
    source = UdpSampleSource(port=9, snr_db=snr, idle_chunk_samples=4096, seed=1)
    for _ in range(20):
        assert np.isfinite(source._idle_chunk().view(np.float32)).all()


def test_the_startup_line_names_the_feed_values_and_their_sources(tmp_path, monkeypatch,
                                                                   recorded_budget, capsys):
    def process_worker(profile, spec, listener, **kwargs):
        raise _Reached

    monkeypatch.setattr(node_app, "ProcessNodeWorker", process_worker)
    jetson = _overlay(tmp_path, rx_min_feed_samples=65536, rx_max_merge_wait_ms=15)
    with pytest.raises(_Reached):
        node_app.main(["--node-id", "1", "--headless", "--config", DEFAULT, "--overlay", DUPLEX,
                       "--overlay", jetson, "--rx-max-merge-wait-ms", "2.5"])
    assert ("; rx_min_feed_samples 65536 from configuration acceleration.rx_min_feed_samples; "
            "rx_max_merge_wait_ms 2.5 from --rx-max-merge-wait-ms") in capsys.readouterr().err

    def window(resolved, egress, **kwargs):
        raise _Reached

    monkeypatch.setattr(rx_app, "ReceiveWindow", window)
    monkeypatch.setattr(rx_app.qt_runtime, "create_application", lambda: None)
    from ofdm_link.runtime import allocator

    monkeypatch.setattr(allocator, "keep_freed_memory", lambda: None)
    with pytest.raises(_Reached):
        rx_app.main(["--config", DEFAULT, "--overlay", ONE_WAY])
    line = capsys.readouterr().err
    assert line.rstrip().endswith("; rx_min_feed_samples 8192 from app default; "
                                  "rx_max_merge_wait_ms 2 from app default")


def test_tx_app_startup_line_has_no_feed_values(monkeypatch, recorded_budget, capsys):
    from ofdm_message_link import tx_app

    resolved = options.resolve(tx_app.build_parser().parse_args(["--config", DEFAULT]))
    options.apply_cpu_budget(resolved)
    assert "rx_min_feed_samples" not in capsys.readouterr().err
