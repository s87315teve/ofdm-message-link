"""The M0/M1 duplex scripts' analysis and decision rule, without radios."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

EXPERIMENTS = Path(__file__).resolve().parents[2] / "experiments" / "duplex_fdd"


@pytest.fixture(scope="module")
def si_scan():
    sys.path.insert(0, str(EXPERIMENTS))
    try:
        yield importlib.import_module("si_scan")
    finally:
        sys.path.remove(str(EXPERIMENTS))


def _noise(n, scale=0.003, seed=1):
    rng = np.random.default_rng(seed)
    return ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * scale).astype(np.complex64)


def test_noise_floor_finds_a_tone_in_a_guard_bin(si_scan):
    samples = _noise(64 * 4000)
    n = np.arange(samples.size)
    samples = samples + (0.01 * np.exp(2j * np.pi * 30 * n / 64)).astype(np.complex64)
    floor = si_scan.noise_floor(samples, mask_bursts=False)
    assert floor["spur_bin"] == 30
    assert floor["spur_guard_db"] > 10.0
    # White noise of variance 2 * 0.003**2 per sample is about -47.4 dBFS per bin.
    assert floor["p_data_dbfs"] == pytest.approx(10 * np.log10(2 * 0.003**2), abs=0.3)


def test_masking_ignores_peer_bursts_in_the_floor(si_scan):
    samples = _noise(64 * 8000)
    samples[: 64 * 2000] *= 30  # a quarter of the blocks carry a strong burst
    plain = si_scan.noise_floor(samples, mask_bursts=False)["p_data_dbfs"]
    masked = si_scan.noise_floor(samples, mask_bursts=True)["p_data_dbfs"]
    assert masked == pytest.approx(10 * np.log10(2 * 0.003**2), abs=0.3)
    assert plain > masked + 10


def test_saturation_counts_clipped_samples(si_scan):
    samples = np.full(1000, 0.1 + 0.1j, dtype=np.complex64)
    samples[:10] = 0.95 + 0j
    result = si_scan.saturation(samples)
    assert result["clip_fraction"] == pytest.approx(0.01)
    assert result["max_abs_lsb"] == round(0.95 * 2048)


def test_burst_recovery_measures_the_tail(si_scan):
    samples = _noise(200_000, scale=0.001)
    for start in range(10_000, 190_000, 40_000):
        samples[start : start + 10_000] += 0.3
    result = si_scan.burst_recovery(samples)
    assert result["bursts"] == 5
    assert result["recovery_samples_p90"] < 64


def _phase(
    combo,
    phase,
    *,
    p_data=-60.0,
    snr=None,
    loss=None,
    clip=0.0,
    ovf=0.0,
    dut="A",
    spacing=40e6,
    gain=15.0,
    bw=5e6,
):
    record = {
        "kind": "phase",
        "combo_id": combo,
        "phase": phase,
        "dut": dut,
        "spacing_hz": spacing,
        "rx_gain_db": gain,
        "rx_bw_hz": bw,
        "f_rx_hz": 2.46e9,
        "noise": {"p_data_dbfs": p_data, "occupancy_1ms": 0.1},
        "saturation": {"clip_fraction": clip, "max_abs_lsb": 10},
        "overflows_per_min": ovf,
        "rssi_db": 90.0,
    }
    if snr is not None:
        record["peer_rx"] = {"snr_median_db": snr, "loss": loss}
    return record


def _combo(dut, spacing, *, dn=0.2, dsnr=0.3, dloss=0.0, gain=15.0, bw=5e6):
    combo = f"{dut}_{spacing}_{gain}_{bw}"
    common = {"dut": dut, "spacing": spacing, "gain": gain, "bw": bw}
    records = [_phase(combo, "a", **common)]
    records.append(_phase(combo, "c-mid", snr=15.0, loss=0.0, **common))
    records.append(_phase(combo, "c-edge", snr=10.0, loss=0.002, **common))
    for index, level in enumerate((-20, -10, -5, 0), start=1):
        records.append(_phase(combo, f"b{index}", p_data=-60 + dn * (level + 25) / 20, **common))
    records.append(_phase(combo, "d-mid-m5", snr=15.0 - dsnr, loss=0.0, **common))
    records.append(_phase(combo, "d-edge-m5", snr=10.0 - dsnr, loss=0.002 + dloss / 100, **common))
    return records


def test_decision_step1_when_40mhz_passes_on_both_duts(si_scan):
    records = _combo("A", 40e6) + _combo("B", 40e6)
    result = si_scan.decide(records)
    assert result["decision"]["step"] == 1
    assert result["decision"]["frequencies_mhz"] == [2420, 2460]


def test_one_failing_dut_moves_the_decision_to_70mhz(si_scan):
    records = (
        _combo("A", 40e6, dsnr=4.0) + _combo("B", 40e6) + _combo("A", 70e6) + _combo("B", 70e6)
    )
    result = si_scan.decide(records)
    assert result["decision"]["step"] == 3
    assert result["decision"]["frequencies_mhz"] == [2405, 2475]


def test_marginal_then_lbt(si_scan):
    marginal = _combo("A", 40e6, dsnr=2.0) + _combo("B", 40e6, dsnr=2.5)
    assert si_scan.decide(marginal)["decision"]["step"] == 4
    failing = _combo("A", 40e6, dsnr=5.0) + _combo("B", 40e6, dsnr=5.0)
    decision = si_scan.decide(failing)["decision"]
    assert decision["step"] == 5 and decision["mode"] == "lbt"


def test_overflow_everywhere_is_blamed_on_usb(si_scan):
    records = _combo("A", 40e6) + _combo("B", 40e6)
    for record in records:
        if record["phase"] == "b3":
            record["overflows_per_min"] = 5.0
    decision = si_scan.decide(records)["decision"]
    assert decision["step"] == 1 and decision["overflow_fails_everywhere"]


def test_frequencies_put_node_a_on_the_lower_tx(si_scan):
    assert si_scan.frequencies(40e6, True) == (2.46e9, 2.42e9)
    assert si_scan.frequencies(70e6, False) == (2.405e9, 2.475e9)
