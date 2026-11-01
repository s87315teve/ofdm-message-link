# Duplex run m2/m2_mcs8_g-20_oneway_BA

- duration 70.5 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 8, '2': 8}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 3.02 3.40 3.20 4/2613 722474

## AB: node1 -> node2

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -20.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 10.4 | - |
| 30-60 | -20.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 10.3 | - |
| 60-71 | -20.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 10.1 | - |
| all | - | 0.000 | - | - | 8/8.0/8/8 | 0 | 10.3 | - |

## BA: node2 -> node1

udp_load: sent 10800, delivered 9358, lost 1442 (13.35%), corrupt 0, goodput 1.123 Mb/s, latency p50/p99 1719/1783 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -14.0 | 0.903 | 15.71% | 4.71% | 8/8.0/8/8 | 1 | 8.6 | - |
| 30-60 | -14.0 | 1.090 | 15.82% | 5.65% | 8/8.0/8/8 | 0 | 8.5 | - |
| 60-71 | -14.0 | 0.725 | 0.00% | 5.96% | 8/8.0/8/8 | 0 | 8.7 | - |
| all | - | 0.956 | 13.35% | 5.31% | 8/8.0/8/8 | 1 | 8.6 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 1, start-up 0, total 1

## CPU (% of one core)

- node1: main 3, workers 7 x sum 90, total 93
- node2: main 2, workers 7 x sum 92, total 94
