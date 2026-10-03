"""Deterministic tests for the one-way OFDM message-link example.

Everything here runs headless with no radio, no display and no root.  The
Qt windows are exercised only through their argument parsing and the RF gate,
because a message that reaches the receiver is proved by the link layer, not
by a widget.
"""

from __future__ import annotations

import json
import time

import numpy as np
import pytest

from ofdm_message_link import datagram, options, qt_runtime, transport
from ofdm_message_link.link import (
    MessageReceiver,
    MessageTransmitter,
    RateMeter,
    _SequenceTracker,
    build_phy_profile,
    concatenate_bursts,
    load_link_config,
)

CONFIG = "configs/default.yaml"
OVERLAY = "configs/profiles/b210_ota_3p8ghz.yaml"


@pytest.fixture(scope="module")
def profile():
    return build_phy_profile(load_link_config(CONFIG, [OVERLAY]))


def _awgn(samples, snr_db, seed):
    rng = np.random.default_rng(seed)
    power = float(np.mean(np.abs(samples) ** 2))
    variance = power / (10.0 ** (snr_db / 10.0))
    noise = rng.normal(scale=np.sqrt(variance / 2.0), size=(samples.size, 2))
    return (samples + (noise[:, 0] + 1j * noise[:, 1])).astype(np.complex64)


# -- datagram layer --------------------------------------------------------


def test_datagram_round_trip_preserves_every_field():
    original = datagram.Datagram(
        message_id=7,
        fragment_index=2,
        fragment_count=5,
        origin=0xDEADBEEFCAFEF00D,
        tx_monotonic_ns=1234567890,
        payload=b"payload bytes",
    )
    assert datagram.decode(original.encode()) == original


def test_datagram_rejects_foreign_bytes():
    with pytest.raises(datagram.DatagramError):
        datagram.decode(b"not a datagram at all, just some arbitrary text")


def test_empty_message_still_produces_one_fragment():
    pieces = list(
        datagram.fragment(b"", message_id=1, max_payload_bytes=100, origin=0)
    )
    assert len(pieces) == 1
    assert pieces[0].payload == b""


def test_reassembly_restores_a_fragmented_message():
    message = bytes(range(256)) * 4
    pieces = list(
        datagram.fragment(message, message_id=9, max_payload_bytes=100, origin=3)
    )
    assert len(pieces) > 1

    reassembler = datagram.Reassembler()
    delivered = [reassembler.accept(piece) for piece in pieces]
    assert all(result is None for result in delivered[:-1])
    assert delivered[-1] is not None
    assert delivered[-1].payload == message
    assert delivered[-1].fragment_count == len(pieces)


def test_incomplete_message_is_dropped_and_counted_not_held_forever():
    reassembler = datagram.Reassembler(max_pending=2)
    for message_id in range(6):
        # One fragment of a two-fragment message: never completable.
        first = next(
            datagram.fragment(
                b"x" * 150, message_id=message_id, max_payload_bytes=100, origin=0
            )
        )
        assert reassembler.accept(first) is None
    assert reassembler.pending_count <= 3
    assert reassembler.incomplete_dropped > 0


# -- sequence accounting ---------------------------------------------------


def test_sequence_tracker_counts_a_gap_as_missing_bursts():
    tracker = _SequenceTracker()
    for sequence in (10, 11, 15, 16):
        tracker.observe(sequence)
    assert tracker.gaps == 1
    assert tracker.missing == 3


def test_sequence_tracker_follows_the_16_bit_wrap():
    tracker = _SequenceTracker()
    for sequence in (65534, 65535, 0, 1):
        tracker.observe(sequence)
    assert tracker.gaps == 0
    assert tracker.missing == 0


def test_sequence_tracker_does_not_invent_loss_from_a_late_burst():
    tracker = _SequenceTracker()
    for sequence in (10, 11, 9, 12):
        tracker.observe(sequence)
    assert tracker.missing == 0


def test_payload_preview_keeps_readable_text():
    assert qt_runtime.preview_payload("hello  \n 世界".encode()) == "hello 世界"


def test_payload_preview_never_hands_qt_a_control_character():
    # appendPlainText segfaults on C0 controls, DEL and U+2028 (seen with MPEG-TS video).
    payload = bytes(range(256)) + "a\u2028b".encode()
    preview = qt_runtime.preview_payload(payload, limit=1000)
    assert preview.isprintable()


def test_binary_payload_preview_is_plain_ascii():
    payload = bytes([0x47, 0x40, 0x11, 0xFF, 0xFE, 0xDE, 0x99]) + b"video"
    assert qt_runtime.preview_payload(payload) == "G@·····video"


def test_payload_preview_is_truncated_to_the_limit():
    assert len(qt_runtime.preview_payload(b"x" * 500, limit=120)) == 120


def test_rate_meter_reports_the_recent_rate_not_the_lifetime_average():
    meter = RateMeter(window_s=2.0)
    meter.update(0.0, 0)
    meter.update(10.0, 0)  # ten idle seconds drag a lifetime average down
    meter.update(11.0, 125_000)
    meter.update(12.0, 250_000)
    assert meter.bits_per_second == pytest.approx(1_000_000.0)


def test_rate_meter_falls_to_zero_when_the_counter_stops():
    meter = RateMeter(window_s=2.0)
    meter.update(0.0, 0)
    meter.update(1.0, 125_000)
    meter.update(4.0, 125_000)
    assert meter.bits_per_second == 0.0


def test_rate_meter_is_zero_before_two_samples():
    meter = RateMeter(window_s=2.0)
    assert meter.bits_per_second == 0.0
    meter.update(0.0, 500)
    assert meter.bits_per_second == 0.0


# -- link layer ------------------------------------------------------------


def test_typed_message_survives_the_whole_waveform_path(profile):
    transmitter = MessageTransmitter(profile)
    receiver = MessageReceiver(profile)
    message = b"Hello from USRP B210!"

    waveform = concatenate_bursts(transmitter.encode_message(message))
    delivered = receiver.feed(_awgn(waveform, 15.0, seed=11))

    assert [item.message.payload for item in delivered] == [message]
    assert receiver.stats.bursts_decoded == 1
    assert receiver.stats.missing_bursts == 0


def test_transmitter_can_switch_all_mcs_entries_without_reconfiguring_receiver(profile):
    from ofdm_link.phy.mcs_table import MCS_TABLE

    transmitter = MessageTransmitter(profile)
    receiver = MessageReceiver(profile)
    payload = b"x" * profile.max_message_payload_bytes

    for entry in MCS_TABLE:
        transmitter.select_mcs_entry(entry)
        bursts = transmitter.encode_message(payload)
        delivered = receiver.feed(concatenate_bursts(bursts))
        observations = receiver.drain_observations()

        assert [item.message.payload for item in delivered] == [payload]
        assert {item.mcs_index for item in bursts} == {entry.index}
        assert {item.wire_version for item in bursts} == {entry.wire_version}
        assert {item.modulation for item in observations} == {entry.modulation}
        assert {item.wire_version for item in observations} == {entry.wire_version}


def test_message_larger_than_one_frame_is_fragmented_and_rebuilt(profile):
    transmitter = MessageTransmitter(profile)
    receiver = MessageReceiver(profile)
    message = bytes(range(256)) * 12  # 3072 bytes: several frames

    bursts = transmitter.encode_message(message)
    assert len(bursts) > 1
    delivered = receiver.feed(_awgn(concatenate_bursts(bursts), 15.0, seed=12))

    assert [item.message.payload for item in delivered] == [message]
    assert delivered[0].message.fragment_count == len(bursts)


def test_receiver_finds_bursts_inside_a_continuously_fed_noisy_stream(profile):
    """The receiver must free-run, not be handed burst-aligned buffers."""

    transmitter = MessageTransmitter(profile)
    receiver = MessageReceiver(profile)
    rng = np.random.default_rng(13)
    messages = [f"message {index}".encode() for index in range(4)]

    blocks = []
    for message in messages:
        idle = (rng.normal(size=(2000, 2)) * 0.01).view(np.complex128).ravel()
        blocks.append(idle.astype(np.complex64))
        blocks.append(concatenate_bursts(transmitter.encode_message(message)))
    stream = _awgn(np.concatenate(blocks).astype(np.complex64), 15.0, seed=14)

    delivered = []
    for start in range(0, stream.size, 1337):  # deliberately unaligned chunks
        delivered += list(receiver.feed(stream[start : start + 1337]))

    assert [item.message.payload for item in delivered] == messages


def test_lost_burst_is_reported_as_missing_and_never_recovered(profile):
    transmitter = MessageTransmitter(profile)
    receiver = MessageReceiver(profile)

    kept = []
    for index in range(4):
        bursts = transmitter.encode_message(f"m{index}".encode())
        if index != 1:  # drop the second message's waveform entirely
            kept.append(concatenate_bursts(bursts))
    stream = _awgn(np.concatenate(kept).astype(np.complex64), 15.0, seed=15)

    delivered = []
    for start in range(0, stream.size, 4096):
        delivered += list(receiver.feed(stream[start : start + 4096]))

    stats = receiver.stats
    assert len(delivered) == 3
    assert stats.missing_bursts == 1
    assert stats.sequence_gaps == 1
    assert stats.burst_loss_ratio == pytest.approx(0.25)


def test_a_frame_that_is_not_ours_is_counted_separately_not_as_loss(profile):
    """A valid CRC from another application must not look like a link fault."""

    from ofdm_link.phy import CURRENT_PROTOCOL_VERSION, Frame, FrameKind, encode_burst
    from ofdm_link.runtime.factory import build_burst_config, select_frame_decoder

    selection = select_frame_decoder(profile.config)
    foreign = encode_burst(
        Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, profile.mcs, 0, b"not our format"),
        build_burst_config(profile.config),
        symbol_mapper=selection.symbol_mapper,
        wire_version=selection.wire_version,
        frame_encoder=selection.frame_encoder,
    )
    receiver = MessageReceiver(profile)
    delivered = receiver.feed(_awgn(foreign.samples, 15.0, seed=16))

    assert delivered == ()
    assert receiver.stats.foreign_bursts == 1
    assert receiver.stats.missing_bursts == 0


def test_latency_is_only_reported_within_one_clock_domain(profile):
    transmitter = MessageTransmitter(profile)
    receiver = MessageReceiver(profile)
    waveform = concatenate_bursts(transmitter.encode_message(b"same host"))
    delivered = receiver.feed(_awgn(waveform, 15.0, seed=17))

    assert delivered[0].same_clock_domain is True
    assert delivered[0].latency_s is not None


