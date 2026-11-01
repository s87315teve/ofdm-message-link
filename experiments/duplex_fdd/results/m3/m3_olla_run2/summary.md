# Duplex run experiments/duplex_fdd/results/m3/m3_olla_run2

- duration 100.5 s, git 6ba386898e, LA {'1': 'olla', '2': 'olla'}, MCS {'1': 8, '2': 8}, gain profile {'1': '0:-5,30:-20,60:-5', '2': None}, workers 3
- loadavg at start: 3.77 2.63 2.76 3/2615 788406

## AB: node1 -> node2

udp_load: sent 49950, delivered 40401, lost 9549 (19.12%), corrupt 0, goodput 3.232 Mb/s, latency p50/p99 72/1266 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 3.472 | 0.00% | 0.07% | 0/15.5/17/17 | 11 | 23.2 | 3.0 |
| 30-60 | -20.0 | 1.733 | 56.71% | 3.49% | 8/9.0/17/9 | 30 | 9.5 | 1.8 |
| 60-101 | -5.0 | 3.327 | 1.26% | 0.02% | 9/14.0/17/17 | 3 | 23.1 | 3.0 |
| all | - | 2.894 | 19.12% | 0.67% | 0/10.0/17/17 | 44 | 18.5 | 3.0 |

| gain step | at s | convergence s | frames lost in first 1 s |
|---|---|---|---|
| -5 -> -20 dB | 30 | - | 188 |
| -20 -> -5 dB | 60 | - | 0 |

## BA: node2 -> node1

udp_load: sent 12510, delivered 12375, lost 135 (1.08%), corrupt 0, goodput 0.990 Mb/s, latency p50/p99 58/128 ms

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.813 | 1.97% | 1.17% | 8/12.0/15/13 | 37 | 17.5 | -3.0 |
| 30-60 | -5.0 | 0.993 | 0.53% | 1.00% | 10/12.0/13/12 | 25 | 17.7 | -2.9 |
| 60-101 | -5.0 | 0.862 | 0.92% | 1.08% | 9/10.0/13/10 | 31 | 17.6 | -4.8 |
| all | - | 0.887 | 1.08% | 1.08% | 8/10.0/15/10 | 93 | 17.6 | -3.2 |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 2, start-up 0, total 2

## CPU (% of one core)

- node1: main 3, workers 7 x sum 130, total 133
- node2: main 3, workers 7 x sum 157, total 160
