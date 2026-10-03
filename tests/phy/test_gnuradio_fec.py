from __future__ import annotations

import builtins
import os
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import numpy as np
import pytest

# White-box exception: these tests replace the optional GNU Radio loader and
# scheduler watchdog to trigger external-runtime failures deterministically.
# Those failures cannot be induced safely through received bits alone.
from ofdm_link.phy import gnuradio_fec
from ofdm_link.phy.codec import (
    CURRENT_PROTOCOL_VERSION,
    MAX_PAYLOAD_LENGTH,
    MCS,
    Frame,
    FrameDecodeError,
    FrameIntegrityError,
    FrameKind,
    decode_frame,
    encode_frame,
    make_convolutional_frame_codec,
    make_soft_convolutional_frame_codec,
)
from ofdm_link.phy.gnuradio_fec import (
    MAX_CODED_BITS,
    GnuRadioFecTimeoutError,
    GnuRadioFecUnavailableError,
    NativeFrameDecoder,
    NativeSoftFecDecoder,
    decode_fec_bits_native,
    decode_frame_native,
    decode_frame_native_timed,
    decode_soft_fec_bits_native,
)


@pytest.mark.gnuradio
def test_native_decoder_matches_portable_frame_decoder() -> None:
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QPSK,
        0x1234,
        bytes(np.arange(32, dtype=np.uint8)),
    )
    coded = encode_frame(frame)

    native, timing = decode_frame_native_timed(coded)

    assert native == decode_frame_native(coded) == decode_frame(coded) == frame
    assert timing.coded_bits == coded.size
    assert timing.decoded_bits == coded.size // 2


@pytest.mark.gnuradio
@pytest.mark.parametrize("payload_length", [0, 1, 47, 48, 49, 1024])
def test_native_decoder_implements_formal_fec_interface(payload_length: int) -> None:
    codec = make_convolutional_frame_codec(hard_decoder=decode_fec_bits_native)
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QPSK,
        payload_length,
        bytes((index * 17 + 3) & 0xFF for index in range(payload_length)),
    )

    assert codec.decode(codec.encode(frame)) == frame


@pytest.mark.gnuradio
@pytest.mark.parametrize("mcs", list(MCS))
@pytest.mark.parametrize("payload_length", [0, 1, 47, 48, 49, 1024])
def test_native_soft_decoder_round_trip_through_formal_interface(
    mcs: MCS,
    payload_length: int,
) -> None:
    codec = make_soft_convolutional_frame_codec(
        soft_decoder=decode_soft_fec_bits_native
    )
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        mcs,
        payload_length,
        bytes((index * 29 + 7) & 0xFF for index in range(payload_length)),
    )
    coded = codec.encode(frame)
    llrs = np.where(coded == 0, 8.0, -8.0).astype(np.float32)

    assert codec.decode(llrs) == frame


@pytest.mark.gnuradio
def test_reusable_native_soft_decoder_corrects_coded_errors() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QAM16, 37, b"soft native")
    coded = encode_frame(frame)
    llrs = np.where(coded == 0, 8.0, -8.0).astype(np.float32)
    llrs[[25, 91, 173]] *= -1.0

    with NativeSoftFecDecoder(coded.size) as decoder:
        codec = make_soft_convolutional_frame_codec(soft_decoder=decoder.decode)
        assert codec.decode(llrs) == frame


@pytest.mark.gnuradio
def test_native_soft_decoder_crc_rejects_uncorrectable_corruption() -> None:
    codec = make_soft_convolutional_frame_codec(
        soft_decoder=decode_soft_fec_bits_native
    )
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, 9, b"crc soft")
    coded = codec.encode(frame)
    llrs = np.where(coded == 0, 8.0, -8.0).astype(np.float32)
    llrs[100:180] *= -1.0

    with pytest.raises(FrameIntegrityError, match="CRC-32"):
        codec.decode(llrs)


@pytest.mark.gnuradio
def test_native_soft_decoder_accepts_maximum_frame_length() -> None:
    codec = make_soft_convolutional_frame_codec(
        soft_decoder=decode_soft_fec_bits_native
    )
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QAM16,
        0xFFFF,
        bytes(MAX_PAYLOAD_LENGTH),
    )
    coded = codec.encode(frame)
    llrs = np.where(coded == 0, 8.0, -8.0).astype(np.float32)

    assert codec.decode(llrs) == frame