def test_profile_rejects_a_frame_payload_smaller_than_its_own_header(profile):
    with pytest.raises(ValueError):
        build_phy_profile(profile.config, frame_payload_bytes=datagram.HEADER_SIZE)


# -- transport -------------------------------------------------------------


def test_peak_scaling_keeps_every_sample_inside_the_dac_range(profile):
    """Unscaled bursts peak above 1.0 and would clip a UHD sink."""

    transmitter = MessageTransmitter(profile)
    samples = transmitter.encode_message(b"x" * 900)[0].samples
    assert np.max(np.abs(samples)) > 1.0

    scaled = transport.scale_to_peak(samples, 0.7)
    assert np.max(np.abs(scaled)) == pytest.approx(0.7, rel=1e-5)
    assert np.max(np.abs(np.real(scaled))) <= 0.7 + 1e-6
    assert np.max(np.abs(np.imag(scaled))) <= 0.7 + 1e-6


def test_udp_transport_carries_a_message_between_the_two_halves(profile):
    import time

    transmitter = MessageTransmitter(profile)
    receiver = MessageReceiver(profile)
    source = transport.UdpSampleSource(port=53177, snr_db=15.0, seed=19)
    sink = transport.UdpSampleSink(port=53177)
    source.start()
    sink.start()
    try:
        message = b"over the localhost sample transport"
        for burst in transmitter.encode_message(message):
            assert sink.send(burst.samples) is True

        delivered = []
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not delivered:
            chunk = source.recv(0.2)
            if chunk is not None:
                delivered += list(receiver.feed(chunk))
        assert [item.message.payload for item in delivered] == [message]
    finally:
        sink.stop()
        source.stop()


def test_unstarted_sink_refuses_to_send():
    sink = transport.UdpSampleSink(port=53178)
    with pytest.raises(transport.TransportError):
        sink.send(np.zeros(16, dtype=np.complex64))


# -- command-line safety ---------------------------------------------------


def _tx_args(argv):
    from ofdm_message_link.tx_app import build_parser

    return build_parser().parse_args(argv)


def test_radio_transmit_is_refused_without_the_rf_capability():
    args = _tx_args(["--transport", "uhd"])
    resolved = options.resolve(args)
    with pytest.raises(SystemExit, match="enable-rf"):
        options.build_sink(args, resolved, None)


def test_radio_transmit_is_refused_with_a_wrong_acknowledgement():
    args = _tx_args(["--transport", "uhd", "--enable-rf", "--acknowledgement", "sure"])
    resolved = options.resolve(args)
    with pytest.raises(SystemExit, match="acknowledgement"):
        options.build_sink(args, resolved, None)


def test_udp_transport_needs_no_rf_capability():
    args = _tx_args([])
    sink = options.build_sink(args, options.resolve(args), None)
    assert sink.snapshot()["transport"] == "udp"


@pytest.mark.parametrize("value", ["0", "61"])
def test_ui_fps_rejects_values_outside_the_supported_range(value):
    with pytest.raises(SystemExit):
        _tx_args(["--ui-fps", value])


def test_ui_fps_accepts_the_documented_range():
    assert _tx_args(["--ui-fps", "1"]).ui_fps == 1
    assert _tx_args(["--ui-fps", "60"]).ui_fps == 60


def test_mcs_index_takes_priority_over_the_legacy_modulation_option():
    resolved = options.resolve(_tx_args(["--mcs", "qpsk", "--mcs-index", "7"]))
    assert resolved.mcs_entry.index == 7
    assert resolved.profile.mcs.name == "QAM16"


def test_legacy_mcs_option_keeps_the_configured_code_rate():
    resolved = options.resolve(_tx_args(["--mcs", "qam16"]))
    assert resolved.mcs_entry.index == 4
    assert resolved.mcs_entry.fec_scheme == "turbo"


def test_receiver_help_marks_startup_mcs_as_display_only():
    from ofdm_message_link.rx_app import build_parser

    help_text = build_parser().format_help()
    assert help_text.count("display hint only") == 2
    assert "actual MCS" in help_text
    assert "burst header" in help_text


def test_receiver_rx_buffer_is_automatic_unless_given_and_zero_keeps_uhd_default(monkeypatch):
    from ofdm_message_link.rx_app import build_parser

    built = []

    class FakeSource:
        def __init__(self, settings, *, recv_frames):
            built.append(recv_frames)

    monkeypatch.setattr(transport, "UhdSampleSource", FakeSource)
    base = ["--transport", "uhd", "--serial", "ABC123"]
    for argv, expected in (
        (base, None),
        ([*base, "--rx-recv-frames", "0"], 0),
        ([*base, "--rx-recv-frames", "1024"], 1024),
    ):
        args = build_parser().parse_args(argv)
        options.build_source(args, options.resolve(args))
        assert built.pop() == expected
    with pytest.raises(SystemExit):
        build_parser().parse_args([*base, "--rx-recv-frames", "1025"])


@pytest.mark.parametrize(
    ("rate", "frames", "chunk"),
    [(5_000_000, 256, 65536), (10_000_000, 512, 131072), (2_000_000, 256, 65536)],
)
def test_uhd_source_keeps_its_receive_buffering_time_as_the_rate_grows(rate, frames, chunk):
    from types import SimpleNamespace

    source = transport.UhdSampleSource(SimpleNamespace(sample_rate=rate))
    snapshot = source.snapshot()
    assert snapshot["uhd_num_recv_frames"] == frames
    assert snapshot["chunk_samples"] == chunk
    assert transport.UhdSampleSource(
        SimpleNamespace(sample_rate=rate), recv_frames=0
    ).snapshot()["uhd_num_recv_frames"] is None


def test_a_serial_selects_one_usrp():
    args = _tx_args(["--transport", "uhd", "--serial", "ABC123"])
    assert options.resolve(args).config.radio.device_args == "serial=ABC123"


def test_added_noise_is_refused_on_a_real_channel():
    from ofdm_message_link.rx_app import main as rx_main

    with pytest.raises(SystemExit, match="udp transport only"):
        rx_main(["--transport", "uhd", "--snr-db", "15"])


def test_the_evidence_banner_never_claims_acceptance():
    args = _tx_args([])
    banner = options.evidence_banner(options.resolve(args))
    assert "DEMO ONLY" in banner
    assert "No ACK, no ARQ, no TDD" in banner
    assert "Not throughput, reliability or RF acceptance evidence" in banner


# -- GNU Radio blocks used by the radio transport --------------------------
#
# These run the flowgraph blocks without a USRP, so the tagged-burst source
# and the chunking sink are covered on machines with no hardware.  They do
# not transmit and are not RF evidence.


@pytest.mark.gnuradio
def test_tx_source_block_emits_one_length_tagged_run_per_burst():
    gr = pytest.importorskip("gnuradio.gr")
    blocks = pytest.importorskip("gnuradio.blocks")
    pmt = pytest.importorskip("pmt")

    from ofdm_link.radio.uhd import TX_LENGTH_TAG
    from ofdm_message_link.transport import _build_tx_source_block

    source = _build_tx_source_block(8)()
    first = np.exp(1j * np.linspace(0, 6.0, 300)).astype(np.complex64)
    second = np.exp(1j * np.linspace(0, 3.0, 150)).astype(np.complex64)
    assert source.enqueue(first) is True
    assert source.enqueue(second) is True

    collected = blocks.vector_sink_c()
    tags = blocks.tag_debug(gr.sizeof_gr_complex, "tags")
    tags.set_save_all(True)
    top = gr.top_block("tx_source_block_test")
    top.connect(source, collected)
    top.connect(source, tags)
    top.start()
    time.sleep(0.5)
    source.shutdown()
    top.stop()
    top.wait()

    emitted = np.asarray(collected.data(), dtype=np.complex64)
    assert emitted.size >= first.size + second.size
    np.testing.assert_allclose(emitted[: first.size], first, rtol=1e-5, atol=1e-6)
    np.testing.assert_allclose(
        emitted[first.size : first.size + second.size], second, rtol=1e-5, atol=1e-6
    )

    lengths = [
        (int(tag.offset), pmt.to_long(tag.value))
        for tag in tags.current_tags()
        if pmt.symbol_to_string(tag.key) == TX_LENGTH_TAG
    ]
    assert (0, first.size) in lengths
    assert (first.size, second.size) in lengths
    assert source.snapshot() == {
        "capacity": 8,
        "depth": 0,
        "bursts_enqueued": 2,
        "bursts_emitted": 2,
        "samples_enqueued": first.size + second.size,
        "samples_emitted": first.size + second.size,
    }


@pytest.mark.gnuradio
def test_tx_source_block_times_each_scheduled_burst():
    gr = pytest.importorskip("gnuradio.gr")
    blocks = pytest.importorskip("gnuradio.blocks")
    pmt = pytest.importorskip("pmt")

    from ofdm_link.radio.uhd import TX_LENGTH_TAG, TX_TIME_TAG
    from ofdm_message_link.transport import _build_tx_source_block

    source = _build_tx_source_block(8)()
    assert source.enqueue(np.ones(100, dtype=np.complex64), 12.25) is True
    assert source.enqueue(np.ones(50, dtype=np.complex64)) is True
    collected = blocks.vector_sink_c()
    top = gr.top_block("tx_source_time_test")
    top.connect(source, collected)
    top.start()
    deadline = time.monotonic() + 2.0
    while len(collected.data()) < 150 and time.monotonic() < deadline:
        time.sleep(0.005)
    source.shutdown()
    top.stop()
    top.wait()

    tags = {
        (int(tag.offset), pmt.symbol_to_string(tag.key)): tag.value
        for tag in collected.tags()
    }
    assert pmt.to_long(tags[(0, TX_LENGTH_TAG)]) == 100
    timed = tags[(0, TX_TIME_TAG)]
    assert pmt.to_uint64(pmt.tuple_ref(timed, 0)) == 12
    assert pmt.to_double(pmt.tuple_ref(timed, 1)) == pytest.approx(0.25)
    assert pmt.to_long(tags[(100, TX_LENGTH_TAG)]) == 50
    assert (100, TX_TIME_TAG) not in tags


