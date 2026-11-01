# Duplex run m2/m2_mcs12_g-12.5_duplex

- duration 71.0 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 12, '2': 12}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 3.17 3.11 2.84 5/2636 654912

## AB: node1 -> node2

udp_load: sent 27000, delivered 26994, lost 6 (0.02%), corrupt 0, goodput 3.239 Mb/s, latency p50/p99 235/374 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -12.5 | 2.803 | 0.00% | 0.05% | 8/10.0/12/12 | 1 | 16.4 | - |
| 30-60 | -12.5 | 3.249 | 0.00% | 0.00% | 12/12.0/12/12 | 0 | 16.4 | - |
| 60-71 | -12.5 | 1.164 | 3.20% | 0.00% | 12/12.0/12/12 | 0 | 16.5 | - |
| all | - | 2.737 | 0.02% | 0.02% | 8/10.0/12/12 | 1 | 16.4 | - |

## BA: node2 -> node1

udp_load: sent 27000, delivered 26100, lost 900 (3.33%), corrupt 0, goodput 3.132 Mb/s, latency p50/p99 585/698 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -6.5 | 2.521 | 6.06% | 3.87% | 8/10.0/12/12 | 1 | 15.6 | - |
| 30-60 | -6.5 | 3.133 | 3.30% | 2.42% | 12/12.0/12/12 | 0 | 15.7 | - |
| 60-71 | -6.5 | 1.661 | 0.00% | 1.89% | 12/12.0/12/12 | 0 | 15.8 | - |
| all | - | 2.647 | 3.33% | 2.96% | 8/10.0/12/12 | 1 | 15.7 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 1, start-up 0, total 1
- node2: loaded 1, start-up 0, total 1

## CPU (% of one core)

- node1: main 3, workers 7 x sum 155, total 158
- node2: main 3, workers 7 x sum 151, total 154
