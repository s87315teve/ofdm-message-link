# Duplex run experiments/duplex_fdd/results/m3/m3_olla_ba_idle

- duration 100.5 s, git 6ba386898e, LA {'1': 'olla', '2': 'olla'}, MCS {'1': 8, '2': 8}, gain profile {'1': '0:-5,30:-20,60:-5', '2': None}, workers 3
- loadavg at start: 3.88 3.06 2.90 2/2603 800242

## AB: node1 -> node2

udp_load: sent 49950, delivered 40132, lost 9818 (19.66%), corrupt 0, goodput 3.211 Mb/s, latency p50/p99 61/1647 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 3.278 | 4.68% | 0.00% | 0/12.0/17/17 | 3 | 23.4 | 3.0 |
| 30-60 | -20.0 | 1.751 | 56.11% | 3.62% | 8/9.0/17/8 | 31 | 9.5 | 1.2 |
| 60-101 | -5.0 | 3.410 | 0.00% | 0.06% | 8/13.5/17/17 | 3 | 23.3 | 3.0 |
| all | - | 2.875 | 19.66% | 0.71% | 0/9.0/17/17 | 37 | 18.7 | 3.0 |

| gain step | at s | convergence s | frames lost in first 1 s |
|---|---|---|---|
| -5 -> -20 dB | 30 | - | 0 |
| -20 -> -5 dB | 60 | - | 11 |

## BA: node2 -> node1

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 17.7 | 0.0 |
| 30-60 | -5.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 17.9 | 0.0 |
| 60-101 | -5.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 17.6 | 0.0 |
| all | - | 0.000 | - | - | 8/8.0/8/8 | 0 | 17.7 | 0.0 |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 1, start-up 0, total 1
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 2, workers 7 x sum 97, total 99
- node2: main 3, workers 7 x sum 144, total 147
