"""The node_app window builds, shows a node's state and steers the worker (offscreen)."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets  # noqa: E402

from ofdm_message_link import node_app, node_options  # noqa: E402
from ofdm_message_link.link import ReceiveStats  # noqa: E402


@pytest.fixture(scope="module")
def application():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FakeWorker:
    instances: list = []

    def __init__(self, profile, spec, listener, **kwargs):
        self.listener = listener
        self.spec = spec
        self.calls = []
        self.attached = None
        FakeWorker.instances.append(self)

    def start(self):
        self.calls.append("start")

    def attach_from(self, factory):
        self.calls.append(("attach", type(factory).__name__))
        self.attached = "fake"
        return "fake radio"

    def set_gain(self, direction, gain_db):
        self.calls.append(("gain", direction, gain_db))
        return gain_db

    def select_mcs_entry(self, entry):
        self.calls.append(("mcs", None if entry is None else entry.index))

    def set_la_mode(self, mode):
        self.calls.append(("la", mode))

    def mark(self, text):
        self.calls.append(("mark", text))

    def submit(self, source, payload):
        self.calls.append(("submit", source, payload))
        return True

    def detach(self):
        self.calls.append("detach")
        self.attached = None

    def stop(self):
        self.calls.append("stop")


def _stats():
    return {
        "tx": {"bursts": 10, "control_bursts": 2, "subframes": 8, "payload_bytes": 7200,
               "messages": 8, "samples": 200_000, "last_mcs": 12, "pending": 1,
               "ingress_datagrams": 8, "ingress_dropped_queue_full": 0,
               "transport": {"transport": "udp"}},
        "rx": {"receiver": ReceiveStats(bursts_decoded=5, message_bytes_delivered=4500,
                                        control_frames=7),
               "decoder": None, "egress_datagrams": 5,
               "transport": {"transport": "udp"}, "decode_pool": None},
        "la": {"mode": "auto", "mcs_index": 12, "reason": "up", "snr_f_db": 18.0,
               "offset_db": -0.5, "snr_eff_db": 17.5, "feedback_age_s": 0.05, "stale": False,
               "peer_loss_10s": 0.01, "decisions": 3, "feedback_sent": 4,
               "feedback_received": 5, "feedback_stale": 0},
        "cpu": {}, "device": None,
    }


def test_window_shows_cards_and_steers_the_worker(application, tmp_path):
    from ofdm_message_link.node_window import NodeWindow, feedback_colour

    args = node_options.parse_args(["--node-id", "1", "--gain-profile", "0:-5"])
    setup = node_options.resolve_node(args)
    spec = node_app.build_spec(args, setup)
    window = NodeWindow(args, setup, spec, worker_class=FakeWorker,
                        ready_file=str(tmp_path / "ready"))
    worker = FakeWorker.instances[-1]
    try:
        assert window.windowTitle() == "OFDM node 1"
        assert ("attach", "UdpDuplexFactory") in worker.calls
        assert (tmp_path / "ready").exists()
        for _ in range(3):
            window._on_event("link_snr", (18.0,))
            window._on_event("stats", _stats())
            window._on_event("la", {"mcs_index": 12, "reason": "up"})
        window._refresh_stats()
        assert window._mcs_card.value_text() == "MCS 12"
        assert window._snr_card.value_text() == "18.0 dB"
        assert window._la_trend.point_count >= 2
        assert ("gain", "tx", -5.0) in worker.calls  # the gain profile's first step
        window._fixed_button.setChecked(True)
        assert ("mcs", 8) in worker.calls
        window._auto_button.setChecked(True)
        assert ("mcs", None) in worker.calls
        window._input.setText("hello")
        window._send_typed()
        assert ("submit", "typed", b"hello") in worker.calls
        assert feedback_colour(0.1) == "green" and feedback_colour(None) == "red"
    finally:
        window.close()
    assert "detach" in worker.calls and "stop" in worker.calls
    assert not (tmp_path / "ready").exists()
