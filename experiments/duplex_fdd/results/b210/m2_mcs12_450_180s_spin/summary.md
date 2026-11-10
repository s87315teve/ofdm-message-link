# Duplex run experiments/duplex_fdd/results/b210/m2_mcs12_450_180s_spin

- duration 193.5 s, git 3abca7ea61, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 12, '2': 12}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 0.48 0.68 1.59 2/2863 3167522

## AB: node1 -> node2

udp_load: sent 81000, delivered 61067, lost 19933 (24.61%), corrupt 0, goodput 2.443 Mb/s, latency p50/p99 399/475 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | 68.0 | 2.120 | 23.72% | 24.03% | 8/10.0/12/12 | 1 | 15.3 | - |
| 30-60 | 68.0 | 2.464 | 24.09% | 23.91% | 12/12.0/12/12 | 0 | 15.4 | - |
| 60-90 | 68.0 | 2.406 | 25.88% | 25.91% | 12/12.0/12/12 | 0 | 15.9 | - |
| 90-120 | 68.0 | 2.457 | 24.35% | 24.30% | 12/12.0/12/12 | 0 | 16.3 | - |
| 120-150 | 68.0 | 2.434 | 24.95% | 25.05% | 12/12.0/12/12 | 0 | 16.2 | - |
| 150-180 | 68.0 | 2.413 | 24.97% | 24.99% | 12/12.0/12/12 | 0 | 16.5 | - |
| 180-194 | 68.0 | 0.804 | 21.62% | 20.13% | 12/12.0/12/12 | 0 | 16.7 | - |
| all | - | 2.272 | 24.61% | 24.61% | 8/10.0/12/12 | 1 | 16.0 | - |

## BA: node2 -> node1

udp_load: sent 81000, delivered 56953, lost 24047 (29.69%), corrupt 0, goodput 2.278 Mb/s, latency p50/p99 415/493 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | 68.0 | 1.690 | 29.99% | 28.90% | 8/10.0/12/12 | 1 | 25.0 | - |
| 30-60 | 68.0 | 2.308 | 28.31% | 28.30% | 12/12.0/12/12 | 0 | 24.9 | - |
| 60-90 | 68.0 | 2.281 | 29.73% | 29.65% | 12/12.0/12/12 | 0 | 25.0 | - |
| 90-120 | 68.0 | 2.282 | 29.68% | 29.72% | 12/12.0/12/12 | 0 | 24.8 | - |
| 120-150 | 68.0 | 2.257 | 30.39% | 30.50% | 12/12.0/12/12 | 0 | 25.1 | - |
| 150-180 | 68.0 | 2.252 | 30.60% | 30.53% | 12/12.0/12/12 | 0 | 24.9 | - |
| 180-194 | 68.0 | 1.330 | 27.68% | 30.83% | 12/12.0/12/12 | 0 | 25.2 | - |
| all | - | 2.119 | 29.69% | 29.69% | 8/10.0/12/12 | 1 | 25.0 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 1800, start-up 1, total 1801
- node2: loaded 1570, start-up 0, total 1570

## CPU (% of one core)

- node1: main 3, workers 7 x sum 184, total 187
- node2: main 3, workers 7 x sum 188, total 191
