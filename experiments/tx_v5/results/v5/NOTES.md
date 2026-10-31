# Wire v5 on two PlutoSDRs, phase 1: recordings + offline decoding

Date 2026-11-01 (UTC+8). Placement unchanged from the improve-rx baseline session (02:50-03:36) and between all v5 recordings.
2.45 GHz, 5 MS/s, RX gain 15 dB, TX gain <= 0 dB, TX Pluto ...0c00f2ff1e6e77, RX Pluto ...0b00f2ff1ef9c6.
400 bursts per point at 100 bursts/s, 900-byte random datagrams (seeds 0..399, the same as the baseline), one datagram per burst
(aggregation off), default guards (16, 64), scrambler on, unless stated otherwise.

## Timeline
- 04:00-04:02 A: noise recording, TX off, 3 x 30 s raw (one RX overflow at the start, one at a segment boundary during the save; neither
  matters for the noise statistics).
- 04:02-04:20 B: 111 sweep recordings. 04:21-04:23 D: guards (4 x 2000 bursts). 04:23-04:24 E: scrambler (6 x 1000 bursts).
  All recordings: 0 RX overflows, 0 late sends, 1-min load 0.64-2.94 at start (gate < 4 never hit).
- Crossing points were placed with quick decodes using the pre-fix tree (8aa8a8f). The coordinator then merged RX fixes
  (v5 acquisition with steady DC/CW removed from the lag-32 metric, local-peak NMS, FFT backoff 3, timing slope unwrapped along time,
  candidates inside a v5 fixed part). All numbers below come from **77caa7f** (decode 04:54-05:08, noise re-analysis to ~05:45).
- No Pluto hang and no reruns.

## Commands (cwd = repo root, PYTHONPATH=src, OMP/OPENBLAS/MKL_NUM_THREADS=1)
```
python experiments/tx_v5/noise_scan_v5.py record --dir $S/caps_v5/noise --seconds 90
python experiments/tx_v5/ota_capture_v5.py --out $S/caps_v5/sweep --mcs-index M --tx-gains ... --enable-rf --acknowledgement '...'
python experiments/tx_v5/ota_capture_v5.py --out $S/caps_v5/guards --mcs-index 8 --tx-gains -26.25 --bursts 2000 --guards PRE POST --tag gPRE_POST
python experiments/tx_v5/ota_capture_v5.py --out $S/caps_v5/scrambler --mcs-index 8 --tx-gains G --bursts 1000 --payload zeros|random --scrambler on|off --tag ...
bash experiments/tx_v5/decode_all_v5.sh          # every offline decode + the noise re-analysis
python experiments/tx_v5/analyze_v5.py > experiments/tx_v5/results/v5/summary.md
python experiments/tx_v5/tx_power_compare.py legacy qpsk|qam16   # in the baseline tree
python experiments/tx_v5/tx_power_compare.py v5 8|12             # in this tree
```
$S = session scratchpad. Raw IQ: $S/caps_v5/{noise,sweep,guards,scrambler} (about 15 GB, not in the repo); the per-capture .json files are in `captures/`.

## Tools
- `ota_capture_v5.py`: the current `MessageTransmitter` with `--mcs-index`, `--guards`, `--scrambler`, `--payload random|zeros`.
  Records load, overflow and late sends into the .json.
