"""The float32 Turbo kernel (``decode``) against the double-precision reference.

Same LLRs into both kernels: hard decisions and CRC outcomes must agree on
nearly every block, iteration counts within +-1 on every block, and the
block error rate within 0.1 dB over the waterfall (report_phase2 section 5.1).
"""

from __future__ import annotations

import zlib

import numpy as np
import pytest

from ofdm_link.phy import native_backend
from ofdm_link.phy import turbo as turbo_module
from ofdm_link.phy.turbo import _qpp_for_plan, turbo_segmentation_plan

native = pytest.importorskip("ofdm_link.phy._turbo_native")


def _frame(rng, payload_bytes):
    body = rng.integers(0, 256, payload_bytes, dtype=np.uint8).tobytes()
    crc = zlib.crc32(body).to_bytes(4, "big")
    return np.unpackbits(np.frombuffer(body + crc, np.uint8))


def _encode(bits):
    plan = turbo_segmentation_plan(bits.size)
    coded = np.frombuffer(
        native.encode(bits.tobytes(), plan.block_sizes, plan.filler_bit_count,
                      _qpp_for_plan(plan)), np.uint8)
    return plan, coded


def _llrs(coded, noise, ebn0_db, rate=1 / 3):
    sigma2 = 1.0 / (2.0 * rate * 10 ** (ebn0_db / 10.0))
    received = (1.0 - 2.0 * coded) + np.sqrt(sigma2) * noise[: coded.size]
    return np.clip(2.0 * received / sigma2, -64, 64).astype(np.float32)


def _decode(kernel, llrs, bits, plan, iterations=8, *extra):
    decoded, report = kernel(llrs.tobytes(), bits.size, plan.block_sizes,
                             plan.filler_bit_count, _qpp_for_plan(plan), iterations, *extra)
    return np.frombuffer(decoded, np.uint8), report


def _block_ok(report, decoded, bits):
    outcomes = report["code_block_crc_ok"]
    if outcomes and outcomes[0] is not None:
        return all(outcomes)
    return bool(report["outer_crc_ok"]) and np.array_equal(decoded, bits)


@pytest.mark.parametrize("payload_bytes", [40, 300, 1200])
def test_high_snr_frames_decode_identically(payload_bytes):
    rng = np.random.default_rng(payload_bytes)
    for _ in range(5):
        bits = _frame(rng, payload_bytes)
        plan, coded = _encode(bits)
        llrs = _llrs(coded, rng.standard_normal(coded.size), 3.0)
        fast, fast_report = _decode(native.decode, llrs, bits, plan)
        ref, ref_report = _decode(native.decode_reference, llrs, bits, plan)
        assert np.array_equal(fast, bits) and np.array_equal(ref, bits)
        assert fast_report == ref_report


def test_segmented_frames_and_filler_match():
    rng = np.random.default_rng(9)
    bits = _frame(rng, 2000)  # > 6144 bits: segmented, CRC-24B per block, filler
    plan, coded = _encode(bits)
    assert plan.segmented and plan.filler_bit_count >= 0
    for ebn0 in (0.2, 0.6, 1.5):
        llrs = _llrs(coded, rng.standard_normal(coded.size), ebn0)
        fast, fast_report = _decode(native.decode, llrs, bits, plan)
        ref, ref_report = _decode(native.decode_reference, llrs, bits, plan)
        assert fast_report["code_block_crc_ok"] == ref_report["code_block_crc_ok"]
        for a, b in zip(fast_report["actual_iterations_per_block"],
                        ref_report["actual_iterations_per_block"], strict=True):
            assert abs(a - b) <= 1


def test_waterfall_bler_and_iterations_match_within_tolerance():
    """BLER(fast, x) lies between BLER(ref, x - 0.1 dB) and BLER(ref, x + 0.1 dB)."""

    rng = np.random.default_rng(2026)
    blocks = 60
    frames = []
    for _ in range(blocks):
        bits = _frame(rng, 100)  # 832 bits: one block
        plan, coded = _encode(bits)
        frames.append((bits, plan, coded, rng.standard_normal(coded.size)))
    grid = (0.0, 0.3, 0.6, 0.9)

    def bler(kernel, ebn0):
        errors, iterations = 0, []
        for bits, plan, coded, noise in frames:
            decoded, report = _decode(kernel, _llrs(coded, noise, ebn0), bits, plan)
            errors += not _block_ok(report, decoded, bits)
            iterations.append(report["actual_iterations_per_block"][0])
        return errors, iterations

    disagreements = 0
    for ebn0 in grid:
        fast_errors, fast_iterations = bler(native.decode, ebn0)
        ref_errors, ref_iterations = bler(native.decode_reference, ebn0)
        lower, _ = bler(native.decode_reference, ebn0 + 0.1)
        upper, _ = bler(native.decode_reference, ebn0 - 0.1)
        assert lower - 1 <= fast_errors <= upper + 1, (ebn0, lower, fast_errors, upper)
        assert all(abs(a - b) <= 1 for a, b in zip(fast_iterations, ref_iterations, strict=True))
        disagreements += abs(fast_errors - ref_errors)
    assert disagreements <= 3


def test_python_backend_selects_the_reference_kernel(monkeypatch):
    calls = []
    fast, reference = native.decode, native.decode_reference

    def record(name, kernel):
        def wrapped(*arguments):
            calls.append(name)
            return kernel(*arguments)

        return wrapped

    monkeypatch.setattr(native, "decode", record("fast", fast))
    monkeypatch.setattr(native, "decode_reference", record("ref", reference))
    adapter = turbo_module.NativeTurboFecAdapter(max_information_bits=8192)
    rng = np.random.default_rng(1)
    bits = _frame(rng, 50)
    plan, coded = _encode(bits)
    llrs = _llrs(coded, rng.standard_normal(coded.size), 4.0)
    for choice, expected in (("python", "ref"), ("native", "fast")):
        monkeypatch.setenv(native_backend.env_name("turbo"), choice)
        calls.clear()
        result = adapter.decode_result(llrs, information_bit_count=bits.size)
        assert np.array_equal(result.information_bits, bits)
        assert calls[0] == expected


def test_give_up_is_opt_in_and_only_stops_failing_blocks():
    rng = np.random.default_rng(77)
    good = bad = abandoned_good = 0
    for _ in range(30):
        bits = _frame(rng, 100)
        plan, coded = _encode(bits)
        noise = rng.standard_normal(coded.size)
        for ebn0 in (-1.0, 1.5):
            llrs = _llrs(coded, noise, ebn0)
            plain, plain_report = _decode(native.decode, llrs, bits, plan)
            early, early_report = _decode(native.decode, llrs, bits, plan, 8, 4)
            assert "abandoned" not in plain_report["early_stop_reasons"]
            if _block_ok(plain_report, plain, bits):
                good += 1
                abandoned_good += early_report["early_stop_reasons"][0] == "abandoned"
            else:
                bad += 1
                assert early_report["actual_iterations_per_block"][0] <= 8
    assert good and bad
    assert abandoned_good <= 1
    with pytest.raises(ValueError):
        _decode(native.decode_reference, llrs, bits, plan, 8, 4)


def test_set_hopeless_block_give_up_validates():
    try:
        for bad in (1, 8, -1, True):
            with pytest.raises(ValueError):
                turbo_module.set_hopeless_block_give_up(bad)
        turbo_module.set_hopeless_block_give_up(4)
        assert turbo_module.hopeless_block_give_up() == 4
    finally:
        turbo_module.set_hopeless_block_give_up(0)
