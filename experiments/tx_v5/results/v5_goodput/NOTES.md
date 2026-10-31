# v5 live UDP goodput (wire-v5, tx-v5 @ 5a0cf7a) vs improve-rx baseline

2026-11-01. Stage 1 05:58-07:13, stage 2/3 07:13-07:47, extra + H7 07:47-07:50 (local). Two PlutoSDRs, 2.45 GHz, 5 MS/s, RX gain 15.
rx_app and tx_app from the main tree (H7: tx_app from the improve-rx worktree). Rule: loss < 0.1 % and 0 RX overflow while loaded, 30 s runs,
1.5x bracketing + bisection to ~5 % (same goodput_search.py as the baseline). goodput = rate*size*8.

## Commands
- 900 B, aggregation on: `run_v5_goodput.sh "-5 14" ...` -> `goodput_search.py --root MAIN --tx-gain G --mcs M --est-rate E --out 900B_agg_on/<G>_mcs<M>`
- Aggregation off / 300 B: `run_v5_goodput_stage2.sh` (`--tx-arg "--aggregation off"`, `--size 300`); extra + H7: `run_v5_goodput_stage3.sh`.
- Script changes (untracked/tracked under experiments/tx_v5 only): live_run.sh gets env TX_ROOT (tx_app tree) and TX_EXTRA_ARGS;
  goodput_search.py gets --tx-arg, --tx-root, --est-rate, --cap (6000, was hard 800) and records latency p50/p99 and psdus/subframes.
  udp_load.py paced up to 3000/s with sender_max_behind_ms <= 5.5 ms (fine).
- Mean subframes/burst = subframes_ok / psdus_received of the last rx_stats record.

## Results, 900 B, aggregation on (best passing rate)
| gain | MCS | rate/s | Mb/s | sf/burst | worker CPU sum % | load at start | overflow | p50/p99 ms |
|---|---|---|---|---|---|---|---|---|
| -5 | 14 | 990 | 7.128 | 7.4 | 201 | 2.71 | 0 | 65/151 |
| -5 | 15 | 900 | 6.480 | 8.0 | 212 | 3.41 | 0 | 275/353 |
| -5 | 13 | 866 | 6.237 | 6.3 | 191 | 2.99 | 0 | 59/117 |
| -5 | 12 | 709 | 5.103 | 6.0 | 177 | 3.26 | 0 | 224/358 |
| -5 | 10 | 459 | 3.308 | 3.2 | 136 | 3.23 | 0 | 53/117 |
| -5 | 9 | 371 | 2.668 | 3.0 | 126 | 1.91 | 0 | 451/775 |
| -5 | 8 | 243 | 1.748 | 2.0 | 110 | 2.27 | 0 | 443/736 |
| -5 | 16 | 228 | 1.641 | 1.0 | 158 | 3.73 | 0 | 75/147 |
| -5 | 17 | 210 | 1.512 | 1.0 | 154 | 3.38 | 0 | 142/201 |
| -15 | 12 | 658 | 4.738 | 4.6 | 181 | 3.31 | 0 | 55/121 |
| -15 | 10 | 486 | 3.496 | 4.0 | 137 | 2.08 | 0 | 348/552 |
| -15 | 9 | 371 | 2.668 | 3.0 | 123 | 3.42 | 0 | 440/760 |
| -15 | 8 | 243 | 1.748 | 2.0 | 106 | 2.37 | 0 | 468/888 |
| -15 | 15 | 124 | 0.895 | 1.0 | 140 | 3.18 | 0 | 43/81 (loss 0.080 %, edge) |
| -15 | 13 | 12.5 | 0.090 | 1.0 | 80 | 2.32 | 0 | 40/52 (375 datagrams only) |
| -15 | 14 | 4.4 | 0.032 | 1.0 | 77 | 1.45 | 0 | 41/51 (132 datagrams only) |
| -15 | 16, 17 | 0 | 0 | | | | | fail 20/s probe (24 %, 60 % loss) |

Best v5 vs best baseline: -5 dB 7.128 vs 2.339 Mb/s (MCS 14 vs MCS 4) = +205 %; -15 dB 4.738 vs 1.539 Mb/s (MCS 12 vs MCS 0) = +208 %.

