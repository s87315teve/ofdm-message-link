# tx_v5 simulation ablations (partial)

Done: S5 (scrambler), S2 (header FER). Not run (CPU reserved for hardware measurements): S1, S4, S3, S7, S8.
Written but never run: ../../sim_s1_acq.py, ../../sim_s3_estimator.py, ../../sim_s4_e2e.py. Helpers: ../../sim_common.py.

## S5 (sim_s5_scrambler.py, 400 bursts per case, raw 943 B bodies, whole burst incl. preamble, scale_to_peak 0.7)
Mean power after scaling (dB), spread = max-min over 5 payload types (random, zero, 0xFF, 4 B pattern, 13 B text):

| MCS | scrambler off spread | scrambler on spread |
|---|---|---|
| 8 | 6.65 dB | 0.36 dB |
| 12 | 9.20 dB | 0.38 dB |
| 17 | 10.95 dB | 0.49 dB |

Per-sample PAPR CCDF with scrambler on: 6.3-6.6 dB at 1e-2 and 7.8-8.3 dB at 1e-3 for all payloads.
Without: 7-14 dB at 1e-2 and 14-19 dB at 1e-3 for non-random payloads. Raw numbers: s5_scrambler.json, s5_scrambler.log.

## S2 header FER (sim_s2_header.py, 1000 trials/point, oracle acquisition, SNR = burst mean power / noise variance)
1% FER SNR (dB): v5 SIG AWGN 0.95, multipath 1.27; v5 SIG no interleaver AWGN 1.43, multipath 1.90;
legacy header without ML list AWGN 6.54, multipath not reached below 8 dB; legacy with ML list AWGN 3.0, multipath 3.38.
Interleaver worth 0.63 dB (multipath) / 0.48 dB (AWGN). Raw numbers: s2_header.json, s2_header.log.
S1, S3, S4, S7, S8 were not run; the hardware results cover the same questions (see ../../README.md section 4.9).
