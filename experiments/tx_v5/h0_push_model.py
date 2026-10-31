"""H0: PlutoSampleSink push-time model t_push(N) = a + b*N (TX only).

Pushes random OFDM-like complex bursts of N samples back-to-back through
PlutoSampleSink.send (which scales to peak and pushes exactly N samples; the
guard zeros are part of N, the sink adds none) for --seconds per size, then
fits a line to the mean push time.  Transmits RF at the given TX gain.

    PYTHONPATH=src python experiments/tx_v5/h0_push_model.py \
        --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF'
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from ofdm_link.phy import MCS
from ofdm_message_link import pluto
from ofdm_message_link.link import MessageTransmitter, build_phy_profile, load_link_config
from ofdm_message_link.options import DEFAULT_CONFIG, DEFAULT_OVERLAY, require_rf_capability

OUT = Path(__file__).parent / "results" / "hw_char" / "h0_push_model.json"
SIZES = [1000, 2500, 5000, 10000, 20000, 50000, 100000]
USED = {"v5_5k": 5000, "v5_10k": 10000, "v5_25k": 25000, "v5_50k": 50000}


def loadavg() -> float:
    return float(open("/proc/loadavg").read().split()[0])


def ofdm_like(n: int, rng) -> np.ndarray:
    sym = 80
    x = np.zeros(n, np.complex64)
    for i in range(0, n - sym + 1, sym):
        f = (rng.choice([-1, 1], 64) + 1j * rng.choice([-1, 1], 64)) / np.sqrt(2)
        t = np.fft.ifft(f) * 8
        x[i : i + sym] = np.concatenate([t[-16:], t])
    return x


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--tx-gain", type=float, default=-10.0)
    p.add_argument("--tx-serial", default="104473541196000419000c00f2ff1e6e77")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--overlay", default=DEFAULT_OVERLAY)
    p.add_argument("--enable-rf", action="store_true")
    p.add_argument("--acknowledgement")
    a = p.parse_args()
    a.transport = "pluto"
    token = require_rf_capability(a)
    if a.tx_gain > 0:
        raise SystemExit("TX gain must be <= 0")
    dev = {d.serial: d for d in pluto.discover()}[a.tx_serial]
    cfg = load_link_config(a.config, [a.overlay])
    prof = build_phy_profile(cfg, mcs=MCS.QPSK)
    legacy = {}
    for name in ("QPSK", "QAM16"):
        pr = build_phy_profile(cfg, mcs=MCS[name])
        tx = MessageTransmitter(pr)
        (b,) = tx.encode_message(bytes(900))
        legacy[f"legacy_{name}_900B"] = int(len(b.samples))
    rate = int(prof.sample_rate)
    sink = pluto.PlutoSampleSink(
        pluto.PlutoSettings(dev.address, dev.serial, rate, float(prof.center_frequency), a.tx_gain),
        token,
    )
    sink.start()
    rng = np.random.default_rng(1)
    rows = []
    try:
        for n in SIZES:
            x = ofdm_like(n, rng)
            load = loadavg()
            sink.send(x)  # warm-up / buffer sizing
            t_end = time.perf_counter() + a.seconds
            times = []
            while time.perf_counter() < t_end:
                t0 = time.perf_counter()
                sink.send(x)
                times.append(time.perf_counter() - t0)
            t = np.array(times)
            row = dict(N=n, bursts=len(t), duration_s=float(t.sum()), loadavg1=load,
                       bursts_per_s=len(t) / float(t.sum()), samples_per_s=n * len(t) / float(t.sum()),
                       mean_ms=float(t.mean() * 1e3), median_ms=float(np.median(t) * 1e3),
                       p99_ms=float(np.percentile(t, 99) * 1e3), max_ms=float(t.max() * 1e3),
                       airtime_ms=n / rate * 1e3)
            rows.append(row)
            print(json.dumps(row), flush=True)
    finally:
        sink.stop()
    N = np.array([r["N"] for r in rows], float)
    T = np.array([r["mean_ms"] for r in rows])
    A = np.vstack([np.ones_like(N), N]).T
    (ao, bo), *_ = np.linalg.lstsq(A, T, rcond=None)
    res = T - (ao + bo * N)
    r2 = 1 - (res**2).sum() / ((T - T.mean()) ** 2).sum()
    fit = dict(a_ms=float(ao), b_us_per_sample=float(bo * 1e3), r2=float(r2),
               residuals_ms=[float(r) for r in res])
    sizes = {**legacy, **USED}
    rates = {k: dict(N=v, t_push_ms=float(ao + bo * v), max_bursts_per_s=float(1e3 / (ao + bo * v)),
                     airtime_ms=v / rate * 1e3, max_duty_cycle=float(v / rate / ((ao + bo * v) / 1e3)))
             for k, v in sizes.items()}
    OUT.write_text(json.dumps(dict(sample_rate=rate, tx_gain_db=a.tx_gain, fit=fit, table=rows,
                                   predicted_rates=rates), indent=1))
    print(json.dumps(fit), json.dumps(rates, indent=1))


if __name__ == "__main__":
    main()