@pytest.mark.gnuradio
def test_tx_source_block_emits_a_burst_promptly_after_idle():
    # A source whose work() returns 0 is parked by the scheduler for about
    # 250 ms, which made OTA bursts leave in back-to-back clumps.
    gr = pytest.importorskip("gnuradio.gr")
    blocks = pytest.importorskip("gnuradio.blocks")

    from ofdm_message_link.transport import _build_tx_source_block

    source = _build_tx_source_block(8)()
    collected = blocks.vector_sink_c()
    top = gr.top_block("tx_source_idle_test")
    top.connect(source, collected)
    top.start()
    try:
        latencies = []
        for _ in range(3):
            time.sleep(0.3)
            before = len(collected.data())
            started = time.monotonic()
            assert source.enqueue(np.ones(64, dtype=np.complex64)) is True
            while len(collected.data()) == before and time.monotonic() - started < 1.0:
                time.sleep(0.001)
            latencies.append(time.monotonic() - started)
    finally:
        stop_started = time.monotonic()
        source.shutdown()
        top.stop()
        top.wait()
    assert max(latencies) < 0.05
    assert time.monotonic() - stop_started < 1.0


@pytest.mark.gnuradio
def test_tx_source_block_refuses_to_grow_past_its_capacity():
    from ofdm_message_link.transport import _build_tx_source_block

    pytest.importorskip("gnuradio.gr")
    source = _build_tx_source_block(2)()
    samples = np.zeros(64, dtype=np.complex64)
    assert source.enqueue(samples) is True
    assert source.enqueue(samples) is True
    assert source.enqueue(samples) is False


@pytest.mark.gnuradio
def test_rx_sink_block_chunks_the_stream_and_applies_back_pressure():
    gr = pytest.importorskip("gnuradio.gr")
    blocks = pytest.importorskip("gnuradio.blocks")

    from ofdm_message_link.transport import _build_rx_sink_block

    received: list[np.ndarray] = []
    accept = True

    def enqueue(values):
        if not accept:
            return False
        received.append(values)
        return True

    stream = np.arange(5000, dtype=np.complex64)
    # Both blocks are held in locals: a Python block reachable only from the
    # C++ flowgraph is collected under it and segfaults the scheduler.
    source = blocks.vector_source_c(stream.tolist(), False)
    sink = _build_rx_sink_block(512, enqueue, lambda offset, seconds: None)()
    top = gr.top_block("rx_sink_block_test")
    top.connect(source, sink)
    top.start()
    top.wait()

    assert received, "sink delivered nothing"
    assert all(chunk.size <= 512 for chunk in received)
    np.testing.assert_allclose(
        np.concatenate(received)[: stream.size], stream, rtol=0, atol=0
    )
    assert all(not chunk.flags.writeable for chunk in received)


def test_rx_sink_block_feeds_rx_time_tags_to_the_gap_counter():
    gr = pytest.importorskip("gnuradio.gr")
    blocks = pytest.importorskip("gnuradio.blocks")
    pmt = pytest.importorskip("pmt")

    from ofdm_message_link.transport import RxTimeGapCounter, _build_rx_sink_block

    def rx_time(offset: int, seconds: int, fraction: float):
        return gr.tag_utils.python_to_tag(
            (
                offset,
                pmt.intern("rx_time"),
                pmt.make_tuple(pmt.from_uint64(seconds), pmt.from_double(fraction)),
                pmt.intern("src"),
            )
        )

    # 1 MS/s: an overflow at sample 3000 dropped 2000 samples (2 ms).
    tags = [rx_time(0, 7, 0.0), rx_time(3000, 7, 0.005)]
    source = blocks.vector_source_c([0j] * 5000, False, 1, tags)
    counter = RxTimeGapCounter(sample_rate=1_000_000.0)
    sink = _build_rx_sink_block(512, lambda values: True, counter.observe)()
    top = gr.top_block("rx_sink_gap_test")
    top.connect(source, sink)
    top.start()
    top.wait()

    assert counter.discontinuities == 1
    assert counter.samples_lost == 2000


# -- window construction ---------------------------------------------------
#
# These build the real windows offscreen and close them.  They exist because
# every earlier test passed while `main()` was aborting on startup: nothing
# covered the path from the command line to a constructed widget.


@pytest.fixture
def offscreen_qt(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    qt_widgets = pytest.importorskip("PyQt5.QtWidgets")
    from ofdm_message_link import qt_runtime

    application = qt_runtime.create_application()
    assert qt_widgets.QApplication.instance() is application
    return application


@pytest.mark.gui
def test_transmit_window_builds_and_closes(offscreen_qt):
    from ofdm_link.phy.mcs_table import MCS_TABLE, describe_mcs_entry
    from ofdm_message_link.tx_app import TransmitWindow, _airtime_ms, build_parser

    args = build_parser().parse_args(["--udp-port", "53301", "--ingress-port", "53302"])
    resolved = options.resolve(args)
    window = TransmitWindow(
        resolved,
        53302,
        build_sink=lambda selection: options.build_sink(args, resolved, selection),
    )
    try:
        window.show()
        offscreen_qt.processEvents()
        assert window._mcs_combo.count() == len(MCS_TABLE) == 8
        assert [window._mcs_combo.itemText(i) for i in range(8)] == [
            describe_mcs_entry(entry) for entry in MCS_TABLE
        ]
        expected_ms = [4.16, 4.11, 2.77, 1.42, 2.13, 2.10, 1.42, 0.75]
        assert [_airtime_ms(resolved, entry) for entry in MCS_TABLE] == pytest.approx(
            expected_ms,
            abs=0.01,
        )
        assert [window._tabs.tabText(i) for i in range(window._tabs.count())] == [
            "Send",
            "Signal",
            "Hardware",
            "Log",
        ]
        assert window._light_card.light.state in {"OFF", "OK"}  # never the RX-only WAITING
        # The MCS selector sits in the top card row, outside every tab.
        assert not window._tabs.isAncestorOf(window._mcs_combo)
        window._mcs_combo.setCurrentIndex(window._mcs_combo.findData(7))
        assert window._worker.active_mcs_entry.index == 7
        assert window._mcs_warning.isVisible()
        assert "no error correction" in window._mcs_warning.text()
        window._send_typed()  # empty input must be a no-op, not a crash
        window._input.setText("hello")
        window._send_typed()
        assert window._input.text() == ""
        window._refresh_stats()
        offscreen_qt.processEvents()
    finally:
        window.close()


@pytest.mark.gui
def test_receive_window_builds_and_closes(offscreen_qt, tmp_path):
    from ofdm_link.phy.mcs_table import mcs_entry
    from ofdm_message_link.rx_app import ReceiveWindow, build_parser

    args = build_parser().parse_args(["--udp-port", "53303", "--egress-port", "53304"])
    resolved = options.resolve(args)
    window = ReceiveWindow(
        resolved,
        ("127.0.0.1", 53304),
        build_source=lambda selection: options.build_source(args, resolved, selection),
    )
    try:
        window.show()
        offscreen_qt.processEvents()
        window._refresh_stats()
        assert [window._tabs.tabText(i) for i in range(window._tabs.count())] == [
            "Signal",
            "Decode",
            "Hardware",
            "Log",
        ]
        assert window._light_card.light.state == "WAITING"
        assert "last 10 s" in window._decode_text.toPlainText()
        assert "burst loss ratio" in window._decode_text.toPlainText()
        transmitter = MessageTransmitter(resolved.profile)
        transmitter.select_mcs_entry(mcs_entry(7))
        receiver = MessageReceiver(resolved.profile)
        delivered = receiver.feed(
            concatenate_bursts(transmitter.encode_message(b"actual MCS metadata"))
        )
        observations = receiver.drain_observations()
        window._on_message(delivered[0])
        window._record_burst(observations[0])
        window._refresh_stats()
        assert "MCS 7 wire v4" in window._log.toPlainText()
        log = tmp_path / "stats.jsonl"
        window._stats_log = log.open("a", encoding="utf-8")
        window._refresh_stats()
        window._stats_log.flush()
        record = json.loads(log.read_text().splitlines()[-1])
        assert record["receiver"]["bursts_decoded"] == 0
        assert "detected_burst_candidates" in record["decoder"]
        assert "MCS 7  bursts" in window._decode_text.toPlainText()
        assert record["link_state"] in {"WAITING", "GOOD"}
        assert {"loss_10s", "snr_2s", "link_reason"} <= set(record)
        offscreen_qt.processEvents()
    finally:
        window.close()


@pytest.mark.gui
def test_stats_log_rotates_at_its_cap_and_keeps_cumulative_lines(offscreen_qt, tmp_path):
    """A long run's --stats-log stays under --stats-log-max-mb on disk."""

    from ofdm_message_link.rx_app import ReceiveWindow, build_parser, open_stats_log

    log = tmp_path / "rx_stats.jsonl"
    args = build_parser().parse_args(
        ["--udp-port", "53305", "--egress-port", "53306", "--stats-log", str(log),
         "--stats-log-max-mb", "0.01", "--stats-log-backups", "1"]
    )
    resolved = options.resolve(args)
    window = ReceiveWindow(
        resolved,
        ("127.0.0.1", 53306),
        build_source=lambda selection: options.build_source(args, resolved, selection),
        stats_log=open_stats_log(args),
    )
    cap = int(0.01 * 1024 * 1024)
    try:
        for _ in range(40):
            window._refresh_stats()
        window._reset_stats()
        window._refresh_stats()
        window._stats_log.flush()
        rotated = log.with_name("rx_stats.jsonl.1")
        assert rotated.exists(), "40 refreshes of about 1 KiB each must rotate a 10 KiB log"
        assert not log.with_name("rx_stats.jsonl.2").exists()
        records = []
        for path in (rotated, log):
            assert path.stat().st_size <= cap
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            assert lines, f"{path.name} is empty"
            records += lines
        assert "receiver" in records[-1] and "elapsed_s" in records[-1]
        assert any(record.get("event") == "reset" for record in records)
    finally:
        window.close()


@pytest.mark.gui
def test_log_tabs_keep_only_the_newest_log_lines(offscreen_qt):
    """A video demo logs hundreds of messages a second; the Log tab stays bounded."""

    from ofdm_message_link.rx_app import ReceiveWindow
    from ofdm_message_link.rx_app import build_parser as rx_parser
    from ofdm_message_link.tx_app import SentRecord, TransmitWindow
    from ofdm_message_link.tx_app import build_parser as tx_parser

    rx_args = rx_parser().parse_args(
        ["--udp-port", "53307", "--egress-port", "53308", "--log-lines", "50"]
    )
    tx_args = tx_parser().parse_args(
        ["--udp-port", "53307", "--ingress-port", "53309", "--log-lines", "50"]
    )
    assert rx_parser().parse_args([]).log_lines == options.DEFAULT_LOG_LINES == 2000
    rx_resolved, tx_resolved = options.resolve(rx_args), options.resolve(tx_args)
    receiver = ReceiveWindow(
        rx_resolved,
        ("127.0.0.1", 53308),
        build_source=lambda selection: options.build_source(rx_args, rx_resolved, selection),
        log_lines=rx_args.log_lines,
    )
    transmitter = TransmitWindow(
        tx_resolved,
        53309,
        build_sink=lambda selection: options.build_sink(tx_args, tx_resolved, selection),
        log_lines=tx_args.log_lines,
    )
    delivered = MessageReceiver(rx_resolved.profile).feed(
        concatenate_bursts(MessageTransmitter(rx_resolved.profile).encode_message(b"frame"))
    )[0]
    try:
        for n in range(300):
            receiver._on_message(delivered)
            transmitter._on_sent(
                SentRecord("udp", f"message {n}", 752, 1, 4096, n, 4, "MCS 4")
            )
        offscreen_qt.processEvents()
        for window in (receiver, transmitter):
            assert window._log.document().blockCount() <= 50, type(window).__name__
        assert "seq   299" in transmitter._log.toPlainText().splitlines()[-1]
        assert "seq   249" not in transmitter._log.toPlainText()
        assert receiver._worker is not None  # still a working window
        receiver._refresh_stats()
        transmitter._refresh_stats()
    finally:
        receiver.close()
        transmitter.close()


def test_stats_log_is_capped_by_default_and_zero_removes_the_cap(tmp_path):
    pytest.importorskip("PyQt5.QtWidgets")
    from ofdm_message_link.rx_app import build_parser, open_stats_log

    parser = build_parser()
    assert open_stats_log(parser.parse_args([])) is None
    log = open_stats_log(parser.parse_args(["--stats-log", str(tmp_path / "a.jsonl")]))
    assert (log.max_bytes, log.backups) == (16 * 1024 * 1024, 1)
    log.close()
    unlimited = open_stats_log(
        parser.parse_args(["--stats-log", str(tmp_path / "b.jsonl"), "--stats-log-max-mb", "0"])
    )
    assert unlimited.max_bytes is None
    unlimited.close()
    for bad in (["--stats-log-max-mb", "-1"], ["--stats-log-backups", "-1"]):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)


