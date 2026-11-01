# Duplex run experiments/duplex_fdd/results/m2/m2_mcs12_g-20_oneway_BA

- duration 71.0 s, git 16af5b940b, LA {'1': 'fixed', '2': 'fixed'}, MCS {'1': 12, '2': 12}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 3.75 3.78 3.24 2/2619 698398

## AB: node1 -> node2

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -20.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 10.1 | - |
| 30-60 | -20.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 10.2 | - |
| 60-71 | -20.0 | 0.000 | - | - | 8/8.0/8/8 | 0 | 10.1 | - |
| all | - | 0.000 | - | - | 8/8.0/8/8 | 0 | 10.1 | - |

## BA: node2 -> node1

udp_load: sent 27000, delivered 10798, lost 16202 (60.01%), corrupt 0, goodput 1.296 Mb/s, latency p50/p99 96/156 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -14.0 | 1.097 | 59.20% | 60.20% | 8/10.0/12/12 | 1 | 8.8 | - |
| 30-60 | -14.0 | 1.314 | 59.39% | 59.52% | 12/12.0/12/12 | 0 | 8.7 | - |
| 60-71 | -14.0 | 0.492 | 67.54% | 62.08% | 12/12.0/12/12 | 0 | 9.1 | - |
| all | - | 1.095 | 60.01% | 60.00% | 8/10.0/12/12 | 1 | 8.8 | - |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 2, workers 7 x sum 249, total 251
- node2: main 2, workers 7 x sum 84, total 86
