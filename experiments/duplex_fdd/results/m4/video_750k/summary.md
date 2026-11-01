# Duplex run experiments/duplex_fdd/results/m4/video_750k

- duration 179.5 s, git 6ba386898e, LA {'1': 'olla', '2': 'olla'}, MCS {'1': 8, '2': 8}, gain profile {'1': None, '2': None}, workers 3
- loadavg at start: 2.93 3.71 3.31 6/2611 861373

## AB: node1 -> node2

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.614 | 4.67% | 0.00% | 8/11.5/17/17 | 3 | 23.3 | 3.0 |
| 30-60 | -5.0 | 0.835 | 0.05% | 0.02% | 17/17.0/17/17 | 0 | 23.3 | 3.0 |
| 60-90 | -5.0 | 0.214 | 0.00% | 0.00% | 17/17.0/17/17 | 0 | 23.3 | 3.0 |
| 90-120 | -5.0 | 0.000 | - | - | 17/17.0/17/17 | 0 | 23.4 | 3.0 |
| 120-150 | -5.0 | 0.000 | - | - | 17/17.0/17/17 | 0 | 23.5 | 3.0 |
| 150-180 | -5.0 | 0.000 | - | - | 17/17.0/17/17 | 0 | 23.5 | 3.0 |
| all | - | 0.278 | 0.01% | 0.01% | 8/11.5/17/17 | 3 | 23.3 | 3.0 |

## BA: node2 -> node1

| segment (s) | TX gain | goodput Mb/s | dgram loss | subframe loss | MCS min/med/max/final | MCS changes | SNR dB | offset dB |
|---|---|---|---|---|---|---|---|---|
| 0-30 | -5.0 | 0.646 | 0.00% | 1.41% | 8/10.0/15/10 | 39 | 17.0 | -3.1 |
| 30-60 | -5.0 | 0.829 | 0.90% | 0.97% | 8/10.0/12/10 | 45 | 16.8 | -4.4 |
| 60-90 | -5.0 | 0.170 | 13.77% | 1.01% | 10/12.0/13/10 | 8 | 17.0 | -3.2 |
| 90-120 | -5.0 | 0.000 | - | - | 10/10.0/10/10 | 0 | 17.7 | -3.2 |
| 120-150 | -5.0 | 0.000 | - | - | 10/10.0/10/10 | 0 | 17.5 | -3.2 |
| 150-180 | -5.0 | 0.000 | - | - | 10/10.0/10/10 | 0 | 17.5 | -3.2 |
| all | - | 0.275 | 1.15% | 1.15% | 8/10.0/15/10 | 92 | 17.0 | -3.2 |

## RX overflow (events; start-up = first 3 s)

- node1: loaded 0, start-up 0, total 0
- node2: loaded 1, start-up 0, total 1

## CPU (% of one core)

- node1: main -, workers 0 x sum 0, total 0
- node2: main -, workers 0 x sum 0, total 0
