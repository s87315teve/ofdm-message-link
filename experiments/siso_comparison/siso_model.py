"""Faithful numpy port of SISO-master's PHY (lib/*.c, app/siso-rx.cc) for a BER/goodput sweep.

TX: RS codeword (204 B, success = <=8 byte errors, as RS(204,188) t=8) -> byte interleave(16)
    -> K=7 (171,133) tail-biting conv r1/2 -> bit interleave(64) -> QPSK -> comb pilots (1 per 7,
    (+-2+-2j) boosted) -> guard -> IFFT 2048 -> CP 256 = 2 x 128 sub-CP.
RX: MNC 3-way CP correlation sync (ofdm_sym_acquisition.c), fractional CFO derotate,
    integer CFO by pilot-energy search, pilot linear interpolation, hard QPSK, deinterleave,
    hard Viterbi, deinterleave, RS error count.
"""
import numpy as np

FFT, CP, SUB = 2048, 256, 128
DPP, NPIL = 6, 273
TPP = DPP + 1
NDATA = (NPIL - 1) * DPP          # 1632
NTONE = NPIL + NDATA              # 1905
ZR = (FFT - NTONE) // 2           # 71
ZL = FFT - NTONE - ZR             # 72
MSG = NDATA // 8                  # 204
SYM = FFT + CP


def _lfsr_seq(taps, state, n):
    # lfsr.c is not shown; any +-1 m-sequence is fine for the model.
    out = []
    reg = state
    for _ in range(n):
        bit = bin(reg & taps).count("1") & 1
        out.append(1 if (reg & 1) else -1)
        reg = (reg >> 1) | (bit << 7)
    return np.array(out)

M1 = _lfsr_seq(0o435 >> 1, 0o107 >> 2, 255)
PILOT = np.array([(M1[i % 255] + 1j * M1[i % 255]) * 2 for i in range(NPIL)])


def _perm(n, cols):
    rows, res = divmod(n, cols)
    return np.array([j * cols + i for i in range(cols)
                     for j in range(rows + (1 if i < res else 0))])


def interleave(x, cols):
    return x[_perm(x.size, cols)]


def deinterleave(x, cols):
    out = np.empty_like(x)
    out[_perm(x.size, cols)] = x
    return out


def conv_encode(bits):
    d = 0
    for b in bits[-8:]:
        d = ((d | (int(b) << 7)) >> 1)
    out = np.empty(bits.size * 2, np.uint8)
    for i, b in enumerate(bits):
        d = (d | (int(b) << 7)) >> 1
        t = (d >> 6) ^ (d >> 4) ^ (d >> 3) ^ d
        out[2 * i] = (t ^ (d >> 5)) & 1
        out[2 * i + 1] = (t ^ (d >> 1)) & 1
    return out

# trellis: state = d>>1 (6 bits); d' = (b<<6)|state
_S = np.arange(64)
_OUT = np.zeros((64, 2, 2), np.uint8)
_NEXT = np.zeros((64, 2), np.int64)
for s in range(64):
    for b in (0, 1):
        d = (b << 6) | s
        t = (d >> 6) ^ (d >> 4) ^ (d >> 3) ^ d
        _OUT[s, b] = ((t ^ (d >> 5)) & 1, (t ^ (d >> 1)) & 1)
        _NEXT[s, b] = d >> 1
# predecessor table: next state n reached from (p, b): n = (b<<5)|(p>>1) -> p = ((n&31)<<1)|lsb
_PRED = np.zeros((64, 2), np.int64)
for n in range(64):
    b = n >> 5
    _PRED[n] = (((n & 31) << 1) | 0, ((n & 31) << 1) | 1)
_PB = (_S >> 5)  # input bit that leads into state n
_POUT = np.stack([_OUT[_PRED[:, k], _PB] for k in (0, 1)], axis=1)  # (64, 2 preds, 2 bits)


def viterbi_hard_tb(coded, wrap=96):
    n = coded.size // 2
    r = coded.reshape(n, 2)
    ext = np.concatenate((r[-wrap:], r, r[:wrap]))
    metric = np.zeros(64)
    surv = np.empty((ext.shape[0], 64), np.uint8)
    for t, (c0, c1) in enumerate(ext):
        bm = (_POUT[..., 0] != c0).astype(np.int32) + (_POUT[..., 1] != c1)
        cand = metric[_PRED] + bm
        choice = np.argmin(cand, axis=1)
        metric = cand[_S, choice]
        surv[t] = choice
    state = int(np.argmin(metric))
    bits = np.empty(ext.shape[0], np.uint8)
    for t in range(ext.shape[0] - 1, -1, -1):
        bits[t] = state >> 5
        state = _PRED[state, surv[t, state]]
    return bits[wrap: wrap + n]


