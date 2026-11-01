# Duplex run m2/m2_mcs12_g-12.5_oneway_AB

- duration 70.5 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 12, '2': 12}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 3.58 3.25 2.91 3/2642 663279

## AB: node1 -> node2

udp_load: sent 27000, delivered 27000, lost 0 (0.00%), corrupt 0, goodput 3.240 Mb/s, latency p50/p99 79/143 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -12.5 | 2.828 | 0.00% | 0.00% | 8/10.0/12/12 | 1 | 16.4 | - |
| 30-60 | -12.5 | 3.251 | 0.00% | 0.00% | 12/12.0/12/12 | 0 | 16.4 | - |
| 60-71 | -12.5 | 1.146 | 9.63% | 0.00% | 12/12.0/12/12 | 0 | 16.4 | - |
| all | - | 2.757 | 0.00% | 0.00% | 8/10.0/12/12 | 1 | 16.4 | - |

## BA: node2 -> node1

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -6.5 | 0.000 | - | - | 8/8.0/8/8 | 0 | 16.0 | - |
| 30-60 | -6.5 | 0.000 | - | - | 8/8.0/8/8 | 0 | 16.0 | - |
| 60-71 | -6.5 | 0.000 | - | - | 8/8.0/8/8 | 0 | 16.3 | - |
| all | - | 0.000 | - | - | 8/8.0/8/8 | 0 | 16.1 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 1, start-up 0, total 1
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 2, workers 7 x sum 93, total 95
- node2: main 3, workers 7 x sum 136, total 139
