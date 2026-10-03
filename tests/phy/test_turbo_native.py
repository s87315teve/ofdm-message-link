from __future__ import annotations

import builtins
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from ofdm_link.phy import (
    CURRENT_PROTOCOL_VERSION,
    MAX_PAYLOAD_LENGTH,
    MCS,
    Frame,
    FrameDecodeError,
    FrameDecodeResult,
    FrameIntegrityError,
    FrameKind,
    NativeTurboFecAdapter,
    TurboNativeUnavailableError,
    information_frame_bit_count,
    make_convolutional_frame_codec,
    make_dual_version_frame_decoder,
    make_turbo_frame_codec,
    turbo_coded_frame_bit_count,
)
from ofdm_link.phy.fec import FecError
from ofdm_link.phy.turbo import turbo_encode_oracle


@pytest.mark.parametrize(
    "information_bit_count",
    [0, 1, 39, 40, 41, 1_024, 6_143, 6_144, 6_145, 10_240, 524_368],
)
def test_native_encoder_is_bit_exact_with_portable_oracle(
    information_bit_count: int,
) -> None:
    information = np.random.default_rng(31 + information_bit_count).integers(
        0,
        2,
        size=information_bit_count,
        dtype=np.uint8,
    )
    codec = NativeTurboFecAdapter(max_information_bits=max(20_000, information_bit_count))

    actual = codec.encode(information)

    np.testing.assert_array_equal(actual, turbo_encode_oracle(information))
    assert actual.size == codec.profile.coded_bit_count(information_bit_count)
    assert not actual.flags.writeable


@pytest.mark.parametrize("payload_size", [0, 996, 1_280, 4_096, MAX_PAYLOAD_LENGTH])
def test_native_encoder_preserves_frame_level_payload_bounds(payload_size: int) -> None:
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QPSK,
        7,
        bytes(payload_size),
    )
    codec = make_turbo_frame_codec()

    coded = codec.encode(frame)

    assert coded.size == turbo_coded_frame_bit_count(frame)
    assert information_frame_bit_count(frame) == (7 + payload_size + 4) * 8


@pytest.mark.parametrize("iterations", [2, 4, 6, 8])
def test_native_max_log_map_ideal_round_trip_and_early_stop(iterations: int) -> None:
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QAM16,
        0xA15E,
        b"native Max-Log-MAP",
    )
    codec = make_turbo_frame_codec(max_iterations=iterations)
    coded = codec.encode(frame)
    llrs = np.where(coded == 0, 12.0, -12.0).astype(np.float32)

    recovered = codec.decode(
        llrs,
        information_bit_count=information_frame_bit_count(frame),
    )

    assert recovered == frame
    assert codec.last_decode_report is not None
    assert codec.last_decode_report.configured_max_iterations == iterations
    assert codec.last_decode_report.actual_iterations_per_block == (1,)
    assert codec.last_decode_report.early_stop_reasons == ("outer_crc32",)
    assert codec.last_decode_report.outer_crc_ok is True


def test_native_max_log_map_corrects_sparse_adverse_observations() -> None:
    frame = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QPSK,
        11,
        b"correct sparse corruption",
    )
    codec = make_turbo_frame_codec(max_iterations=8)
    coded = codec.encode(frame)
    llrs = np.where(coded == 0, 9.0, -9.0).astype(np.float32)
    llrs[[19, 97, 181, 263]] *= -0.35

    assert codec.decode(
        llrs,
        information_bit_count=information_frame_bit_count(frame),
    ) == frame


def test_native_segmented_decode_reports_block_crc_and_filler() -> None:
    information = np.random.default_rng(914).integers(0, 2, size=6_145, dtype=np.uint8)
    codec = NativeTurboFecAdapter(max_information_bits=10_000, max_iterations=4)
    coded = codec.encode(information)
    llrs = np.where(coded == 0, 10.0, -10.0).astype(np.float32)

    decoded = codec.decode(llrs, information_bit_count=information.size)

    np.testing.assert_array_equal(decoded, information)
    assert codec.last_decode_report is not None
    assert codec.last_decode_report.block_count == 2
    assert codec.last_decode_report.filler_bit_count == 15
    assert codec.last_decode_report.code_block_crc_ok == (True, True)
    assert codec.last_decode_report.filler_ok


def test_native_uncorrectable_frame_is_rejected_by_outer_crc() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, 3, b"reject")
    codec = make_turbo_frame_codec(max_iterations=2)
    llrs = np.zeros(turbo_coded_frame_bit_count(frame), dtype=np.float32)

    with pytest.raises(FrameIntegrityError, match="CRC-32"):
        codec.decode(llrs, information_bit_count=information_frame_bit_count(frame))

    assert codec.last_decode_report is not None
    assert codec.last_decode_report.outer_crc_ok is False
    assert codec.last_decode_report.early_stop_reasons == ("max_iterations",)


