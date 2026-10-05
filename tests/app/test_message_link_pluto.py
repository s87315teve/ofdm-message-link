"""ADALM-Pluto support in the message link example, without a Pluto.

Discovery reads a fake USB sysfs tree and every device access goes through a
fake libiio context, so these run headless with no hardware and no libiio.
"""

from __future__ import annotations

import errno
import os
import threading
import time

import numpy as np
import pytest

from ofdm_link.radio import RFTransmissionDisabledError, acknowledge_rf_transmission
from ofdm_message_link import devices, options, pluto, rx_app, tx_app
from ofdm_message_link.transport import TransportError

SERIAL_A = "1044000000000000000000000000000b02"
SERIAL_B = "1044000000000000000000000000000a01"
PHY = "ad9361-phy"


class _FakeBuffer:
    def __init__(self, context, samples, output):
        self.context = context
        self.samples = samples
        self.output = output
        self.pushed = []
        self.dead = False
        self.closed = False

    def push(self, interleaved):
        assert interleaved.dtype == np.int16 and not self.closed
        assert interleaved.size <= 2 * self.samples
        if self.dead:
            raise OSError(errno.EBADF, "the buffer is gone")
        if interleaved.size < 2 * self.samples and not self.context.partial_push:
            self.dead = True
            raise OSError(errno.EFBIG, "partial push refused")
        self.pushed.append(np.array(interleaved, copy=True))

    def refill(self):
        if not self.context.incoming:
            raise OSError("stream ended")
        return self.context.incoming.pop(0)

    def close(self):
        self.closed = True


class _FakePluto:
    """Stands in for one open libiio context on a Pluto.

    ``partial_push`` is the firmware difference that matters to the sink: the
    iiod in v0.39 sends fewer samples than the buffer holds, the one in v0.30
    refuses and drops the buffer.
    """

    def __init__(self, serial=SERIAL_B, *, partial_push=True):
        self.partial_push = partial_push
        self.attrs = {
            "hw_serial": serial,
            "hw_model": "Analog Devices PlutoSDR Rev.B (Z7010-AD9363)",
            "fw_version": "v0.31",
            "ad9361-phy,model": "ad9363a",
        }
        self.values = {
            (PHY, "voltage0", "hardwaregain_available", False): "[-3 1 71]",
            (PHY, "voltage0", "hardwaregain_available", True): "[-89.750000 0.250000 0.000000]",
            (PHY, "altvoltage0", "frequency_available", True): "[325000000 1 3800000000]",
            (PHY, "altvoltage1", "frequency_available", True): "[325000000 1 3800000000]",
        }
        self.writes = []
        self.buffers = []
        self.incoming = []
        self.overflows = []
        self.tunes_to = None
        self.closed = False

    def __call__(self, uri):
        self.uri = uri
        return self

    def attr(self, name):
        return self.attrs.get(name, "")

    def read(self, device, channel, attr, *, output=False):
        if attr == "frequency" and self.tunes_to is not None:
            return str(self.tunes_to)
        value = self.values[(device, channel, attr, output)]
        return f"{value} dB" if attr == "hardwaregain" else value

    def write(self, device, channel, attr, value, *, output=False):
        self.writes.append((channel, attr, value, output))
        self.values[(device, channel, attr, output)] = value

    def take_overflow(self):
        return self.overflows.pop(0) if self.overflows else False

    def open_buffer(self, samples, *, output):
        self.buffers.append(_FakeBuffer(self, samples, output))
        return self.buffers[-1]

    def close(self):
        self.closed = True


def _settings(**overrides):
    values = {
        "uri": "usb:1.12.5",
        "serial": SERIAL_B,
        "sample_rate": 5_000_000,
        "center_frequency": 1.2e9,
        "gain_db": -30.0,
    }
    values.update(overrides)
    return pluto.PlutoSettings(**values)


def _token():
    return acknowledge_rf_transmission(options.RF_ACKNOWLEDGEMENT)


def _usb_device(root, name, *, vendor, product, serial, bus, number, iio_interface=None):
    base = root / name
    base.mkdir(parents=True)
    for attribute, value in (
        ("idVendor", vendor),
        ("idProduct", product),
        ("serial", serial),
        ("busnum", bus),
        ("devnum", number),
    ):
        (base / attribute).write_text(f"{value}\n")
    if iio_interface is not None:
        interface = root / f"{name}:1.{iio_interface}"
        interface.mkdir()
        (interface / "interface").write_text("IIO\n")
        (interface / "bInterfaceNumber").write_text(f"{iio_interface:02x}\n")
    return base


