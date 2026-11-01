# Duplex run experiments/duplex_fdd/results/m3/m3_fixed8

- duration 101.0 s, git 6ba386898e, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 8, '2': 8}, gain profile {'1': '0:-5,30:-20,60:-5', '2': None}, workers 3
- loadavg at start: 3.41 3.20 2.98 2/2617 811695

## AB: node1 -> node2

udp_load: sent 49950, delivered 14335, lost 35615 (71.30%), corrupt 0, goodput 1.147 Mb/s, latency p50/p99 1780/1857 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.987 | 71.29% | 0.00% | 0/4.0/8/8 | 1 | 23.1 | - |
| 30-60 | -20.0 | 1.123 | 71.92% | 0.00% | 8/8.0/8/8 | 0 | 9.7 | - |
| 60-101 | -5.0 | 0.973 | 70.76% | 0.00% | 8/8.0/8/8 | 0 | 23.1 | - |
| all | - | 1.022 | 71.30% | 0.00% | 0/4.0/8/8 | 1 | 18.7 | - |

| gain step | at s | convergence s | frames lost in first 1 s |
|---|---|---|---|
| -5 -> -20 dB | 30 | 0 | 0 |
| -20 -> -5 dB | 60 | - | 0 |

## BA: node2 -> node1

udp_load: sent 12510, delivered 12510, lost 0 (0.00%), corrupt 0, goodput 1.001 Mb/s, latency p50/p99 60/136 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.824 | 0.20% | 0.00% | 8/8.0/8/8 | 1 | 17.0 | - |
| 30-60 | -5.0 | 1.002 | 0.00% | 0.00% | 8/8.0/8/8 | 0 | 17.1 | - |
| 60-101 | -5.0 | 0.861 | 0.14% | 0.00% | 8/8.0/8/8 | 0 | 17.1 | - |
| all | - | 0.892 | 0.00% | 0.00% | 8/8.0/8/8 | 1 | 17.1 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 3, workers 7 x sum 108, total 111
- node2: main 3, workers 7 x sum 103, total 106
