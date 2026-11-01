"""node_engine pieces that need no child process: duty cap, worker order, factories."""

from __future__ import annotations

import numpy as np

from ofdm_message_link.node_engine import (
    DuplexEndpoint,
    DutyCappedSink,
    NodeSpec,
    NodeWorker,
    PlutoDuplexFactory,
    UdpDuplexFactory,
    build_algorithms,
)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Sink:
    def __init__(self, clock):
        self.clock = clock
        self.sent = []
        self.gain = None

    def send(self, samples):
        self.sent.append((self.clock.now, len(samples)))
        self.clock.now += len(samples) / 5e6  # the push blocks for the airtime
        return True

    def set_gain(self, gain):
        self.gain = gain
        return gain

    def snapshot(self):
        return {"transport": "fake"}


def test_duty_cap_holds_back_back_to_back_bursts():
    clock = _Clock()
    inner = _Sink(clock)
    sink = DutyCappedSink(inner, 0.5, 5e6, clock=clock, sleep=clock.sleep)
    burst = np.zeros(35_000, dtype=np.complex64)  # 7 ms
    for _ in range(200):
        sink.send(burst)
    starts = [start for start, _ in inner.sent]
    airtime = 200 * 0.007
    elapsed = clock.now - starts[0]
    assert airtime / elapsed <= 0.5 + 0.02
    assert sink.snapshot()["tx_duty_cap"] == 0.5 and sink.waits > 0
    assert sink.set_gain(-5.0) == -5.0 and inner.gain == -5.0  # delegated


def test_duty_cap_lets_a_burst_after_a_pause_go_at_once():
    clock = _Clock()
    inner = _Sink(clock)
    sink = DutyCappedSink(inner, 0.5, 5e6, clock=clock, sleep=clock.sleep)
    burst = np.zeros(35_000, dtype=np.complex64)
    sink.send(burst)
    clock.now += 1.0
    before = sink.waits
    sink.send(burst)
    assert sink.waits == before


class _Direction:
    def __init__(self, name, log):
        self.name = name
        self.log = log

    def start(self):
        pass

    def attach(self, transport):
        self.log.append(("attach", self.name))

    def detach(self):
        self.log.append(("detach", self.name))

    def set_gain(self, gain):
        return gain

    def stop(self):
        pass


class _Shared:
    def __init__(self, log):
        self.log = log

    def configure(self, rate):
        self.log.append(("configure", rate))

    def close(self):
        self.log.append(("close",))

    def snapshot(self):
        return {"ensm_mode": "fdd"}


class _Controller:
    def __init__(self):
        self.tx_gain = None

    def set_tx_gain(self, gain):
        self.tx_gain = gain


def test_node_worker_starts_transmit_first_and_stops_it_first():
    log = []
    controller = _Controller()
    worker = NodeWorker(_Direction("tx", log), _Direction("rx", log), controller=controller)
    endpoint = DuplexEndpoint("sink", "source", shared=_Shared(log), sample_rate=5_000_000)
    worker.attach(endpoint)
    assert worker.set_gain("tx", -12.0) == -12.0 and controller.tx_gain == -12.0
    worker.detach()
    assert log == [
        ("configure", 5_000_000),
        ("attach", "tx"),
        ("attach", "rx"),
        ("detach", "tx"),
        ("detach", "rx"),
        ("close",),
    ]


def test_an_ingress_port_clash_fails_the_attach():
    log = []
    worker = NodeWorker(_Direction("tx", log), _Direction("rx", log),
                        ingress_error="cannot open UDP ingress port 52211: in use")
    try:
        worker.attach(DuplexEndpoint("sink", "source"))
    except Exception as error:
        assert "52211" in str(error)
    else:
        raise AssertionError("attach should fail")
    assert log == []


def test_factories_pickle_and_build_udp_endpoints():
    import pickle

    udp = UdpDuplexFactory(host="127.0.0.1", tx_port=52214, rx_port=52224)
    assert pickle.loads(pickle.dumps(udp)) == udp
    endpoint = udp()
    assert endpoint.shared is None and "52214" in endpoint.description
    pluto = PlutoDuplexFactory("usb:1.2.5", "serial", 5_000_000, 2.42e9, 2.46e9, -5.0, 15.0,
                               "I acknowledge that this process will transmit RF")
    assert pickle.loads(pickle.dumps(pluto)) == pluto


def test_algorithms_build_from_spec_strings():
    spec = NodeSpec(node_id=1, peer_id=2, mcs_index=12, control_mcs_index=8,
                    seed_range=(1, 63), ingress_port=1, egress=("127.0.0.1", 2),
                    la_spec="olla", la_args=("target_loss=0.02",))
    la, reporter, ladder = build_algorithms(spec, 5e6)
    assert ladder and hasattr(la, "decide") and hasattr(reporter, "build")