def _device_node(nodes, bus, number, mode):
    node = nodes / f"{bus:03d}" / f"{number:03d}"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text("")
    node.chmod(mode)


# -- discovery -------------------------------------------------------------


def test_discovery_lists_each_pluto_by_serial_and_ignores_other_usb_devices(tmp_path):
    sysfs, nodes = tmp_path / "sys", tmp_path / "dev"
    _usb_device(sysfs, "1-9", vendor="0456", product="b673", serial=SERIAL_A, bus=1, number=13,
                iio_interface=5)
    _usb_device(sysfs, "1-10.4", vendor="0456", product="b673", serial=SERIAL_B, bus=1, number=12,
                iio_interface=5)
    _usb_device(sysfs, "1-5", vendor="046d", product="085c", serial="WEBCAM", bus=1, number=3)
    _device_node(nodes, 1, 13, 0o660)
    _device_node(nodes, 1, 12, 0o660)

    found = pluto.discover(usb_sysfs=str(sysfs), device_nodes=str(nodes))

    assert [device.serial for device in found] == [SERIAL_B, SERIAL_A]
    assert [device.address for device in found] == ["usb:1.12.5", "usb:1.13.5"]
    assert all(device.driver == "pluto" and device.is_supported for device in found)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can open any device node")
def test_a_pluto_whose_usb_node_is_closed_to_this_user_falls_back_to_its_network_address(
    tmp_path, monkeypatch
):
    sysfs, nodes = tmp_path / "sys", tmp_path / "dev"
    _usb_device(sysfs, "1-9", vendor="0456", product="b673", serial=SERIAL_A, bus=1, number=13,
                iio_interface=5)
    (sysfs / "1-9:1.0" / "net" / "enx020000000001").mkdir(parents=True)
    _device_node(nodes, 1, 13, 0o000)
    monkeypatch.setattr(pluto, "_first_host", lambda interface: "192.168.2.1")

    (found,) = pluto.discover(usb_sysfs=str(sysfs), device_nodes=str(nodes))

    assert found.address == "ip:192.168.2.1"


def test_a_pluto_that_cannot_be_reached_says_how_to_get_usb_access():
    unreachable = devices.DiscoveredDevice(
        serial=SERIAL_A, name="ADALM-Pluto", product="", driver="pluto", address=""
    )

    with pytest.raises(devices.DeviceError, match="udev rule"):
        pluto.probe(unreachable, open_context=_FakePluto())


def test_the_device_list_follows_the_transport(monkeypatch):
    listed = (devices.DiscoveredDevice(SERIAL_B, "ADALM-Pluto", "", "pluto", "usb:1.12.5"),)
    monkeypatch.setattr(pluto, "discover", lambda: listed)

    assert devices.discover(backend="pluto") == listed


# -- probing ---------------------------------------------------------------


def _discovered(serial=SERIAL_B, address="usb:1.12.5"):
    return devices.DiscoveredDevice(serial, "ADALM-Pluto", "", "pluto", address)


def test_probe_reads_the_ranges_the_device_reports():
    fake = _FakePluto()

    capabilities = pluto.probe(_discovered(), open_context=fake)

    (channel,) = capabilities.channels
    assert channel.rx_antennas == ("RX",) and channel.tx_antennas == ("TX",)
    assert channel.rx_gain_range_db == (-3.0, 71.0)
    assert channel.tx_gain_range_db == (-89.75, 0.0)
    assert capabilities.rx_freq_range_hz == (325e6, 3800e6)
    assert "v0.31" in capabilities.mboard_name
    assert fake.closed, "a probe must not keep the device open"


def test_probe_refuses_a_different_pluto_answering_at_the_same_address():
    # Two Plutos left on the factory 192.168.2.1 answer as one.
    with pytest.raises(devices.DeviceError, match=f"{SERIAL_B}.*not {SERIAL_A}"):
        pluto.probe(
            _discovered(SERIAL_A, "ip:192.168.2.1"), open_context=_FakePluto(SERIAL_B)
        )


def test_a_pluto_offers_only_the_rate_usb_can_carry():
    capabilities = pluto.probe(_discovered(), open_context=_FakePluto())
    selection = devices.default_selection(
        capabilities, direction="rx", center_frequency_hz=1.2e9, sample_rate=10_000_000
    )

    assert capabilities.sample_rates == (5_000_000,)
    assert selection.address == "usb:1.12.5"
    problems = devices.validate(selection, capabilities, direction="rx")
    assert problems == ["sample rate must be one of 5 MS/s on this device"]
    with pytest.raises(ValueError, match="5 MS/s"):
        _settings(sample_rate=10_000_000)


