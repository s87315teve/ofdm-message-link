# M2: fixed MCS, both directions at once vs. one way

Two Plutos, FDD 2420/2460 MHz (`configs/profiles/duplex_fdd_2420_2460.yaml`), RX gain 15 dB, 900-B
datagrams from `udp_load.py`, 60 s per run, 3 decode workers per node, TX duty cap 0.7.
Node 1 = A (`…6e77`, TX 2420, RX 2460), node 2 = B (`…f9c6`, TX 2460, RX 2420). The path is
asymmetric: A needs about 6 dB more peer TX gain than B for the same SNR (M1). So **node 2
transmits 6 dB above node 1** (`run_matrix.sh fixed --m2-gain2-offset 6`, capped at 0 dB). Every
run started with load1 < 4 (1.4–3.7). The highest load1 during a run was 5.9, in the MCS 12 runs
at −20 dB, where the decode workers work on bursts that fail. git `16af5b9` plus the
not-yet-committed ingress-drop counter (no effect on the radio path).

```bash
bash experiments/duplex_fdd/run_matrix.sh fixed --out-root experiments/duplex_fdd/results/m2 \
    --enable-rf --m2-gain2-offset 6
PYTHONPATH=src python experiments/duplex_fdd/summarize_duplex.py experiments/duplex_fdd/results/m2/m2_*
python experiments/duplex_fdd/run_table.py experiments/duplex_fdd/results/m2/m2_*
```

## Results

Loss = datagrams that did not arrive byte-exact at the far egress (`udp_load`). Air loss =
receiver sequence gaps (`summarize_duplex.py`, `summary.json`). SNR = receiver median 2-s preamble
SNR. RX ovf = receiver overflows after the first 3 s. CPU in cores, from the stats lines: engine
child of the sender and of the receiver, and the receiver's decode workers.

| MCS, TX dB A / B | dir | run | rate/s | loss | air loss | goodput Mb/s | latency p50 / p99 ms | SNR dB | RX ovf | engine CPU tx / rx | workers rx |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 12, −5 / 0 | A→B | duplex | 450 | 0.00 % | 0.00 % | 3.251 | 91 / 293 | 22.8 | 0 | 0.66 / 0.65 | 0.81 |
| | A→B | one way | 450 | 0.00 % | 0.00 % | 3.243 | 77 / 160 | 22.9 | 0 | 0.84 / 0.52 | 0.81 |
| | B→A | duplex | 450 | 0.86 % | 0.86 % | 3.212 | 284 / 382 | 21.0 | 3 | 0.65 / 0.66 | 0.84 |
| | B→A | one way | 450 | 0.81 % | 0.81 % | 3.213 | 107 / 231 | 21.2 | 0 | 0.83 / 0.52 | 0.83 |
| 12, −12.5 / −6.5 | A→B | duplex | 450 | 0.02 % | 0.02 % | 3.254 | 235 / 374 | 16.4 | 1 | 0.67 / 0.66 | 0.81 |
| | A→B | one way | 450 | 0.00 % | 0.00 % | 3.240 | 79 / 143 | 16.4 | 0 | 0.85 / 0.52 | 0.81 |
| | B→A | duplex | 450 | 3.33 % | 2.96 % | 3.115 | 585 / 698 | 15.7 | 1 | 0.66 / 0.67 | 0.87 |
| | B→A | one way | 450 | 3.51 % | 3.51 % | 3.128 | 85 / 180 | 15.7 | 0 | 0.84 / 0.53 | 0.87 |
| 12, −20 / −14 | A→B | duplex | 450 | 24.7 % | 24.7 % | 2.427 | 546 / 670 | 9.5 | 1 | 0.63 / 0.64 | 1.55 |
| | A→B | one way | 450 | 24.1 % | 24.0 % | 2.455 | 85 / 150 | 9.6 | 0 | 0.81 / 0.51 | 1.52 |
| | B→A | duplex | 450 | 60.8 % | 60.4 % | 1.271 | 637 / 726 | 8.7 | 2 | 0.64 / 0.63 | 1.99 |
| | B→A | one way | 450 | 60.0 % | 60.0 % | 1.299 | 96 / 156 | 8.7 | 0 | 0.78 / 0.48 | 1.99 |
| 8, −20 / −14 | A→B | duplex | 180 | 10.5 % | 0.00 % | 1.131 | 1766 / 1843 | 9.6 | 1 | 0.62 / 0.61 | 0.40 |
| | A→B | one way | 180 | 5.88 % | 0.02 % | 1.191 | 1670 / 1751 | 9.6 | 0 | 0.82 / 0.49 | 0.41 |
| | B→A | duplex | 180 | 16.8 % | 5.04 % | 1.051 | 1802 / 1886 | 8.3 | 0 | 0.61 / 0.62 | 0.41 |
| | B→A | one way | 180 | 13.4 % | 5.31 % | 1.095 | 1719 / 1783 | 8.5 | 0 | 0.82 / 0.48 | 0.42 |