def _pump_until(application, condition, timeout_s=20.0):
    deadline = time.monotonic() + timeout_s
    while not condition() and time.monotonic() < deadline:
        application.processEvents()
        time.sleep(0.02)
    application.processEvents()
    return condition()


@pytest.mark.gui
def test_both_windows_survive_binary_payloads_reset_and_resizing(offscreen_qt, tmp_path):
    """Both dashboards over the UDP transport, the way a video demo drives them.

    Video payloads are binary; handing their bytes to a Qt text view has
    segfaulted this Qt build before.  Reset and a short window must not
    crash either, and the tab area folds away only while the window is short.
    """

    import socket

    from ofdm_message_link.rx_app import ReceiveWindow
    from ofdm_message_link.rx_app import build_parser as rx_parser
    from ofdm_message_link.tx_app import TransmitWindow
    from ofdm_message_link.tx_app import build_parser as tx_parser

    rx_args = rx_parser().parse_args(["--udp-port", "53341", "--egress-port", "53342"])
    tx_args = tx_parser().parse_args(["--udp-port", "53341", "--ingress-port", "53343"])
    rx_resolved = options.resolve(rx_args)
    tx_resolved = options.resolve(tx_args)
    log = tmp_path / "rx_stats.jsonl"
    receiver = ReceiveWindow(
        rx_resolved,
        ("127.0.0.1", 53342),
        build_source=lambda selection: options.build_source(rx_args, rx_resolved, selection),
        stats_log=log.open("a", encoding="utf-8"),
    )
    transmitter = TransmitWindow(
        tx_resolved,
        53343,
        build_sink=lambda selection: options.build_sink(tx_args, tx_resolved, selection),
    )
    binary = bytes(range(256)) + "\u2028\u202e".encode() + b"\x00\x7f\x1b[2J"
    try:
        receiver.show()
        transmitter.show()
        offscreen_qt.processEvents()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as ingress:
            ingress.sendto(binary, ("127.0.0.1", 53343))
        transmitter._input.setText("hello dashboard")
        transmitter._send_typed()
        assert _pump_until(
            offscreen_qt, lambda: receiver._worker.stats.messages_delivered >= 2
        ), "both messages should cross the UDP transport"
        assert _pump_until(offscreen_qt, lambda: transmitter._messages >= 2)
        receiver._refresh_stats()
        transmitter._refresh_stats()
        received = receiver._log.toPlainText()
        assert "hello dashboard" in received
        assert "\x00" not in received
        assert receiver._light_card.light.colour == "green"
        assert transmitter._light_card.light.state in {"OK", "BUSY"}
        assert receiver._goodput_card.value_text() != "\u2014"

        delivered_before = receiver._worker.stats.bursts_decoded
        receiver._reset_button.click()
        transmitter._reset_button.click()
        receiver._refresh_stats()
        transmitter._refresh_stats()
        assert receiver._last_view.totals["bursts_decoded"] == 0
        assert transmitter._last_view.totals == {"sent_bytes": 0, "samples": 0}
        assert receiver._trend.point_count <= 1
        receiver._stats_log.flush()
        records = [json.loads(line) for line in log.read_text().splitlines()]
        assert any(record.get("event") == "reset" for record in records)
        # The log keeps its cumulative meaning across a reset.
        assert records[-1]["receiver"]["bursts_decoded"] == delivered_before

        for window in (receiver, transmitter):
            window.resize(1000, 400)
            offscreen_qt.processEvents()
            assert not window._tabs.isVisible(), type(window).__name__
            window._refresh_stats()
            window.resize(1000, 800)
            offscreen_qt.processEvents()
            assert window._tabs.isVisible(), type(window).__name__
            for index in range(window._tabs.count()):
                window._tabs.setCurrentIndex(index)
                offscreen_qt.processEvents()
            window.grab()
    finally:
        transmitter.close()
        receiver.close()


@pytest.mark.gui
@pytest.mark.parametrize("direction", ["rx", "tx"])
def test_a_tall_radio_panel_does_not_stop_a_window_from_collapsing(offscreen_qt, direction):
    """The device panel lives on the Hardware tab; its height must not set the
    window's minimum, or a short window could never reach the collapse height."""

    from PyQt5 import QtCore, QtWidgets

    from ofdm_message_link import dashboard

    class _TallPanel(QtWidgets.QGroupBox):
        startRequested = QtCore.pyqtSignal(object)
        stopRequested = QtCore.pyqtSignal()
        gainChangeRequested = QtCore.pyqtSignal(float)
        _auto_start = True

        def __init__(self):
            super().__init__("Radio")
            self.setMinimumHeight(360)

    if direction == "rx":
        from ofdm_message_link.rx_app import ReceiveWindow, build_parser

        args = build_parser().parse_args(["--udp-port", "53351", "--egress-port", "53352"])
        resolved = options.resolve(args)
        window = ReceiveWindow(
            resolved,
            ("127.0.0.1", 53352),
            build_source=lambda selection: options.build_source(args, resolved, selection),
            radio_panel=_TallPanel(),
        )
    else:
        from ofdm_message_link.tx_app import TransmitWindow, build_parser

        args = build_parser().parse_args(["--udp-port", "53353", "--ingress-port", "53354"])
        resolved = options.resolve(args)
        window = TransmitWindow(
            resolved,
            53354,
            build_sink=lambda selection: options.build_sink(args, resolved, selection),
            radio_panel=_TallPanel(),
        )
    try:
        window.show()
        window.resize(1000, 450)
        offscreen_qt.processEvents()
        assert window.height() < dashboard.COLLAPSE_BELOW_PX
        assert not window._tabs.isVisible()
    finally:
        window.close()


@pytest.mark.gui
def test_plot_widgets_paint_without_data_and_with_data(offscreen_qt):
    from ofdm_link.phy import MCS
    from ofdm_message_link.plots import ConstellationPlot, SpectrumPlot

    spectrum = SpectrumPlot()
    constellation = ConstellationPlot()
    for widget in (spectrum, constellation):
        widget.resize(320, 200)
        widget.grab()  # empty state must paint, not raise

    rng = np.random.default_rng(21)
    spectrum.set_samples(
        (rng.normal(size=(4000, 2)) @ [1, 1j]).astype(np.complex64),
        sample_rate=5_000_000,
    )
    constellation.add_symbols(
        (rng.normal(size=(600, 2)) @ [1, 1j]).astype(np.complex64),
        modulation=MCS.QPSK,
    )
    for widget in (spectrum, constellation):
        assert not widget.grab().isNull()


@pytest.mark.gui
def test_trend_plot_paints_empty_gapped_and_alerted_series(offscreen_qt):
    from ofdm_message_link.dashboard import TrendPoint
    from ofdm_message_link.plots import TrendPlot

    trend = TrendPlot(
        "Last 60 s",
        primary_label="Goodput",
        primary_scale=1e6,
        primary_unit="Mbit/s",
        secondary_label="SNR",
        secondary_unit="dB",
    )
    trend.resize(600, 160)
    trend.grab()
    trend.set_points(
        [
            TrendPoint(t=0.5 * step, primary=8e5, secondary=None if step % 5 else 11.0,
                       alert=step == 7)
            for step in range(121)
        ]
    )
    assert trend.point_count == 121
    assert not trend.grab().isNull()


@pytest.mark.gui
def test_burst_arrivals_are_coalesced_until_the_plot_timer_refreshes(
    offscreen_qt,
    monkeypatch,
):
    from ofdm_link.phy import MCS
    from ofdm_message_link.plots import (
        ConstellationPlot,
        PlotBuffers,
        SpectrumPlot,
    )

    buffers = PlotBuffers(max_symbol_batches=4)
    spectrum = SpectrumPlot()
    constellation = ConstellationPlot()
    repaints = {"spectrum": 0, "constellation": 0}
    monkeypatch.setattr(
        spectrum,
        "update",
        lambda: repaints.__setitem__("spectrum", repaints["spectrum"] + 1),
    )
    monkeypatch.setattr(
        constellation,
        "update",
        lambda: repaints.__setitem__("constellation", repaints["constellation"] + 1),
    )

    samples = np.ones(512, dtype=np.complex64)
    for sequence in range(100):
        buffers.set_samples(samples * (sequence + 1))
        buffers.add_symbols(samples[:48], context=sequence)

    assert repaints == {"spectrum": 0, "constellation": 0}
    snapshot = buffers.snapshot()
    assert [item.context for item in snapshot.symbols] == [96, 97, 98, 99]
    spectrum.set_samples(snapshot.samples, sample_rate=5_000_000)
    for batch in snapshot.symbols:
        constellation.add_symbols(batch.values, modulation=MCS.QPSK)
    spectrum.refresh()
    constellation.refresh()

    assert repaints == {"spectrum": 1, "constellation": 1}


