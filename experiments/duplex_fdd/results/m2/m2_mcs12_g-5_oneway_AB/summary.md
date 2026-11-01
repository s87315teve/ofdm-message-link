# Duplex run experiments/duplex_fdd/results/m2/m2_mcs12_g-5_oneway_AB

- duration 70.5 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 12, '2': 12}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 3.87 2.99 2.75 2/2618 637015

## AB: node1 -> node2

udp_load: sent 27000, delivered 27000, lost 0 (0.00%), corrupt 0, goodput 3.240 Mb/s, latency p50/p99 77/160 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 2.657 | 4.92% | 0.00% | 8/10.0/12/12 | 1 | 22.8 | - |
| 30-60 | -5.0 | 3.245 | 0.00% | 0.00% | 12/12.0/12/12 | 0 | 22.8 | - |
| 60-71 | -5.0 | 1.652 | 0.00% | 0.00% | 12/12.0/12/12 | 0 | 22.9 | - |
| all | - | 2.757 | 0.00% | 0.00% | 8/10.0/12/12 | 1 | 22.8 | - |

## BA: node2 -> node1

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | 0.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 22.0 | - |
| 30-60 | 0.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 21.7 | - |
| 60-71 | 0.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 22.0 | - |
| all | - | 0.000 | - | - | 8/8.0/8/8 | 0 | 21.9 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 2, workers 7 x sum 92, total 94
- node2: main 3, workers 7 x sum 135, total 138
