# M3: link adaptation through a TX gain step

Same bench as M2 (FDD 2420/2460 MHz, RX gain 15 dB, 900-B datagrams, 3 decode workers per node,
TX duty cap 0.7). Code is git `337b445`: duplex-fdd with the la-fix, the RX acquisition fix and
the start-to-start minimum burst interval (tx-v5 f3f84fc). Node A (1) follows the gain profile
`0:-5,30:-20,60:-5` (s from ready); node B (2) stays at −5 dB. Load: A→B 555 datagrams/s
(4.0 Mb/s offered, more than any rung carries at −20 dB), B→A 139/s (1.0 Mb/s). Each run lasts
90 s plus start and tail. Runs: OLLA ×2, OLLA with B→A idle (feedback only in control bursts),
and fixed MCS 8/10/12/15. MCS 15 replaces the plan's 14, which the threshold ladder drops as
dominated (ladder 8 9 10 12 13 15 16 17). load1 at every start < 4. One earlier run with the old
burst interval is in `../m3_prefix/`.

```bash
bash experiments/duplex_fdd/run_matrix.sh la --out-root experiments/duplex_fdd/results/m3 --enable-rf
PYTHONPATH=src python experiments/duplex_fdd/summarize_duplex.py experiments/duplex_fdd/results/m3/m3_*
python experiments/duplex_fdd/segment_table.py experiments/duplex_fdd/results/m3/m3_*
python experiments/duplex_fdd/run_table.py experiments/duplex_fdd/results/m3/m3_*
```

Each run directory has `summary.md` and `duplex_{AB,BA}.png`: MCS, SNR_f, OLLA offset, and 1-s
goodput and loss over time, with the gain steps marked. `step_metrics.py` gives the per-step numbers.

Correction (after the first version of this README): `summarize_duplex.py` put the receiver's
counters on the receiver's own time axis, but the gain steps on the sender's. The two nodes become
ready seconds apart. The summaries and the tables below are now on the sender's axis, and the
first version's "0 frames lost in the first second after the down step" was wrong (see the step
table).

## A→B per 30-s segment

Goodput = receiver bytes delivered per segment. Datagram loss includes datagrams dropped at the
sender when the offered 4 Mb/s exceeds the rung (`tx.ingress_dropped_queue_full`). Subframe loss
= over-the-air sequence gaps. SNR = receiver preamble SNR.

| run | 0–30 s at −5 dB | 30–60 s at −20 dB | 60–90 s at −5 dB | whole run (udp_load) |
|---|---|---|---|---|
| OLLA run 1 | 3.43 Mb/s, air 0.00 %, MCS → 17 | 1.71 Mb/s, air 3.50 %, MCS 9↔10 (30 changes) | 3.36 Mb/s, air 0.01 %, MCS 17 | 3.21 Mb/s, p50 70 ms |
| OLLA run 2 | 3.47 Mb/s, air 0.07 %, → 17 | 1.73 Mb/s, air 3.49 %, 9↔10 (30) | 3.33 Mb/s, air 0.02 %, 17 | 3.23 Mb/s, p50 72 ms |
| OLLA, B→A idle | 3.49 Mb/s, air 0.00 %, → 17 | 1.62 Mb/s, air 4.05 %, 8–10 (31) | 3.35 Mb/s, air 0.01 %, 17 | 3.19 Mb/s, p50 61 ms |
| fixed 8 | 0.99 Mb/s, air 0 % | 1.12 Mb/s, air 0 % | 0.97 Mb/s, air 0 % | 1.13 Mb/s, p50 1780 ms |
| fixed 10 | 1.91 Mb/s, air 0 % | 2.14 Mb/s, air 4.13 % | 1.96 Mb/s, air 0.05 % | 2.22 Mb/s, p50 912 ms |
| fixed 12 | 2.76 Mb/s, air 0 % | 2.41 Mb/s, air 25.2 % | 2.69 Mb/s, air 0.13 % | 2.92 Mb/s, p50 664 ms |
| fixed 15 | 3.50 Mb/s, air 0 % | 0.02 Mb/s (99.6 % lost) | 3.31 Mb/s, air 0.18 % | 2.64 Mb/s, p50 78 ms |

Per gain step (A→B, `step_metrics.py`; windows have the 0.5-s resolution of the stats lines):

| run | step | subframes lost in the first 0.5 / 1 / 1.5 s | loss from 1.5 s after the step to the next step | time to MCS ≤ 10 (down) / ≥ 15 (up) |
|---|---|---:|---:|---:|
| OLLA run 1 | −5 → −20 dB | 0 / 0 / 188 | 1.04 % | 0.47 s |
| OLLA run 1 | −20 → −5 dB | 0 / 0 / 0 | 0.01 % | 0.24 s |
| OLLA run 2 | −5 → −20 dB | 0 / 188 / 188 | 1.05 % | 0.48 s |
| OLLA run 2 | −20 → −5 dB | 0 / 0 / 0 | 0.02 % | 0.26 s |
| OLLA, B→A idle | −5 → −20 dB | 0 / 209 / 209 | 1.17 % | 0.52 s |
| OLLA, B→A idle | −20 → −5 dB | 0 / 0 / 0 | 0.01 % | 0.27 s |