@pytest.mark.gnuradio
@pytest.mark.parametrize("mcs", list(MCS))
@pytest.mark.parametrize("payload_length", [0, 1, 7, 31, 32, 33, 255, 1024])
def test_native_decoder_is_bit_exact_across_frame_sizes(
    mcs: MCS,
    payload_length: int,
) -> None:
    payload = bytes((index * 37 + 11) & 0xFF for index in range(payload_length))
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA if payload_length else FrameKind.CONTROL,
        mcs,
        payload_length,
        payload,
    )
    coded = encode_frame(frame)

    native, timing = decode_frame_native_timed(coded)

    assert native == decode_frame(coded) == frame
    assert timing.coded_bits == coded.size
    assert timing.decoded_bits == coded.size // 2
    assert timing.construction_seconds >= 0.0
    assert timing.scheduler_seconds >= 0.0
    assert timing.native_service_seconds == pytest.approx(
        timing.construction_seconds + timing.scheduler_seconds
    )
    assert timing.total_seconds >= timing.native_service_seconds


@pytest.mark.gnuradio
def test_native_decoder_timing_is_immutable() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 3, b"")

    _, timing = decode_frame_native_timed(encode_frame(frame))

    with pytest.raises(FrozenInstanceError):
        timing.coded_bits = 0  # type: ignore[misc]


@pytest.mark.gnuradio
def test_native_decoder_accepts_exact_maximum_frame_length() -> None:
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QAM16,
        0xFFFF,
        bytes(MAX_PAYLOAD_LENGTH),
    )
    coded = encode_frame(frame)

    native, timing = decode_frame_native_timed(coded, timeout_s=5.0)

    assert coded.size == MAX_CODED_BITS
    assert native == frame
    assert timing.decoded_bits == MAX_CODED_BITS // 2


@pytest.mark.gnuradio
def test_native_decoder_corrects_separated_coded_bit_errors() -> None:
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QAM16,
        42,
        b"native viterbi correction",
    )
    corrupted = encode_frame(frame).copy()
    corrupted[[25, 91, 173]] ^= 1

    native, _ = decode_frame_native_timed(corrupted)

    assert native == decode_frame(corrupted) == frame


@pytest.mark.gnuradio
def test_reusable_native_decoder_is_bit_exact_across_consecutive_frames() -> None:
    frames = [
        Frame(
            CURRENT_PROTOCOL_VERSION,
            FrameKind.DATA,
            MCS.QPSK if sequence % 2 == 0 else MCS.QAM16,
            sequence,
            bytes((index * 37 + sequence) & 0xFF for index in range(1024)),
        )
        for sequence in range(24)
    ]
    coded_frames = [encode_frame(frame) for frame in frames]

    with NativeFrameDecoder(coded_frames[0].size) as decoder:
        decoded = [decoder.decode(coded) for coded in coded_frames]

    assert decoded == frames
    assert decoder.closed


@pytest.mark.gnuradio
def test_reusable_native_decoder_remains_usable_until_its_context_closes() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 7, b"")
    coded = encode_frame(frame)

    with NativeFrameDecoder(coded.size) as decoder:
        assert decoder.decode(coded) == frame
        assert decoder.decode(coded) == frame

    assert decoder.closed


@pytest.mark.gnuradio
def test_reusable_native_decoder_rejects_a_different_coded_length() -> None:
    short = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, 1, b"short")
    longer = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, 2, b"longer")
    short_coded = encode_frame(short)

    with NativeFrameDecoder(short_coded.size) as decoder:
        with pytest.raises(FrameDecodeError, match="configured for"):
            decoder.decode(encode_frame(longer))

        assert decoder.decode(short_coded) == short


@pytest.mark.gnuradio
def test_reusable_native_decoder_close_is_idempotent_and_prevents_decode() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 9, b"")
    coded = encode_frame(frame)
    decoder = NativeFrameDecoder(coded.size)

    decoder.close()
    decoder.close()

    assert decoder.closed
    with pytest.raises(RuntimeError, match="closed"):
        decoder.decode(coded)


