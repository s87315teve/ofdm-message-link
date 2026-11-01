# Duplex run m3b/m3b_olla_run1

- duration 100.5 s, git 50fb42fc0b, LA {'1': 'olla', '2': 'olla'}, MCS {'1': 8, '2': 8}, gain profile {'1': '0:-5,30:-20,60:-5', '2': None}, workers 3
- loadavg at start: 1.69 2.29 2.27 4/2653 1117669

## AB: node1 -> node2

udp_load: sent 49950, delivered 40574, lost 9376 (18.77%), corrupt 0, goodput 3.246 Mb/s, latency p50/p99 71/1232 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 3.486 | 0.00% | 0.01% | 0/8.0/17/17 | 2 | 23.4 | 3.0 |
| 30-60 | -20.0 | 1.740 | 56.54% | 2.40% | 8/9.5/17/10 | 33 | 9.7 | 1.6 |
| 60-101 | -5.0 | 3.342 | 1.17% | 0.01% | 9/12.0/17/17 | 4 | 23.3 | 3.0 |
| all | - | 2.907 | 18.77% | 0.45% | 0/9.5/17/17 | 39 | 18.7 | 3.0 |

| gain step | at s | convergence s | frames lost in first 1 s |
|---|---|---|---|
| -5 -> -20 dB | 30 | - | 108 |
| -20 -> -5 dB | 60 | - | 2 |

## BA: node2 -> node1

udp_load: sent 12510, delivered 12374, lost 136 (1.09%), corrupt 0, goodput 0.990 Mb/s, latency p50/p99 58/265 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.819 | 0.50% | 1.16% | 8/12.0/15/10 | 23 | 17.5 | -3.3 |
| 30-60 | -5.0 | 0.985 | 2.17% | 0.94% | 9/12.0/13/13 | 26 | 17.6 | -2.8 |
| 60-101 | -5.0 | 0.864 | 0.57% | 1.16% | 9/10.0/13/10 | 29 | 17.4 | -3.9 |
| all | - | 0.886 | 1.09% | 1.09% | 8/10.0/15/10 | 78 | 17.5 | -3.3 |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 1, start-up 0, total 1
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 3, workers 7 x sum 130, total 133
- node2: main 3, workers 7 x sum 157, total 161
