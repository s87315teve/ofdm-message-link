"""TX health export: one flat record, and the ingress drops tx_app used to lose silently."""

from __future__ import annotations

import socket
import time

from ofdm_message_link import tx_app
from ofdm_message_link.tx_health import tx_health


def test_a_uhd_snapshot_gives_every_counter():
    snapshot = {
        "bursts_rejected_queue_full": 7,
        "tx_source": {"depth": 3, "capacity": 16},
        "uhd_faults": {"tx_underflow": 2, "tx_underflow_in_packet": 1, "tx_time_error": 5,
                       "events": []},
    }
    assert tx_health(snapshot, queue_datagrams=11, ingress_dropped_queue_full=4) == {
        "queue_datagrams": 11,
        "queue_bursts": 3,
        "ingress_dropped_queue_full": 4,
        "bursts_rejected_queue_full": 7,
        "tx_underflow": 2,
        "tx_underflow_in_packet": 1,
        "tx_late": 5,
    }


def test_missing_counters_are_none_not_zero():
    for snapshot in (None, {"transport": "udp"}, {"transport": "pluto", "bursts_sent": 3}):
        health = tx_health(snapshot, queue_datagrams=0, ingress_dropped_queue_full=0)
        assert health["tx_underflow"] is None and health["tx_late"] is None
        assert health["queue_bursts"] is None and health["bursts_rejected_queue_full"] is None
        assert health["ingress_dropped_queue_full"] == 0


def test_ingress_counts_what_a_full_queue_refuses():
    accepted = []

    def submit(source, payload):
        if len(accepted) >= 3:
            return False
        accepted.append(payload)
        return True

    listener = tx_app.IngressListener("127.0.0.1", 0, submit)
    listener.start()
    try:
        port = listener._socket.getsockname()[1]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for index in range(8):
                sender.sendto(bytes([index]) * 10, ("127.0.0.1", port))
        deadline = time.monotonic() + 5.0
        while listener.datagrams < 8 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        listener.stop()
    assert listener.datagrams == 8
    assert len(accepted) == 3 and listener.dropped_queue_full == 5


def test_tx_app_has_a_stats_log_option_off_by_default():
    args = tx_app.build_parser().parse_args([])
    assert args.stats_log is None
    args = tx_app.build_parser().parse_args(["--stats-log", "x.jsonl", "--stats-log-max-mb", "2"])
    assert args.stats_log == "x.jsonl" and args.stats_log_max_mb == 2.0


def test_the_transmit_window_writes_tx_health_to_its_stats_log(tmp_path, monkeypatch):
    import json

    import pytest

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5.QtWidgets")
    from ofdm_message_link import log_limits, options, qt_runtime

    application = qt_runtime.create_application()
    args = tx_app.build_parser().parse_args(
        ["--udp-port", "53311", "--ingress-port", "53312", "--engine", "thread"]
    )
    resolved = options.resolve(args)
    log = log_limits.RotatingLog(str(tmp_path / "tx.jsonl"), max_bytes=1 << 20, backups=1)
    window = tx_app.TransmitWindow(
        resolved,
        53312,
        build_sink=lambda selection: options.build_sink(args, resolved, selection),
        stats_log=log,
    )
    try:
        window._ingress.dropped_queue_full = 3  # as if a full queue had refused three
        window._refresh_stats()
        application.processEvents()
    finally:
        window.close()
    rows = [json.loads(line) for line in (tmp_path / "tx.jsonl").read_text().splitlines()]
    assert rows and rows[-1]["tx_health"]["ingress_dropped_queue_full"] == 3
    assert rows[-1]["tx_health"]["queue_datagrams"] == 0
    assert rows[-1]["tx_health"]["tx_late"] is None  # the udp transport has no UHD faults