- `offline_decode_v5.py`: `MessageReceiver` (inline, `decode_workers=1`, like the baseline's offline decode) fed in 65536-sample chunks.
  Taps every `_decode_burst_with_acquisition` call (position, family, outcome) and every SNR reading. Each missing sequence gets a position
  interpolated from its decoded neighbours (±10000 samples; leave-one-out jitter of the host pacing is p50 ≈ 700, max ≈ 7000 samples),
  and the attempts near that position decide its stage: preamble missed / SIG failed / payload failed (header valid; v5 FCS failed with no
  CRC-valid subframe). Below 5 decoded bursts it falls back to clustering the attempts. `--set key=value` overrides config keys.
  The stage attribution needs `--workers 1`, because payload decodes in worker processes are not tapped.
- `noise_scan_v5.py`: records raw noise, then scans it offline in 1 Mi-sample blocks. Reports M128 over every offset (the fixed detector's
  metric with the steady part removed, plus the raw metric), c_stf / c_pair per NMS peak, legacy loose scores, and `acquire_all` candidates/s
  at the configured thresholds, with and without the prefilter.
- `analyze_v5.py` builds the tables in `summary.md`. `tx_power_compare.py` computes the mean TX power after `scale_to_peak(0.7)`.

## A. Noise scan (TX off, RX gain 15, 90.0 s of samples, all processed offline) -> `noise_scan.json`
| score (fixed detector, 77caa7f) | noise max | noise max, prefilter | configured | noise max + 0.1 | verdict |
|---|---:|---:|---:|---:|---|
| v5 M128 (steady part removed) | 0.89 (raw metric 0.56) | 1.00 (raw 0.68) | 0.10 | n/a (> 1) | does not separate noise from preambles on this air; it only bounds work: 4554 NMS peaks/s pass 0.10 (5874/s at 0.05) |
| v5 c_stf (per peak that passes M128) | 0.084 | 0.130 | 0.20 | 0.18 (0.23 with prefilter) | safe without the prefilter (margin 0.12); with the prefilter the margin is 0.07, so use 0.23 there |
| v5 c_pair (per peak that passes M128) | 0.067 | 0.112 | 0.25 | 0.17 (0.21 with prefilter) | safe, margin 0.18 |
| legacy coherent confidence (loose 0.10/0.05) | 0.201 | 0.233 | 0.30 | 0.30 | safe (the same as improve-rx's 0.21) |
| candidates/s from `acquire_all` at the configured thresholds | v5 0, legacy 0 | v5 0, legacy 0 | | | no false acquisition in 90 s |

No noise peak passed c_stf >= 0.20, so the c_pair-given-STF distribution is empty. The design's white-noise picture of M128 does not hold:
Pluto noise (coloured, guard-band heavy) reaches M128 ≈ 0.9, so the coherent STF and LTF stages do all the separating. Where the CPU
goes: ~4.5k STF checks per second of noise. Analysis cost: 35 min for 90 s with the prefilter variant included (offline only).

Threshold ablation on the MCS 8 edge recordings (`threshold_ablation.json`). Decoded counts out of 400; preamble misses in brackets:

| TX dB | configured 0.20/0.25 | 0.18/0.17 (noise max + 0.1) | 0.12/0.12 |
|---:|---:|---:|---:|
| -30 | 15 (231) | 20 (152) | 21 (116) |
| -28.75 | 120 (118) | 134 (64) | 136 (47) |
| -27.5 | 316 (30) | 328 (15) | 330 (10) |
| -26.25 | 386 (4) | 389 (0) | 389 (0) |
| -25 | 399 (1) | 400 (0) | 400 (0) |

**Recommendation**: v5_stf_threshold 0.18 and v5_correlation_threshold 0.17 (noise max + 0.1). Noise gives 0 false v5 acquisitions at
those values too (no noise peak reached c_stf 0.18). The change halves the preamble misses at the edge but moves the 1 % point by well
under 1.25 dB, because the payload now dominates the losses. Keep v5_detection_threshold 0.10 as a work bound. The configured defaults
are safe. Not checked: lower v5 thresholds against cross-family false acquisitions on legacy bursts.

## B. v5 sweep -> `ota_v5.json` (full per-point tables in `summary.md`)
Lowest TX gain with loss < 1 % (that point and every higher one pass; no point sits at exactly 1.00 %), with the median in-band
preamble SNR there (v5 LTF pair, all readings):

| MCS | format | lowest pass TX dB | loss there | median pre SNR | next lower point | GUI threshold (SNR + 1, rounded up to 0.5) |
|---:|---|---:|---:|---:|---|---:|
| 8 | QPSK 1/3 | -25 | 0.25 % | 6.7 | -26.25: 3.50 % | 8.0 |
| 9 | QPSK 1/2 | -23.75 | 0.25 % | 7.9 | -25: 3.75 % | 9.0 |
| 10 | QPSK 2/3 | -21.25 | 0.50 % | 10.2 | -22.5: 2.75 % | 11.5 |
| 11 | 16QAM 1/3 (hidden) | not measured | | | | |
| 12 | 16QAM 1/2 | -18.75 | 0.25 % | 13.0 | -20: 2.25 % | 14.0 |
| 13 | 16QAM 2/3 | -17.5 | 0.75 % | 14.3 | -18.75: 9.25 % | 15.5 |
| 14 | 16QAM 3/4 | -15 | 0.25 % | 16.7 | -16.25: 4.50 % | 18.0 |
| 15 | 64QAM 1/2 | -16.25 | 0.75 % | 15.7 | -17.5: 5.00 % | 17.0 |
| 16 | 64QAM 2/3 | -11.25 | 0.25 % | 20.2 | -12.5: 1.25 % | 21.5 |
| 17 | 64QAM 3/4 | -11.25 | 0.50 % | 20.8 | -12.5: 2.50 % | 22.0 |

Notes: MCS 13 also lost 2/400 (0.50 %) at -15 (payload, isolated). MCS 15 beats MCS 14 (64QAM 1/2 at 15.7 dB vs 16QAM 3/4 at 16.7 dB, the
same 3 bit/symbol), so MCS 14 is dominated on this link. MCS 16 and 17 cross at the same 1.25 dB step. The 1.25 dB grid and 400 bursts
mean ±0.6 dB on each crossing.

**Against the baseline** (improve-rx, same placement, same day, ~1-1.5 h earlier):
- v5 MCS 8 at -25 dB vs baseline MCS 0 at -18.75 dB: **6.25 dB lower TX gain** (target >= 2.5 dB; the baseline needed -21.25 for the target, v5 passes
  with 3.75 dB to spare). About 3 dB of this is TX power, not the receiver (see "SNR offset" below). At equal in-band SNR the gain is about 3.2 dB:
  the baseline's 9.2 dB legacy reading is about 9.5 dB on the v5 scale, vs 6.7 dB for v5.
- v5 MCS 12 (rate 2.0 b/sym) at -18.75 and MCS 13 (2.67 b/sym) at -17.5 vs baseline MCS 4 (16QAM 1/3, 1.33 b/sym) at -12.5: 6.25 / 5.0 dB lower
  TX gain at 1.5x / 2x the code rate. At equal in-band SNR (baseline 13.7 dB legacy = about 14.0 dB v5 scale): MCS 12 needs 1.0 dB less and
  MCS 13 0.3 dB more, i.e. about 2x the bits per symbol at the same SNR.

**Stage split at the edge** (preamble missed / SIG failed / payload failed):
| point | loss | pre / SIG / pay |
|---|---:|---|
| MCS 8 -25 | 0.25 % | 1 / 0 / 0 |
| MCS 8 -26.25 | 3.50 % | 4 / 0 / 10 |
| MCS 8 -27.5 | 21.0 % | 30 / 0 / 54 |
| MCS 8 -28.75 | 70.0 % | 118 / 3 / 159 |
| MCS 8 -30 | 96.25 % | 231 / 9 / 145 |
| baseline MCS 0 -22.5 (new RX) | 14.25 % | 54 / 1 / 2 |
| MCS 12 -20 / -21.25 | 2.25 / 8.25 % | 1/0/8, 0/0/33 |
| MCS 14 -16.25 | 4.50 % | 0 / 0 / 18 |

The preamble is **no longer the limit for MCS 8**. Around the 1-20 % loss region about 2/3 of the losses are payload, and the preamble takes
over only below ~3 dB SNR (-28.75 and lower). For legacy MCS 0 the preamble was and still is the limit (54 of 57 losses at -22.5). SIG
failures are rare everywhere (< 3 % of losses down to 70 % loss), so the SIG has the predicted margin over MCS 8. 16/64QAM losses are
all payload.

**64QAM usable?** The best SNR this setup reaches is a median LTF SNR of ~29.7 dB (EVM SNR 27.5) at TX 0 dB; rule threshold 26.7 dB.
MCS 15 passes down to 15.7 dB, MCS 16 to 20.2 and MCS 17 to 20.8, all >= 6 dB below the threshold, so **all three are usable** by the rule.
That was not true before the RX fix: with 8aa8a8f MCS 17 needed -6.25 dB (≈25 dB SNR) and MCS 16 -8.75 dB. EVM SNR at TX 0 also rose
from 23.9 to 27.6 dB (MCS 10).

**SNR offset v5 vs legacy at the same TX gain (asked by the coordinator)**, medians from the two recording sets, TX gains -22.5..0:
| pair | measured LTF minus legacy pre SNR | measured EVM SNR difference (-22.5..-10) | predicted real power difference after scale_to_peak |
|---|---:|---:|---:|
| v5 MCS 8 vs legacy MCS 0 | +3.17 dB (2.87..3.37) | +3.2 dB (2.6..4.2; grows to 5.6 at TX 0) | **+2.95 dB** |
| v5 MCS 12 vs legacy MCS 4 | +4.93 dB (4.35..5.29) | +4.3 dB (3.5..4.8) | **+4.65 dB** |

Predicted from `tx_power_compare.py` (200 bursts, the sink's `scale_to_peak(0.7)`): PAPR legacy QPSK 12.9 dB / 16QAM 14.1 dB vs v5 MCS 8
9.9 dB / MCS 12 9.5 dB. Mean power after scaling: legacy -15.96 / -17.18 dBFS vs v5 -13.01 / -12.58 dBFS. The legacy chirp preamble is
flat over all 64 bins (in-band 52/64 = -0.90 dB), so its in-band power equals the data's, like the v5 LTF's. **The offset is a real TX
power difference** from peak normalization of a lower-PAPR waveform (v5 uses an 802.11 STF/LTF, the payload is scrambled, and the legacy
waveform has peakier bursts). A possible estimator bias is at most ~0.2-0.3 dB (measured minus predicted, inside the median spread).
The EVM SNR difference grows at high SNR because the new RX's EVM floor is lower. Consequence: about 3 dB of v5 MCS 8's 6.25 dB TX-gain
advantage is peak-normalization headroom the legacy TX wasted, about 3 dB is coding/receiver.

## C. Legacy captures on the new RX -> `legacy_on_new_rx.json`, `legacy_on_new_rx_v5acq_off.json`
All 27 baseline captures (MCS 0 and 4, -30..0, plus MCS 0 -18.75) decode to **exactly the baseline's counts** (difference 0.00 % at every
point), with identical median preamble SNRs, both with both families on and with `phy.rx.v5_acquisition=false`. No v5 false acquisition on
legacy bursts changed any outcome. No point differs by > 1 %.

## D. Guards at the MCS 8 edge (TX -26.25 dB, 2000 bursts each) -> `guards_low_snr.json`
| guards (pre, post) | order | loss | 95 % CI (Wilson) | pre / SIG / pay |
|---|---:|---:|---|---|
| (16, 64) default | 1st | 3.60 % | 2.87-4.51 % | 13 / 0 / 59 |
| (0, 32) | 2nd | 3.55 % | 2.82-4.45 % | 29 / 0 / 42 |
| (256, 256) | 3rd | 4.20 % | 3.41-5.17 % | 34 / 0 / 50 |
| (16, 64) repeat | 4th | 5.40 % | 4.49-6.48 % | 50 / 0 / 58 |

The two identical (16, 64) runs, ~1 min apart, differ by 1.8 % (z = 2.7). The run-to-run drift (channel or interference) is larger than any
guard effect. (0, 32) vs (256, 256): z = -1.1. Pooled (16, 64) 4.5 % vs (256, 256): z = 0.5. **No guard setting is measurably worse than
(256, 256) at low SNR**, and (0, 32) is not worse than (16, 64). The design's ±0.3 % criterion cannot be resolved with 2000 bursts (CI half
width ≈ 0.8 %) at this drift. Keep (16, 64); (0, 32) is equally fine.

## E. Scrambler (MCS 8; 1000 bursts each) -> `scrambler_ota.json`
| TX dB | payload / scrambler | TX PAPR (median) | loss | 95 % CI | pre SNR | EVM SNR | decoded-burst power dBFS |
|---:|---|---:|---:|---|---:|---:|---:|
| -26.25 | random / on | 10.0 dB | 3.50 % | 2.53-4.83 % | 5.3 | 4.3 | -48.0 |
| -26.25 | zeros / on | 9.8 dB | 4.00 % | 2.95-5.40 % | 5.4 | 4.5 | -47.9 |
| -26.25 | zeros / off | 17.0 dB | 100 % | 99.6-100 % | -1.8 (2 readings) | - | - |
| -23.75 | random / on | 10.0 dB | 0.10 % | 0.02-0.56 % | 8.1 | 6.3 | -46.4 |
| -23.75 | zeros / on | 9.6 dB | 0.20 % | 0.05-0.73 % | 8.0 | 6.3 | -46.2 |
| -23.75 | zeros / off | 17.0 dB | 99.8 % | 99.3-99.95 % | 1.0 | -1.8 | -50.0 (2 bursts only) |

With the scrambler, all-zero bodies behave like random ones (z = 0.6). Without it the all-zero burst is a few constant constellation points,
so it is peaky (PAPR 17.0 dB). After TX peak normalization it carries ~7 dB less mean power, and the RX preamble SNR drops by 7.1 dB
(8.1 -> 1.0 dB at -23.75), which takes the link from 0.1 % to 99.8 % loss. The scrambler is essential. Keep `phy.v5.scrambler` an
experiment-only toggle.

## F. RX ablations (offline, MCS 8 -28.75..-25 and MCS 12 -23.75..-20) -> `rx_ablation.json`
| setting | MCS 8 decoded at -28.75 / -27.5 / -26.25 / -25 | MCS 12 decoded at -23.75 / -22.5 / -21.25 / -20 |
|---|---|---|
| default (ltf_average, smoothing, llr max) | 120 / 316 / 386 / 399 | 159 / 269 / 367 / 391 |
| ltf_average=false (L1 only) | 42 / 204 / 328 / 384 | 58 / 161 / 301 / 362 |
| channel_smoothing=false | 52 / 242 / 359 / 396 | 54 / 140 / 306 / 372 |
| llr_noise_source=header | identical to default | identical to default |
| llr_noise_source=ltf | 121 / 316 / 386 / 399 | 159 / 269 / 368 / 393 |

LTF averaging and channel smoothing each buy ≈ 1-1.5 dB at the 1-10 % loss points (e.g. MCS 8 -25: 0.25 % -> 4.0 % without averaging,
1.0 % without smoothing; MCS 12 -20: 2.25 % -> 9.5 % / 7.0 %). That is more than the sim's ~0.5 dB, since Pluto's in-band SNR is low at
the edge and the channel-estimate noise matters more. `llr_noise_source` makes no difference (≤ 2 bursts). That is expected: v5 uses one
scalar noise variance per burst (77caa7f documents this), and a common LLR scale does not change a max-log Turbo decode. The toggle only
matters once the variance is per carrier.

## Anomalies / caveats
- Host-paced bursts: leave-one-out arrival jitter p50 ≈ 700, max ≈ 7000 samples (sleep pacing). The attribution window ±10000 covers it.
- The guard runs show ~2 % absolute run-to-run variation at the edge within 1-2 minutes. Edge-point losses (1-10 %) in B carry the same
  drift on top of the binomial CI.
- The pre-fix quick decodes (8aa8a8f, used only to place points) gave the same MCS 8/9/14 crossings. 64QAM was much worse before the fix
  (see B). MCS 12 at -20 lost 3 before vs 9 after (p ≈ 0.08, noise).
- MCS 11 (hidden 16QAM 1/3) was not in the plan and was not measured.
- No src bug found that needed a repro. The `phy.rx.llr_noise_source` null result follows from the decoder's scale invariance and is not a bug.

