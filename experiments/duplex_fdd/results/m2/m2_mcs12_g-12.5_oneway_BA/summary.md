# Duplex run m2/m2_mcs12_g-12.5_oneway_BA

- duration 71.0 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 12, '2': 12}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 3.09 3.19 2.91 4/2641 671590

## AB: node1 -> node2

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -12.5 | 0.000 | - | - | 8/8.0/8/8 | 0 | 17.0 | - |
| 30-60 | -12.5 | 0.000 | - | - | 8/8.0/8/8 | 0 | 17.1 | - |
| 60-71 | -12.5 | 0.000 | - | - | 8/8.0/8/8 | 0 | 16.9 | - |
| all | - | 0.000 | - | - | 8/8.0/8/8 | 0 | 17.0 | - |

## BA: node2 -> node1

udp_load: sent 27000, delivered 26052, lost 948 (3.51%), corrupt 0, goodput 3.126 Mb/s, latency p50/p99 85/180 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -6.5 | 2.582 | 3.52% | 3.61% | 8/10.0/12/12 | 1 | 15.6 | - |
| 30-60 | -6.5 | 3.101 | 4.09% | 3.59% | 12/12.0/12/12 | 0 | 15.7 | - |
| 60-71 | -6.5 | 1.551 | 0.17% | 2.63% | 12/12.0/12/12 | 0 | 15.9 | - |
| all | - | 2.642 | 3.51% | 3.51% | 8/10.0/12/12 | 1 | 15.7 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 3, workers 7 x sum 141, total 144
- node2: main 2, workers 7 x sum 92, total 94
