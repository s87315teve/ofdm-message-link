# Duplex run m2/cap_mcs8_cap1.0

- duration 40.5 s, git e31f4dc519, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 8, '2': 8}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 2.46 1.81 2.32 4/2617 942301

## AB: node1 -> node2

udp_load: sent 5400, delivered 4987, lost 413 (7.65%), corrupt 0, goodput 1.197 Mb/s, latency p50/p99 1750/1807 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.991 | 11.24% | 0.00% | 8/8.0/8/8 | 1 | 23.0 | - |
| 30-41 | -5.0 | 0.588 | 0.00% | 0.00% | 8/8.0/8/8 | 0 | 23.2 | - |
| all | - | 0.887 | 7.65% | 0.00% | 8/8.0/8/8 | 1 | 23.0 | - |

## BA: node2 -> node1

udp_load: sent 5400, delivered 4859, lost 541 (10.02%), corrupt 0, goodput 1.166 Mb/s, latency p50/p99 1789/1875 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.929 | 14.92% | 0.00% | 8/8.0/8/8 | 1 | 16.9 | - |
| 30-41 | -5.0 | 0.677 | 0.00% | 0.00% | 8/8.0/8/8 | 0 | 16.9 | - |
| all | - | 0.864 | 10.02% | 0.00% | 8/8.0/8/8 | 1 | 16.9 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 1, start-up 0, total 1
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 2, workers 7 x sum 99, total 102
- node2: main 3, workers 7 x sum 100, total 103