def test_atomic_result_and_failure_context_never_reuse_a_previous_report() -> None:
    decoder = make_dual_version_frame_decoder()
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QPSK, 17, b"atomic")
    coded = decoder.v2.encode(frame)
    llrs = np.where(coded == 0, 12.0, -12.0).astype(np.float32)

    success = decoder.decode_result(
        llrs,
        wire_version=2,
        information_bit_count=information_frame_bit_count(frame),
    )

    assert isinstance(success, FrameDecodeResult)
    assert success.frame == frame
    assert success.wire_version == 2
    assert success.fec_profile.decision_mode == "turbo"
    assert success.fec_report is not None
    assert success.fec_report.outer_crc_ok is True

    with pytest.raises(FrameDecodeError) as invalid:
        decoder.decode_result(
            llrs[:-1],
            wire_version=2,
            information_bit_count=information_frame_bit_count(frame),
        )

    assert invalid.value.failure is not None
    assert invalid.value.failure.wire_version == 2
    assert invalid.value.failure.fec_profile == success.fec_profile
    assert invalid.value.failure.fec_report is None

    with pytest.raises(FrameIntegrityError) as rejected:
        decoder.decode_result(
            np.zeros_like(llrs),
            wire_version=2,
            information_bit_count=information_frame_bit_count(frame),
        )

    assert rejected.value.failure is not None
    assert rejected.value.failure.fec_report is not None
    assert rejected.value.failure.fec_report.outer_crc_ok is False


def test_dual_version_atomic_results_support_bounded_concurrent_calls() -> None:
    decoder = make_dual_version_frame_decoder()
    v1_frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 21, b"")
    v2_short = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, MCS.QAM16, 22, b"short")
    v2_segmented = Frame(
        CURRENT_PROTOCOL_VERSION,
        FrameKind.DATA,
        MCS.QPSK,
        23,
        bytes(996),
    )
    v1_coded = make_convolutional_frame_codec().encode(v1_frame)
    v2_short_coded = decoder.v2.encode(v2_short)
    v2_segmented_coded = decoder.v2.encode(v2_segmented)
    cases = (
        (v1_frame, 1, v1_coded, None, 0),
        (
            v2_short,
            2,
            np.where(v2_short_coded == 0, 12.0, -12.0).astype(np.float32),
            information_frame_bit_count(v2_short),
            1,
        ),
        (
            v2_segmented,
            2,
            np.where(v2_segmented_coded == 0, 12.0, -12.0).astype(np.float32),
            information_frame_bit_count(v2_segmented),
            2,
        ),
    )

    def decode(case_index: int) -> tuple[Frame, int, int]:
        frame, wire_version, observations, information_count, expected_blocks = cases[
            case_index % len(cases)
        ]
        result = decoder.decode_result(
            observations,
            wire_version=wire_version,
            information_bit_count=information_count,
        )
        block_count = 0 if result.fec_report is None else result.fec_report.block_count
        return result.frame, result.wire_version, block_count

    with ThreadPoolExecutor(max_workers=4) as executor:
        actual = list(executor.map(decode, range(24)))

    expected = [
        (cases[index % len(cases)][0], cases[index % len(cases)][1], cases[index % len(cases)][4])
        for index in range(24)
    ]
    assert actual == expected


@pytest.mark.parametrize(
    ("observations", "information_bit_count", "message"),
    [
        (np.zeros(131, dtype=np.float32), 40, "expected 132"),
        (np.full(132, np.nan, dtype=np.float32), 40, "finite"),
        (np.full(132, np.inf, dtype=np.float32), 40, "finite"),
        (np.zeros((1, 132), dtype=np.float32), 40, "one-dimensional"),
    ],
)
def test_native_adapter_rejects_invalid_observations(
    observations: np.ndarray,
    information_bit_count: int,
    message: str,
) -> None:
    codec = NativeTurboFecAdapter(max_information_bits=1_000)

    with pytest.raises(FecError, match=message):
        codec.decode(observations, information_bit_count=information_bit_count)


def test_native_adapter_requires_information_length_and_valid_iterations() -> None:
    codec = NativeTurboFecAdapter(max_information_bits=128)

    with pytest.raises(FecError, match="requires information_bit_count"):
        codec.decode(np.zeros(132, dtype=np.float32))
    with pytest.raises(FecError, match="explicit information_bit_count"):
        codec.profile.information_bit_count(132)
    with pytest.raises(ValueError, match="2, 4, 6, or 8"):
        NativeTurboFecAdapter(max_information_bits=128, max_iterations=3)


def test_phy_import_is_headless_and_optional_stack_safe() -> None:
    environment = os.environ.copy()
    environment.pop("DISPLAY", None)
    command = (
        "import sys; import ofdm_link.phy; "
        "assert 'torch' not in sys.modules; "
        "assert 'sionna' not in sys.modules; "
        "assert not any(name.startswith(('PyQt', 'PySide')) for name in sys.modules)"
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


def test_turbo_frame_codec_rejects_wrong_explicit_information_length() -> None:
    frame = Frame(CURRENT_PROTOCOL_VERSION, FrameKind.ACK, MCS.QPSK, 1, b"")
    codec = make_turbo_frame_codec()
    coded = codec.encode(frame)
    llrs = np.where(coded == 0, 8.0, -8.0).astype(np.float32)

    with pytest.raises(FrameDecodeError):
        codec.decode(
            llrs,
            information_bit_count=information_frame_bit_count(frame) + 8,
        )


def test_public_adapter_reports_an_actionable_remedy_for_unbuilt_native_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unbuilt extension must name the rebuild, not just the missing module.

    The native core backs the default wire-v2 profile, so this is the first
    failure a stale installation hits.
    """

    real_import = builtins.__import__

    def import_without_native_core(
        name: str,
        globals_: object = None,
        locals_: object = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if "_turbo_native" in fromlist:
            raise ModuleNotFoundError("forced unbuilt extension")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_native_core)
    adapter = NativeTurboFecAdapter(max_information_bits=64)

    with pytest.raises(FecError, match="pip install -e") as encode_error:
        adapter.encode(np.zeros(64, dtype=np.uint8))

    assert isinstance(encode_error.value.__cause__, TurboNativeUnavailableError)
