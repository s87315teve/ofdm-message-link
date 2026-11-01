# Duplex run experiments/duplex_fdd/results/m3/m3_fixed12

- duration 100.5 s, git 6ba386898e, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 12, '2': 12}, gain profile {'1': '0:-5,30:-20,60:-5', '2': None}, workers 3
- loadavg at start: 3.78 3.30 3.04 5/2617 834584

## AB: node1 -> node2

udp_load: sent 49950, delivered 36629, lost 13321 (26.67%), corrupt 0, goodput 2.930 Mb/s, latency p50/p99 664/718 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 2.755 | 19.45% | 0.00% | 0/6.0/12/12 | 1 | 22.8 | - |
| 30-60 | -20.0 | 2.407 | 40.02% | 25.18% | 12/12.0/12/12 | 0 | 9.6 | - |
| 60-101 | -5.0 | 2.688 | 20.32% | 0.13% | 12/12.0/12/12 | 0 | 22.8 | - |
| all | - | 2.624 | 26.67% | 8.48% | 0/6.0/12/12 | 1 | 18.8 | - |

| gain step | at s | convergence s | frames lost in first 1 s |
|---|---|---|---|
| -5 -> -20 dB | 30 | - | 94 |
| -20 -> -5 dB | 60 | - | 20 |

## BA: node2 -> node1

udp_load: sent 12510, delivered 12259, lost 251 (2.01%), corrupt 0, goodput 0.981 Mb/s, latency p50/p99 57/82 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.820 | 2.95% | 1.98% | 8/10.0/12/12 | 1 | 17.8 | - |
| 30-60 | -5.0 | 0.983 | 1.73% | 2.24% | 12/12.0/12/12 | 0 | 17.8 | - |
| 60-101 | -5.0 | 0.844 | 1.56% | 1.82% | 12/12.0/12/12 | 0 | 17.8 | - |
| all | - | 0.878 | 2.01% | 2.01% | 8/10.0/12/12 | 1 | 17.8 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 1, start-up 0, total 1
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 3, workers 7 x sum 133, total 136
- node2: main 3, workers 7 x sum 173, total 176
