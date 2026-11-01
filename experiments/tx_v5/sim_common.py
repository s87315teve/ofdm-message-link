"""Shared helpers for the sim_*.py wire-v5 ablations (channel, bursts, curves).

Not a script.  Channel = zero padding, optional fixed 5-tap multipath (same
taps as experiments/rx_improvement/sim_sweep.py), 10 kHz CFO at 5 MS/s, complex
white noise with variance = mean burst sample power / 10^(SNR/10).
Single-threaded: set OMP/OPENBLAS/MKL_NUM_THREADS=1 before running.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import numpy as np  # noqa: E402

from ofdm_link.phy import (  # noqa: E402
    MCS, BurstConfig, BurstDecodeError, DecodedPsduBurst, Frame, FrameKind,
    decode_burst, encode_burst, encode_burst_v5, mcs_entry,
)
from ofdm_link.phy.sync_v5 import V5RxConfig  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results" / "sim"
TAPS = np.array([0.82 + 0.13j, 0, 0.21 - 0.17j, 0, 0.08j])
TAPS = TAPS / np.linalg.norm(TAPS)
FS = 5e6
CFO_HZ = 10e3
V5_BODY = 943  # 900 B datagram + 43 B aggregation overhead, as in the earlier sim_decode.py


def channel(tx, snr_db, rng, multipath, pad=600, cfo=CFO_HZ, noise_shape=None):
    """Return the received buffer.  ``noise_shape(n)`` may colour the unit noise."""
    x = np.concatenate((np.zeros(pad), tx, np.zeros(pad))).astype(np.complex128)
    if multipath:
        x = np.convolve(x, TAPS)[: x.size]
    x *= np.exp(2j * np.pi * cfo / FS * np.arange(x.size))
    var = np.mean(np.abs(tx) ** 2) / 10 ** (snr_db / 10)
    n = rng.standard_normal(x.size) + 1j * rng.standard_normal(x.size)
    if noise_shape is not None:
        n = noise_shape(n)
    return (x + np.sqrt(var / 2) * n).astype(np.complex64)


def stage(error) -> str:
    t = str(error)
    if "preamble" in t or "acquisition" in t:
        return "acq"
    if "header" in t or t.startswith("signaled"):
        return "hdr"
    return "pay"


def v5_burst(rng, mcs, body=V5_BODY, seed=None):
    payload = rng.integers(0, 256, body, dtype=np.uint8).tobytes()
    s = int(rng.integers(1, 128)) if seed is None else seed
    return payload, np.asarray(encode_burst_v5(payload, mcs_entry(mcs), scrambler_seed=s).samples)


def legacy_burst(rng, mcs=MCS.QPSK, seq=1, payload_len=900):
    payload = rng.integers(0, 256, payload_len, dtype=np.uint8).tobytes()
    f = Frame(1, FrameKind.DATA, mcs, seq & 0xFFFF, payload)
    return f, np.asarray(encode_burst(f, wire_version=2).samples)


def decode_outcome(rx, cfg, expect_body=None, expect_frame=None):
    """Return 'ok' or the failing stage ('acq','hdr','pay')."""
    try:
        d = decode_burst(rx, cfg)
    except BurstDecodeError as e:
        return stage(e)
    except Exception:  # noqa: BLE001 - count anything else as payload loss
        return "pay"
    if isinstance(d, DecodedPsduBurst):
        return "ok" if (d.fcs_ok and (expect_body is None or bytes(d.body) == expect_body)) else "pay"
    return "ok" if (expect_frame is None or d.frame == expect_frame) else "pay"


def rx_cfg(**v5):
    return BurstConfig(v5=V5RxConfig(**v5))


def snr_at(snrs, loss, target=0.01, n=150):
    """SNR where loss crosses ``target`` (interpolating log10 loss, floored at 0.5/n).

    Returns None if never reached; the lowest grid SNR with a '<' prefix is handled by caller.
    """
    floor = 0.5 / n
    ls = [math.log10(max(v, floor)) for v in loss]
    t = math.log10(target)
    for i, v in enumerate(ls):
        if v <= t:
            if i == 0:
                return float(snrs[0])
            a, b = ls[i - 1], v
            return float(snrs[i - 1] + (snrs[i] - snrs[i - 1]) * (a - t) / (a - b))
    return None


def save(name, obj):
    RESULTS.mkdir(parents=True, exist_ok=True)
    with open(RESULTS / name, "w") as h:
        json.dump(obj, h, indent=1)


def sweep_curve(make, cfg, start, mp, trials=300, topup=1000, step=0.5, max_span=12.0,
                seed=0, label="", stop_zero=2):
    """Loss vs SNR for ``make(rng) -> (tx, expect_body, expect_frame)`` decoded with ``cfg``.

    Starts at ``start`` dB, steps up by ``step`` until ``stop_zero`` consecutive points have
    zero losses.  Points with 0 < loss <= 5% get topped up to ``topup`` trials.  The noise
    stream depends only on (seed, snr, multipath) so variants with the same ``make`` are
    paired trial by trial.  Returns rows with per-stage loss fractions.
    """
    rows, zeros, snr = [], 0, start
    while snr <= start + max_span and zeros < stop_zero:
        rng = np.random.default_rng(seed * 100003 + int(round(snr * 100)) * 31 + 7 * mp)
        counts = {"ok": 0, "acq": 0, "hdr": 0, "pay": 0}
        n = 0
        for target in (trials, topup):
            while n < target:
                tx, body, frame = make(rng)
                rx = channel(tx, snr, rng, mp)
                counts[decode_outcome(rx, cfg, body, frame)] += 1
                n += 1
            loss = 1 - counts["ok"] / n
            if not (0 < loss <= 0.05):
                break
        row = {"snr": round(snr, 2), "n": n, "loss": 1 - counts["ok"] / n,
               **{k: counts[k] / n for k in ("acq", "hdr", "pay")}}
        rows.append(row)
        print(label, "mp" if mp else "awgn", row, flush=True)
        zeros = zeros + 1 if row["loss"] == 0 else 0
        snr += step
    return rows


def v5_maker(mcs, body=V5_BODY):
    def make(rng):
        p, tx = v5_burst(rng, mcs, body)
        return tx, p, None
    return make


def legacy_maker(mcs):
    def make(rng):
        f, tx = legacy_burst(rng, mcs, seq=int(rng.integers(0, 65535)))
        return tx, None, f
    return make