@pytest.mark.gui
def test_constellation_marks_old_data_stale_and_clears_on_modulation_change(
    offscreen_qt,
):
    from ofdm_link.phy import MCS
    from ofdm_message_link.plots import ConstellationPlot

    plot = ConstellationPlot(stale_after_s=3.0)
    qpsk = np.ones(24, dtype=np.complex64)
    qam16 = (np.ones(24) * (1 + 1j)).astype(np.complex64)

    assert plot.status_text(now=10.0) == "no decoded burst yet"
    plot.add_symbols(qpsk, modulation=MCS.QPSK, received_at=10.0)
    assert plot.reference_point_count == 4
    assert plot.status_text(now=12.9) == "live decoded burst (QPSK)"
    assert plot.status_text(now=13.1) == "stale, last burst 3.1 s ago"

    plot.add_symbols(qam16, modulation=MCS.QAM16, received_at=14.0)
    assert plot.reference_point_count == 16
    assert len(plot._history) == 1, "QPSK points must not remain under a 16QAM reference"


@pytest.mark.gui
def test_receive_worker_updates_spectrum_without_a_decoded_burst(offscreen_qt):
    from ofdm_message_link.plots import PlotBuffers
    from ofdm_message_link.rx_app import ReceiveWorker, _Bridge

    class _ReceiverWithoutDecodes:
        stats = object()

        def feed(self, samples):
            return ()

        def drain_observations(self):
            return ()

    class _OneChunkSource:
        def __init__(self):
            self.sent = False

        def start(self):
            pass

        def recv(self, timeout):
            if not self.sent:
                self.sent = True
                return np.ones(512, dtype=np.complex64)
            time.sleep(min(timeout, 0.01))
            return None

        def stop(self):
            pass

    buffers = PlotBuffers()
    worker = ReceiveWorker(_ReceiverWithoutDecodes(), _Bridge(), buffers)
    worker.attach(_OneChunkSource())
    worker.start()
    try:
        deadline = time.monotonic() + 1.0
        snapshot = buffers.snapshot()
        while snapshot.samples is None and time.monotonic() < deadline:
            time.sleep(0.01)
            snapshot = buffers.snapshot()
    finally:
        worker.stop()

    assert snapshot.samples is not None
    assert snapshot.symbols == ()


@pytest.mark.gui
@pytest.mark.parametrize(
    ("module", "argv"),
    [
        ("ofdm_message_link.tx_app", ["--udp-port", "53311", "--ingress-port", "53312"]),
        ("ofdm_message_link.rx_app", ["--udp-port", "53313", "--egress-port", "53314"]),
    ],
)
def test_app_starts_from_the_command_line_without_aborting(module, argv):
    """Launch each app the way the README does and check it stays up.

    This has to be a subprocess.  A dropped ``QApplication`` reference aborts
    only the first widget built in a process, and any earlier in-process test
    leaves a live instance behind that hides the fault.
    """

    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONPATH": str(root)}
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", module, *argv],
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # Long enough for argument parsing, configuration, the PHY profile and
        # the whole window to be built.
        exited = process.poll()
        deadline = time.monotonic() + 15.0
        while exited is None and time.monotonic() < deadline:
            time.sleep(0.25)
            exited = process.poll()
            if time.monotonic() > deadline - 10.0:
                break
        assert exited is None, (
            f"{module} exited with {exited} instead of staying up:\n"
            f"{process.communicate()[0]}"
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


# -- device discovery, probing and selection -------------------------------


class _FakeRange:
    def __init__(self, low, high):
        self._low, self._high = low, high

    def start(self):
        return self._low

    def stop(self):
        return self._high


class _FakeSubdevSpec:
    def to_string(self):
        return "A:A A:B"


class _FakeMultiUsrp:
    """Stands in for a B210: two front ends, RX2 only on receive."""

    def __init__(self, args):
        self.args = args

    def get_rx_num_channels(self):
        return 2

    def get_tx_num_channels(self):
        return 2

    def get_rx_antennas(self, chan):
        return ["TX/RX", "RX2"]

    def get_tx_antennas(self, chan):
        return ["TX/RX"]

    def get_rx_gain_range(self, chan):
        return _FakeRange(0.0, 76.0)

    def get_tx_gain_range(self, chan):
        return _FakeRange(0.0, 89.75)

    def get_rx_freq_range(self, chan):
        return _FakeRange(42e6, 6008e6)

    def get_tx_freq_range(self, chan):
        return _FakeRange(42e6, 6008e6)

    def get_rx_subdev_spec(self, mboard):
        return _FakeSubdevSpec()


class _FakeUhdModule:
    def __init__(self, addresses):
        self._addresses = addresses
        self.usrp = self
        self.opened = []

    def find(self, args):
        return list(self._addresses)

    def MultiUSRP(self, args):  # noqa: N802 (mirrors the UHD API name)
        self.opened.append(args)
        return _FakeMultiUsrp(args)


_B210_ADDRESSES = [
    "type=b200,name=MyB210,serial=30B0002,product=B210",
    "type=b200,name=NI2901,serial=30B0001,product=B210",
]


def test_discovery_parses_each_device_address():
    from ofdm_message_link import devices

    found = devices.discover(uhd_api=_FakeUhdModule(_B210_ADDRESSES))

    assert [device.serial for device in found] == ["30B0001", "30B0002"]
    ni2901 = found[0]
    assert ni2901.name == "NI2901"
    assert ni2901.product == "B210"
    assert ni2901.driver == "b200"
    assert ni2901.device_args == "serial=30B0001"
    assert ni2901.is_supported is True


def test_a_device_without_a_serial_cannot_be_selected():
    from ofdm_message_link import devices

    fake = _FakeUhdModule(["type=b200,name=Nameless,product=B210"])
    assert devices.discover(uhd_api=fake) == ()


def test_b210_channel_zero_is_rf_a_and_channel_one_is_rf_b():
    """The front-panel mapping comes from the subdev spec order, A:A then A:B."""

    from ofdm_message_link import devices

    assert devices.channel_label("b200", 0) == "RF A"
    assert devices.channel_label("b200", 1) == "RF B"


def test_an_unlisted_device_family_still_gets_usable_labels():
    from ofdm_message_link import devices

    assert devices.channel_label("x300", 0) == "Channel 0"
    assert devices.family_for("x300") is None


class _FakeN210(_FakeMultiUsrp):
    """Stands in for an N210 with a CBX: one slot, a CAL port, 1.2-6 GHz."""

    def get_rx_num_channels(self):
        return 1

    def get_tx_num_channels(self):
        return 1

    def get_rx_antennas(self, chan):
        return ["TX/RX", "RX2", "CAL"]

    def get_tx_antennas(self, chan):
        return ["TX/RX", "CAL"]

    def get_rx_gain_range(self, chan):
        return _FakeRange(0.0, 31.5)

    def get_tx_gain_range(self, chan):
        return _FakeRange(0.0, 31.5)

    def get_rx_freq_range(self, chan):
        return _FakeRange(1180e6, 6020e6)

    def get_tx_freq_range(self, chan):
        return _FakeRange(1180e6, 6020e6)

    def get_mboard_name(self, mboard):
        return "N210r4"


class _FakeN210Uhd(_FakeUhdModule):
    def MultiUSRP(self, args):  # noqa: N802 (mirrors the UHD API name)
        self.opened.append(args)
        return _FakeN210(args)


# What uhd.find() returns for an N210: no name, no product.
_N210_ADDRESSES = ["type=usrp2,addr=192.168.10.2,name=,serial=F12345"]


def test_an_n210_is_discovered_over_ethernet_as_a_supported_family():
    from ofdm_message_link import devices

    (n210,) = devices.discover(uhd_api=_FakeN210Uhd(_N210_ADDRESSES))

    assert n210.driver == "usrp2"
    assert n210.address == "192.168.10.2"
    assert n210.device_args == "serial=F12345"
    assert n210.is_supported is True
    assert n210.describe() == "Ettus USRP N200/N210 (serial F12345, 192.168.10.2)"


def test_n210_probe_offers_only_streamable_ports_on_its_single_slot():
    """CAL is a daughterboard loopback that B210Settings would reject."""

    from ofdm_message_link import devices

    fake = _FakeN210Uhd(_N210_ADDRESSES)
    capabilities = devices.probe(devices.discover(uhd_api=fake)[0], uhd_api=fake)

    assert capabilities.mboard_name == "N210r4"
    assert [channel.label for channel in capabilities.channels] == ["Slot A"]
    assert capabilities.channels[0].rx_antennas == ("TX/RX", "RX2")
    assert capabilities.channels[0].tx_antennas == ("TX/RX",)
    assert capabilities.freq_range_hz("tx") == (1180e6, 6020e6)


def test_n210_ports_carry_their_front_panel_names():
    from ofdm_message_link import devices

    assert devices.antenna_label("usrp2", "TX/RX") == "TX/RX (RF1)"
    assert devices.antenna_label("usrp2", "RX2") == "RX2 (RF2)"
    assert devices.antenna_label("b200", "TX/RX") == "TX/RX"

    selection = devices.RadioSelection(
        serial="F12345",
        channel=0,
        antenna="TX/RX",
        gain_db=10.0,
        center_frequency_hz=1.2e9,
        sample_rate=5_000_000,
        driver="usrp2",
    )
    assert selection.describe().startswith("serial F12345 Slot A TX/RX (RF1) @ 1200.000 MHz")


def test_n210_with_a_cbx_refuses_915_mhz_before_starting():
    from ofdm_message_link import devices

    fake = _FakeN210Uhd(_N210_ADDRESSES)
    capabilities = devices.probe(devices.discover(uhd_api=fake)[0], uhd_api=fake)
    for frequency, expected in ((915e6, 1), (1.2e9, 0)):
        selection = devices.default_selection(
            capabilities, direction="tx", center_frequency_hz=frequency, sample_rate=5_000_000
        )
        problems = devices.validate(selection, capabilities, direction="tx")
        assert len(problems) == expected, problems


@pytest.mark.parametrize(
    ("actual_hz", "refused"),
    [(1_200_000_000.0, False), (1_200_000_000.0046, False), (1_180_000_000.0046566, True)],
)
def test_a_radio_that_did_not_reach_the_requested_frequency_is_refused(actual_hz, refused):
    """UHD clamps silently; the peer would then listen on another frequency."""

    from ofdm_message_link.transport import TransportError, require_tuned

    class _Block:
        def get_center_freq(self, chan):
            return actual_hz

    if refused:
        with pytest.raises(TransportError, match="1180.000 MHz instead of the requested 1200.000"):
            require_tuned(_Block(), 1.2e9)
    else:
        require_tuned(_Block(), 1.2e9)


def test_discovery_and_probing_hold_the_cross_process_uhd_lock(tmp_path, monkeypatch):
    """Concurrent N210 discovery corrupts the serial; only one process may look."""

    import fcntl

    from ofdm_message_link import devices

    lock = tmp_path / "uhd.lock"
    monkeypatch.setattr(devices, "UHD_ACCESS_LOCK", str(lock))

    def held() -> bool:
        with open(lock, "a", encoding="utf-8") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(other, fcntl.LOCK_UN)
            return False

    seen = []

    class _Checking(_FakeN210Uhd):
        def find(self, args):
            seen.append(("find", held()))
            return super().find(args)

        def MultiUSRP(self, args):  # noqa: N802 (mirrors the UHD API name)
            seen.append(("open", held()))
            return super().MultiUSRP(args)

    fake = _Checking(_N210_ADDRESSES)
    devices.probe(devices.discover(uhd_api=fake)[0], uhd_api=fake)

    assert seen == [("find", True), ("open", True)]
    assert held() is False  # released afterwards


def test_probe_reports_the_front_ends_the_device_declares():
    from ofdm_message_link import devices

    fake = _FakeUhdModule(_B210_ADDRESSES)
    device = devices.discover(uhd_api=fake)[0]

    capabilities = devices.probe(device, uhd_api=fake)

    assert fake.opened == ["serial=30B0001"]
    assert capabilities.subdev_spec == "A:A A:B"
    assert [channel.label for channel in capabilities.channels] == ["RF A", "RF B"]
    assert capabilities.channels[0].rx_antennas == ("TX/RX", "RX2")
    assert capabilities.channels[0].tx_antennas == ("TX/RX",)
    assert capabilities.channels[1].rx_gain_range_db == (0.0, 76.0)
    assert capabilities.sample_rates == (5_000_000, 10_000_000, 20_000_000)


def test_probe_failure_is_reported_not_raised_as_an_arbitrary_error():
    from ofdm_message_link import devices

    class _Busy(_FakeUhdModule):
        def MultiUSRP(self, args):  # noqa: N802
            raise RuntimeError("device is already in use")

    fake = _Busy(_B210_ADDRESSES)
    device = devices.discover(uhd_api=fake)[0]
    with pytest.raises(devices.DeviceError, match="already in use"):
        devices.probe(device, uhd_api=fake)


def test_transmit_gain_defaults_to_the_bottom_of_the_range():
    """Starting a radio must not key an amplifier at an arbitrary power."""

    from ofdm_message_link import devices

    fake = _FakeUhdModule(_B210_ADDRESSES)
    capabilities = devices.probe(devices.discover(uhd_api=fake)[0], uhd_api=fake)

    selection = devices.default_selection(
        capabilities,
        direction="tx",
        center_frequency_hz=3.8e9,
        sample_rate=5_000_000,
    )
    assert selection.gain_db == 0.0
    assert selection.antenna == "TX/RX"
    assert selection.channel == 0
    assert devices.validate(selection, capabilities, direction="tx") == []


def test_a_preferred_antenna_is_honoured_only_when_the_port_exists():
    from ofdm_message_link import devices

    fake = _FakeUhdModule(_B210_ADDRESSES)
    capabilities = devices.probe(devices.discover(uhd_api=fake)[0], uhd_api=fake)

    receive = devices.default_selection(
        capabilities,
        direction="rx",
        center_frequency_hz=3.8e9,
        sample_rate=5_000_000,
        preferred_antenna="RX2",
    )
    assert receive.antenna == "RX2"

    transmit = devices.default_selection(
        capabilities,
        direction="tx",
        center_frequency_hz=3.8e9,
        sample_rate=5_000_000,
        preferred_antenna="RX2",  # transmit has no RX2 port
    )
    assert transmit.antenna == "TX/RX"


@pytest.mark.parametrize(
    ("overrides", "direction", "expected"),
    [
        ({"antenna": "RX2"}, "tx", "no TX port"),
        ({"channel": 7}, "rx", "no channel 7"),
        ({"gain_db": 120.0}, "rx", "outside"),
        ({"center_frequency_hz": 20e9}, "rx", "outside"),
        ({"sample_rate": 7_000_000}, "rx", "sample rate must be one of"),
    ],
)
def test_validate_rejects_a_selection_the_device_cannot_honour(overrides, direction, expected):
    import dataclasses

    from ofdm_message_link import devices

    fake = _FakeUhdModule(_B210_ADDRESSES)
    capabilities = devices.probe(devices.discover(uhd_api=fake)[0], uhd_api=fake)
    base = devices.default_selection(
        capabilities,
        direction=direction,
        center_frequency_hz=3.8e9,
        sample_rate=5_000_000,
    )

    problems = devices.validate(
        dataclasses.replace(base, **overrides), capabilities, direction=direction
    )
    assert any(expected in problem for problem in problems), problems


def test_a_selection_becomes_validated_radio_settings():
    from ofdm_message_link import devices

    config = options.resolve(_tx_args([])).config
    selection = devices.RadioSelection(
        serial="30B0001",
        channel=1,
        antenna="TX/RX",
        gain_db=12.5,
        center_frequency_hz=3.8e9,
        sample_rate=5_000_000,
    )

    settings = options.settings_for(selection, config, direction="tx")

    assert settings.device_args == "serial=30B0001"
    assert settings.channel == 1  # RF B
    assert settings.tx_antenna == "TX/RX"
    assert settings.tx_gain == 12.5
    assert settings.center_frequency == 3.8e9
    assert settings.sample_rate == 5_000_000
    # Analog bandwidth follows the sample rate rather than the file, so moving
    # to a wider rate does not leave the filter clipping the signal.
    assert settings.bandwidth == 5_000_000


def test_receive_settings_take_the_gain_for_their_own_direction():
    from ofdm_message_link import devices

    config = options.resolve(_tx_args([])).config
    selection = devices.RadioSelection(
        serial="30B0002",
        channel=0,
        antenna="RX2",
        gain_db=55.0,
        center_frequency_hz=3.8e9,
        sample_rate=10_000_000,
    )

    settings = options.settings_for(selection, config, direction="rx")

    assert settings.rx_antenna == "RX2"
    assert settings.rx_gain == 55.0
    assert settings.tx_gain == config.radio.tx_gain
    assert settings.bandwidth == 10_000_000


# -- reading the windows while they refresh --------------------------------


@pytest.mark.gui
def test_a_refreshing_panel_does_not_drag_the_reader_back(offscreen_qt):
    """Stats refresh twice a second; scrolling must survive a refresh."""

    from PyQt5 import QtWidgets

    from ofdm_message_link import qt_runtime

    view = QtWidgets.QPlainTextEdit()
    view.setReadOnly(True)
    view.resize(300, 80)
    qt_runtime.set_text_preserving_scroll(view, "\n".join(f"row {i}" for i in range(200)))
    offscreen_qt.processEvents()

    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    parked = bar.value()
    assert parked > 0

    qt_runtime.set_text_preserving_scroll(
        view, "\n".join(f"row {i} changed" for i in range(200))
    )
    offscreen_qt.processEvents()
    assert bar.value() == parked


@pytest.mark.gui
def test_a_panel_left_at_the_bottom_keeps_following_new_content(offscreen_qt):
    from PyQt5 import QtWidgets

    from ofdm_message_link import qt_runtime

    view = QtWidgets.QPlainTextEdit()
    view.resize(300, 80)
    qt_runtime.set_text_preserving_scroll(view, "\n".join(f"row {i}" for i in range(50)))
    offscreen_qt.processEvents()
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum())

    qt_runtime.set_text_preserving_scroll(view, "\n".join(f"row {i}" for i in range(200)))
    offscreen_qt.processEvents()
    assert bar.value() == bar.maximum()


