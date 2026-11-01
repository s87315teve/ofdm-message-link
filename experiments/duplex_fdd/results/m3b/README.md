# M3b: the M3 OLLA runs again, with la-tune

Same profile and load as `../m3/`: node A follows `0:-5,30:-20,60:-5` dB, node B stays at −5 dB,
A→B 555 datagrams/s (4 Mb/s offered), B→A 139/s or idle, 900-B datagrams, OLLA from MCS 8,
3 decode workers, TX duty cap 0.7. The code is wp-node `50fb42f`, which includes la-tune
(duplex-fdd `2276d04`): the reporter makes a report due as soon as the peer's FCS failures grow,
and OLLA restarts its SNR filter after two fresh reports more than 6 dB below it. load1 at the
start was 1.7.

```bash
common=(--size 900 --enable-rf --gain-profile1 0:-5,30:-20,60:-5 --tx-gain1 -5 --tx-gain2 -5
        --duration 90 --la olla --mcs 8 --rate-ab 555)
bash experiments/duplex_fdd/run_duplex.sh --out experiments/duplex_fdd/results/m3b/m3b_olla_run1 "${common[@]}" --rate-ba 139
bash experiments/duplex_fdd/run_duplex.sh --out experiments/duplex_fdd/results/m3b/m3b_olla_ba_idle "${common[@]}" --rate-ba 0
python experiments/duplex_fdd/step_metrics.py experiments/duplex_fdd/results/m3/m3_olla_* experiments/duplex_fdd/results/m3b/m3b_olla_*
```

## Before (M3) and after (M3b), A→B

Lost = receiver subframes lost after the gain step, in windows of the 0.5-s stats-line
resolution. The receiver counts a lost subframe when the next one decodes. Loss after 1.5 s runs
from 1.5 s after the step to the next step. Segment = the whole 30 s at that gain. Time to MCS = from
the step to the first LA decision at MCS ≤ 10 (down step) or ≥ 15 (up step). Goodput =
receiver bytes per segment, and over the whole run from `udp_load`.

Step −5 → −20 dB at 30 s:

| run | lost in first 0.5 / 1 / 1.5 s | loss after 1.5 s | segment air loss | time to MCS ≤ 10 | segment goodput | `snr_drops` |
|---|---:|---:|---:|---:|---:|---:|
| M3 OLLA run 1 | 0 / 0 / 188 | 1.04 % | 3.50 % | 0.47 s | 1.71 Mb/s | – |
| M3 OLLA run 2 | 0 / 188 / 188 | 1.05 % | 3.49 % | 0.48 s | 1.73 Mb/s | – |
| M3 OLLA, B→A idle | 0 / 209 / 209 | 1.17 % | 4.05 % | 0.52 s | 1.62 Mb/s | – |
| **M3b OLLA** | **108 / 108 / 108** | **1.00 %** | **2.40 %** | **0.22 s** | **1.74 Mb/s** | **1** |
| **M3b OLLA, B→A idle** | **91 / 91 / 91** | **1.16 %** | **2.27 %** | **0.16 s** | **1.82 Mb/s** | **1** |

Step −20 → −5 dB at 60 s:

| run | lost in first 0.5 / 1 / 1.5 s | loss after 1.5 s | time to MCS ≥ 15 | segment goodput |
|---|---:|---:|---:|---:|
| M3 OLLA run 1 / run 2 / B→A idle | 0 / 0 / 0 (all three) | 0.01–0.02 % | 0.24–0.27 s | 3.33–3.36 Mb/s |
| M3b OLLA | 2 / 2 / 2 | 0.00 % | 0.29 s | 3.34 Mb/s |
| M3b OLLA, B→A idle | 0 / 0 / 2 | 0.01 % | 0.25 s | 3.35 Mb/s |

Whole run (A→B, `udp_load`, 4 Mb/s offered): M3 3.19–3.23 Mb/s; M3b 3.23 and 3.27 Mb/s; p50
latency 61–72 ms in both. B→A (139/s): M3b 0.99 Mb/s at 1.09 % loss, as in M3. RX overflows 0–1
per run. Feedback was never stale.

## What changed

* **The down step costs about half as much.** 91–108 subframes are lost instead of 188–209.
  OLLA reaches MCS ≤ 10 in 0.16–0.22 s instead of 0.47–0.52 s, and the SNR filter restarted
  once per run (`snr_drops` = 1), as designed. The expectation was 60–90 lost; one run is inside
  that range and the other is 18 subframes above it.
* **The −20 dB segment's loss falls from 3.5–4.1 % to 2.3–2.4 %.** After the first 1.5 s it
  stays at OLLA's 1 % target (1.00–1.16 %, unchanged), still alternating between MCS 9 and 10.
  The segment's goodput rises slightly (1.74–1.82 vs 1.62–1.73 Mb/s).
* **Nothing else got worse.** The up step and the −5 dB segments behave as before (MCS ≥ 15 after
  0.25–0.29 s, ≤ 2 subframes lost, ~0.01 % air loss). Whole-run goodput is the same or slightly
  higher.