Smoke (`smoke2/`, 20 s, MCS 8, −20 / −14 dB, 100/s each way): A→B 0 % loss, B→A 5.1 % at 8.4 dB
SNR, latency p50 61 / 63 ms, 0 RX overflows after start-up, engines 0.64 core, workers 0.25 core.
The first smoke (`smoke/`, both nodes at −20 dB, before the RX acquisition fix) failed; see its
section below.

## What the data say

* **Duplex costs no link quality.** At every gain point the air loss and SNR of a direction are
  the same, within counting noise, whether the other direction is busy or idle: 0.86 vs 0.81 %,
  2.96 vs 3.51 %, 24.7 vs 24.0 %, 60.4 vs 60.0 %, 5.0 vs 5.3 %. This confirms M1: the own
  transmitter does not raise the floor 40 MHz away. Neither node heard its own bursts
  (`own_bursts_heard` = `own_seed_drops` = 0). Feedback flowed both ways in every run.
* **RX overflows are rare.** 0–3 per 60-s duplex run, almost all on node 1 (`…6e77`, the Pluto
  that overflowed in M0), and 0 in every one-way run. The RX acquisition fix is what made duplex
  work at all. Before it, the engine sat at 1 core and overflowed 1–2 times a second (`smoke/`).
  Now an engine uses 0.61–0.67 core whether it sends, receives or both. The decode workers use
  0.4–0.9 core per node for good bursts and 1.5–2.0 cores when most bursts fail (MCS 12 at
  −20 dB). The two nodes together use about 2–5 cores (`cpu.jsonl`: 106–267 % per node). 3 workers per node did not overflow,
  so no 2-worker repeat was needed.
* **Latency is higher in duplex, at the same loss.** MCS 12 at 450/s keeps the transmitter at
  airtime 0.63–0.64, close to its limit: bursts of 6 subframes, about 66 data bursts/s. In duplex the
  same node also runs its receiver, so it sends ~5 % slower. The TX queue then holds 70–230
  messages instead of ~13 (`tx.pending`), and the p50 latency is 91–637 ms instead of 77–107 ms.
  No datagram is lost to it at MCS 12.
* **MCS 8 at 180/s is above what one Pluto sends.** The transmitter reaches airtime 0.67–0.70
  without ever waiting for the duty cap (`duty_cap_waits` = 0). The push path is the limit, as
  in M0, where TX duty topped out at 0.78. The send queue fills (`tx.pending` reaches 272), and
  6–11 % of the datagrams are dropped at the sender. That shows as loss ≫ air loss, with ~1.7 s
  of latency. Duplex sends about 5 % less than one way (1.13 vs 1.19 Mb/s A→B). These drops were
  not counted in `tx.ingress_dropped_queue_full`: the UDP ingress ignored a full queue. The
  counter now includes them, from the next run on.
* **B→A (2460 MHz at node A) has a loss floor.** It loses 0.8 % even at 21 dB SNR, 7 dB above
  MCS 12's threshold, while A→B at 16–23 dB loses nothing. 2460 MHz is in Wi-Fi channel 11, and
  M1 measured 1.6–29 % occupancy there. The LA in M3 will see that as channel loss.
* **SNR per gain point:** A→B 22.8 / 16.4 / 9.6 dB at −5 / −12.5 / −20 dB. B→A 21.0 / 15.7 /
  8.5 dB at 0 / −6.5 / −14 dB. MCS 12 needs ≥ 16 dB here (threshold 14 dB + Wi-Fi), and MCS 8 is
  clean at 9.6 dB. At node 1 −20 dB the A→B SNR (9.6 dB) is above T(8) = 7.5 dB, so the M2 dip
  variant is not needed.

## First smoke run (`smoke/`, before the RX acquisition fix)

20 s, MCS 8, both nodes at −20 dB, 100/s each way: A→B 10.8 % loss (SNR 10 dB), B→A 43 % (SNR
3 dB, below MCS 8's threshold; this is what showed the asymmetry), 25 and 55 RX overflows after
start-up and ~290 ms latency. Each engine was at ~1 core before any load. Cause: on real Pluto
samples the RX owner alone took 0.88 core. Offline it took 0.17 core on clean noise, 0.81 with a
DC offset equal to the noise, and 8.8 with 3× that (CW alike), all in
`sync._acquisition_candidates` and `sync_v5.v5_candidates`. The rx-rate fix (duplex-fdd
`b4b8f35`) brought the offline numbers to 0.13 / 0.21 / 0.31 core.