def tx_symbol(codeword):
    bits = np.unpackbits(interleave(codeword, 16))
    coded = interleave(conv_encode(bits), 64)
    q = 0.7071067811865475 * ((1 - 2.0 * coded[0::2]) + 1j * (1 - 2.0 * coded[1::2]))
    tones = np.empty(NTONE, complex)
    tones[0::TPP] = PILOT
    for k in range(DPP):
        tones[1 + k::TPP][: NPIL - 1] = q[k::DPP]
    grid = np.zeros(FFT, complex)
    grid[ZL: ZL + NTONE] = tones
    body = np.fft.ifft(np.fft.ifftshift(grid)) * np.sqrt(FFT)   # index FFT/2 -> DC, as fft.h
    tail = body[-SUB:]
    return np.concatenate((tail, tail, body))


class MNCSync:
    """Port of ofdm_sym_acquisition.c incl. its static IIR state."""

    def __init__(self):
        self.avg_max = 0.0

    def __call__(self, x):
        p = np.abs(x) ** 2
        cs = np.concatenate(([0.0], np.cumsum(p)))
        sp = cs[SUB:] - cs[:-SUB]                      # sync_metric_p[i] = sum p[i..i+127]

        def corr(a, lag):
            prod = np.conj(x[a:a + FFT + SUB]) * x[a + lag: a + lag + FFT + SUB]
            c = np.concatenate(([0], np.cumsum(prod)))
            return c[SUB:SUB + FFT] - c[:FFT]
        i = np.arange(FFT)
        c0 = corr(0, FFT + SUB)
        c1 = corr(SUB, FFT)
        c2 = corr(0, SUB)
        m0 = np.abs(c0) ** 2 / (sp[i] * sp[i + FFT + SUB])
        m1 = np.abs(c1) ** 2 / (sp[i + SUB] * sp[i + FFT + SUB])
        m2 = np.abs(c2) ** 2 / (sp[i] * sp[i + SUB])
        ms = (m0, m1, m2)
        cond = np.zeros(FFT, bool)
        for m in ms:
            f = np.empty(FFT); f[0] = m[0]
            for k in range(1, FFT):
                f[k] = 0.8 * f[k - 1] + 0.2 * m[k]
            for k in range(FFT - 2, -1, -1):
                f[k] = 0.8 * f[k + 1] + 0.2 * m[k]
            cond |= m > f
        tot = np.where(cond, m0 + m1 + m2, -999.999)
        idx = int(np.argmax(tot))
        mx = tot[idx]
        if mx > 0.5 * self.avg_max:
            self.avg_max = 0.95 * self.avg_max + 0.05 * mx
            phase2 = -np.angle(c1[idx]) / FFT
            out = np.exp(1j * phase2 * np.arange(FFT)) * x[idx + CP: idx + CP + FFT]
            metric = min(abs(c0[idx]) / (0.5 * (sp[idx] + sp[idx + FFT + SUB])), 0.999999)
            self.snr_meter = metric / (1.0 - metric)          # the value siso-rx prints
            return True, idx, out, idx + FFT + CP
        return False, idx, None, FFT


def rx_symbol(td):
    f = np.fft.fftshift(np.fft.fft(td)) / np.sqrt(FFT)
    base = ZL
    best, cfo = -1, 0
    for s in range(-ZR, ZR + 1):
        m = np.sum(np.abs(f[base + s: base + s + NTONE: TPP]) ** 2)
        if m > best:
            best, cfo = m, s
    t = f[base + cfo: base + cfo + NTONE]
    h = t[0::TPP] / PILOT
    data = np.empty(NDATA, complex)
    for k in range(DPP):
        hk = (k * h[1:] + (TPP - k) * h[:-1]) / TPP
        data[k::DPP] = t[1 + k::TPP][: NPIL - 1] * np.conj(hk)
    bits = np.empty(NDATA * 2, np.uint8)
    bits[0::2] = data.real < 0
    bits[1::2] = data.imag < 0
    dec = viterbi_hard_tb(deinterleave(bits, 64))
    return deinterleave(np.packbits(dec), 16)