@pytest.mark.parametrize(
    ("coded_bit_count", "message"),
    [
        (True, "integer"),
        (188.0, "integer"),
        (186, "too short"),
        (MAX_CODED_BITS + 2, "too long"),
        (189, "even"),
        (190, "decoded byte boundary"),
    ],
)
def test_reusable_native_decoder_rejects_invalid_length_before_loading_runtime(
    monkeypatch: pytest.MonkeyPatch,
    coded_bit_count: object,
    message: str,
) -> None:
    monkeypatch.setattr(
        gnuradio_fec,
        "_load_gnuradio",
        lambda: pytest.fail("invalid length must be rejected before loading GNU Radio"),
    )

    with pytest.raises(ValueError, match=message):
        NativeFrameDecoder(coded_bit_count)  # type: ignore[arg-type]


@pytest.mark.gnuradio
def test_reusable_native_decoder_is_poisoned_after_scheduler_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 10, b"")
    coded = encode_frame(frame)
    decoder = NativeFrameDecoder(coded.size)

    def fail_scheduler(
        flowgraph: object,
        timeout_s: float,
        *,
        wait_worker: object | None = None,
    ) -> None:
        del flowgraph, timeout_s, wait_worker
        raise GnuRadioFecTimeoutError("forced watchdog failure")

    monkeypatch.setattr(gnuradio_fec, "_run_with_watchdog", fail_scheduler)

    with pytest.raises(GnuRadioFecTimeoutError, match="forced"):
        decoder.decode(coded)

    assert decoder.closed
    with pytest.raises(RuntimeError, match="closed"):
        decoder.decode(coded)
    decoder.close()


@pytest.mark.gnuradio
def test_native_decoder_rejects_uncorrectable_frame_by_crc() -> None:
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QPSK,
        19,
        b"integrity matters",
    )
    corrupted = encode_frame(frame).copy()
    corrupted[180:196] ^= 1

    with pytest.raises(FrameIntegrityError, match="CRC-32"):
        decode_frame_native(corrupted)


@pytest.mark.parametrize(
    ("coded", "message"),
    [
        (np.zeros((2, 94), dtype=np.uint8), "one-dimensional"),
        (np.zeros(188, dtype=np.float32), "integer bits"),
        (np.full(188, 2, dtype=np.uint8), "only 0 and 1"),
        (np.zeros(186, dtype=np.uint8), "too short"),
        (np.zeros(189, dtype=np.uint8), "even number"),
        (np.zeros(190, dtype=np.uint8), "decoded byte boundary"),
        (np.zeros(MAX_CODED_BITS + 2, dtype=np.uint8), "too long"),
    ],
)
def test_native_decoder_rejects_malformed_coded_bits_before_loading_runtime(
    monkeypatch: pytest.MonkeyPatch,
    coded: np.ndarray,
    message: str,
) -> None:
    def unexpected_runtime_load() -> None:
        pytest.fail("malformed input must be rejected before loading GNU Radio")

    monkeypatch.setattr(gnuradio_fec, "_load_gnuradio", unexpected_runtime_load)

    with pytest.raises(FrameDecodeError, match=message):
        decode_frame_native(coded)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "1"])
def test_native_decoder_rejects_invalid_watchdog_timeout(
    monkeypatch: pytest.MonkeyPatch,
    timeout: object,
) -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 0, b"")
    monkeypatch.setattr(
        gnuradio_fec,
        "_load_gnuradio",
        lambda: pytest.fail("invalid timeout must be rejected before loading GNU Radio"),
    )

    with pytest.raises(ValueError, match="finite positive"):
        decode_frame_native(encode_frame(frame), timeout_s=timeout)  # type: ignore[arg-type]


def test_import_is_lazy_and_headless() -> None:
    environment = os.environ.copy()
    environment.pop("DISPLAY", None)
    command = (
        "import sys; import ofdm_link.phy.gnuradio_fec; "
        "assert not any(name == 'gnuradio' or name.startswith('gnuradio.') "
        "for name in sys.modules)"
    )

    completed = subprocess.run(
        [sys.executable, "-c", command],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


def test_native_decoder_reports_unavailable_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 0, b"")
    real_import = builtins.__import__

    def import_without_gnuradio(name: str, *args: object, **kwargs: object) -> object:
        if name == "gnuradio":
            raise ModuleNotFoundError("forced unavailable runtime")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_gnuradio)

    with pytest.raises(GnuRadioFecUnavailableError, match="unavailable"):
        decode_frame_native(encode_frame(frame))


