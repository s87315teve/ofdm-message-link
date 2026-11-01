# Duplex run experiments/duplex_fdd/results/m2/cap_mcs8_cap0.7

- duration 41.0 s, git e31f4dc519, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 8, '2': 8}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 2.05 1.64 2.29 2/2654 937464

## AB: node1 -> node2

udp_load: sent 5400, delivered 4936, lost 464 (8.59%), corrupt 0, goodput 1.185 Mb/s, latency p50/p99 1747/1875 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.924 | 16.62% | 0.00% | 8/8.0/8/8 | 1 | 23.2 | - |
| 30-41 | -5.0 | 0.712 | 0.00% | 0.00% | 8/8.0/8/8 | 0 | 23.4 | - |
| all | - | 0.867 | 8.59% | 0.00% | 8/8.0/8/8 | 1 | 23.2 | - |

## BA: node2 -> node1

udp_load: sent 5400, delivered 4856, lost 544 (10.07%), corrupt 0, goodput 1.166 Mb/s, latency p50/p99 1796/1893 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.937 | 12.53% | 0.03% | 8/8.0/8/8 | 1 | 17.3 | - |
| 30-41 | -5.0 | 0.624 | 0.00% | 0.00% | 8/8.0/8/8 | 0 | 17.3 | - |
| all | - | 0.853 | 10.07% | 0.02% | 8/8.0/8/8 | 1 | 17.3 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 1, start-up 0, total 1
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 2, workers 7 x sum 99, total 102
- node2: main 2, workers 7 x sum 99, total 102
