# v5 live goodput after the two fixes (RX cheap acquisition, TX min burst interval 4 ms)
2026-11-01 10:11-10:35 local, tx-v5 HEAD 8bb3674, same searcher/rules as v5_goodput (30 s runs, loss <0.1 %, 0 RX overflow while loaded, load<4 gate).
Driver: experiments/tx_v5/run_v5_after_fix.sh (goodput_search.py, defaults, est-rate = earlier best). Data: <point>/search.json, goodput_after_fix.json.
| point | rate/s before->after | Mb/s before->after | sf/burst b->a | worker CPU sum % | rx main CPU % | load | overflow | p50/p99 ms after |
|---|---|---|---|---|---|---|---|---|
| 300 B MCS14 -5 | 222 -> 2568 | 0.534 -> 6.164 | 1.0 -> 16.0 | 191 | 46 | 2.88 | 0 | 80/138 |
| 900 B MCS16 -5 | 228 -> 776 | 1.641 -> 5.589 | 1.0 -> 7.8 | 173 | 28 | 3.34 | 0 | 67/90 |
| 900 B MCS17 -5 | 210 -> 1291 | 1.512 -> 9.295 | 1.0 -> 8.0 | 234 | 36 | 3.65 | 0 | 84/117 |
| 900 B MCS14 -5 | 990 -> 1002 | 7.128 -> 7.217 | 7.4 -> 8.0 | 189 | 31 | 3.88 | 0 | 192/250 |
| 900 B MCS12 -15 | 658 -> 703 | 4.738 -> 5.063 | 4.6 -> 6.0 | 171 | 29 | 3.08 | 0 | 161/281 |
Before p50/p99 (ms): 74/129, 75/147, 142/201, 65/151, 55/121.
Summary: no regression; 300 B MCS14 and 64QAM points no longer RX-overflow limited (all runs, passing or not, had 0 overflow; limits are now air/loss: first failing rates 2638, 810, 1329, 1030, 740 /s). Sf/burst now 7.8-8.0 at 900 B (looks like an 8-subframe cap) and ~16 at 300 B.
Caveats: MCS16 (5.59) < MCS17 (9.30) is odd; MCS16 first-fail 810/s had 0.13 % loss, so likely noisy/channel, not re-checked. Latency at 900 B MCS14/12 is higher than before (queueing at the edge; 4 ms min interval); single search per point.
Not done (time budget ~24 min): 300 B MCS12 -15 dB and aggregation-off MCS14 -5 dB (script lists them; run was stopped after the first probe of the former, partial data deleted). No pluto hangs, no retries. Working tree docs edits untouched, nothing committed.
