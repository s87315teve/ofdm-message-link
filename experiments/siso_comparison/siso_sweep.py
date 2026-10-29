import sys, json, numpy as np
sys.path.insert(0, sys.argv[1])
import siso_model as m

NSYM = int(sys.argv[2])
FS = 3.2e6
TAPS = [0.82 + 0.13j, 0, 0.21 - 0.17j, 0, 0.08j]

def run(snr_db, multipath, seed):
    rng = np.random.default_rng(seed)
    cws = [rng.integers(0, 256, m.MSG, dtype=np.uint8) for _ in range(NSYM)]
    x = np.concatenate([m.tx_symbol(c) for c in cws])
    if multipath:
        x = np.convolve(x, TAPS)[: x.size]
    x = x * np.exp(2j * np.pi * 10e3 / FS * np.arange(x.size))      # 10 kHz CFO
    var = np.mean(np.abs(x) ** 2) / 10 ** (snr_db / 10)
    x = x + np.sqrt(var / 2) * (rng.standard_normal(x.size) + 1j * rng.standard_normal(x.size))
    sync = m.MNCSync()
    acq_len = 2 * m.FFT + m.CP
    pos, ok, seen, est = 1000, set(), set(), []
    meter = []
    while pos + acq_len <= x.size:
        found, idx, td, consume = sync(x[pos: pos + acq_len])
        if found:
            k = int(round((pos + idx) / m.SYM))
            if 0 <= k < NSYM and k not in seen:
                seen.add(k)
                meter.append(sync.snr_meter)
                if np.count_nonzero(m.rx_symbol(td) != cws[k]) <= 8:
                    ok.add(k)
        pos += consume
    usable = NSYM - 2            # symbol 0 starts before pos=1000, last may be cut
    return len(ok & set(range(1, NSYM - 1))) / usable, len(seen & set(range(1, NSYM - 1))) / usable, 10 * np.log10(np.mean(meter))

out = {}
for mp in (False, True):
    rows = []
    for snr in range(-4, 13):
        ok, acq, meter = run(snr, mp, 500 + snr)
        rows.append({"snr": snr, "sym_ok": ok, "acquired": acq, "meter_db": float(meter)})
        print(mp, rows[-1], flush=True)
    out["multipath" if mp else "awgn"] = rows
json.dump(out, open(sys.argv[3], "w"), indent=1)
