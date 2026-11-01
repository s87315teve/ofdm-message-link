# Duplex run experiments/duplex_fdd/results/m2/m2_mcs12_g-5_duplex

- duration 71.0 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 12, '2': 12}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 1.41 2.29 2.52 3/2649 627766

## AB: node1 -> node2

udp_load: sent 27000, delivered 27000, lost 0 (0.00%), corrupt 0, goodput 3.240 Mb/s, latency p50/p99 91/293 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 2.679 | 4.07% | 0.00% | 8/10.0/12/12 | 1 | 22.8 | - |
| 30-60 | -5.0 | 3.240 | 0.01% | 0.00% | 12/12.0/12/12 | 0 | 22.7 | - |
| 60-71 | -5.0 | 1.530 | 0.00% | 0.00% | 12/12.0/12/12 | 0 | 22.9 | - |
| all | - | 2.738 | 0.00% | 0.00% | 8/10.0/12/12 | 1 | 22.7 | - |

## BA: node2 -> node1

udp_load: sent 27000, delivered 26767, lost 233 (0.86%), corrupt 0, goodput 3.212 Mb/s, latency p50/p99 284/382 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | 0.0 | 2.735 | 0.00% | 1.16% | 8/10.0/12/12 | 1 | 21.0 | - |
| 30-60 | 0.0 | 3.205 | 1.04% | 0.69% | 12/12.0/12/12 | 0 | 21.0 | - |
| 60-71 | 0.0 | 1.322 | 12.41% | 0.30% | 12/12.0/12/12 | 0 | 21.0 | - |
| all | - | 2.714 | 0.86% | 0.86% | 8/10.0/12/12 | 1 | 21.0 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 3, start-up 0, total 3
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 3, workers 7 x sum 151, total 154
- node2: main 3, workers 7 x sum 150, total 153