@pytest.mark.gui
def test_a_table_view_keeps_its_heading_row_in_view(offscreen_qt):
    from PyQt5 import QtWidgets

    from ofdm_message_link import qt_runtime

    view = QtWidgets.QPlainTextEdit()
    view.resize(300, 80)
    view.show()
    for rows in (50, 60):
        qt_runtime.set_text_preserving_scroll(
            view, "\n".join(f"row {i}" for i in range(rows)), follow_tail=False
        )
        offscreen_qt.processEvents()
    assert view.verticalScrollBar().value() == 0


@pytest.mark.gui
def test_appending_a_message_does_not_yank_a_scrolled_up_log(offscreen_qt):
    from PyQt5 import QtWidgets

    from ofdm_message_link import qt_runtime

    view = QtWidgets.QPlainTextEdit()
    view.setReadOnly(True)
    view.resize(300, 80)
    for index in range(200):
        qt_runtime.append_line_following_tail(view, f"message {index}")
    offscreen_qt.processEvents()

    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 3)
    parked = bar.value()

    qt_runtime.append_line_following_tail(view, "a new message arrives")
    offscreen_qt.processEvents()
    assert bar.value() == parked


# -- live constellation ----------------------------------------------------


def test_every_decoded_burst_is_observable_not_only_completed_messages(profile):
    """A fragmented message must not freeze the constellation between parts."""

    transmitter = MessageTransmitter(profile)
    receiver = MessageReceiver(profile)
    message = bytes(range(256)) * 12  # several frames

    bursts = transmitter.encode_message(message)
    assert len(bursts) > 1
    delivered = receiver.feed(_awgn(concatenate_bursts(bursts), 15.0, seed=31))
    observations = receiver.drain_observations()

    assert len(delivered) == 1, "one message"
    assert len(observations) == len(bursts), "one observation per burst"
    assert [item.sequence for item in observations] == [b.sequence for b in bursts]
    assert all(item.is_ours for item in observations)
    assert all(item.modulation is profile.mcs for item in observations)
    assert all(item.payload_symbols.size > 0 for item in observations)


def test_observations_are_drained_once_and_stay_bounded(profile):
    transmitter = MessageTransmitter(profile)
    receiver = MessageReceiver(profile, max_observations=2)

    stream = np.concatenate(
        [concatenate_bursts(transmitter.encode_message(f"m{i}".encode())) for i in range(5)]
    ).astype(np.complex64)
    receiver.feed(_awgn(stream, 15.0, seed=32))

    first = receiver.drain_observations()
    assert len(first) == 2, "oldest observations are dropped, not accumulated"
    assert receiver.drain_observations() == ()


