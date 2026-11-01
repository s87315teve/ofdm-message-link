# Duplex run m2/m2_mcs8_g-20_oneway_AB

- duration 70.5 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 8, '2': 8}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 2.84 3.50 3.22 3/2644 714406

## AB: node1 -> node2

udp_load: sent 10800, delivered 10165, lost 635 (5.88%), corrupt 0, goodput 1.220 Mb/s, latency p50/p99 1670/1751 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -20.0 | 1.033 | 7.44% | 0.00% | 8/8.0/8/8 | 1 | 9.6 | - |
| 30-60 | -20.0 | 1.188 | 8.25% | 0.04% | 8/8.0/8/8 | 0 | 9.6 | - |
| 60-71 | -20.0 | 0.624 | 0.00% | 0.00% | 8/8.0/8/8 | 0 | 9.4 | - |
| all | - | 1.038 | 5.88% | 0.02% | 8/8.0/8/8 | 1 | 9.6 | - |

## BA: node2 -> node1

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -14.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 9.1 | - |
| 30-60 | -14.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 9.2 | - |
| 60-71 | -14.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 9.1 | - |
| all | - | 0.000 | - | - | 8/8.0/8/8 | 0 | 9.2 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 1, start-up 0, total 1
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 2, workers 7 x sum 91, total 93
- node2: main 3, workers 7 x sum 91, total 94