def test_pluto_transmit_gain_defaults_to_full_attenuation():
    capabilities = pluto.probe(_discovered(), open_context=_FakePluto())

    selection = devices.default_selection(
        capabilities, direction="tx", center_frequency_hz=1.2e9, sample_rate=5_000_000
    )

    assert selection.gain_db == -89.75
    assert selection.antenna == "TX"


# -- transmit --------------------------------------------------------------


def test_pluto_transmit_opens_nothing_without_the_rf_capability():
    fake = _FakePluto()
    sink = pluto.PlutoSampleSink(_settings(), None, open_context=fake)

    with pytest.raises(RFTransmissionDisabledError):
        sink.start()

    assert not fake.writes and not hasattr(fake, "uri")


def test_pluto_transmit_is_quiet_until_it_is_tuned_and_quiet_again_after_stop():
    fake = _FakePluto()
    sink = pluto.PlutoSampleSink(_settings(gain_db=-30.0), _token(), open_context=fake)

    sink.start()

    gains = [index for index, write in enumerate(fake.writes) if write[1] == "hardwaregain"]
    tones = [index for index, write in enumerate(fake.writes) if write[1] == "scale"]
    tuned = next(index for index, write in enumerate(fake.writes) if write[1] == "frequency")
    assert fake.writes[gains[0]][2] == "-89.75", "the attenuator goes to maximum first"
    assert len(tones) == 4 and all(fake.writes[index][2] == "0" for index in tones)
    assert gains[0] < min(tones) < max(tones) < tuned < gains[-1]
    assert fake.writes[gains[-1]][2] == "-30.00"
    assert fake.writes[tuned] == ("altvoltage1", "frequency", "1200000000", True)

    sink.stop()

    assert fake.writes[-1] == ("voltage0", "hardwaregain", "-89.75", True)
    assert fake.closed


def _bursts():
    rng = np.random.default_rng(3)
    long = (rng.normal(size=1200) + 1j * rng.normal(size=1200)).astype(np.complex64) * 4
    return long, long[:500]


def test_bursts_of_any_length_share_one_buffer_and_never_clip():
    fake = _FakePluto()
    sink = pluto.PlutoSampleSink(_settings(), _token(), peak_amplitude=0.7, open_context=fake)
    sink.start()
    long, short = _bursts()

    assert sink.send(long) and sink.send(short) and sink.send(long)
    sink.stop()

    (buffer,) = fake.buffers
    assert buffer.samples >= 21_000, "room for the longest burst a 996-byte frame makes"
    assert [pushed.size for pushed in buffer.pushed] == [2400, 1000, 2400]
    assert buffer.closed
    sent = buffer.pushed[0].astype(np.float32).view(np.complex64)
    assert np.max(np.abs(sent)) == pytest.approx(0.7 * 32767, abs=1.5)
    np.testing.assert_allclose(sent / np.max(np.abs(sent)), long / np.max(np.abs(long)), atol=2e-4)
    assert sink.snapshot()["bursts_sent"] == 3 and sink.snapshot()["partial_push"]


def test_a_burst_longer_than_the_buffer_grows_it():
    fake = _FakePluto()
    sink = pluto.PlutoSampleSink(_settings(), _token(), open_context=fake)
    sink.start()
    short = np.ones(1000, dtype=np.complex64)
    huge = np.ones(50_000, dtype=np.complex64)

    assert sink.send(short) and sink.send(huge) and sink.send(short)
    sink.stop()

    assert [buffer.samples for buffer in fake.buffers] == [32768, 50_000]
    assert [pushed.size for pushed in fake.buffers[1].pushed] == [100_000, 2000]


def test_firmware_that_refuses_partial_pushes_gets_whole_buffers_and_loses_no_burst():
    fake = _FakePluto(partial_push=False)
    sink = pluto.PlutoSampleSink(_settings(), _token(), open_context=fake)
    sink.start()
    long, short = _bursts()

    assert sink.send(long) and sink.send(long) and sink.send(short)
    sink.stop()

    # The first buffer is the one the refused partial push took with it.
    assert [buffer.samples for buffer in fake.buffers] == [32768, 1200, 500]
    assert [len(buffer.pushed) for buffer in fake.buffers] == [0, 2, 1]
    assert all(buffer.closed for buffer in fake.buffers)
    assert sink.snapshot()["bursts_sent"] == 3 and not sink.snapshot()["partial_push"]


