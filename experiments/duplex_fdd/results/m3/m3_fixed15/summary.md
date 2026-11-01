# Duplex run experiments/duplex_fdd/results/m3/m3_fixed15

- duration 100.5 s, git 6ba386898e, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 15, '2': 15}, gain profile {'1': '0:-5,30:-20,60:-5', '2': None}, workers 3
- loadavg at start: 3.87 3.57 3.17 1/2636 846121

## AB: node1 -> node2

udp_load: sent 49950, delivered 33254, lost 16696 (33.43%), corrupt 0, goodput 2.660 Mb/s, latency p50/p99 78/370 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 3.498 | 0.00% | 0.00% | 0/7.5/15/15 | 1 | 22.9 | - |
| 30-60 | -20.0 | 0.017 | 99.57% | 0.00% | 15/15.0/15/15 | 0 | 10.2 | - |
| 60-101 | -5.0 | 3.308 | 1.96% | 0.18% | 15/15.0/15/15 | 0 | 22.8 | - |
| all | - | 2.382 | 33.43% | 0.10% | 0/7.5/15/15 | 1 | 22.1 | - |

| gain step | at s | convergence s | frames lost in first 1 s |
|---|---|---|---|
| -5 -> -20 dB | 30 | - | 0 |
| -20 -> -5 dB | 60 | - | 33 |

## BA: node2 -> node1

udp_load: sent 12510, delivered 12209, lost 301 (2.41%), corrupt 0, goodput 0.977 Mb/s, latency p50/p99 61/157 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.820 | 2.82% | 2.06% | 8/11.5/15/15 | 1 | 17.8 | - |
| 30-60 | -5.0 | 0.985 | 1.30% | 1.98% | 15/15.0/15/15 | 0 | 17.9 | - |
| 60-101 | -5.0 | 0.833 | 3.06% | 3.02% | 15/15.0/15/15 | 0 | 17.8 | - |
| all | - | 0.875 | 2.41% | 2.41% | 8/11.5/15/15 | 1 | 17.8 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 42, start-up 0, total 42

## CPU (% of one core)

- node1: main 3, workers 7 x sum 138, total 141
- node2: main 3, workers 7 x sum 224, total 227