def test_a_foreign_burst_is_still_observable(profile):
    """It demodulated, so it has a constellation worth showing."""

    from ofdm_link.phy import CURRENT_PROTOCOL_VERSION, Frame, FrameKind, encode_burst
    from ofdm_link.runtime.factory import build_burst_config, select_frame_decoder

    selection = select_frame_decoder(profile.config)
    foreign = encode_burst(
        Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, profile.mcs, 0, b"not our format"),
        build_burst_config(profile.config),
        symbol_mapper=selection.symbol_mapper,
        wire_version=selection.wire_version,
        frame_encoder=selection.frame_encoder,
    )
    receiver = MessageReceiver(profile)
    receiver.feed(_awgn(foreign.samples, 15.0, seed=33))

    observations = receiver.drain_observations()
    assert len(observations) == 1
    assert observations[0].is_ours is False


# -- transmit pacing and the minimum-gain trap -----------------------------


@pytest.mark.gui
def test_a_full_transport_paces_the_sender_instead_of_dropping_bursts(offscreen_qt):
    """A batch outruns the radio's queue; that must not look like link loss."""

    import threading

    from ofdm_message_link.tx_app import TransmitWorker, _Bridge

    class _SlowSink:
        """Accepts one burst at a time, as a radio queue does."""

        def __init__(self):
            self.accepted = []
            self.refusals = 0
            self._room = threading.Semaphore(1)

        def start(self):
            pass

        def send(self, samples):
            if not self._room.acquire(blocking=False):
                self.refusals += 1
                return False
            self.accepted.append(samples)
            threading.Timer(0.01, self._room.release).start()
            return True

        def stop(self):
            pass

    args = _tx_args([])
    resolved = options.resolve(args)
    worker = TransmitWorker(MessageTransmitter(resolved.profile), _Bridge())
    sink = _SlowSink()
    worker.start()
    worker.attach(sink)
    try:
        for index in range(12):
            assert worker.submit("batch", f"m{index}".encode()) is True
        deadline = time.monotonic() + 20.0
        while len(sink.accepted) < 12 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        worker.stop()

    assert len(sink.accepted) == 12, "every burst is sent, none dropped"
    assert sink.refusals > 0, "the sink really did run out of room"


@pytest.mark.gui
def test_the_window_says_when_transmit_gain_is_as_good_as_off(offscreen_qt):
    """The safe default is the minimum, which looks broken rather than quiet."""

    import dataclasses

    from ofdm_message_link import devices
    from ofdm_message_link.tx_app import TransmitWindow, build_parser

    class _FakePanel:
        def gain_range_db(self):
            return (0.0, 89.75)

    args = build_parser().parse_args(["--udp-port", "53321", "--ingress-port", "53322"])
    resolved = options.resolve(args)
    window = TransmitWindow(
        resolved,
        53322,
        build_sink=lambda selection: options.build_sink(args, resolved, selection),
    )
    try:
        window._panel = _FakePanel()
        window._selection = devices.RadioSelection(
            serial="30B0002",
            channel=0,
            antenna="TX/RX",
            gain_db=0.0,
            center_frequency_hz=3.8e9,
            sample_rate=5_000_000,
        )
        warning = window._minimum_gain_warning()
        assert warning is not None
        assert "minimum" in warning
        # Gain is live now, so the remedy no longer involves stopping the radio.
        assert "Raise Gain on the Hardware tab" in warning
        assert "takes effect immediately" in warning

        window._selection = dataclasses.replace(window._selection, gain_db=70.0)
        assert window._minimum_gain_warning() is None
    finally:
        window.close()


class _QuantisingGainBlock:
    """A UHD block whose gain moves in the CBX's 0.5 dB steps."""

    def __init__(self):
        self.gain = 0.0
        self.calls = []

    def set_gain(self, gain, chan):
        self.calls.append((gain, chan))
        self.gain = round(gain * 2.0) / 2.0

    def get_gain(self, chan):
        return self.gain


def test_a_running_uhd_sink_changes_gain_and_reports_the_device_readback():
    from ofdm_link.radio.uhd import B210Settings

    sink = transport.UhdSampleSink(B210Settings(tx_gain=5.0), rf_enable=None)
    with pytest.raises(transport.TransportError, match="not started"):
        sink.set_gain(10.0)

    block = _QuantisingGainBlock()
    sink._sink_block = block
    sink._readback = {"gain_db": 5.0}

    assert transport.set_transport_gain(sink, 10.3) == 10.5
    assert block.calls == [(10.3, 0)]
    assert sink.snapshot()["tx_gain_db"] == 10.5
    assert sink.snapshot()["readback"]["gain_db"] == 10.5
    with pytest.raises(transport.TransportError, match="finite"):
        sink.set_gain(float("nan"))


def test_a_running_uhd_source_changes_gain_without_restarting():
    from ofdm_link.radio.uhd import B210Settings

    source = transport.UhdSampleSource(B210Settings(rx_gain=15.0))
    assert source.snapshot()["rx_gain_db"] == 15.0
    source._source_block = _QuantisingGainBlock()

    assert transport.set_transport_gain(source, 20.0) == 20.0
    assert source.snapshot()["rx_gain_db"] == 20.0


def test_gain_cannot_be_changed_without_a_running_adjustable_radio():
    with pytest.raises(transport.TransportError, match="not running"):
        transport.set_transport_gain(None, 10.0)
    udp = transport.UdpSampleSink(port=53399)
    with pytest.raises(transport.TransportError, match="no adjustable gain"):
        transport.set_transport_gain(udp, 10.0)


def test_ready_file_appears_atomically_and_is_withdrawn(tmp_path):
    ready = tmp_path / "tx.ready"

    options.mark_ready(str(ready), "serial F12345 Slot A TX/RX (RF1)")
    assert ready.read_text(encoding="utf-8") == "serial F12345 Slot A TX/RX (RF1)\n"
    assert not (tmp_path / "tx.ready.tmp").exists()

    options.clear_ready(str(ready))
    assert not ready.exists()
    options.clear_ready(str(ready))  # already gone is fine
    options.mark_ready(None, "ignored")
    options.clear_ready(None)


def test_ready_file_option_is_accepted_by_both_apps():
    from ofdm_message_link.rx_app import build_parser as rx_parser
    from ofdm_message_link.tx_app import build_parser as tx_parser

    for parser in (rx_parser(), tx_parser()):
        assert parser.parse_args(["--ready-file", "/tmp/x"]).ready_file == "/tmp/x"
        assert parser.parse_args([]).ready_file is None


def test_engine_child_reports_a_failed_gain_change_instead_of_dying():
    from ofdm_message_link.engine import _apply_gain_command

    class _Failed:
        def __init__(self):
            self.messages = []

        def emit(self, message):
            self.messages.append(message)

    class _Sink:
        failed = _Failed()

    class _Worker:
        def set_gain(self, gain_db):
            raise transport.TransportError("the radio is not running")

    sink = _Sink()
    _apply_gain_command(_Worker(), 12.0, sink)
    assert sink.failed.messages == ["gain change failed: TransportError: the radio is not running"]


def test_choosing_radio_means_a_panel_that_is_neither_running_nor_auto_starting():
    pytest.importorskip("PyQt5")
    from ofdm_message_link.dashboard_widgets import choosing_radio

    class _Panel:
        running = False
        _auto_start = False

    panel = _Panel()
    assert choosing_radio(None) is False
    assert choosing_radio(panel) is True
    panel.running = True
    assert choosing_radio(panel) is False
    panel.running, panel._auto_start = False, True
    assert choosing_radio(panel) is False


@pytest.mark.gui
def test_radio_panel_gain_stays_live_while_running(offscreen_qt, monkeypatch):
    from ofdm_message_link import devices, radio_panel

    monkeypatch.setattr(devices, "discover", lambda: ())
    panel = radio_panel.RadioPanel(
        direction="tx", center_frequency_hz=1.2e9, sample_rate=5_000_000
    )
    requested = []
    panel.gainChangeRequested.connect(requested.append)
    try:
        panel._gain.setRange(0.0, 31.5)
        panel._gain.setValue(3.0)
        assert requested == []  # stopped: the value is only a choice for Start

        panel.set_running(True, "transmitting")
        assert panel.running
        assert panel._gain.isEnabled()
        assert not panel._device.isEnabled()
        assert not panel._frequency.isEnabled()
        panel._gain.setValue(7.5)
        assert requested == [7.5]

        panel.set_running(False, "radio stopped")
        panel._gain.setValue(9.0)
        assert requested == [7.5]
    finally:
        panel.close()


@pytest.mark.gui
@pytest.mark.parametrize("direction", ["rx", "tx"])
def test_a_window_applies_a_live_gain_change_to_its_running_radio(
    offscreen_qt, tmp_path, direction
):
    from PyQt5 import QtCore, QtWidgets

    class _Panel(QtWidgets.QGroupBox):
        startRequested = QtCore.pyqtSignal(object)
        stopRequested = QtCore.pyqtSignal()
        gainChangeRequested = QtCore.pyqtSignal(float)
        _auto_start = False
        running = False

        def __init__(self):
            super().__init__("Radio")
            self.statuses = []

        def set_running(self, running, detail=""):
            self.running = running

        def show_status(self, message, *, error=False):
            self.statuses.append((message, error))

        def gain_range_db(self):
            return (0.0, 31.5)

    class _Radio:
        description = "fake radio"

        def __init__(self):
            self.gains = []

        def start(self):
            pass

        def stop(self):
            pass

        def recv(self, timeout):
            time.sleep(timeout)

        def send(self, samples):
            return True

        def snapshot(self):
            return {}

        def set_gain(self, gain_db):
            self.gains.append(gain_db)
            return gain_db

    radio = _Radio()
    panel = _Panel()
    ready = tmp_path / f"{direction}.ready"
    selection = _n210_selection()
    if direction == "rx":
        from ofdm_message_link.rx_app import ReceiveWindow, build_parser

        args = build_parser().parse_args(["--udp-port", "53361", "--egress-port", "53362"])
        window = ReceiveWindow(
            options.resolve(args),
            ("127.0.0.1", 53362),
            build_source=lambda chosen: radio,
            radio_panel=panel,
            ready_file=str(ready),
        )
    else:
        from ofdm_message_link.tx_app import TransmitWindow, build_parser

        args = build_parser().parse_args(["--udp-port", "53363", "--ingress-port", "53364"])
        window = TransmitWindow(
            options.resolve(args),
            53364,
            build_sink=lambda chosen: radio,
            radio_panel=panel,
            ready_file=str(ready),
        )
    try:
        window.show()
        window.resize(1000, 450)
        offscreen_qt.processEvents()
        # Short window, radio not chosen yet: the device panel's tab stays
        # and the still-empty cards and chart make room for it.
        assert window._tabs.isVisible()
        assert not window._trend.isVisible()
        assert not window._cards.isVisible()

        panel.gainChangeRequested.emit(12.0)
        assert panel.statuses[-1][1] is True  # nothing to apply it to yet
        assert radio.gains == []

        panel.startRequested.emit(selection)
        offscreen_qt.processEvents()
        assert ready.read_text(encoding="utf-8").startswith("serial F12345")
        assert not window._tabs.isVisible()  # running: the short window folds
        assert window._trend.isVisible()
        assert window._cards.isVisible()

        panel.gainChangeRequested.emit(12.0)
        assert radio.gains == [12.0]
        assert panel.statuses[-1] == (
            f"{direction.upper()} gain set to 12 dB while "
            f"{'receiving' if direction == 'rx' else 'transmitting'}",
            False,
        )
        if direction == "tx":
            assert window._selection.gain_db == 12.0

        panel.stopRequested.emit()
        offscreen_qt.processEvents()
        assert not ready.exists()
    finally:
        window.close()
    assert not ready.exists()