The receiver counts a lost subframe when the next one decodes, so the losses right after the down
step appear 0.5–1.5 s late.

SNR A→B: 22.8–23.4 dB at −5 dB, 9.5–10.2 dB at −20 dB. Thresholds: MCS 9 9.0, 10 11.0, 12 14.0,
15 16.5, 17 22.0 dB. The first segment's goodput includes ~3.5 s at MCS 8 before the first feedback
arrived.

B→A (139/s at −5 dB, SNR 17.5 dB) carried 0.98–1.00 Mb/s in every run, with 1.1 % loss under OLLA
(MCS around 10, 81–93 changes) and 0–2.4 % under the fixed rungs. M2 measured a loss floor of about
0.8 % on this Wi-Fi channel-11 path.

## What the data say

* **Over the whole run OLLA beats every fixed MCS.** It delivers 3.19–3.23 Mb/s against 2.92 (fixed
  12), 2.64 (15), 2.22 (10) and 1.13 (8), with the lowest latency of all (p50 61–72 ms). It loses
  0.6–0.7 % of subframes in the air. The fixed rungs either waste capacity at −5 dB (8, 10, 12) or
  collapse at −20 dB (15: 96 % lost; 12: 24.5 % air loss).
* **At −5 dB OLLA picks the best rung and holds it.** After the first feedback (3.5 s after start)
  and after the step back up at 60 s, it reaches MCS 17 within 0.1–0.5 s. It then holds 17 with
  offset +3 dB (the cap) and air loss ≤ 0.07 %. Its goodput, 3.3–3.5 Mb/s, equals the best fixed
  rung (15: 3.31–3.50 Mb/s). The transmitter is then at its airtime limit (0.66–0.68, no duty-cap
  waits), so 17 does not carry more than 15 here.
* **At −20 dB OLLA holds its loss target, after a costly first half second.** It steps
  17 → 12 → 9 → 8 and reaches MCS ≤ 10 0.47–0.52 s after the gain step. 188–209 subframes are lost
  in that time: the bursts at MCS 17/12 fail FCS, and the reporter stays silent until its 200-ms
  keepalive. From 1.5 s after the step to the next step the air loss is 1.04–1.17 %, on the 1 %
  target. It does that by alternating 9 ↔ 10 about once a second (~250 subframes/s move the
  offset quickly). The 3.5–4.1 % segment loss is therefore almost all the first half second. The
  summarizer's "converged" criterion (every 1-s window < 2 % loss) is too strict for a 1 % target
  with loss in clusters of ~3 subframes; it is never met. OLLA delivers 1.62–1.73 Mb/s: more than
  fixed 8 (1.12 Mb/s, 0 % loss), less than fixed 10 (2.14 Mb/s at 4.1 % loss). The la-tune
  branch addresses the first half second: a report is due when FCS failures grow, and the SNR
  filter restarts after a deep drop. The rerun is in `../m3b/`.
* **Feedback carried only by control bursts works as well as piggybacked feedback.** With B→A idle,
  OLLA's trajectory, goodput (3.19 Mb/s) and loss (4.1 % air at −20 dB, 1.17 % after the first
  1.5 s) match the loaded runs.
  Feedback was never stale in any run.
* **Step back up:** 0 subframes lost in the first 1.5 s after −20 → −5 dB under OLLA, and MCS ≥ 15
  after 0.24–0.27 s.
* **Capacity per rung while also receiving:** fixed 8 carries only 0.93–1.12 Mb/s. The
  transmitter reaches airtime 0.69 without duty-cap waits (`duty_cap_waits` = 0), and 35,615 of
  49,950 datagrams are dropped at its full send queue, counted now in
  `tx.ingress_dropped_queue_full`. The start-to-start interval fix did not lift MCS 8 above
  airtime ~0.69 in duplex. Fixed 12 reaches 2.67–2.75 Mb/s and 15/17 3.3–3.4 Mb/s, all near
  airtime 0.6–0.7.
* **RX overflows:** 0–2 per run, except fixed 15. There node 2's receiver overflowed 42 times
  while its decode workers took 1.55 cores on the −20 dB segment, where almost every MCS 15 burst
  fails. CPU per node: engine 0.6–0.9 core, decode workers 0.4–1.6 cores.

## Summary

| | OLLA (mean of 2) | best fixed per segment |
|---|---|---|
| 0–30 s, −5 dB | 3.45 Mb/s, air 0.04 % | 15: 3.50 Mb/s, air 0 % |
| 30–60 s, −20 dB | 1.72 Mb/s, air 3.5 % (1.05 % after 1.5 s) | 10: 2.14 Mb/s, air 4.1 % (8: 1.12 Mb/s, 0 %) |
| 60–90 s, −5 dB | 3.35 Mb/s, air 0.02 % | 15: 3.31 Mb/s, air 0.18 % |
| whole run | 3.21 Mb/s, p50 71 ms | 12: 2.92 Mb/s, p50 664 ms |
