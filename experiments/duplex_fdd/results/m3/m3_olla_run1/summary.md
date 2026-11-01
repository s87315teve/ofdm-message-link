# Duplex run experiments/duplex_fdd/results/m3/m3_olla_run1

- duration 100.5 s, git 6ba386898e, LA {'1': 'olla', '2': 'olla'}, MCS {'1': 8, '2': 8}, gain profile {'1': '0:-5,30:-20,60:-5', '2': None}, workers 3
- loadavg at start: 1.04 1.99 2.59 5/2648 777041

## AB: node1 -> node2

udp_load: sent 49950, delivered 40267, lost 9683 (19.39%), corrupt 0, goodput 3.221 Mb/s, latency p50/p99 70/1679 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 3.428 | 0.10% | 0.00% | 0/8.0/17/17 | 2 | 23.2 | 3.0 |
| 30-60 | -20.0 | 1.706 | 57.41% | 3.50% | 8/9.0/17/9 | 30 | 9.5 | 1.8 |
| 60-101 | -5.0 | 3.356 | 0.46% | 0.01% | 9/15.0/17/17 | 4 | 23.1 | 3.0 |
| all | - | 2.885 | 19.39% | 0.64% | 0/9.0/17/17 | 36 | 18.5 | 3.0 |

| gain step | at s | convergence s | frames lost in first 1 s |
|---|---|---|---|
| -5 -> -20 dB | 30 | 29 | 0 |
| -20 -> -5 dB | 60 | - | 0 |

## BA: node2 -> node1

udp_load: sent 12510, delivered 12375, lost 135 (1.08%), corrupt 0, goodput 0.990 Mb/s, latency p50/p99 58/264 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.830 | 0.00% | 1.14% | 8/12.0/15/12 | 30 | 17.6 | -3.0 |
| 30-60 | -5.0 | 0.994 | 1.43% | 1.00% | 8/10.0/12/12 | 24 | 17.5 | -3.9 |
| 60-101 | -5.0 | 0.849 | 1.95% | 1.10% | 9/10.0/12/10 | 27 | 17.5 | -3.9 |
| all | - | 0.887 | 1.08% | 1.08% | 8/10.0/15/10 | 81 | 17.5 | -3.7 |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 2, start-up 0, total 2
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 3, workers 7 x sum 128, total 131
- node2: main 3, workers 7 x sum 157, total 161