class _BlockingFlowgraph:
    def __init__(self) -> None:
        self._release = threading.Event()
        self.stopped = False

    def connect(self, *blocks: object) -> None:
        del blocks

    def start(self) -> None:
        pass

    def wait(self) -> None:
        self._release.wait()

    def stop(self) -> None:
        self.stopped = True
        self._release.set()


class _UnstoppableFlowgraph(_BlockingFlowgraph):
    def stop(self) -> None:
        self.stopped = True


class _Sink:
    def data(self) -> tuple[int, ...]:
        return ()


def _blocking_runtime(flowgraph: _BlockingFlowgraph) -> SimpleNamespace:
    decoder = SimpleNamespace()
    return SimpleNamespace(
        gr=SimpleNamespace(sizeof_char=1, top_block=lambda name: flowgraph),
        blocks=SimpleNamespace(
            vector_source_f=lambda values, repeat: object(),
            head=lambda item_size, count: object(),
            vector_sink_b=lambda: _Sink(),
        ),
        fec=SimpleNamespace(
            CC_TRUNCATED=object(),
            cc_decoder=SimpleNamespace(make=lambda *args: decoder),
            extended_decoder=lambda decoder_object, threading_mode: object(),
        ),
    )


def _reusable_runtime(flowgraph: _BlockingFlowgraph) -> SimpleNamespace:
    source = SimpleNamespace(set_data=lambda values: None, rewind=lambda: None)
    output_limit = SimpleNamespace(reset=lambda: None)
    sink = SimpleNamespace(reset=lambda: None, data=lambda: ())
    decoder = SimpleNamespace()
    return SimpleNamespace(
        gr=SimpleNamespace(sizeof_char=1, top_block=lambda name: flowgraph),
        blocks=SimpleNamespace(
            vector_source_f=lambda values, repeat: source,
            head=lambda item_size, count: output_limit,
            vector_sink_b=lambda: sink,
        ),
        fec=SimpleNamespace(
            CC_TRUNCATED=object(),
            cc_decoder=SimpleNamespace(make=lambda *args: decoder),
            extended_decoder=lambda decoder_object, threading_mode: object(),
        ),
    )


def test_native_decoder_timeout_stops_flowgraph(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 0, b"")
    flowgraph = _BlockingFlowgraph()
    monkeypatch.setattr(
        gnuradio_fec,
        "_load_gnuradio",
        lambda: _blocking_runtime(flowgraph),
    )

    with pytest.raises(GnuRadioFecTimeoutError, match="watchdog"):
        decode_frame_native(encode_frame(frame), timeout_s=0.01)

    assert flowgraph.stopped


def test_reusable_native_decoder_close_has_a_bounded_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flowgraph = _UnstoppableFlowgraph()
    monkeypatch.setattr(
        gnuradio_fec,
        "_load_gnuradio",
        lambda: _reusable_runtime(flowgraph),
    )
    monkeypatch.setattr(gnuradio_fec, "_STOP_GRACE_SECONDS", 0.01)
    decoder = NativeFrameDecoder(188)

    with pytest.raises(GnuRadioFecTimeoutError, match="close grace period"):
        decoder.close()

    assert flowgraph.stopped
    assert decoder.closed


def test_native_decoder_rejects_wrong_native_output_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 0, b"")
    flowgraph = _BlockingFlowgraph()
    flowgraph.wait = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr(
        gnuradio_fec,
        "_load_gnuradio",
        lambda: _blocking_runtime(flowgraph),
    )

    with pytest.raises(FrameDecodeError, match="expected exactly"):
        decode_frame_native(encode_frame(frame))


def test_native_decoder_propagates_scheduler_wait_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 0, b"")
    flowgraph = _BlockingFlowgraph()

    def fail_wait() -> None:
        raise RuntimeError("scheduler failed")

    flowgraph.wait = fail_wait  # type: ignore[method-assign]
    monkeypatch.setattr(
        gnuradio_fec,
        "_load_gnuradio",
        lambda: _blocking_runtime(flowgraph),
    )

    with pytest.raises(RuntimeError, match="scheduler failed"):
        decode_frame_native(encode_frame(frame))
