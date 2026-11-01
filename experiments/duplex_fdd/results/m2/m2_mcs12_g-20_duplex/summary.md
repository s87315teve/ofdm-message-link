# Duplex run m2/m2_mcs12_g-20_duplex

- duration 70.0 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 12, '2': 12}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 3.47 3.36 3.00 6/2602 679664

## AB: node1 -> node2

udp_load: sent 27000, delivered 20326, lost 6674 (24.72%), corrupt 0, goodput 2.439 Mb/s, latency p50/p99 546/670 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -20.0 | 2.124 | 23.43% | 23.59% | 8/10.0/12/12 | 1 | 9.6 | - |
| 30-60 | -20.0 | 2.400 | 26.12% | 25.80% | 12/12.0/12/12 | 0 | 9.5 | - |
| 60-70 | -20.0 | 1.063 | 22.56% | 23.68% | 12/12.0/12/12 | 0 | 9.7 | - |
| all | - | 2.091 | 24.72% | 24.70% | 8/10.0/12/12 | 1 | 9.6 | - |

## BA: node2 -> node1

udp_load: sent 27000, delivered 10596, lost 16404 (60.76%), corrupt 0, goodput 1.272 Mb/s, latency p50/p99 637/726 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -14.0 | 1.069 | 60.61% | 59.85% | 8/10.0/12/12 | 1 | 8.7 | - |
| 30-60 | -14.0 | 1.255 | 61.37% | 60.55% | 12/12.0/12/12 | 0 | 8.7 | - |
| 60-70 | -14.0 | 0.658 | 57.67% | 62.54% | 12/12.0/12/12 | 0 | 8.9 | - |
| all | - | 1.090 | 60.76% | 60.44% | 8/10.0/12/12 | 1 | 8.7 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 2, start-up 0, total 2
- node2: loaded 1, start-up 0, total 1

## CPU (% of one core)

- node1: main 2, workers 7 x sum 265, total 267
- node2: main 3, workers 7 x sum 223, total 225
