"""Opt-in overload protection: off by default, and every drop or give-up counted apart."""

from __future__ import annotations

import functools
import os
import time
from types import SimpleNamespace

import pytest
from stream_fixtures import MEASUREMENT, deliver, profile, v5_stream

from ofdm_link.phy.streaming import PayloadFailure, StreamingBurstDecoder
from ofdm_link.phy.turbo import ABANDONED_REASON, hopeless_block_give_up
from ofdm_link.runtime import overload
from ofdm_link.runtime.segment_decode_pool import SegmentDecodePool
from ofdm_message_link import node_options, options, rx_app, tx_app
from ofdm_message_link.link import MessageReceiver


@pytest.fixture(autouse=True)
def _no_inherited_setting():
    saved = os.environ.pop(overload.ENVIRONMENT, None)
    yield
    os.environ.pop(overload.ENVIRONMENT, None)
    if saved is not None:
        os.environ[overload.ENVIRONMENT] = saved


def test_every_app_defaults_to_off():
    assert rx_app.build_parser().parse_args([]).overload_protection == "off"
    assert node_options.build_parser().parse_args(["--node-id", "1"]).overload_protection == "off"
    assert not hasattr(tx_app.build_parser().parse_args([]), "overload_protection")
    assert overload.from_environment() == overload.OFF
    link = profile(MEASUREMENT)
    receiver = MessageReceiver(link)
    try:
        assert receiver._overload == overload.OFF
    finally:
        receiver.close()


def test_modes_map_to_the_two_switches():
    assert overload.OverloadProtection.from_mode("off") == overload.OFF
    assert overload.OverloadProtection.from_mode("drop").drop_when_full
    assert overload.OverloadProtection.from_mode("give-up").give_up_after == 4
    both = overload.OverloadProtection.from_mode("both")
    assert both.drop_when_full and both.give_up_after == 4 and both.mode == "both"
    with pytest.raises(ValueError):
        overload.OverloadProtection.from_mode("on")


def test_the_flag_reaches_the_environment_children_inherit(monkeypatch, capsys):
    args = rx_app.build_parser().parse_args(["--overload-protection", "drop"])
    options.apply_performance_options(args)
    assert os.environ[overload.ENVIRONMENT] == "drop"
    assert overload.from_environment().drop_when_full
    assert "do not use it for measurement runs" in capsys.readouterr().err


def test_an_abandoned_payload_has_its_own_counter():
    decoder = StreamingBurstDecoder()
    ordinary = PayloadFailure(diagnosed=True, crc_failed=True, v5=True)
    abandoned = PayloadFailure(diagnosed=True, crc_failed=True, v5=True, abandoned=True)
    decoder.record_payload_outcome(ordinary)
    decoder.record_payload_outcome(abandoned)
    decoder.record_payload_outcome(abandoned)
    counts = decoder.snapshot()
    assert counts.payload_abandoned == 2
    assert counts.payload_decode_attempts == 1
    assert counts.payload_decode_failure == 1
    assert counts.crc_failures == 1 and counts.psdu_fcs_failures == 1


def test_from_burst_and_from_error_see_the_abandoned_reason():
    diagnostics = SimpleNamespace(early_stop_reasons=("outer_crc32", ABANDONED_REASON),
                                  outer_crc_ok=False, wire_version=5)
    burst = SimpleNamespace(fcs_ok=False, diagnostics=diagnostics)
    assert PayloadFailure.from_burst(burst).abandoned
    burst.diagnostics = SimpleNamespace(early_stop_reasons=("max_iterations",))
    assert not PayloadFailure.from_burst(burst).abandoned
    error = SimpleNamespace(diagnostics=diagnostics)
    assert PayloadFailure.from_error(error).abandoned


def test_give_up_off_and_on_deliver_the_same_at_good_snr():
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=31, snr_db=25.0)
    plain = deliver(link, stream)
    try:
        receiver = MessageReceiver(link, overload=overload.OverloadProtection.from_mode("give-up"))
        assert hopeless_block_give_up() == 4
        delivered = []
        for start in range(0, stream.size, 4096):
            delivered.extend(receiver.feed(stream[start : start + 4096]))
        assert [m.message.payload for m in delivered] == plain
        assert len(plain) == len(payloads)
        assert receiver.decoder_snapshot().payload_abandoned == 0
    finally:
        from ofdm_link.phy.turbo import set_hopeless_block_give_up

        set_hopeless_block_give_up(0)


def _annotate_after_gate(gate_path, burst):
    from ofdm_message_link.link import burst_quality

    deadline = time.monotonic() + 60.0
    while not os.path.exists(gate_path) and time.monotonic() < deadline:
        time.sleep(0.01)
    return burst_quality(burst)


def test_a_full_pool_drops_instead_of_waiting_and_counts_it(tmp_path):
    link = profile(MEASUREMENT)
    stream, payloads = v5_stream(link, seed=41, bursts=14, mcs_indices=(8,))
    gate = tmp_path / "open"
    receiver = MessageReceiver(link, decode_workers=2)
    receiver._pool.close()
    receiver._pool = SegmentDecodePool(
        link.config, workers=2,
        annotate=functools.partial(_annotate_after_gate, str(gate)),
        overload=overload.OverloadProtection.from_mode("drop"),
    )
    try:
        started = time.monotonic()
        delivered = list(receiver.feed(stream))  # one chunk: the owner never waits
        assert time.monotonic() - started < 30.0
        held = receiver.decode_pool_snapshot()
        assert held.completed == 0 and held.in_flight == held.max_in_flight == 8
        assert held.backpressure_waits == 0
        assert held.dropped_full == len(payloads) - 8
        gate.touch()
        delivered.extend(receiver.flush(120.0))
        snapshot = receiver.decode_pool_snapshot()
        counts = receiver.decoder_snapshot()
    finally:
        gate.touch()
        receiver.close()
    assert [m.message.payload for m in delivered] == payloads[:8]
    assert snapshot.dropped_full == len(payloads) - 8
    assert counts.payload_decode_attempts == 8  # dropped segments are not attempts
