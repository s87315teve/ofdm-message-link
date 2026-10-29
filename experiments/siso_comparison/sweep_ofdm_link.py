"""ofdm-message-link SNR sweep: MCS0/MCS4, app as shipped vs. two receiver-only fixes."""
import json, sys
import numpy as np
import ofdm_link.phy.burst as burst_mod
from ofdm_link.phy import (CURRENT_PROTOCOL_VERSION, MCS, TURBO_BURST_HEADER_VERSION,
                           BurstDecodeError, Frame, FrameKind, decode_burst, encode_burst)
from ofdm_link.phy.channel import ChannelEstimate, estimate_channel as ls_estimate
from ofdm_message_link.link import measure_quality

TAPS = [0.82 + 0.13j, 0, 0.21 - 0.17j, 0, 0.08j]

# Fix 2: model the channel as <=20 time-domain taps and fit it to the 52 LS values (-4 dB est. noise)
def smoothed_estimate(received, numerology, **kw):
    ls = ls_estimate(received, numerology, **kw)
    k = np.asarray(ls.active_carriers, dtype=float)
    lags = np.arange(-4, 16)
    F = np.exp(-2j * np.pi * np.outer(k, lags) / numerology.fft_size)
    h, *_ = np.linalg.lstsq(F, ls.coefficients.astype(complex), rcond=None)
    return ChannelEstimate((F @ h).astype(np.complex64), ls.active_carriers)

def frame(seq, mcs):
    rng = np.random.default_rng(seq)
    return Frame(CURRENT_PROTOCOL_VERSION, FrameKind.DATA, mcs, seq & 0xFFFF,
                 rng.integers(0, 256, 900, dtype=np.uint8).tobytes())

def channel(tx, snr_db, rng, multipath):
    x = np.concatenate((np.zeros(1500), tx, np.zeros(1500))).astype(np.complex128)
    if multipath:
        x = np.convolve(x, TAPS)[: x.size]
    x *= np.exp(2j * np.pi * 10e3 / 5e6 * np.arange(x.size))          # 10 kHz CFO at 5 MS/s
    p = np.mean(np.abs(x[np.abs(x) > 1e-9]) ** 2)
    var = p / 10 ** (snr_db / 10)
    return (x + np.sqrt(var / 2) * (rng.standard_normal(x.size) + 1j * rng.standard_normal(x.size))).astype(np.complex64), var

def stage(err):
    m = str(err)
    return "acq" if "preamble" in m else "header" if "header" in m else "payload"

def attempt(rx, f, nv, smooth):
    burst_mod.estimate_channel = smoothed_estimate if smooth else ls_estimate
    try:
        d = decode_burst(rx, input_noise_variance=nv)
        return ("ok" if d.frame == f else "payload"), d
    except BurstDecodeError as e:
        return stage(e), None

trials = int(sys.argv[1])
variants = {"app": (False, False), "llr": (True, False), "llr_smooth": (True, True)}
out = {}
for mcs in (MCS.QPSK, MCS.QAM16):
    for multipath in (False, True):
        rows = []
        for snr in range(-4, 19):
            if mcs is MCS.QPSK and snr > 14: continue
            if mcs is MCS.QAM16 and snr < 2: continue
            rng = np.random.default_rng(7000 + snr)
            row = {"snr": snr}
            for v in variants: row.update({f"{v}_{s}": 0 for s in ("ok", "acq", "header", "payload")})
            meter = []
            for t in range(trials):
                f = frame(t, mcs)
                rx, var = channel(encode_burst(f, wire_version=TURBO_BURST_HEADER_VERSION).samples, snr, rng, multipath)
                for v, (use_nv, smooth) in variants.items():
                    res, d = attempt(rx, f, var if use_nv else None, smooth)
                    row[f"{v}_{res}"] += 1
                    if v == "app" and d is not None and res == "ok":
                        meter.append(measure_quality(d.payload_symbols, mcs)[1])
            for k in list(row):
                if k != "snr": row[k] /= trials
            row["app_meter_db"] = float(np.mean(meter)) if meter else None
            rows.append(row)
            print(mcs.name, multipath, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()}, flush=True)
        out[f"{mcs.name}_{'multipath' if multipath else 'awgn'}"] = rows
json.dump(out, open(sys.argv[2], "w"), indent=1)
