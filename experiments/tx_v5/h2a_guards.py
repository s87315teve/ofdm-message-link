"""H2a: zero-guard / transient characterization of a Pluto TX->RX burst link.

Builds a known burst (160-sample ZC preamble + 40 OFDM-like symbols of 80
samples = 3360 samples), pads it with (pre, post) zeros inside ONE push buffer
(PlutoSampleSink does not pad; the 256+256 default guards come from
MessageTransmitter, which is bypassed here), transmits 1000 bursts at 100/s per
guard configuration and records raw int16 RX IQ to the scratchpad.  Extra
"b2b" runs push without pacing to look at glue between consecutive pushes.
Analysis aligns each burst by cross-correlation, estimates CFO, fits a 17-tap
complex FIR on the burst middle and studies the residual at the start/end.

    PYTHONPATH=src python experiments/tx_v5/h2a_guards.py --phase all \
        --enable-rf --acknowledgement 'I acknowledge that this process will transmit RF'
    PYTHONPATH=src python experiments/tx_v5/h2a_guards.py --phase analyze   # raw already captured
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from ofdm_link.phy import MCS
from ofdm_message_link import pluto
from ofdm_message_link.link import build_phy_profile, load_link_config
from ofdm_message_link.options import DEFAULT_CONFIG, DEFAULT_OVERLAY, require_rf_capability

RAW = Path("/tmp/claude-1000/-home-steve-Desktop-steve-my-github-ofdm-message-link/"
           "7347b886-2f82-4a84-a215-d295fcf06e93/scratchpad/hw_char")
OUT = Path(__file__).parent / "results" / "hw_char" / "h2a_guards.json"
CONFIGS = [(0, 0), (0, 32), (16, 64), (32, 128), (256, 256)]
B2B_CONFIGS = [(0, 0), (16, 64), (256, 256)]
RX_FULL_SCALE = 2048.0
NSYM, SYM = 40, 80
PRE_LEN = 160
TAPS = list(range(-6, 11))


def make_burst() -> np.ndarray:
    rng = np.random.default_rng(7)
    n = np.arange(PRE_LEN)
    zc = np.exp(-1j * np.pi * 25 * n * (n + 1) / PRE_LEN)
    blocks = []
    for _ in range(NSYM):
        f = (rng.choice([-1, 1], 64) + 1j * rng.choice([-1, 1], 64)) / np.sqrt(2)
        t = np.fft.ifft(f) * 8
        blocks.append(np.concatenate([t[-16:], t]))
    ofdm = np.concatenate(blocks)
    return np.concatenate([1.5 * zc * np.sqrt(np.mean(np.abs(ofdm) ** 2)), ofdm]).astype(np.complex64)


def loadavg() -> float:
    return float(open("/proc/loadavg").read().split()[0])


class Recorder(threading.Thread):
    def __init__(self, source):
        super().__init__(daemon=True)
        self.source, self.stop, self.armed = source, threading.Event(), threading.Event()
        self.chunks: list[np.ndarray] = []

    def run(self):
        while not self.stop.is_set():
            c = self.source.recv(0.1)
            if c is not None and self.armed.is_set():
                self.chunks.append(c)

    def take(self):
        ch, self.chunks = self.chunks, []
        x = np.concatenate(ch).view(np.float32) * RX_FULL_SCALE
        return np.rint(x).astype(np.int16)


def capture(args) -> None:
    a = args
    a.transport = "pluto"
    token = require_rf_capability(a)
    if a.tx_gain > 0:
        raise SystemExit("TX gain must be <= 0")
    devs = {d.serial: d for d in pluto.discover()}
    cfg = load_link_config(a.config, [a.overlay])
    prof = build_phy_profile(cfg, mcs=MCS.QPSK)
    rate, fc = int(prof.sample_rate), float(prof.center_frequency)

    def st(serial, gain):
        return pluto.PlutoSettings(devs[serial].address, serial, rate, fc, gain)

    burst = make_burst()
    RAW.mkdir(parents=True, exist_ok=True)
    runs = [(c, "paced", 1000) for c in CONFIGS] + [(c, "b2b", 300) for c in B2B_CONFIGS]
    meta_all = {}
    for pre, post in [(0, 0)]:  # placeholder to keep structure simple
        pass
    for (pre, post), mode, count in runs:
        if mode == "b2b" and not a.b2b:
            continue
        for attempt in range(3):
            try:
                source = pluto.PlutoSampleSource(st(a.rx_serial, a.rx_gain))
                sink = pluto.PlutoSampleSink(st(a.tx_serial, a.tx_gain), token)
                source.start()
                rec = Recorder(source)
                rec.start()
                sink.start()
                break
            except Exception as e:  # noqa: BLE001
                print("open failed", attempt, e, flush=True)
                time.sleep(5)
                devs = {d.serial: d for d in pluto.discover()}
        else:
            print("giving up", pre, post, mode)
            continue
        load = loadavg()
        buf = np.concatenate([np.zeros(pre, np.complex64), burst, np.zeros(post, np.complex64)])
        try:
            time.sleep(0.5)
            ovf0 = source.snapshot()["rx_overflow_events"]
            rec.armed.set()
            time.sleep(0.2)
            t0 = time.monotonic()
            for i in range(count):
                if mode == "paced":
                    d = t0 + i / a.rate - time.monotonic()
                    if d > 0:
                        time.sleep(d)
                sink.send(buf)
            dur = time.monotonic() - t0
            time.sleep(0.4)
            rec.armed.clear()
            raw = rec.take()
            ovf = source.snapshot()["rx_overflow_events"] - ovf0
        finally:
            sink.stop()
            rec.stop.set()
            rec.join(2)
            source.stop()
        stem = f"{mode}_pre{pre}_post{post}"
        np.save(RAW / f"{stem}.npy", raw)
        meta = dict(pre=pre, post=post, mode=mode, count=count, buf_len=len(buf), send_s=dur,
                    rx_overflow_events=ovf, loadavg1=load, samples=int(raw.size // 2),
                    tx_gain_db=a.tx_gain, rx_gain_db=a.rx_gain, rate=rate)
        (RAW / f"{stem}.json").write_text(json.dumps(meta))
        print(json.dumps(meta), flush=True)
        time.sleep(1)


# ----------------------------------------------------------------- analysis
def detect(y: np.ndarray, burst: np.ndarray, min_dist: int):
    segs = [burst[k * 40:(k + 1) * 40] for k in range(4)]
    hs = [np.conj(s[::-1]).astype(np.complex64) for s in segs]
    L = 1 << 21
    mets = []
    for s in range(0, len(y), L):
        ch = y[s:s + L + 200]
        if len(ch) < 200:
            break
        m = None
        for k, h in enumerate(hs):
            c = fftconvolve(ch, h, mode="valid")  # c[i]: corr starting at i
            c = np.abs(c) ** 2
            part = c[40 * k:]
            n = len(c) - 120
            m = part[:n] if m is None else m[:n] + part[:n]
            m = m[:n]
        mets.append(m[:min(L, len(m))])
    m = np.concatenate(mets)
    thr = 30 * np.median(m)
    idx = np.flatnonzero(m > thr)
    peaks = []
    if idx.size:
        groups = np.split(idx, np.flatnonzero(np.diff(idx) > min_dist) + 1)
        for g in groups:
            peaks.append(int(g[np.argmax(m[g])]))
    return np.array(peaks), m, thr


def fit_burst(y, n0, burst, N):
    """Return (n0 refined, f, taps) or None."""
    best = None
    mid = slice(100, N - 100)
    x = burst
    for d in range(-3, 4):
        s = n0 + d
        if s < 20 or s + N + 300 > len(y):
            return None
        z = y[s:s + N] * np.conj(x)
        zz = z[mid]
        # lag-product weighted by |x|^2 via z itself
        L = 40
        r = np.sum(zz[L:] * np.conj(zz[:-L]))
        if best is None or abs(r) > best[0]:
            best = (abs(r), d, np.angle(r) / L)
    _, d, f = best
    s = n0 + d
    raw = y[s - 16:s + N + 300].astype(np.complex128)
    nn = np.arange(-16, N + 300)
    xp = np.zeros(N + 316 + 32, np.complex128)
    xp[16 + 16:16 + 16 + N] = x  # x index k at xp[k+32]
    A = np.stack([xp[nn - l + 32] for l in TAPS], 1)
    rows = np.arange(100, N - 100) + 16
    c = 0.0
    for it in range(6):
        seg = raw * np.exp(-1j * (f * nn + c * nn * nn))
        h, *_ = np.linalg.lstsq(A[rows], seg[rows], rcond=None)
        pred = A @ h
        zz = seg[rows] * np.conj(pred[rows])
        r = np.sum(zz[40:] * np.conj(zz[:-40]))
        f += np.angle(r) / 40
        k = 100
        zs = np.add.reduceat(zz[: len(zz) // k * k], np.arange(0, len(zz) // k * k, k))
        ph = np.unwrap(np.angle(zs))
        xs = (np.arange(len(zs)) + 0.5) * k + 100 - 16
        c2 = np.polyfit(xs, ph, 2)
        c += c2[0]
        f += c2[1] * 0.0
    seg = raw * np.exp(-1j * (f * nn + c * nn * nn))
    h, *_ = np.linalg.lstsq(A[rows], seg[rows], rcond=None)
    pred = A @ h
    return s, f, h, seg, pred


def analyze_run(raw_path: Path, meta: dict, burst: np.ndarray, floor_ref=None, idle=True):
    raw = np.load(raw_path, mmap_mode="r")
    y = np.asarray(raw).astype(np.float32).view(np.complex64).ravel()
    N = len(burst)
    peaks, m, thr = detect(y, burst, min_dist=1500)
    res = dict(detections=int(len(peaks)), expected=meta["count"])
    E, P, Pm, Y, PW, LS = [], [], [], [], [], []
    cfos = []
    for p in peaks:
        r = fit_burst(y, int(p), burst, N)
        if r is None:
            continue
        s, f, h, seg, pred = r
        # window: index 16 = burst start; range -16..N+300
        e = seg - pred
        E.append(np.abs(e) ** 2)
        Pm.append(np.abs(pred) ** 2)
        P.append(np.abs(y[s - 16:s + N + 300]) ** 2)
        Y.append(seg)
        PW.append(y[s + N + 64:s + N + 256].mean())
        LS.append(y[s + N - 1])
        cfos.append(f * meta["rate"] / (2 * np.pi))
    E, P, Pm = np.array(E), np.array(P), np.array(Pm)
    res["analysed"] = int(len(E))
    res["cfo_hz_median"] = float(np.median(cfos))
    res["cfo_hz_std"] = float(np.std(cfos))
    sig = float(np.mean(Pm[:, 16 + 200:16 + N - 200]))
    # (a) leading transient
    Em = E.mean(0)
    mid = Em[16 + 200:16 + N - 200]
    med = float(np.median(Em[16 + 100:16 + N - 100]))
    lead = 10 * np.log10(Em[16:16 + 64] / med)
    ok = np.flatnonzero(lead > 1.0)
    res["a_lead_excess_db_first16"] = [round(float(v), 2) for v in lead[:16]]
    res["a_lead_excess_db_first64"] = [float(v) for v in lead]
    res["a_lead_samples_to_within_1dB"] = int(ok[-1] + 1) if ok.size else 0
    res["a_pre_burst_err_db_rel_median_last8"] = [round(float(v), 2) for v in
                                                 10 * np.log10(Em[8:16] / med)]
    res["median_err_power"] = med
    res["signal_power"] = sig
    res["evm_median_db"] = float(10 * np.log10(med / sig))
    # (d) last 80
    def evm_win(a, b):
        return float(10 * np.log10(Em[16 + a:16 + b].mean() / sig))
    wins = [evm_win(k, k + 80) for k in range(160, N - 160, 80)]
    res["d_evm_last80_db"] = evm_win(N - 80, N)
    res["d_evm_first80_db"] = evm_win(0, 80)
    res["d_evm_median_window_db"] = float(np.median(wins))
    res["d_last80_minus_median_db"] = res["d_evm_last80_db"] - res["d_evm_median_window_db"]
    res["d_first80_minus_median_db"] = res["d_evm_first80_db"] - res["d_evm_median_window_db"]
    # (b) trailing: raw power after nominal end vs idle floor
    Pt = P.mean(0)
    post = slice(16 + N, 16 + N + 300)
    if idle and floor_ref is None:
        # idle floor from region 5k..20k samples after end, ignoring bursts (paced run)
        fl = []
        for p in peaks:
            a0 = int(p) + N + 5000
            if a0 + 15000 < len(y):
                fl.append(np.abs(y[a0:a0 + 15000]) ** 2)
        floor = float(np.mean(fl))
    else:
        floor = floor_ref
    res["floor_power"] = floor
    tail_db = 10 * np.log10(Pt[post] / floor)
    res["b_tail_over_floor_db_first32"] = [round(float(v), 2) for v in tail_db[:32]]
    ok = np.flatnonzero(tail_db > 1.0)
    res["b_tail_samples_to_within_1dB_of_floor"] = int(ok[-1] + 1) if ok.size else 0
    res["b_tail_mean_over_floor_db_samples_64_256"] = float(10 * np.log10(Pt[16 + N + 64:16 + N + 256].mean() / floor))
    # excess residual tail (error power beyond model, which predicts expected filter ring)
    et = 10 * np.log10(Em[post] / floor)
    res["b_resid_tail_over_floor_db_first32"] = [round(float(v), 2) for v in et[:32]]
    ok = np.flatnonzero(et > 1.0)
    res["b_resid_tail_samples_to_within_1dB"] = int(ok[-1] + 1) if ok.size else 0
    res["b_model_tail_power_over_floor_db_first32"] = [round(float(v), 2) for v in
                                                     10 * np.log10(Pm.mean(0)[post][:32] / floor)]
    # (c) DC / hold
    Ymean = np.mean(np.array(Y), axis=0)  # coherent after CFO derotation
    last = Ymean[16 + N - 1]
    pw = np.mean(PW)  # raw (not derotated) mean: a DC level would survive
    res["c_post_window_mean_re_im"] = [float(pw.real), float(pw.imag)]
    res["c_post_window_mean_abs"] = float(abs(pw))
    res["c_noise_se_of_mean"] = float(np.sqrt(floor / (len(E) * 192)))
    res["c_last_burst_sample_abs"] = float(abs(last))
    res["c_post_mean_over_last_sample"] = float(abs(pw) / abs(last))
    if idle:
        ids = [y[int(p) + N + 5000:int(p) + N + 20000].mean() for p in peaks
               if int(p) + N + 20000 < len(y)]
        res["c_idle_dc_mean_re_im"] = [float(np.mean(ids).real), float(np.mean(ids).imag)]
    # b2b spacing
    if len(peaks) > 1:
        sp = np.diff(peaks)
        res["spacing_samples_median"] = float(np.median(sp))
        res["spacing_samples_p5_p95"] = [float(np.percentile(sp, 5)), float(np.percentile(sp, 95))]
        res["buf_len"] = meta["buf_len"]
        res["gap_samples_median_minus_buflen"] = float(np.median(sp) - meta["buf_len"])
    return res


def analyze() -> None:
    burst = make_burst()
    # use the same scaling the sink applies (peak 0.7 of full scale 32767) is irrelevant: gain is fitted
    results = {}
    floors = {}
    for pre, post in CONFIGS + [("b2b", c) for c in B2B_CONFIGS]:
        pass
    for mode, cfgs in (("paced", CONFIGS), ("b2b", B2B_CONFIGS)):
        for pre, post in cfgs:
            stem = f"{mode}_pre{pre}_post{post}"
            mp = RAW / f"{stem}.json"
            if not mp.exists():
                continue
            meta = json.loads(mp.read_text())
            ref = floors.get((pre, post)) if mode == "b2b" else None
            r = analyze_run(RAW / f"{stem}.npy", meta, burst, floor_ref=ref, idle=(mode == "paced"))
            r["meta"] = meta
            if mode == "paced":
                floors[(pre, post)] = r["floor_power"]
            results[stem] = r
            print(stem, r["detections"], r["a_lead_samples_to_within_1dB"],
                  r["b_tail_samples_to_within_1dB_of_floor"], round(r["d_last80_minus_median_db"], 2), flush=True)
    ref = results.get("paced_pre256_post256")
    for k, r in results.items():
        if ref is None or k == "paced_pre256_post256":
            continue
        d = np.array(r["a_lead_excess_db_first64"]) - np.array(ref["a_lead_excess_db_first64"])
        r["a_lead_minus_ref256_db"] = dict(max_abs_first64=float(np.abs(d).max()),
                                           rms_first64=float(np.sqrt(np.mean(d**2))),
                                           mean_first8=float(d[:8].mean()))
        r["d_last80_minus_ref256_db"] = r["d_last80_minus_median_db"] - ref["d_last80_minus_median_db"]
        r["d_first80_minus_ref256_db"] = r["d_first80_minus_median_db"] - ref["d_first80_minus_median_db"]
    OUT.write_text(json.dumps(results, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--phase", choices=("capture", "analyze", "all"), default="all")
    p.add_argument("--tx-gain", type=float, default=-10.0)
    p.add_argument("--rx-gain", type=float, default=15.0)
    p.add_argument("--rate", type=float, default=100.0, help="paced bursts per second")
    p.add_argument("--b2b", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--tx-serial", default="104473541196000419000c00f2ff1e6e77")
    p.add_argument("--rx-serial", default="10447354119600041b000b00f2ff1ef9c6")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--overlay", default=DEFAULT_OVERLAY)
    p.add_argument("--enable-rf", action="store_true")
    p.add_argument("--acknowledgement")
    a = p.parse_args()
    if a.phase in ("capture", "all"):
        capture(a)
    if a.phase in ("analyze", "all"):
        analyze()


if __name__ == "__main__":
    main()
