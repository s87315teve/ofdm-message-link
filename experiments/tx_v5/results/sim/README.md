# tx_v5 simulation ablations (partial)

Done: S5 (scrambler). Not run (paused for a hardware run, no resume received within 60 min): S2, S1, S4, S3, S7, S8.
Written but never run: ../../sim_s2_header.py (untested, may need fixes). Helpers: ../../sim_common.py.

## S5 (sim_s5_scrambler.py, 400 bursts per case, raw 943 B bodies, whole burst incl. preamble, scale_to_peak 0.7)
Mean power after scaling (dB), spread = max-min over 5 payload types (random, zero, 0xFF, 4 B pattern, 13 B text):

| MCS | scrambler off spread | scrambler on spread |
|---|---|---|
| 8 | 6.65 dB | 0.36 dB |
| 12 | 9.20 dB | 0.38 dB |
| 17 | 10.95 dB | 0.49 dB |

Per-sample PAPR CCDF with scrambler on: 6.3-6.6 dB at 1e-2 and 7.8-8.3 dB at 1e-3 for all payloads.
Without: 7-14 dB at 1e-2 and 14-19 dB at 1e-3 for non-random payloads. Raw numbers: s5_scrambler.json, s5_scrambler.log.
