# Duplex run m2/smoke2

- duration 30.5 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 8, '2': 8}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 1.00 2.36 2.56 2/2614 622561

## AB: node1 -> node2

udp_load: sent 2000, delivered 2000, lost 0 (0.00%), corrupt 0, goodput 0.720 Mb/s, latency p50/p99 61/93 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -20.0 | 0.480 | 0.00% | 0.00% | 8/8.0/8/8 | 1 | 9.6 | - |
| 30-31 | -20.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | - | - |
| all | - | 0.472 | 0.00% | 0.00% | 8/8.0/8/8 | 1 | 9.6 | - |

## BA: node2 -> node1

udp_load: sent 2000, delivered 1898, lost 102 (5.10%), corrupt 0, goodput 0.684 Mb/s, latency p50/p99 63/106 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -14.0 | 0.456 | 5.10% | 5.10% | 8/8.0/8/8 | 1 | 8.4 | - |
| 30-31 | -14.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 9.9 | - |
| all | - | 0.448 | 5.10% | 5.10% | 8/8.0/8/8 | 1 | 8.4 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 2, workers 7 x sum 90, total 92
- node2: main 2, workers 7 x sum 92, total 94
