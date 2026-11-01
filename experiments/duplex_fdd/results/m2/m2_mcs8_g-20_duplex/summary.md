# Duplex run m2/m2_mcs8_g-20_duplex

- duration 70.5 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 8, '2': 8}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 3.67 3.78 3.28 4/2611 706334

## AB: node1 -> node2

udp_load: sent 10800, delivered 9665, lost 1135 (10.51%), corrupt 0, goodput 1.160 Mb/s, latency p50/p99 1766/1843 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -20.0 | 0.973 | 12.85% | 0.00% | 8/8.0/8/8 | 1 | 9.6 | - |
| 30-60 | -20.0 | 1.123 | 13.33% | 0.00% | 8/8.0/8/8 | 0 | 9.6 | - |
| 60-71 | -20.0 | 0.638 | 0.00% | 0.00% | 8/8.0/8/8 | 0 | 9.6 | - |
| all | - | 0.987 | 10.51% | 0.00% | 8/8.0/8/8 | 1 | 9.6 | - |

## BA: node2 -> node1

udp_load: sent 10800, delivered 8991, lost 1809 (16.75%), corrupt 0, goodput 1.079 Mb/s, latency p50/p99 1802/1886 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -14.0 | 0.882 | 17.67% | 4.77% | 8/8.0/8/8 | 1 | 8.4 | - |
| 30-60 | -14.0 | 1.045 | 19.14% | 5.51% | 8/8.0/8/8 | 0 | 8.3 | - |
| 60-71 | -14.0 | 0.659 | 0.00% | 3.90% | 8/8.0/8/8 | 0 | 8.5 | - |
| all | - | 0.918 | 16.75% | 5.04% | 8/8.0/8/8 | 1 | 8.4 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 1, start-up 0, total 1

## CPU (% of one core)

- node1: main 3, workers 7 x sum 104, total 106
- node2: main 3, workers 7 x sum 103, total 106
