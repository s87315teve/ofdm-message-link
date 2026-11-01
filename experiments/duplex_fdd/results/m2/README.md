# M2: fixed MCS, duplex — smoke run failed, matrix not run yet

## Smoke (`smoke/`, 20 s, both nodes MCS 8, TX −20 dB, RX 15 dB, 100 datagrams/s of 900 B each way)

```bash
bash experiments/duplex_fdd/run_duplex.sh --enable-rf --out experiments/duplex_fdd/results/m2/smoke \
    --duration 20 --mcs 8 --tx-gain -20 --rate-ab 100 --rate-ba 100
```

| Direction | delivered / sent | loss | preamble SNR (2 s) | latency p50 / p99 | RX overflows at the receiver |
|---|---:|---:|---:|---:|---:|
| A→B (node 1 → node 2, 2420 MHz) | 1785 / 2000 | 10.8 % | 10 dB | 284 / 451 ms | 25 (node 2) |
| B→A (node 2 → node 1, 2460 MHz) | 1138 / 2000 | 43.1 % | 3 dB | 299 / 375 ms | 55 (node 1) |

Overflows start with the load (0 in the first 3 s) and grow by about 1–2 per second. Each node's
engine child runs at ~1.0 core from the start, even before the load. Control and feedback work:
both nodes sent and received feedback (752/531 and 838/700), every burst carried its CONTROL
frame (1544 and 2148 counted), and no own bursts were heard. TX duty stayed far below the 0.7 cap
(no cap waits).

* **B→A at −20 dB is below MCS 8's threshold** (3 dB vs. 7.5 dB). M1 had already shown that the
  path is asymmetric: node A needs about 6 dB more peer TX gain than node B for the same SNR.
* **The engine is CPU-bound in RX acquisition, not in TX or decode.** On one Pluto, receive only
  (no TX), with real samples and no bursts, the RX owner thread alone uses 0.88 core and gets 7
  overflows in 8 s. Offline, on synthetic noise, the RX owner needs 0.17 core per second of
  5 MS/s. With a DC offset as large as the noise it needs 0.81 core, and with 3× the noise
  9 core. A CW tone does the same: 0.98 and 9.4 core. In none of these cases is a burst
  candidate found. cProfile puts the time in `ofdm_link/phy/sync.py` `_acquisition_candidates`
  (a Python loop over above-threshold positions) and `sync_v5.v5_candidates`. A Pluto's LO
  leakage is such a DC component. In node_app, TX encode in the same process tips the RX owner
  over the edge, which causes the overflows, the queueing latency and much of the loss.

The matrix waits for a fix to the acquisition cost (or a decision to run it as a baseline).
