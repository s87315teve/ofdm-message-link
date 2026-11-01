# Duplex run experiments/duplex_fdd/results/m3/m3_olla_run1

- duration 98.5 s, git 073ca15e2a, LA {'1': 'olla', '2': 'olla'}, MCS {'1': 8, '2': 8}, gain profile {'1': '0:-5,30:-20,60:-5', '2': None}, workers 3
- loadavg at start: 0.46 2.03 2.71 2/2629 748242

## AB: node1 -> node2

udp_load: sent 49950, delivered 40731, lost 9219 (18.46%), corrupt 0, goodput 3.259 Mb/s, latency p50/p99 67/1232 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 3.302 | 3.64% | 0.00% | 0/12.0/17/17 | 3 | 23.3 | 3.0 |
| 30-60 | -20.0 | 1.882 | 52.91% | 3.08% | 8/9.0/17/9 | 36 | 9.6 | 1.7 |
| 60-99 | -5.0 | 3.578 | 0.00% | 0.03% | 9/15.0/17/17 | 4 | 23.2 | 3.0 |
| all | - | 2.977 | 18.46% | 0.62% | 0/9.0/17/17 | 43 | 18.7 | 3.0 |

| gain step | at s | convergence s | frames lost in first 1 s |
|---|---|---|---|
| -5 -> -20 dB | 30 | 29 | 0 |
| -20 -> -5 dB | 60 | - | 5 |

## BA: node2 -> node1

udp_load: sent 12510, delivered 12374, lost 136 (1.09%), corrupt 0, goodput 0.990 Mb/s, latency p50/p99 60/96 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.845 | 0.00% | 1.29% | 8/10.0/15/9 | 47 | 17.6 | -3.6 |
| 30-60 | -5.0 | 0.991 | 0.65% | 0.98% | 8/10.0/12/10 | 41 | 17.5 | -4.3 |
| 60-99 | -5.0 | 0.883 | 3.57% | 1.03% | 8/10.0/12/10 | 46 | 17.4 | -3.6 |
| all | - | 0.904 | 1.09% | 1.09% | 8/10.0/15/10 | 134 | 17.5 | -3.9 |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 3, workers 7 x sum 121, total 123
- node2: main 3, workers 7 x sum 156, total 160
