# Duplex run experiments/duplex_fdd/results/m2/m2_mcs12_g-20_oneway_AB

- duration 70.5 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 12, '2': 12}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 3.90 3.75 3.19 1/2627 690378

## AB: node1 -> node2

udp_load: sent 27000, delivered 20506, lost 6494 (24.05%), corrupt 0, goodput 2.461 Mb/s, latency p50/p99 85/150 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -20.0 | 2.032 | 26.62% | 23.48% | 8/10.0/12/12 | 1 | 9.6 | - |
| 30-60 | -20.0 | 2.455 | 24.71% | 24.31% | 12/12.0/12/12 | 0 | 9.6 | - |
| 60-71 | -20.0 | 1.241 | 3.47% | 25.14% | 12/12.0/12/12 | 0 | 9.8 | - |
| all | - | 2.094 | 24.05% | 24.04% | 8/10.0/12/12 | 1 | 9.6 | - |

## BA: node2 -> node1

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -14.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 9.3 | - |
| 30-60 | -14.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 9.5 | - |
| 60-71 | -14.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 9.4 | - |
| all | - | 0.000 | - | - | 8/8.0/8/8 | 0 | 9.4 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 2, start-up 0, total 2
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 2, workers 7 x sum 88, total 90
- node2: main 3, workers 7 x sum 207, total 210