def test_a_pluto_that_did_not_reach_the_requested_frequency_is_refused():
    fake = _FakePluto()
    fake.tunes_to = 3_800_000_000  # the stock AD9363 limit, asked for more
    sink = pluto.PlutoSampleSink(
        _settings(center_frequency=5.8e9), _token(), open_context=fake
    )

    with pytest.raises(TransportError, match="3800.000 MHz instead of the requested 5800.000"):
        sink.start()

    assert fake.closed and not fake.buffers


def test_a_running_pluto_sink_changes_gain_and_reports_the_readback():
    fake = _FakePluto()
    sink = pluto.PlutoSampleSink(_settings(), _token(), open_context=fake)
    sink.start()

    assert sink.set_gain(-12.5) == -12.5
    assert sink.snapshot()["tx_gain_db"] == -12.5
    with pytest.raises(TransportError):
        sink.set_gain(float("nan"))
    sink.stop()


# -- receive ---------------------------------------------------------------


def test_pluto_receive_scales_the_adc_counts_and_reports_overflow(monkeypatch):
    monkeypatch.setattr(pluto, "_OVERFLOW_POLL_S", 0.0)
    fake = _FakePluto()
    fake.incoming = [
        np.array([2047, -2048, 1024, 0], dtype=np.int16),
        np.array([0, 512, -512, 0], dtype=np.int16),
    ]
    fake.overflows = [False, False, True]  # start() discards the first reading
    source = pluto.PlutoSampleSource(
        _settings(gain_db=30.0), chunk_samples=256, open_context=fake
    )

    source.start()
    first, second = source.recv(1.0), source.recv(1.0)
    source._thread.join(timeout=2.0)
    snapshot = source.snapshot()
    source.stop()

    np.testing.assert_allclose(first, [2047 / 2048 - 1j, 0.5 + 0j])
    np.testing.assert_allclose(second, [0.25j, -0.25 + 0j])
    assert first.dtype == np.complex64 and not first.flags.writeable
    assert ("voltage0", "gain_control_mode", "manual", False) in fake.writes
    assert ("voltage0", "hardwaregain", "30.00", False) in fake.writes
    assert snapshot["rx_overflow_events"] == 1
    assert snapshot["samples_received"] == 4
    assert snapshot["stream_error"] == "stream ended"
    assert fake.buffers[0].samples == 256 and fake.closed


def test_pluto_receive_survives_an_overflow_poll_while_stopping(monkeypatch):
    """stop() lets go of the context before the reader thread has finished."""

    monkeypatch.setattr(pluto, "_OVERFLOW_POLL_S", 0.0)
    crashes = []
    monkeypatch.setattr(threading, "excepthook", crashes.append)
    fake = _FakePluto()
    source = pluto.PlutoSampleSource(_settings(), chunk_samples=256, open_context=fake)

    class _HeldUntilStopping(list):
        def pop(self, index):
            while source._context is not None:
                time.sleep(0.001)
            return super().pop(index)

    fake.incoming = _HeldUntilStopping([np.zeros(4, dtype=np.int16)])
    fake.overflows = [False, False]

    source.start()
    source.stop()

    assert crashes == []
    assert fake.closed


def test_pluto_receive_refuses_the_wrong_device():
    source = pluto.PlutoSampleSource(
        _settings(serial=SERIAL_A, uri="ip:192.168.2.1"), open_context=_FakePluto(SERIAL_B)
    )

    with pytest.raises(TransportError, match="Two Plutos"):
        source.start()


# -- command line ----------------------------------------------------------


def test_pluto_transmit_is_refused_without_the_rf_capability():
    args = tx_app.build_parser().parse_args(["--transport", "pluto"])
    resolved = options.resolve(args)

    assert resolved.uses_radio
    with pytest.raises(SystemExit, match="--transport pluto transmits RF"):
        options.require_rf_capability(args)


def test_a_panel_selection_becomes_a_pluto_transport():
    selection = devices.RadioSelection(
        driver="pluto",
        address="usb:1.12.5",
        serial=SERIAL_B,
        channel=0,
        antenna="RX",
        gain_db=30.0,
        center_frequency_hz=1.2e9,
        sample_rate=5_000_000,
    )
    args = rx_app.build_parser().parse_args(["--transport", "pluto"])
    resolved = options.resolve(args)

    source = options.build_source(args, resolved, selection)

    assert isinstance(source, pluto.PlutoSampleSource)
    assert source.description == "pluto usb:1.12.5 @ 1200.000 MHz"
    with pytest.raises(TransportError, match="Radio panel"):
        options.build_source(args, resolved, None)