def _n210_selection():
    from ofdm_message_link import devices

    return devices.RadioSelection(
        serial="F12345",
        channel=0,
        antenna="TX/RX",
        gain_db=5.0,
        center_frequency_hz=1.2e9,
        sample_rate=5_000_000,
        driver="usrp2",
    )


def test_rx_time_gap_counter_reports_each_overflow_and_the_samples_it_lost() -> None:
    counter = transport.RxTimeGapCounter(sample_rate=5_000_000.0)

    counter.observe(0, 100.0)  # stream start: the anchor, not an overflow
    counter.observe(50_000, 100.01)  # continuous (retune-style re-tag)
    counter.observe(80_000, 100.016 + 0.002)  # 10,000 samples missing
    counter.observe(90_000, 100.018 + 0.002 + 0.1)  # 500,000 more missing

    assert counter.discontinuities == 2
    assert counter.samples_lost == 510_000


def test_rx_time_gap_counter_ignores_sub_sample_tag_jitter() -> None:
    counter = transport.RxTimeGapCounter(sample_rate=10_000_000.0)

    counter.observe(1_000, 5.0)
    counter.observe(11_000, 5.001 + 0.4e-7)

    assert counter.discontinuities == 0
    assert counter.samples_lost == 0


def test_uhd_sink_never_schedules_timed_bursts_back_to_back():
    """A timed burst abutting the previous one reaches the B210 late ('L').

    OTA, a video I-frame queued 30 bursts at once: with zero spacing 1122 of
    1470 bursts were late, with 20 us spacing none were.
    """

    from types import SimpleNamespace

    class Clock:
        def get_time_now(self):
            return 10.0

    class Queue:
        def __init__(self):
            self.starts = []

        def enqueue(self, samples, start):
            self.starts.append((start, samples.size))
            return True

    rate = 5_000_000.0
    limits = transport.UhdSinkLimits()
    sink = transport.UhdSampleSink(SimpleNamespace(sample_rate=rate), rf_enable=None)
    sink._sink_block = Clock()
    sink._source_block = Queue()

    for _ in range(3):
        assert sink.send(np.ones(20_000, dtype=np.complex64))

    (first, size), *rest = sink._source_block.starts
    assert first == pytest.approx(10.0 + limits.tx_lead_seconds)
    previous_end = first + size / rate
    for start, size in rest:
        assert start - previous_end == pytest.approx(limits.tx_burst_gap_seconds)
        assert limits.tx_burst_gap_seconds >= 20e-6
        previous_end = start + size / rate


class _RecordingSignal:
    def __init__(self):
        self.values = []

    def emit(self, value):
        self.values.append(value)


class _RecordingBridge:
    def __init__(self):
        self.message = _RecordingSignal()
        self.sent = _RecordingSignal()
        self.level = _RecordingSignal()
        self.failed = _RecordingSignal()


def test_process_engines_carry_ingress_to_egress_and_leave_no_process_behind():
    """Both radio paths run in child processes; only UDP ports connect them."""

    import functools
    import multiprocessing
    import socket

    from ofdm_message_link.engine import ProcessReceiveWorker, ProcessTransmitWorker
    from ofdm_message_link.plots import PlotBuffers
    from ofdm_message_link.rx_app import build_parser as rx_parser
    from ofdm_message_link.tx_app import build_parser as tx_parser

    egress = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    egress.bind(("127.0.0.1", 0))
    egress.settimeout(0.2)
    egress_port = egress.getsockname()[1]
    rx_args = rx_parser().parse_args(["--udp-port", "53341"])
    tx_args = tx_parser().parse_args(["--udp-port", "53341", "--ingress-port", "53342"])
    rx_resolved = options.resolve(rx_args)
    tx_resolved = options.resolve(tx_args)
    rx_bridge, tx_bridge = _RecordingBridge(), _RecordingBridge()
    receiver = ProcessReceiveWorker(
        rx_resolved.profile,
        rx_bridge,
        PlotBuffers(),
        egress_address=("127.0.0.1", egress_port),
    )
    transmitter = ProcessTransmitWorker(
        tx_resolved.profile,
        tx_resolved.mcs_entry,
        tx_bridge,
        PlotBuffers(),
        ingress_port=53342,
    )
    children = []
    try:
        receiver.start()
        transmitter.start()
        children = multiprocessing.active_children()
        assert receiver.attach_from(
            functools.partial(options.build_source, rx_args, rx_resolved, None)
        ).startswith("udp")
        transmitter.attach_from(functools.partial(options.build_sink, tx_args, tx_resolved, None))

        assert transmitter.submit("typed", b"typed through the engine")
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(
            b"ingress through the engine", ("127.0.0.1", 53342)
        )
        received = []
        deadline = time.monotonic() + 20.0
        while len(received) < 2 and time.monotonic() < deadline:
            try:
                received.append(egress.recv(65535))
            except TimeoutError:
                pass
        assert sorted(received) == [b"ingress through the engine", b"typed through the engine"]

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and (
            len(rx_bridge.message.values) < 2
            or receiver.egress_datagrams < 2
            or transmitter.ingress_datagrams < 1
            or len(tx_bridge.sent.values) < 2
        ):
            time.sleep(0.05)
        assert receiver.stats.messages_delivered == 2
        assert receiver.egress_datagrams == 2
        assert transmitter.ingress_datagrams == 1
        assert len(tx_bridge.sent.values) == 2
        assert all(item.payload_symbols.size == 0 for item in rx_bridge.message.values)
        assert "chunks_dropped_overflow" in receiver.source.snapshot()
        assert not rx_bridge.failed.values and not tx_bridge.failed.values
    finally:
        transmitter.stop()
        receiver.stop()
        egress.close()
    assert children and all(not child.is_alive() for child in children)


def _mixed_mcs_stream(profile, *, bursts, seed):
    from ofdm_link.phy.mcs_table import mcs_entry

    transmitter = MessageTransmitter(profile)
    rng = np.random.default_rng(seed)
    blocks, messages = [], []
    for index in range(bursts):
        transmitter.select_mcs_entry(mcs_entry(4 if index % 3 else 0))
        message = rng.bytes(int(rng.integers(20, 900)))
        messages.append(message)
        blocks.append(np.zeros(int(rng.integers(0, 3000)), dtype=np.complex64))
        blocks.append(concatenate_bursts(transmitter.encode_message(message)))
    blocks.append(np.zeros(9000, dtype=np.complex64))
    return _awgn(np.concatenate(blocks).astype(np.complex64), 18.0, seed=seed), messages


def _receive_all(receiver, stream, chunk=8192):
    delivered = []
    for start in range(0, stream.size, chunk):
        delivered.extend(receiver.feed(stream[start : start + chunk]))
    delivered.extend(receiver.flush())
    return delivered


def test_parallel_payload_decode_is_byte_exact_and_ordered_like_one_worker(profile):
    import multiprocessing
    import threading

    stream, messages = _mixed_mcs_stream(profile, bursts=36, seed=41)
    inline = MessageReceiver(profile)
    expected = _receive_all(inline, stream)
    threads_before = set(threading.enumerate())
    parallel = MessageReceiver(profile, decode_workers=4)
    try:
        workers = multiprocessing.active_children()
        actual = _receive_all(parallel, stream)
        observations = parallel.drain_observations()
    finally:
        parallel.close()

    assert [item.message.payload for item in expected] == messages
    assert [item.message.payload for item in actual] == messages
    assert [(m.sequence, m.modulation) for m in actual] == [
        (m.sequence, m.modulation) for m in expected
    ]
    for got, want in zip(actual, expected, strict=True):
        np.testing.assert_array_equal(got.payload_symbols, want.payload_symbols)
        # Quality is measured in the worker; a reduction on a differently
        # aligned copy may round differently in the last bit.
        assert got.evm == pytest.approx(want.evm, rel=1e-12)
        assert got.effective_snr_db == pytest.approx(want.effective_snr_db, rel=1e-12)
    assert parallel.stats.bursts_decoded == inline.stats.bursts_decoded == 36
    assert parallel.stats.missing_bursts == 0
    assert parallel.decoder_snapshot().valid_decoded_bursts == 36
    assert len(observations) == min(36, 64)
    assert parallel.decode_pool_snapshot().submitted == 36
    assert len(workers) >= 4 and all(not child.is_alive() for child in workers)
    deadline = time.monotonic() + 5.0
    while set(threading.enumerate()) - threads_before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not set(threading.enumerate()) - threads_before


def test_one_decode_worker_keeps_the_inline_decoder(profile):
    receiver = MessageReceiver(profile, decode_workers=1)
    assert receiver.decode_workers == 1
    assert receiver.decode_pool_snapshot() is None
    assert receiver.flush() == ()
    receiver.close()
    with pytest.raises(ValueError):
        MessageReceiver(profile, decode_workers=0)


def test_parallel_decode_bounds_in_flight_work_and_counts_the_waits(profile):
    stream, messages = _mixed_mcs_stream(profile, bursts=12, seed=43)
    receiver = MessageReceiver(profile, decode_workers=2)
    try:
        delivered = []
        # One chunk holding every burst makes the owner submit faster than two
        # workers can decode, so the bound must push back.
        delivered.extend(receiver.feed(stream))
        delivered.extend(receiver.flush())
        snapshot = receiver.decode_pool_snapshot()
    finally:
        receiver.close()
    assert [item.message.payload for item in delivered] == messages
    assert snapshot.max_in_flight == 8
    assert snapshot.backpressure_waits > 0
    assert snapshot.in_flight == 0
