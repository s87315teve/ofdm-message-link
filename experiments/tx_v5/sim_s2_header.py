"""S2: SIG (v5) vs legacy header frame-error rate vs SNR, AWGN and 5-tap multipath.

    PYTHONPATH=src python experiments/tx_v5/sim_s2_header.py [--trials 1000]

Header-only FER with ORACLE acquisition (timing + CFO taken from the noise-free
copy of the same burst), so acquisition misses do not mask header errors; the
noisy buffer is cut right after the fixed header and the header counts as
decoded iff the receiver then reports "truncated payload" with the right
required length.  Variants: v5 SIG (interleaver on), v5 SIG interleaver off
(TX and RX, via a sim-only patch of burst_v5.decode_v5_header), legacy header
3x repetition without / with the improve-rx ML list (header_list_max_payload_bytes
0 / 1024).  Writes results/sim/s2_header.json.
"""
import argparse
import functools

import ofdm_link.phy.burst_v5 as bv5
from ofdm_link.phy.burst import _acquire_one, _decode_burst_with_acquisition
from ofdm_link.phy.burst import _require_config
from ofdm_link.phy.burst_header import BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT
from ofdm_link.phy.header_v5 import decode_v5_header, v5_header_symbols
from ofdm_link.phy.ofdm import modulate_ofdm
from ofdm_link.phy.sync_v5 import V5_FIXED_LENGTH, V5_SIG_OFFSET
from ofdm_link.phy.codec import demap_symbols  # noqa: F401
from ofdm_link.phy.burst import FrameDecoder  # noqa: F401
from ofdm_link.phy.burst import DEFAULT_DUAL_VERSION_FRAME_DECODER
from ofdm_link.phy import BurstConfig
from sim_common import *  # noqa: F401,F403
from ofdm_link.phy.codec import demap_symbols as _dm

ORIG = bv5.decode_v5_header


def header_ok(rx_full, acq, cfg, end):
    try:
        _decode_burst_with_acquisition(
            rx_full[:end], cfg, acq, frame_decoder=DEFAULT_DUAL_VERSION_FRAME_DECODER,
            symbol_demapper=_dm, soft_symbol_demapper=None, input_noise_variance=None)
    except BurstDecodeError as e:
        return str(e).startswith("truncated payload")
    except Exception:  # noqa: BLE001
        return False
    return True  # buffer unexpectedly complete


def make_v5(rng, interleave):
    body, tx = v5_burst(rng, 8)
    if not interleave:
        enc = encode_burst_v5(body, mcs_entry(8), scrambler_seed=1)
        sig = modulate_ofdm(v5_header_symbols(enc.header, interleave=False), BurstConfig().numerology,
                            first_symbol_index=0, pilot_version=1).samples
        tx = np.asarray(enc.samples).copy()
        tx[V5_SIG_OFFSET:V5_SIG_OFFSET + len(sig)] = sig
    return tx


def run(variant, mp, snrs, trials):
    rows = []
    is_v5 = variant.startswith("v5")
    cfg = BurstConfig() if variant != "legacy_nolist" else BurstConfig(header_list_max_payload_bytes=0)
    bv5.decode_v5_header = functools.partial(ORIG, interleave=False) if variant == "v5_nointerleave" else ORIG
    for snr in snrs:
        rng = np.random.default_rng(2000 + int(snr * 10) + 7 * mp)
        bad = 0
        for _ in range(trials):
            if is_v5:
                tx = make_v5(rng, variant == "v5_sig")
                end = 600 + V5_FIXED_LENGTH + 30
            else:
                _, tx = legacy_burst(rng)
                end = None
            clean = channel(tx, 300.0, rng, mp)  # noise ~1e-30: oracle acquisition
            acq = _acquire_one(clean, _require_config(cfg))
            if acq is None:
                raise SystemExit("clean acquisition failed")
            if end is None:
                end = (acq.preamble_start + _require_config(cfg).sync.preamble_length + 80
                       + BURST_HEADER_SIGNALING_OFDM_SYMBOL_COUNT * 80 + 30)
            rx = channel(tx, snr, rng, mp)
            # same noise draw would be nicer; independent draw is fine statistically
            bad += not header_ok(rx, acq, cfg, end)
        rows.append({"snr": snr, "fer": bad / trials, "n": trials})
        print(variant, "mp" if mp else "awgn", rows[-1], flush=True)
        if bad == 0 and len(rows) > 2 and rows[-2]["fer"] == 0:
            break
    bv5.decode_v5_header = ORIG
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=1000)
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    snrs = [x * 0.5 for x in range(-10, 17)] if not a.quick else [-4.0, 0.0]
    out = {}
    for mp in (False, True):
        for v in ("v5_sig", "v5_nointerleave", "legacy_nolist", "legacy_list"):
            if v == "v5_nointerleave" and not mp:
                pass
            rows = run(v, mp, snrs, a.trials)
            out[f"{v}_{'mp' if mp else 'awgn'}"] = {
                "rows": rows,
                "snr_1pct": snr_at([r["snr"] for r in rows], [r["fer"] for r in rows], 0.01, a.trials),
            }
    save("s2_header.json", out)
    for k, v in out.items():
        print(k, "1% FER SNR", v["snr_1pct"])


main()
