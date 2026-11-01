# Duplex run m2/m2_mcs12_g-5_oneway_BA

- duration 71.0 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 12, '2': 12}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 3.70 3.12 2.82 3/2634 645708

## AB: node1 -> node2

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 23.2 | - |
| 30-60 | -5.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 23.1 | - |
| 60-71 | -5.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 23.1 | - |
| all | - | 0.000 | - | - | 8/8.0/8/8 | 0 | 23.2 | - |

## BA: node2 -> node1

udp_load: sent 27000, delivered 26782, lost 218 (0.81%), corrupt 0, goodput 3.214 Mb/s, latency p50/p99 107/231 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | 0.0 | 2.692 | 0.00% | 1.14% | 8/10.0/12/12 | 1 | 21.3 | - |
| 30-60 | 0.0 | 3.205 | 0.22% | 0.65% | 12/12.0/12/12 | 0 | 21.1 | - |
| 60-71 | 0.0 | 1.448 | 8.44% | 0.09% | 12/12.0/12/12 | 0 | 21.2 | - |
| all | - | 2.716 | 0.81% | 0.81% | 8/10.0/12/12 | 1 | 21.2 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 3, workers 7 x sum 136, total 139
- node2: main 2, workers 7 x sum 92, total 94
