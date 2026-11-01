# Duplex run m3b/m3b_olla_ba_idle

- duration 100.5 s, git 50fb42fc0b, LA {'1': 'olla', '2': 'olla'}, MCS {'1': 8, '2': 8}, gain profile {'1': '0:-5,30:-20,60:-5', '2': None}, workers 3
- loadavg at start: 3.78 3.08 2.57 1/2648 1130527

## AB: node1 -> node2

udp_load: sent 49950, delivered 41008, lost 8942 (17.90%), corrupt 0, goodput 3.281 Mb/s, latency p50/p99 61/1219 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 3.486 | 0.00% | 0.01% | 0/16.0/17/17 | 4 | 23.4 | 3.0 |
| 30-60 | -20.0 | 1.821 | 54.33% | 2.27% | 8/9.0/17/9 | 28 | 9.6 | 1.5 |
| 60-101 | -5.0 | 3.359 | 0.48% | 0.02% | 9/15.0/17/17 | 4 | 23.4 | 3.0 |
| all | - | 2.938 | 17.90% | 0.44% | 0/10.0/17/17 | 36 | 18.7 | 3.0 |

| gain step | at s | convergence s | frames lost in first 1 s |
|---|---|---|---|
| -5 -> -20 dB | 30 | - | 91 |
| -20 -> -5 dB | 60 | - | 0 |

## BA: node2 -> node1

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 17.5 | 0.0 |
| 30-60 | -5.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 17.8 | 0.0 |
| 60-101 | -5.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 17.8 | 0.0 |
| all | - | 0.000 | - | - | 8/8.0/8/8 | 0 | 17.7 | 0.0 |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 2, workers 7 x sum 97, total 99
- node2: main 3, workers 7 x sum 143, total 146
