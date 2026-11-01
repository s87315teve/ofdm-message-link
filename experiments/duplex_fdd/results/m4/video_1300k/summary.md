# Duplex run experiments/duplex_fdd/results/m4/video_1300k

- duration 77.5 s, git e31f4dc519, LA {'1': 'olla', '2': 'olla'}, MCS {'1': 8, '2': 8}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 0.28 1.13 2.21 2/2606 926128

## AB: node1 -> node2

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 1.071 | 4.03% | 0.02% | 8/11.5/17/17 | 3 | 23.1 | 3.0 |
| 30-60 | -5.0 | 1.437 | 0.82% | 0.00% | 17/17.0/17/17 | 0 | 23.1 | 3.0 |
| 60-78 | -5.0 | 0.648 | 0.00% | 0.10% | 17/17.0/17/17 | 0 | 23.0 | 3.0 |
| all | - | 1.117 | 0.02% | 0.02% | 8/11.5/17/17 | 3 | 23.1 | 3.0 |

## BA: node2 -> node1

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 1.122 | 0.00% | 1.39% | 8/9.0/15/10 | 34 | 16.8 | -4.6 |
| 30-60 | -5.0 | 1.433 | 0.18% | 1.04% | 8/10.0/13/10 | 58 | 16.8 | -4.4 |
| 60-78 | -5.0 | 0.508 | 15.19% | 0.79% | 9/10.0/12/12 | 11 | 16.9 | -3.0 |
| all | - | 1.104 | 1.15% | 1.15% | 8/10.0/15/12 | 103 | 16.8 | -4.1 |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 1, start-up 0, total 1
- node2: loaded 0, start-up 0, total 0

## CPU (% of one core)

- node1: main 2, workers 7 x sum 100, total 102
- node2: main 2, workers 7 x sum 113, total 115
