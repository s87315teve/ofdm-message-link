"""One libiio context shared by a Pluto's transmit and receive transports.

Every device access goes through a fake context, so these run without a
Pluto and without libiio.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from ofdm_link.radio import acknowledge_rf_transmission
from ofdm_message_link import options, pluto
from ofdm_message_link.transport import TransportError

SERIAL = "1044000000000000000000000000000a01"
URI = "usb:3.10.5"
PHY = "ad9361-phy"


class _Buffer:
    def __init__(self, context, samples, output):
        self.context = context
        self.samples = samples
        self.output = output
        self.pushed = []
        self.closed = False

    def push(self, interleaved):
        self.pushed.append(np.array(interleaved, copy=True))

    def refill(self):
        time.sleep(0.002)
        return np.zeros(2 * self.samples, dtype=np.int16)

    def close(self):
        self.closed = True
        self.context.log.append(("close_buffer", "tx" if self.output else "rx"))


class _Context:
    """One open Pluto: records every call in order, and any overlapping calls."""

    def __init__(self, factory):
        self.factory = factory
        self.log = factory.log
        self.values = {
            (PHY, "voltage0", "hardwaregain_available", True): "[-89.750000 0.250000 0.000000]",
            (PHY, "voltage0", "rssi", False): "97.25 dB",
        }
        self.buffers = []
        self.closed = False
        self._busy = threading.Lock()

    def _enter(self):
        if not self._busy.acquire(blocking=False):
            self.factory.overlaps += 1
            self._busy.acquire()
        time.sleep(self.factory.call_delay_s)

    def attr(self, name):
        return {"hw_serial": SERIAL, "hw_model": "PlutoSDR", "fw_version": "v0.39"}.get(name, "")

    def read(self, device, channel, attr, *, output=False):
        self._enter()
        try:
            return self.values.get((device, channel, attr, output), "0")
        finally:
            self._busy.release()

    def write(self, device, channel, attr, value, *, output=False):
        self._enter()
        try:
            self.log.append(("write", channel, attr, value, output))
            self.values[(device, channel, attr, output)] = (
                f"{value} dB" if attr == "hardwaregain" else value
            )
        finally:
            self._busy.release()

    def device_read(self, device, attr):
        return {"ensm_mode": "fdd", "rx_path_rates": "BBPLL:983040000 ADC:40000000"}.get(attr, "")

    def take_overflow(self):
        self._enter()
        self._busy.release()
        return False

    def open_buffer(self, samples, *, output, kernel_buffers=None):
        self.log.append(("open_buffer", "tx" if output else "rx", kernel_buffers))
        self.buffers.append(_Buffer(self, samples, output))
        return self.buffers[-1]

    def close(self):
        self.closed = True
        self.log.append(("destroy",))


class _Factory:
    def __init__(self, call_delay_s=0.0):
        self.opened = []
        self.log = []
        self.overlaps = 0
        self.call_delay_s = call_delay_s

    def __call__(self, uri):
        self.opened.append(uri)
        return _Context(self)

    @property
    def context(self):
        return self.contexts[-1]


def _settings(frequency, gain, **extra):
    return pluto.PlutoSettings(URI, SERIAL, 5_000_000, frequency, gain, **extra)


def _token():
    return acknowledge_rf_transmission(options.RF_ACKNOWLEDGEMENT)


def _node(factory, **kwargs):
    shared = pluto.SharedPlutoContext(URI, SERIAL, open_context=factory, **kwargs)
    shared.configure(5_000_000)
    sink = pluto.PlutoSampleSink(_settings(2.420e9, -5.0), _token(), open_context=shared.factory())
    source = pluto.PlutoSampleSource(
        _settings(2.460e9, 15.0, rf_bandwidth=4e6), open_context=shared.factory()
    )
    return shared, sink, source


def test_sink_and_source_share_one_open_context():
    factory = _Factory()
    shared, sink, source = _node(factory)
    sink.start()
    source.start()
    try:
        assert factory.opened == [URI]
        assert shared.leases == 2
    finally:
        sink.stop()
        source.stop()
        shared.close()


def test_the_last_lease_destroys_the_context_and_not_before():
    factory = _Factory()
    shared, sink, source = _node(factory)
    sink.start()
    source.start()
    shared.close()  # drops the configure hold; the two leases keep it open
    assert ("destroy",) not in factory.log
    sink.stop()
    assert ("destroy",) not in factory.log and shared.leases == 1
    source.stop()
    assert factory.log[-1] == ("destroy",)
    assert shared.leases == 0 and not shared.is_open


def test_a_lease_for_another_uri_is_refused():
    shared = pluto.SharedPlutoContext(URI, SERIAL, open_context=_Factory())
    with pytest.raises(OSError, match="usb:3.10.5"):
        shared.factory()("usb:3.11.5")
    with pytest.raises(TransportError, match="not usb:3.11.5"):
        pluto.PlutoSampleSource(
            pluto.PlutoSettings("usb:3.11.5", SERIAL, 5_000_000, 2.46e9, 15.0),
            open_context=shared.factory(),
        ).start()


def test_the_sample_rate_is_written_once_for_both_directions():
    factory = _Factory()
    shared, sink, source = _node(factory)
    sink.start()
    source.start()
    try:
        rates = [
            entry
            for entry in factory.log
            if entry[:3] == ("write", "voltage0", "sampling_frequency")
        ]
        assert len(rates) == 2  # configure(): RX and TX, before any buffer
        # A different rate while a buffer is open would re-run the clock chain.
        with pytest.raises(OSError, match="sample rate"):
            shared.write_attr(PHY, "voltage0", "sampling_frequency", "2500000")
    finally:
        sink.stop()
        source.stop()
        shared.close()


def test_rf_bandwidth_follows_the_setting_or_the_sample_rate():
    factory = _Factory()
    shared, sink, source = _node(factory)
    sink.start()
    source.start()
    try:
        bandwidths = {
            entry[4]: entry[3]
            for entry in factory.log
            if entry[:3] == ("write", "voltage0", "rf_bandwidth")
        }
        assert bandwidths == {True: "5000000", False: "4000000"}
    finally:
        sink.stop()
        source.stop()
        shared.close()


def test_rf_bandwidth_outside_the_filter_range_is_refused():
    with pytest.raises(ValueError, match="analog bandwidth"):
        _settings(2.46e9, 15.0, rf_bandwidth=100e3)


def test_kernel_buffers_are_set_with_the_receive_buffer_only():
    factory = _Factory()
    shared, sink, source = _node(factory, rx_kernel_buffers=8)
    sink.start()
    source.start()
    sink.send(np.ones(100, dtype=np.complex64))
    try:
        opened = [entry for entry in factory.log if entry[0] == "open_buffer"]
        assert ("open_buffer", "rx", 8) in opened
        assert ("open_buffer", "tx", None) in opened
    finally:
        sink.stop()
        source.stop()
        shared.close()


def test_attribute_calls_from_two_threads_never_overlap():
    factory = _Factory(call_delay_s=0.001)
    shared, sink, source = _node(factory)
    sink.start()
    source.start()
    try:
        errors = []

        def hammer(output):
            try:
                for index in range(40):
                    (sink if output else source).set_gain(-10.0 - index * 0.25 if output else 10.0)
            except Exception as error:  # pragma: no cover - reported below
                errors.append(error)

        threads = [threading.Thread(target=hammer, args=(flag,)) for flag in (True, False)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        assert factory.overlaps == 0
    finally:
        sink.stop()
        source.stop()
        shared.close()


def test_stopping_transmit_first_leaves_the_attenuator_at_minimum_output():
    factory = _Factory()
    shared, sink, source = _node(factory)
    sink.start()
    source.start()
    sink.send(np.ones(64, dtype=np.complex64))
    sink.stop()
    tx_gains = [
        entry[3]
        for entry in factory.log
        if entry[:3] == ("write", "voltage0", "hardwaregain") and entry[4]
    ]
    assert tx_gains[-1] == "-89.75"
    assert ("close_buffer", "tx") in factory.log and ("destroy",) not in factory.log
    source.stop()
    shared.close()
    assert factory.log[-1] == ("destroy",)


def test_snapshot_reports_mode_rates_rssi_and_leases():
    factory = _Factory()
    shared, sink, source = _node(factory)
    source.start()
    try:
        snapshot = shared.snapshot()
        assert snapshot["ensm_mode"] == "fdd"
        assert snapshot["rx_path_rates"].startswith("BBPLL")
        assert snapshot["rssi_db"] == 97.25
        assert snapshot["leases"] == 1 and snapshot["open_buffers"] == 1
    finally:
        source.stop()
        shared.close()


def test_configure_with_a_buffer_open_is_refused():
    factory = _Factory()
    shared, sink, source = _node(factory)
    source.start()
    try:
        with pytest.raises(OSError, match="buffer"):
            shared.configure(5_000_000)
    finally:
        source.stop()
        shared.close()
