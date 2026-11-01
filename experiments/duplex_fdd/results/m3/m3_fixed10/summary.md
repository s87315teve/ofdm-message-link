# Duplex run experiments/duplex_fdd/results/m3/m3_fixed10

- duration 100.5 s, git 6ba386898e, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 10, '2': 10}, gain profile {'1': '0:-5,30:-20,60:-5', '2': None}, workers 3
- loadavg at start: 2.96 3.08 2.95 6/2660 823173

## AB: node1 -> node2

udp_load: sent 49950, delivered 27901, lost 22049 (44.14%), corrupt 0, goodput 2.232 Mb/s, latency p50/p99 912/992 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 1.913 | 44.48% | 0.00% | 0/5.0/10/10 | 1 | 22.8 | - |
| 30-60 | -20.0 | 2.142 | 46.39% | 4.13% | 10/10.0/10/10 | 0 | 9.6 | - |
| 60-101 | -5.0 | 1.956 | 41.91% | 0.05% | 10/10.0/10/10 | 0 | 22.8 | - |
| all | - | 1.999 | 44.14% | 1.38% | 0/5.0/10/10 | 1 | 18.6 | - |

| gain step | at s | convergence s | frames lost in first 1 s |
|---|---|---|---|
| -5 -> -20 dB | 30 | - | 13 |
| -20 -> -5 dB | 60 | - | 6 |

## BA: node2 -> node1

udp_load: sent 12510, delivered 12382, lost 128 (1.02%), corrupt 0, goodput 0.991 Mb/s, latency p50/p99 55/78 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.832 | 0.00% | 1.45% | 8/9.0/10/10 | 1 | 17.5 | - |
| 30-60 | -5.0 | 0.995 | 0.53% | 0.60% | 10/10.0/10/10 | 0 | 17.5 | - |
| 60-101 | -5.0 | 0.848 | 2.61% | 1.08% | 10/10.0/10/10 | 0 | 17.4 | - |
| all | - | 0.887 | 1.02% | 1.02% | 8/9.0/10/10 | 1 | 17.5 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 3, workers 7 x sum 119, total 121
- node2: main 3, workers 7 x sum 118, total 121