## Aggregation off (900 B, best MCS)
| gain | MCS | rate/s | Mb/s | on (Mb/s) | ratio on/off | worker CPU % | p50/p99 ms |
|---|---|---|---|---|---|---|---|
| -5 | 14 | 274 | 1.971 | 7.128 | 3.6x | 169 | 55/189 |
| -15 | 12 | 315 | 2.268 | 4.738 | 2.1x | 186 | 113/210 |
Without aggregation the limit is per-burst RX overflow (about 280-300 bursts/s) before air capacity; off at -5 dB MCS 14 is below the baseline's 2.339 (MCS 4, 325 bursts/s), at -15 dB it is +47 % over baseline (MCS 12 vs 0).
Only the best MCS was tested with aggregation off (another MCS might do a little better).

## 300 B, aggregation on (best MCS)
| gain | MCS | rate/s | Mb/s | baseline Mb/s | sf/burst | worker CPU % | p50/p99 ms |
|---|---|---|---|---|---|---|---|
| -5 | 14 (default max wait 0) | 222 | 0.534 | 1.170 | 1.0 | 141 | 74/129 |
| -5 | 14, `--aggregation-max-wait-ms 4` (extra) | 2468 | 5.923 | 1.170 | 14.4 | 204 | 52/73 |
| -15 | 12 | 1772 | 4.252 | 0.952 | 13.2 | 178 | 57/113 |
Anomaly: at -5 dB MCS 14 with 300 B the TX drains each datagram as its own short burst (max wait 0 never builds a backlog until ~1500/s offered), so the
RX sees many tiny bursts and overflows at ~230-300 bursts/s (sf/burst 1.0; loss in this regime is missing bursts / rx overflow, not decode failures). A 4 ms wait fixes it (extra search, dir 300B_wait4ms). At -15 dB the slower TX builds the backlog itself.

## Latency
Latency at the best passing rate is in the tables (load.json p50/p99, includes tx queueing). Low-rate-MCS (8, 9, 10-at-edge) best rates sit at the TX
capacity edge, so queueing gives 350-900 ms; MCS 12-14 sit at 50-150 ms (p99 up to 350 ms for MCS 12/15 at -5, 150-ish elsewhere). Baseline latency not re-measured here.

## H7 legacy compat (improve-rx tx_app, MCS 0-7 -> v5 rx_app), 30 s each (dir h7/)
| run | sent | lost | loss % | RX overflow | decoder counters |
|---|---|---|---|---|---|
| MCS 0, -15 dB, 200/s a | 6000 | 3 | 0.050 | 0 | bursts_legacy 5997, v5 0 |
| MCS 0, -15 dB, 200/s b | 6000 | 0 | 0.000 | 0 | legacy 6000 |
| MCS 4, -5 dB, 300/s a | 9000 | 7 | 0.078 | 0 | legacy 8993 |
| MCS 4, -5 dB, 300/s b | 9000 | 1 | 0.011 | 0 | legacy 8999 |
Baseline (old RX) nearby: MCS 0 171/s 0.019 %, 213.8/s 0.078 %, 224.5/s 0.27 %; MCS 4 297/s 0 %, 315.6/s 0 %, 324.9/s 0.010 %. Same within noise; H7 passes.

## Anomalies / notes
- Limiting factor at 64QAM (MCS 16/17, and 15 at -5 dB when overloaded): RX overflow while loaded at ~210-230 datagrams/s (1 sf/burst; 8 workers sampled, decode pool has 4 workers, worker CPU sum 150-230 %). MCS 16/17 pass only at ~210-228/s with 1 subframe/burst, i.e. aggregation does not help because the RX falls over first (searches show overflow at 282/s and above). Overflow reruns triggered by the search are recorded in search.json (rerun entries).
- MCS 14/15 at -5 dB: limit ~990/900 per s with sf/burst 7-8 and worker CPU ~200 %; loss there is TX/air saturation (no overflow).
- -15 dB MCS 13/14/15 sit at the SNR edge (loss 0.2-0.6 % at 20/s); results for 13/14 are from <400 datagrams so are only indicative; MCS 15 passed at 0.080 % loss.
- Pass/fail near edges is noisy (0.1 % = 3-30 datagrams); e.g. -5 dB MCS 9 passed at 370.5/s with 0.027 % loss.
- load at run start was 0.6-3.9 (decode workers of the previous run); gate (<4) not hit. No pluto hangs. First launch of the driver used a relative --out and failed silently in live_run (three failed retries); fixed (absolute path) and restarted; no data from that attempt kept.
- Working tree also shows README/docs modifications that are not mine.
