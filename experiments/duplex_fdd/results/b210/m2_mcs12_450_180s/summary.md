# Duplex run experiments/duplex_fdd/results/b210/m2_mcs12_450_180s

- duration 193.5 s, git 3abca7ea61, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 12, '2': 12}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 2.84 3.40 2.88 2/2840 3244348

## AB: node1 -> node2

udp_load: sent 81000, delivered 79563, lost 1437 (1.77%), corrupt 0, goodput 3.183 Mb/s, latency p50/p99 96/128 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | 68.0 | 2.763 | 0.39% | 2.03% | 0/6.0/12/12 | 1 | 16.8 | - |
| 30-60 | 68.0 | 3.183 | 1.94% | 2.20% | 12/12.0/12/12 | 0 | 17.1 | - |
| 60-90 | 68.0 | 3.156 | 2.84% | 1.97% | 12/12.0/12/12 | 0 | 17.2 | - |
| 90-120 | 68.0 | 3.196 | 1.52% | 1.76% | 12/12.0/12/12 | 0 | 17.4 | - |
| 120-150 | 68.0 | 3.175 | 1.50% | 1.48% | 12/12.0/12/12 | 0 | 17.4 | - |
| 150-180 | 68.0 | 3.209 | 1.17% | 1.28% | 12/12.0/12/12 | 0 | 17.6 | - |
| 180-194 | 68.0 | 0.916 | 9.54% | 1.32% | 12/12.0/12/12 | 0 | 17.9 | - |
| all | - | 2.960 | 1.77% | 1.77% | 0/6.0/12/12 | 1 | 17.3 | - |

## BA: node2 -> node1

udp_load: sent 81000, delivered 79140, lost 1860 (2.30%), corrupt 0, goodput 3.166 Mb/s, latency p50/p99 98/130 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | 68.0 | 2.351 | 1.56% | 2.25% | 8/10.0/12/12 | 1 | 25.2 | - |
| 30-60 | 68.0 | 3.146 | 3.41% | 2.30% | 12/12.0/12/12 | 0 | 25.3 | - |
| 60-90 | 68.0 | 3.169 | 1.65% | 2.42% | 12/12.0/12/12 | 0 | 25.3 | - |
| 90-120 | 68.0 | 3.171 | 2.48% | 2.38% | 12/12.0/12/12 | 0 | 25.3 | - |
| 120-150 | 68.0 | 3.179 | 2.20% | 2.14% | 12/12.0/12/12 | 0 | 25.2 | - |
| 150-180 | 68.0 | 3.145 | 2.50% | 2.30% | 12/12.0/12/12 | 0 | 25.4 | - |
| 180-194 | 68.0 | 1.851 | 1.48% | 2.25% | 12/12.0/12/12 | 0 | 25.2 | - |
| all | - | 2.945 | 2.30% | 2.30% | 8/10.0/12/12 | 1 | 25.3 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 3, workers 7 x sum 191, total 195
- node2: main 3, workers 7 x sum 192, total 195
